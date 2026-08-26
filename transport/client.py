from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from ..codex_security import safe_error
from .auth import CodexAuthStore
from .models import parse_transport_models
from .quota import rate_limits_from_headers
from .responses import parse_sse_data, response_request
from .types import (
    TransportAuthError,
    TransportModelError,
    TransportNetworkError,
    TransportProtocolError,
    TransportQuotaError,
    TransportResponse,
)


class CodexTransportClient:
    """Direct Codex Responses HTTP/SSE client.

    It deliberately has no thread, turn, approval, shell, filesystem, MCP or
    built-in tool methods.  It is a transport, not an Agent Harness.
    """

    def __init__(
        self,
        codex_home: Path,
        *,
        base_url: str = "https://chatgpt.com/backend-api/codex",
        timeout: float = 600,
        client_version: str = "0.146.0",
        proxy_url: str = "",
        use_system_proxy: bool = True,
    ) -> None:
        self.auth = CodexAuthStore(codex_home)
        self.base_url = base_url.rstrip("/")
        self.timeout = max(30.0, float(timeout))
        self.client_version = client_version
        self.proxy_url = ""
        self.use_system_proxy = bool(use_system_proxy)
        self._opener = None
        self.set_proxy(proxy_url)
        self._rate_limits: dict[str, Any] = {}
        self._last_request_at: float | None = None

    @staticmethod
    def _validated_proxy(value: str | None) -> str:
        proxy = str(value or "").strip()
        if not proxy:
            return ""
        parsed = urlsplit(proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Transport 代理必须是 http:// 或 https:// 地址。")
        if parsed.username or parsed.password:
            raise ValueError("Transport 代理地址不能包含用户名或密码。")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Transport 代理只支持主机和端口，不支持路径或查询参数。")
        return proxy

    def set_proxy(self, proxy_url: str | None) -> None:
        """Set an explicit proxy without ever logging its value.

        AstrBot deliberately removes inherited proxy variables when it starts.
        Direct Responses transport therefore needs an opt-in, non-secret proxy
        setting when the host reaches the Internet through a local proxy.
        """

        self.proxy_url = self._validated_proxy(proxy_url)
        self._opener = (
            build_opener(ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))
            if self.proxy_url
            else None
        )

    def set_use_system_proxy(self, enabled: bool) -> None:
        self.use_system_proxy = bool(enabled)

    def _open(self, request: Request, *, timeout: float) -> Any:
        if self._opener is not None:
            return self._opener.open(request, timeout=timeout)
        if not self.use_system_proxy:
            return build_opener(ProxyHandler({})).open(request, timeout=timeout)
        return urlopen(request, timeout=timeout)

    @staticmethod
    def _http_error(code: int, raw: bytes = b"") -> TransportProtocolError:
        """Expose a bounded redacted server reason without logging request data."""

        detail = ""
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            value = None
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, dict):
                candidate = error.get("message") or error.get("detail") or error.get("code")
            else:
                candidate = value.get("message") or value.get("detail")
            if isinstance(candidate, str):
                detail = safe_error(candidate, limit=300)
        suffix = f": {detail}" if detail else ""
        return TransportProtocolError(f"Codex transport HTTP {code}{suffix}")

    async def get_account(self) -> dict[str, Any]:
        snapshot = await self.auth.snapshot(refresh=False)
        return {
            key: value
            for key, value in {
                "email": snapshot.email,
                "planType": snapshot.plan_type,
                "accountId": snapshot.account_id,
            }.items()
            if value
        }

    async def get_rate_limits(self) -> dict[str, Any]:
        await self.auth.snapshot(refresh=False)
        return {"rateLimits": dict(self._rate_limits), "source": "responses_headers"}

    def _headers(self, access_token: str, account_id: str | None, *, stream: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/event-stream" if stream else "application/json",
            "Content-Type": "application/json",
            "originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.146.0",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        return headers

    async def _json(self, method: str, path: str, *, body: dict[str, Any] | None = None) -> Any:
        snapshot = await self.auth.snapshot()
        url = self.base_url + "/" + path.lstrip("/")
        if method == "GET":
            query = urlencode({"client_version": self.client_version})
            url += "?" + query
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=self._headers(snapshot.access_token, snapshot.account_id),
            method=method,
        )

        def read() -> tuple[int, bytes]:
            try:
                with self._open(request, timeout=30) as response:
                    return response.status, response.read()
            except HTTPError as exc:
                return exc.code, exc.read(4096)
            except (URLError, TimeoutError, OSError) as exc:
                raise TransportNetworkError("Codex transport 网络请求失败") from exc

        status, raw = await asyncio.to_thread(read)
        if status == 401:
            raise TransportAuthError("Codex transport 鉴权失败")
        if status == 429:
            raise TransportQuotaError("Codex transport 配额或速率限制")
        if not 200 <= status < 300:
            raise self._http_error(status, raw)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise TransportProtocolError("Codex transport JSON 响应无效") from exc

    async def list_models(self) -> list[Any]:
        payload = await self._json("GET", "models")
        models = parse_transport_models(payload)
        if not models:
            raise TransportModelError("Codex transport 没有返回可用模型")
        return models

    async def stream_chat(
        self,
        *,
        model: str,
        instructions: str,
        input_items: list[dict[str, Any]],
        effort: str = "auto",
        tools: list[dict[str, Any]] | None = None,
        prompt_cache_key: str | None = None,
        previous_response_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        snapshot = await self.auth.snapshot()
        payload = response_request(
            model=model,
            instructions=instructions,
            input_items=input_items,
            effort=effort,
            tools=tools,
            prompt_cache_key=prompt_cache_key,
            previous_response_id=previous_response_id,
        )
        request = Request(
            self.base_url + "/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(snapshot.access_token, snapshot.account_id, stream=True),
            method="POST",
        )

        def open_stream() -> Any:
            try:
                return self._open(request, timeout=self.timeout)
            except HTTPError as exc:
                if exc.code == 401:
                    raise TransportAuthError("Codex transport 鉴权失败") from exc
                if exc.code == 429:
                    raise TransportQuotaError("Codex transport 配额或速率限制") from exc
                raw = exc.read(4096)
                raise self._http_error(exc.code, raw) from exc
            except (URLError, TimeoutError, OSError) as exc:
                raise TransportNetworkError("Codex transport 连接失败") from exc

        response = await asyncio.to_thread(open_stream)
        result = TransportResponse()
        self._last_request_at = time.time()
        self._rate_limits = rate_limits_from_headers(getattr(response, "headers", {}))
        try:
            data_lines: list[str] = []
            emitted_length = 0
            while True:
                try:
                    raw_line = await asyncio.to_thread(response.readline)
                except (TimeoutError, OSError) as exc:
                    raise TransportNetworkError("Codex transport 流读取超时") from exc
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    previous_length = emitted_length
                    terminal = parse_sse_data(data, result)
                    emitted_length = len(result.text)
                    delta = result.text[previous_length:]
                    if delta:
                        yield {"kind": "delta", "text": delta}
                    if terminal:
                        break
            if data_lines:
                parse_sse_data("\n".join(data_lines), result)
            if result.event_count == 0:
                raise TransportProtocolError("Codex transport 没有返回 SSE 事件")
            yield {
                "kind": "final",
                "text": result.text,
                "response_id": result.response_id,
                "usage": result.usage.as_dict() if result.usage else None,
                "tool_calls": [
                    {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in result.tool_calls
                ],
                "reasoning_signature": result.reasoning_signature,
                "rate_limits": dict(self._rate_limits),
                "event_types": list(result.event_types),
            }
        finally:
            response.close()
