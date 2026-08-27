from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .codex_errors import (
    CodexPluginError,
    CodexRPCError,
    CodexTimeoutError,
    CodexTransportError,
    classify_rpc_error,
)
from .codex_rpc import JsonlRpcClient
from .codex_security import safe_error
from .harness import (
    base_instructions_for,
    lightweight_config,
    normalize_harness_mode,
    normalize_tool_router,
)
from .model_catalog import CodexModel, ModelCatalog, parse_models
from .process_manager import CodexProcessManager
from .session_store import SessionStore
from .tool_bridge import ToolBridge
from .transport import CodexTransportClient
from .transport.types import (
    TransportAuthError,
    TransportError,
    TransportModelError,
)
from .usage.models import UsageSnapshot, parse_usage_snapshot_event
from .usage.service import UsageService


@contextlib.asynccontextmanager
async def _async_timeout(seconds: float):
    """Use asyncio.timeout when available, with a streaming-safe 3.10 fallback."""

    timeout_factory = getattr(asyncio, "timeout", None)
    if timeout_factory is not None:
        async with timeout_factory(seconds):
            yield
        return

    task = asyncio.current_task()
    if task is None:
        yield
        return

    loop = asyncio.get_running_loop()
    timed_out = False

    def cancel_task() -> None:
        nonlocal timed_out
        timed_out = True
        task.cancel()

    handle = loop.call_later(seconds, cancel_task)
    try:
        try:
            yield
        except asyncio.CancelledError:
            if timed_out:
                raise asyncio.TimeoutError from None
            raise
    finally:
        handle.cancel()


class CodexService:
    """Codex App Server lifecycle, auth, model catalog, and thread/turn orchestration."""

    def __init__(
        self, data_dir: Path, config: dict[str, Any], *, logger: logging.Logger | None = None
    ) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self.codex_home = self.data_dir / "CODEX_HOME"
        self.transport = CodexTransportClient(
            self.codex_home,
            timeout=float(config.get("turn_timeout", 600) or 600),
            proxy_url=str(config.get("transport_proxy", "") or ""),
            use_system_proxy=bool(config.get("use_system_proxy", True)),
        )
        self.manager = CodexProcessManager(
            str(config.get("codex_path", "codex")),
            self.codex_home,
            logger=self.logger,
            force_http_transport=bool(config.get("force_http_transport", True)),
            proxy_url=str(config.get("transport_proxy", "") or ""),
            use_system_proxy=bool(config.get("use_system_proxy", True)),
        )
        self.catalog = ModelCatalog(self.data_dir / "models.json")
        self.sessions = SessionStore(self.data_dir / "sessions.sqlite3")
        self.tool_bridge = ToolBridge()
        self.usage = UsageService(
            self.data_dir / "usage.db",
            timezone_name=str(config.get("usage_timezone", "Asia/Shanghai") or "Asia/Shanghai"),
            retention_days=int(config.get("usage_retention_days", 365) or 0),
            debug_enabled=bool(config.get("usage_debug", False)),
        )
        self._rpc: JsonlRpcClient | None = None
        self._rpc_lock = asyncio.Lock()
        self._turn_slots = asyncio.Semaphore(
            max(1, min(32, int(config.get("max_concurrent_turns", 2))))
        )
        self._account: dict[str, Any] | None = None
        self._last_login_error: str | None = None
        self._rate_limits: dict[str, Any] | None = None
        self._login_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._active_threads: dict[str, str] = {}
        self._thread_sessions: dict[str, str] = {}
        self._thread_reused: dict[str, bool] = {}
        self._usage_by_session: dict[str, dict[str, Any]] = {}
        self._usage_by_thread: dict[str, UsageSnapshot] = {}
        self._last_usage: dict[str, Any] | None = None
        self._last_turn: dict[str, Any] | None = None
        self._usage_by_turn: dict[str, UsageSnapshot] = {}
        self._active_turns: dict[str, str] = {}
        self._turn_starting_threads: set[str] = set()
        # Only an ephemeral listener port is retained for browser OAuth.  The
        # authorization URL and its code/state query values are never stored.
        self._browser_callback_port: int | None = None
        self._default_model = str(config.get("default_model", "auto") or "auto")
        self._effort = str(config.get("reasoning_effort", "auto") or "auto")
        self._state_path = self.data_dir / "runtime_settings.json"
        self._setup_completed = False
        self._load_runtime_settings()

    async def initialize(self) -> None:
        """Initialize local usage storage without starting the Codex process."""

        await self.usage.initialize()

    def update_usage_config(self) -> None:
        self.usage.update_config(
            timezone_name=str(
                self.config.get("usage_timezone", "Asia/Shanghai") or "Asia/Shanghai"
            ),
            retention_days=int(self.config.get("usage_retention_days", 365) or 0),
            debug_enabled=bool(self.config.get("usage_debug", False)),
        )

    def _load_runtime_settings(self) -> None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                self._default_model = str(state.get("model", self._default_model) or "auto")
                self._effort = str(state.get("effort", self._effort) or "auto")
                # A state file predating onboarding must not silently suppress
                # the first-run guide.  Authenticated accounts mark setup as
                # complete during account/read, so existing working installs
                # still settle on the completed state automatically.
                self._setup_completed = bool(state.get("setupCompleted", False))
        except (OSError, ValueError, TypeError):
            return

    def _persist_runtime_settings(self) -> None:
        self._state_path.write_text(
            json.dumps(
                {
                    "model": self._default_model,
                    "effort": self._effort,
                    "setupCompleted": self._setup_completed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @property
    def setup_completed(self) -> bool:
        return self._setup_completed

    def mark_setup_completed(self) -> None:
        if self._setup_completed:
            return
        self._setup_completed = True
        self._persist_runtime_settings()

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def reasoning_effort(self) -> str:
        return self._effort

    def set_model(self, model: str) -> None:
        self._default_model = model or "auto"
        self._persist_runtime_settings()

    def set_effort(self, effort: str) -> None:
        self._effort = effort or "auto"
        self._persist_runtime_settings()

    @property
    def harness_mode(self) -> str:
        return normalize_harness_mode(self.config.get("harness_mode", "lightweight"))

    @property
    def tool_router_mode(self) -> str:
        return normalize_tool_router(self.config.get("tool_router", "minimal"))

    @property
    def backend_mode(self) -> str:
        value = str(self.config.get("backend_mode", "transport") or "transport").lower()
        return value if value in {"app_server", "transport", "auto"} else "transport"

    def set_harness_mode(self, mode: str) -> None:
        self.config["harness_mode"] = normalize_harness_mode(mode)

    def set_tool_router_mode(self, mode: str) -> None:
        self.config["tool_router"] = normalize_tool_router(mode)

    def _base_instructions(self) -> str | None:
        return base_instructions_for(self.harness_mode)

    def _thread_config(self) -> dict[str, Any] | None:
        return lightweight_config() if self.harness_mode == "lightweight" else None

    async def _server_request(
        self, request_id: int, method: str, _params: dict[str, Any]
    ) -> dict[str, str]:
        """Deny all approval-capable requests in the secure default mode."""

        if method.endswith("requestApproval") or method == "item/tool/requestUserInput":
            return {"decision": "decline"}
        return {}

    async def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "account/updated":
            # Keep only non-secret account metadata. Never retain token-shaped fields.
            account = {key: params.get(key) for key in ("authMode", "planType") if key in params}
            self._account = account or None
            await self._login_events.put({"kind": "account", **account})
        elif method == "account/login/completed":
            self._browser_callback_port = None
            self._last_login_error = (
                self._login_error_message(params.get("error"))
                if not bool(params.get("success")) and params.get("error")
                else None
            )
            await self._login_events.put(
                {
                    "kind": "login",
                    "success": bool(params.get("success")),
                    "error": self._last_login_error,
                }
            )
        elif method == "account/rateLimits/updated":
            value = params.get("rateLimits")
            if isinstance(value, dict):
                self._rate_limits = value
        elif method == "thread/tokenUsage/updated":
            thread_id, turn_id, snapshot = parse_usage_snapshot_event(params)
            if snapshot is None:
                return
            if thread_id:
                self._usage_by_thread[thread_id] = snapshot
                session_key = self._thread_sessions.get(thread_id)
                if session_key:
                    self._usage_by_session[session_key] = snapshot.as_dict()
            if turn_id:
                self._usage_by_turn[turn_id] = snapshot
            self._last_usage = snapshot.as_dict()
            if (turn_id and turn_id in self._active_turns) or (
                thread_id and thread_id in self._turn_starting_threads
            ):
                return
            # Resume/fork replay is a baseline only. It must update the durable
            # snapshot but must never create a new usage record.
            if thread_id:
                await self.usage.observe_snapshot(snapshot)

    async def _connect(self) -> JsonlRpcClient:
        async with self._rpc_lock:
            if self._rpc and not self._rpc.closed:
                return self._rpc
            process = await self.manager.start()
            if process.stdin is None or process.stdout is None:
                raise CodexPluginError("Codex app-server did not provide stdio pipes")
            rpc = JsonlRpcClient(
                process.stdout, process.stdin, logger=self.logger, request_timeout=30
            )
            rpc.set_server_request_handler(self._server_request)
            for method in (
                "account/updated",
                "account/login/completed",
                "account/rateLimits/updated",
                "thread/tokenUsage/updated",
            ):
                rpc.subscribe(method, self._on_notification)
            rpc.start()
            try:
                await rpc.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "astrbot_plugin_chatgpt_codex",
                            "title": "AstrBot ChatGPT Codex Bridge",
                            "version": "0.2.0",
                        },
                        "capabilities": {
                            "experimentalApi": False,
                            # Avoid receiving reasoning content that this client must not render.
                            "optOutNotificationMethods": [
                                "item/reasoning/summaryTextDelta",
                                "item/reasoning/summaryPartAdded",
                                "item/reasoning/textDelta",
                                "item/plan/delta",
                            ],
                        },
                    },
                    timeout=30,
                )
                await rpc.notify("initialized")
            except Exception:
                await rpc.close()
                await self.manager.stop()
                raise
            self._rpc = rpc
            # A new app-server process must resume persisted threads once before
            # they can be treated as active again.
            self._active_threads.clear()
            self._thread_sessions.clear()
            self._active_turns.clear()
            self._turn_starting_threads.clear()
            self._usage_by_turn.clear()
            return rpc

    async def _request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30
    ) -> Any:
        rpc = await self._connect()
        try:
            return await rpc.request(method, params, timeout=timeout)
        except CodexTransportError:
            if self._rpc is rpc:
                self._rpc = None
                self._active_threads.clear()
                self._thread_sessions.clear()
                self._active_turns.clear()
                self._turn_starting_threads.clear()
            with contextlib.suppress(Exception):
                await self.manager.stop()
            raise
        except CodexRPCError as exc:
            if exc.is_quota:
                raise classify_rpc_error(exc) from exc
            raise

    async def account_read(self, refresh: bool = False) -> dict[str, Any]:
        if self.backend_mode == "transport":
            account = await self.transport.get_account()
            self._account = account or None
            if account:
                self.mark_setup_completed()
            return account
        result = await self._request("account/read", {"refreshToken": bool(refresh)})
        account = result.get("account") if isinstance(result, dict) else None
        if isinstance(account, dict):
            # Allow only documented, non-secret account fields into plugin state.
            safe_account = {
                key: account.get(key) for key in ("type", "email", "planType") if key in account
            }
            avatar_url = self._safe_avatar_url(account)
            if avatar_url is not None:
                safe_account["avatarUrl"] = avatar_url
            self._account = safe_account
            self.mark_setup_completed()
            return safe_account
        self._account = None
        return {}

    @staticmethod
    def _safe_avatar_url(account: dict[str, Any]) -> str | None:
        """Extract an optional public avatar URL without retaining credentials.

        Codex App Server versions differ in the name used for an account picture,
        so accept only known public fields.  HTTPS-only URLs with no userinfo or
        fragment are safe to hand to the page; data URLs and arbitrary objects are
        intentionally rejected.  If the current server does not expose a picture,
        the UI keeps its local initial avatar.
        """

        for key in ("avatarUrl", "avatar_url", "picture", "profileImageUrl", "profile_image_url"):
            value = account.get(key)
            if not isinstance(value, str) or not value or len(value) > 2048:
                continue
            try:
                parsed = urlsplit(value)
            except ValueError:
                continue
            if (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
            ):
                return value
        return None

    async def login_start(self, mode: str) -> dict[str, Any]:
        login_type = "chatgptDeviceCode" if mode == "device_code" else "chatgpt"
        self._last_login_error = None
        try:
            result = await self._request(
                "account/login/start", {"type": login_type}, timeout=30
            )
        except CodexRPCError as exc:
            message = self._login_error_message(exc)
            self._last_login_error = message
            if message != str(exc):
                raise CodexPluginError(message) from exc
            raise
        if not isinstance(result, dict):
            return {}
        self._browser_callback_port = None
        auth_url = result.get("authUrl")
        if login_type == "chatgpt" and isinstance(auth_url, str):
            self._browser_callback_port = self._callback_port_from_auth_url(auth_url)
        # Return only values the client must display to finish the login. Do not log or persist them.
        allowed = ("type", "loginId", "authUrl", "verificationUrl", "userCode")
        response = {key: result[key] for key in allowed if key in result}
        if self._browser_callback_port is not None:
            response["callbackRequired"] = True
            response["callbackPath"] = "/auth/callback"
        return response

    @staticmethod
    def _login_error_message(error: Any) -> str:
        message = safe_error(error)
        lowered = message.lower()
        if "country, region, or territory not supported" in lowered or (
            "403 forbidden" in lowered and ("device code" in lowered or "token" in lowered)
        ):
            return (
                "Codex 登录请求被服务器出口网络的区域限制拒绝（HTTP 403）。"
                "请在插件设置或欢迎页填写“网络代理（推理与登录）”后重试；"
                "Docker 中必须填写容器能够访问的代理地址，不能使用宿主机的 127.0.0.1。"
            )
        if "failed to request device code" in lowered or "sending request for url" in lowered:
            return (
                "无法通过当前网络访问 ChatGPT Device Code 服务。"
                "请检查插件中的显式网络代理是否真实运行并允许 Docker 容器访问；"
                "如果使用系统代理，请确认 HTTP_PROXY、HTTPS_PROXY 或 ALL_PROXY 已传入 AstrBot 容器。"
            )
        return message

    async def set_network_proxy(
        self, proxy_url: str | None, *, use_system_proxy: bool | None = None
    ) -> None:
        """Apply one validated proxy to inference and future login processes."""

        proxy = str(proxy_url or "").strip()
        previous = self.manager.proxy_url
        previous_system = self.manager.use_system_proxy
        system_proxy = previous_system if use_system_proxy is None else bool(use_system_proxy)
        self.transport.set_proxy(proxy)
        self.transport.set_use_system_proxy(system_proxy)
        self.manager.set_proxy(proxy)
        self.manager.set_use_system_proxy(system_proxy)
        if previous == self.manager.proxy_url and previous_system == system_proxy:
            return
        rpc = self._rpc
        self._rpc = None
        if rpc is not None:
            with contextlib.suppress(Exception):
                await rpc.close()
        await self.manager.stop()
        self._active_threads.clear()
        self._thread_sessions.clear()
        self._active_turns.clear()
        self._turn_starting_threads.clear()

    @staticmethod
    def _callback_port_from_auth_url(auth_url: str) -> int | None:
        """Return the App Server callback listener port without retaining the URL."""

        try:
            redirect_values = parse_qs(urlsplit(auth_url).query).get("redirect_uri", [])
            redirect = redirect_values[0] if redirect_values else ""
            parsed = urlsplit(redirect)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.path != "/auth/callback"
                or parsed.port is None
            ):
                return None
            return parsed.port
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _forward_browser_callback(port: int, path: str) -> int:
        """Forward an approved callback path to the local Codex listener.

        ``path`` contains transient OAuth query parameters and must never be
        logged, persisted, or included in an error message.
        """

        connection = HTTPConnection("127.0.0.1", port, timeout=12)
        try:
            connection.request("GET", path, headers={"Host": f"localhost:{port}"})
            response = connection.getresponse()
            # Drain a small response so the connection can be closed cleanly;
            # the body is not useful to AstrBot and can contain provider text.
            response.read(1024)
            return response.status
        finally:
            connection.close()

    async def submit_browser_callback(self, callback_url: str) -> dict[str, bool]:
        """Safely hand a pasted OAuth localhost callback to Codex App Server."""

        expected_port = self._browser_callback_port
        if expected_port is None:
            raise CodexPluginError("没有等待中的浏览器登录。请先重新开始登录流程。")
        if not isinstance(callback_url, str) or len(callback_url) > 8192:
            raise CodexPluginError("请粘贴本次登录跳转后的完整 localhost 回调地址。")
        try:
            parsed = urlsplit(callback_url.strip())
            query = parse_qs(parsed.query, keep_blank_values=True)
            valid = (
                parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and parsed.path == "/auth/callback"
                and parsed.port == expected_port
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and bool(query.get("code", [""])[0])
                and bool(query.get("state", [""])[0])
            )
        except (TypeError, ValueError):
            valid = False
            parsed = None
        if not valid or parsed is None:
            raise CodexPluginError(
                "回调地址无效，或不属于本次 Codex 登录。请重新复制浏览器地址栏中的完整 localhost 链接。"
            )
        try:
            status = await asyncio.to_thread(
                self._forward_browser_callback,
                expected_port,
                parsed.path + "?" + parsed.query,
            )
        except Exception as exc:
            # Never interpolate the callback URL or HTTP request path here.
            self.logger.warning("Unable to forward browser OAuth callback: %s", type(exc).__name__)
            raise CodexPluginError(
                "无法将回调交给本机 Codex 登录服务。请确认 AstrBot 与 Codex 运行在同一台机器，然后重试。"
            ) from exc
        if not 200 <= status < 400:
            raise CodexPluginError("本机 Codex 登录服务没有接受回调。请重新开始登录流程。")
        return {"accepted": True, "awaitingCompletion": True}

    async def logout(self) -> None:
        await self._request("account/logout", {}, timeout=30)
        self._account = None
        self._browser_callback_port = None

    async def read_quota(self) -> dict[str, Any]:
        if self.backend_mode == "transport":
            # Responses Transport does not expose the account quota RPC.  Its
            # response headers are only a best-effort request-local snapshot
            # and are empty after restart in most deployments.  Read the
            # official account window on demand through the already supported
            # App Server RPC instead.  This does not change the inference
            # backend: normal chat requests remain Transport-only.
            try:
                result = await self._request("account/rateLimits/read", {}, timeout=30)
            except Exception as exc:
                # Keep the header snapshot as a degraded fallback.  Do not
                # make a quota-panel failure take down an otherwise working
                # Transport installation, and never include exception text in
                # the response because it may contain provider-specific data.
                self.logger.debug(
                    "Official quota RPC unavailable in Transport mode: %s", type(exc).__name__
                )
                return await self.transport.get_rate_limits()
            return self._store_quota_result(result, source="app_server_rpc")
        result = await self._request("account/rateLimits/read", {}, timeout=30)
        return self._store_quota_result(result, source="app_server_rpc")

    def _store_quota_result(self, result: Any, *, source: str) -> dict[str, Any]:
        """Keep only the documented display fields returned by the quota RPC."""

        if isinstance(result, dict):
            self._rate_limits = (
                result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else None
            )
            # This is a display snapshot, not a credential or raw response archive.
            response = {
                key: result.get(key)
                for key in (
                    "rateLimits",
                    "individualLimit",
                    "spendControlReached",
                    "rateLimitResetCredits",
                )
                if key in result
            }
            if source and "source" not in result:
                response["source"] = source
            return response
        return {}

    async def list_models(self, *, refresh: bool = False) -> list[CodexModel]:
        if self.catalog.is_fresh() and not refresh:
            return self.catalog.models
        if self.backend_mode == "transport":
            models = await self.transport.list_models()
            self.catalog.replace(models)
            return models
        try:
            result = await self._request("model/list", {"includeHidden": False}, timeout=30)
            models = parse_models(result if isinstance(result, dict) else {})
            if models:
                self.catalog.replace(models)
                return models
        except CodexPluginError:
            if not self.catalog.models:
                raise
        return self.catalog.models

    async def reset_session(self, session_key: str) -> bool:
        self._active_threads.pop(session_key, None)
        self._thread_reused.pop(session_key, None)
        return await self.sessions.reset(session_key)

    @staticmethod
    def _stable_system_prompt(value: str | None) -> str:
        """Normalize only transport-level whitespace; preserve prompt meaning."""

        return (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    @classmethod
    def _instructions_from_contexts(
        cls,
        system_prompt: str | None,
        contexts: list[dict[str, Any]] | None,
    ) -> str:
        """Preserve AstrBot system/developer messages as Responses instructions.

        The current AstrBot Agent Runner inserts ``ProviderRequest.system_prompt``
        into ``contexts`` before invoking ``text_chat_stream``.  A provider must
        therefore not rely on the optional argument alone: dropping these
        messages silently removes the configured persona, formatting rules, and
        plugin/tool guidance.  Keep the explicit argument first for direct SDK
        calls, then append context-level instructions while removing exact
        duplicates.
        """

        fragments: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            text = cls._stable_system_prompt(value if isinstance(value, str) else None)
            if text and text not in seen:
                seen.add(text)
                fragments.append(text)

        add(system_prompt)
        for item in contexts or []:
            if not isinstance(item, dict) or item.get("role") not in {"system", "developer"}:
                continue
            content = item.get("content")
            if isinstance(content, str):
                add(content)
                continue
            if isinstance(content, list):
                parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") in {
                        "text",
                        "input_text",
                        "output_text",
                    }:
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                add("".join(parts))
        return "\n\n".join(fragments)

    @staticmethod
    def _transport_continuation_context(
        contexts: list[dict[str, Any]] | None,
        latest_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return only new Agent Runner items after the last function call.

        AstrBot's tool loop appends the assistant function call and its tool
        outputs to ``contexts`` before asking the provider for the next step.
        With ``previous_response_id`` the function call is already part of the
        previous Responses result, so only the suffix after that call belongs in
        the new input.
        """

        normalized: list[dict[str, Any]] = []
        for item in contexts or []:
            if hasattr(item, "model_dump"):
                try:
                    item = item.model_dump()
                except Exception:
                    continue
            if isinstance(item, dict):
                normalized.append(item)
        last_call_index = -1
        last_tool_index = -1
        for index, item in enumerate(normalized):
            if item.get("role") == "assistant" and item.get("tool_calls"):
                last_call_index = index
            if item.get("role") == "tool":
                last_tool_index = index
        start = last_call_index + 1 if last_call_index >= 0 else last_tool_index
        continuation = normalized[start:] if start >= 0 else []
        latest = (latest_prompt or "").strip()
        if latest and continuation:
            last = continuation[-1]
            if last.get("role") == "user" and CodexService._content_text(last.get("content")) == latest:
                content = last.get("content")
                if isinstance(content, list):
                    remaining = [
                        part
                        for part in content
                        if not (
                            isinstance(part, dict)
                            and part.get("type") in {"text", "input_text", "output_text"}
                        )
                    ]
                    if remaining:
                        continuation[-1] = {**last, "content": remaining}
                    else:
                        continuation.pop()
                else:
                    continuation.pop()
        return continuation

    def _tool_schema_json(self) -> str:
        tools = []
        if (
            self.tool_router_mode != "none"
            and self.tool_bridge.enabled
            and self.config.get("enable_local_codex_tools", False)
        ):
            tools = self.tool_bridge.dynamic_tools()
        return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def prompt_version(self, system_prompt: str | None) -> str:
        """Hash only deterministic prompt inputs, never per-turn metadata."""

        payload = {
            "template": "astrbot-codex-prompt-v2",
            "system_prompt": self._stable_system_prompt(system_prompt),
            "harness_mode": self.harness_mode,
            "base_instructions": self._base_instructions(),
            "thread_config": self._thread_config(),
            "tool_router": self.tool_router_mode,
            "tool_schema": self._tool_schema_json(),
            "local_tools": bool(self.config.get("enable_local_codex_tools", False)),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def status(self) -> dict[str, Any]:
        auth_error: str | None = None
        try:
            account = await self.account_read(refresh=False)
        except TransportAuthError as exc:
            # First-run status must remain readable so the WebUI can render
            # onboarding and configure Codex before an auth.json exists.
            account = {}
            auth_error = str(exc)
        stale_removed = await self.sessions.cleanup(
            idle_ttl=float(self.config.get("thread_idle_ttl", 604800)),
            max_age=float(self.config.get("thread_max_age", 2592000)),
        )
        return {
            "process": self.manager.status,
            "backend_mode": self.backend_mode,
            "agent_loop": "astrbot",
            "codex_agent_loop": self.backend_mode != "transport",
            "setup_completed": self.setup_completed,
            "auth_required": bool(auth_error),
            "auth_error": auth_error,
            "login_error": self._last_login_error,
            "login_mode": str(self.config.get("login_mode", "browser") or "browser"),
            "codex": self.manager.diagnostic(),
            "account": account,
            "model": self._default_model,
            "effort": self._effort,
            "harness_mode": self.harness_mode,
            "tool_router": self.tool_router_mode,
            "codex_builtin_tools": "server-controlled; no disable field in current app-server schema",
            "cached_models": len(self.catalog.models),
            "browser_login_pending": self._browser_callback_port is not None,
            "active_threads": len(self._active_threads),
            "stale_mappings_removed": stale_removed,
            "last_usage": self._last_usage,
            "last_turn": self._last_turn,
            "local_tools": bool(self.config.get("enable_local_codex_tools", False)),
            "last_error": self.manager.last_error,
        }

    async def prompt_debug(self) -> dict[str, Any]:
        """Return redacted prompt composition diagnostics for administrators."""

        last_turn = self._last_turn or {}
        context = last_turn.get("context_diagnostics")
        if not isinstance(context, dict):
            context = None
        base = self._base_instructions() or ""
        base_fingerprint = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16] if base else None
        return {
            "harness_mode": self.harness_mode,
            "tool_router": self.tool_router_mode,
            "base_instructions_chars": len(base),
            "base_instructions_fingerprint": base_fingerprint,
            "thread_config_keys": sorted((self._thread_config() or {}).keys()),
            "dynamic_tool_schema_bytes": len(self._tool_schema_json().encode("utf-8")),
            "codex_builtin_tools": "server-controlled; not exposed by current app-server schema",
            "last_turn_prompt_version": last_turn.get("prompt_version"),
            "last_turn_context": context,
            "note": "仅为转发组成估算；exact_token_count=false 时不等于服务端 tokenizer 计数。",
        }

    async def benchmark_backend(self, backend: str) -> dict[str, Any]:
        """Run one explicit, real hello request through one selected backend."""

        if backend not in {"app_server", "transport"}:
            raise ValueError("benchmark backend 只能是 app_server 或 transport")
        previous = self.config.get("backend_mode", "transport")
        self.config["backend_mode"] = backend
        session_key = f"__codex_benchmark__:{backend}:{uuid.uuid4().hex}"
        started = time.monotonic()
        try:
            text = await self.run_turn(
                session_key=session_key,
                prompt="hello",
                contexts=[],
                system_prompt="Reply with one short greeting.",
                model=self._default_model,
            )
            result = dict(self._last_turn or {})
            result.update(
                {
                    "backend": backend,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                    "response_text_chars": len(text),
                }
            )
            return result
        finally:
            self.config["backend_mode"] = previous

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            if content.get("type") in ("text", "input_text", "output_text"):
                text = content.get("text")
                return text if isinstance(text, str) else ""
            return ""
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in (
                    "text",
                    "input_text",
                    "output_text",
                ):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    @classmethod
    def _context_text(cls, contexts: list[dict[str, Any]] | None) -> str:
        lines: list[str] = []
        for message in contexts or []:
            if not isinstance(message, dict) or message.get("role") not in (
                "user",
                "assistant",
            ):
                continue
            text = cls._content_text(message.get("content"))
            try:
                from .transport.responses import _attachment_marker

                content = message.get("content")
                parts = content if isinstance(content, list) else [content]
                markers = [
                    marker
                    for part in parts
                    if isinstance(part, dict)
                    for marker in [_attachment_marker(part)]
                    if marker
                ]
                if markers:
                    text = "\n".join([text, *markers]).strip()
            except ImportError:
                pass
            if text:
                lines.append(f"{message.get('role')}: {text}")
        return "\n".join(lines)[-120000:]

    @classmethod
    def _extra_text(cls, parts: list[Any] | None) -> str:
        result: list[str] = []
        for part in parts or []:
            try:
                value = (
                    part.model_dump_for_context()
                    if hasattr(part, "model_dump_for_context")
                    else part
                )
            except Exception:
                continue
            text = cls._content_text(value.get("text") if isinstance(value, dict) else value)
            if isinstance(value, dict):
                try:
                    from .transport.responses import _attachment_marker

                    marker = _attachment_marker(value)
                except ImportError:
                    marker = None
                if marker:
                    text = f"{text}\n{marker}".strip()
            if text:
                result.append(text)
        return "\n".join(result)

    @staticmethod
    def _app_server_media_ref(value: Any) -> str | None:
        """Extract a URL or local path for app-server media input items."""

        if isinstance(value, str):
            value = value.strip()
            return value or None
        if not isinstance(value, dict):
            return None
        for key in ("url", "image_url", "audio_url", "path"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                candidate = candidate.get("url") or candidate.get("data")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @classmethod
    def _app_server_input_items(
        cls,
        user_text: str,
        *,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        extra_user_content_parts: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build current app-server user input with the protocol's media variants."""

        items: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        unsupported: list[str] = []

        def append_media(kind: str, value: Any) -> None:
            ref = cls._app_server_media_ref(value)
            if not ref:
                return
            if ref.startswith("data:"):
                items.append({"type": kind, "url": ref})
                return
            try:
                path = Path(ref)
                if path.is_file():
                    local_kind = "localImage" if kind == "image" else "localAudio"
                    items.append({"type": local_kind, "path": str(path)})
                    return
            except (OSError, ValueError):
                pass
            unsupported.append(
                "[图片附件无法以内联或本地文件方式转发]"
                if kind == "image"
                else "[音频附件无法以内联或本地文件方式转发]"
            )

        for image_url in image_urls or []:
            append_media("image", image_url)
        for audio_url in audio_urls or []:
            append_media("audio", audio_url)
        for raw_part in extra_user_content_parts or []:
            part = raw_part
            for method_name in ("model_dump_for_context", "model_dump"):
                method = getattr(part, method_name, None)
                if callable(method):
                    try:
                        part = method()
                    except Exception:
                        part = None
                    break
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in {"input_image", "image", "image_url", "local_image", "localImage"}:
                append_media("image", part.get("image_url") or part.get("url") or part.get("path"))
            elif part_type in {"input_audio", "audio", "audio_url", "record", "voice"}:
                append_media("audio", part.get("audio_url") or part.get("url") or part.get("path"))
        if unsupported:
            items.append({"type": "text", "text": "\n".join(unsupported)})
        return items

    async def _thread_for(
        self,
        session_key: str,
        *,
        model: str,
        developer_instructions: str,
        prompt_version: str,
    ) -> tuple[str, bool]:
        existing = await self.sessions.get(session_key)
        if existing:
            now = time.time()
            max_turns = max(0, int(self.config.get("max_thread_turns", 100)))
            expired = (
                existing.get("prompt_version") != prompt_version
                or now - float(existing.get("updated_at", now))
                > max(0.0, float(self.config.get("thread_idle_ttl", 604800)))
                or now - float(existing.get("created_at", now))
                > max(0.0, float(self.config.get("thread_max_age", 2592000)))
                or (max_turns > 0 and int(existing.get("turn_count", 0)) >= max_turns)
            )
            if not expired:
                thread_id = existing["thread_id"]
                if (
                    self._active_threads.get(session_key) == thread_id
                    and self._rpc is not None
                    and not self._rpc.closed
                ):
                    self._thread_reused[session_key] = True
                    self._thread_sessions[thread_id] = session_key
                    return thread_id, bool(existing["bootstrapped"])
                try:
                    resume_params: dict[str, Any] = {
                        "threadId": thread_id,
                        "cwd": str(self.data_dir),
                        "approvalPolicy": "on-request",
                        "sandbox": "read-only",
                    }
                    base_instructions = self._base_instructions()
                    thread_config = self._thread_config()
                    if base_instructions is not None:
                        resume_params["baseInstructions"] = base_instructions
                    if thread_config is not None:
                        resume_params["config"] = thread_config
                    if developer_instructions:
                        resume_params["developerInstructions"] = developer_instructions
                    await self._request("thread/resume", resume_params, timeout=45)
                    self._active_threads[session_key] = thread_id
                    self._thread_sessions[thread_id] = session_key
                    self._thread_reused[session_key] = True
                    return thread_id, bool(existing["bootstrapped"])
                except CodexPluginError:
                    self.logger.info(
                        "Stored Codex thread could not be resumed; creating a new thread"
                    )
            self._active_threads.pop(session_key, None)
            self._thread_reused[session_key] = False
            await self.sessions.reset(session_key)
        params: dict[str, Any] = {
            "cwd": str(self.data_dir),
            "approvalPolicy": "on-request",
            "sandbox": "read-only",
        }
        base_instructions = self._base_instructions()
        thread_config = self._thread_config()
        if base_instructions is not None:
            params["baseInstructions"] = base_instructions
        if thread_config is not None:
            params["config"] = thread_config
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        if model != "auto":
            params["model"] = model
        if (
            self.tool_router_mode != "none"
            and self.tool_bridge.enabled
            and self.config.get("enable_local_codex_tools", False)
        ):
            params["dynamicTools"] = self.tool_bridge.dynamic_tools()
        result = await self._request("thread/start", params, timeout=45)
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise CodexPluginError("Codex did not return a thread id")
        # A pre-set pseudonymous name prevents Codex from launching a separate
        # model turn solely to auto-generate a title for this AstrBot session.
        # Never place the raw AstrBot session id in Codex metadata.
        anonymous_name = hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:12]
        with contextlib.suppress(CodexPluginError):
            await self._request(
                "thread/name/set",
                {"threadId": thread_id, "name": f"AstrBot {anonymous_name}"},
                timeout=15,
            )
        await self.sessions.put(
            session_key,
            thread_id,
            bootstrapped=False,
            model=model,
            prompt_version=prompt_version,
        )
        self._active_threads[session_key] = thread_id
        self._thread_sessions[thread_id] = session_key
        return thread_id, False

    @staticmethod
    def _safe_identifier(value: str | None) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return value[:4] + "…" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _notification_turn_id(params: dict[str, Any]) -> str | None:
        turn_id = params.get("turnId")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
        turn = params.get("turn")
        if isinstance(turn, dict):
            nested_id = turn.get("id")
            if isinstance(nested_id, str) and nested_id:
                return nested_id
        return None

    @staticmethod
    def _final_agent_text(items: Any) -> str:
        """Return the authoritative final answer, excluding commentary/reasoning items."""

        if not isinstance(items, list):
            return ""
        final_text = ""
        final_seen = False
        legacy_text = ""
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            phase = item.get("phase")
            if phase == "final_answer":
                final_text = text
                final_seen = True
            elif phase != "commentary":
                # Older models may omit phase. The latest completed unknown-phase
                # agent message is the best compatible final-answer fallback.
                legacy_text = text
        return final_text if final_seen else legacy_text

    async def stream_turn(
        self,
        *,
        session_key: str,
        prompt: str | None,
        contexts: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        extra_user_content_parts: list[Any] | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run one turn using the selected backend, with explicit auto fallback."""

        if self.backend_mode == "app_server":
            async for event in self._stream_app_server_turn(
                session_key=session_key,
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
                extra_user_content_parts=extra_user_content_parts,
                image_urls=image_urls,
                audio_urls=audio_urls,
                tool_calls_result=tool_calls_result,
                model=model,
                tools=tools,
            ):
                yield event
            return
        if self.backend_mode == "transport":
            async for event in self._stream_transport_turn(
                session_key=session_key,
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
                extra_user_content_parts=extra_user_content_parts,
                image_urls=image_urls,
                audio_urls=audio_urls,
                tool_calls_result=tool_calls_result,
                model=model,
                tools=tools,
                emit_deltas=True,
            ):
                yield event
            return
        try:
            async for event in self._stream_transport_turn(
                session_key=session_key,
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
                extra_user_content_parts=extra_user_content_parts,
                image_urls=image_urls,
                audio_urls=audio_urls,
                tool_calls_result=tool_calls_result,
                model=model,
                tools=tools,
                emit_deltas=False,
            ):
                yield event
        except TransportError as exc:
            # Transport output is buffered until its terminal event by the
            # provider; falling back here cannot duplicate visible text.
            self.logger.warning("Transport backend unavailable; falling back to app-server: %s", type(exc).__name__)
            async for event in self._stream_app_server_turn(
                session_key=session_key,
                prompt=prompt,
                contexts=contexts,
                system_prompt=system_prompt,
                extra_user_content_parts=extra_user_content_parts,
                image_urls=image_urls,
                audio_urls=audio_urls,
                tool_calls_result=tool_calls_result,
                model=model,
                tools=tools,
            ):
                yield event

    async def _stream_transport_turn(
        self,
        *,
        session_key: str,
        prompt: str | None,
        contexts: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        extra_user_content_parts: list[Any] | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        emit_deltas: bool = True,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stateless Responses transport; AstrBot supplies the full history."""

        timeout = max(30.0, min(3600.0, float(self.config.get("turn_timeout", 600))))
        async with self._turn_slots, self.sessions.lock_for(session_key):
            await self.usage.initialize()
            selected_model = model or self._default_model
            if selected_model == "auto":
                models = await self.list_models()
                selected_model = models[0].id if models else ""
            if not selected_model:
                raise TransportModelError("没有可用于 transport 的模型")
            from .transport.responses import build_input_items

            instructions = self._instructions_from_contexts(system_prompt, contexts)
            # This endpoint deliberately sends ``store=false``. A
            # ``previous_response_id`` is server-side response state and is not
            # a valid substitute for replaying the AstrBot conversation here.
            # The Codex transport rejects that id after the first request, which
            # caused an avoidable failed request and made injected context look
            # lost or duplicated. Keep AstrBot's context as the single source
            # of truth and replay it every turn.
            previous_response_id = None
            continuation_contexts: list[dict[str, Any]] = []
            input_contexts = contexts
            include_latest = bool(
                prompt
                or image_urls
                or audio_urls
                or extra_user_content_parts
                or not input_contexts
            )
            input_items = build_input_items(
                input_contexts,
                prompt,
                extra_user_content_parts,
                image_urls=image_urls,
                audio_urls=audio_urls,
                tool_calls_result=tool_calls_result,
                include_latest=include_latest,
            )
            input_type_counts: dict[str, int] = {}
            for item in input_items:
                item_type = str(item.get("type", "unknown")) if isinstance(item, dict) else "unknown"
                input_type_counts[item_type] = input_type_counts.get(item_type, 0) + 1
            context_role_counts: dict[str, int] = {}
            for item in input_contexts or []:
                role = item.get("role") if isinstance(item, dict) else None
                role_name = str(role or "unknown")
                context_role_counts[role_name] = context_role_counts.get(role_name, 0) + 1
            context_diagnostics = {
                "backend": "transport",
                "history_items": len(input_contexts or []),
                "history_role_counts": context_role_counts,
                "input_item_type_counts": input_type_counts,
                "instructions_chars": len(instructions),
                "instructions_fingerprint": (
                    hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:16]
                    if instructions
                    else None
                ),
                "dynamic_content_parts": len(extra_user_content_parts or []),
                "dynamic_context_chars": len(self._extra_text(extra_user_content_parts)),
                "image_inputs": len(image_urls or []),
                "audio_inputs": len(audio_urls or []),
                "tool_schema_bytes": len(
                    json.dumps(tools or [], ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ),
                "previous_response_id_used": bool(previous_response_id),
                "full_history_replayed": not bool(previous_response_id),
            }
            started = time.monotonic()
            final_text = ""
            tool_calls: list[dict[str, Any]] = []
            usage: dict[str, Any] | None = None
            response_id: str | None = None
            reasoning_signature: str | None = None
            response_event_types: list[str] = []
            async def consume_transport_events(
                request_items: list[dict[str, Any]],
                request_previous_id: str | None,
            ) -> AsyncGenerator[dict[str, Any], None]:
                async for event in self.transport.stream_chat(
                    model=selected_model,
                    instructions=instructions,
                    input_items=request_items,
                    effort=self._effort,
                    tools=tools,
                    prompt_cache_key=hashlib.sha256(session_key.encode("utf-8")).hexdigest(),
                    previous_response_id=request_previous_id,
                ):
                    yield event

            async with _async_timeout(timeout):
                async for event in consume_transport_events(input_items, None):
                    if event.get("kind") == "delta":
                        if emit_deltas:
                            yield {
                                "kind": "delta",
                                "text": str(event.get("text", "")),
                            }
                    elif event.get("kind") == "final":
                        final_text = str(event.get("text", ""))
                        tool_calls = (
                            event.get("tool_calls")
                            if isinstance(event.get("tool_calls"), list)
                            else []
                        )
                        usage = (
                            event.get("usage")
                            if isinstance(event.get("usage"), dict)
                            else None
                        )
                        response_id = (
                            event.get("response_id")
                            if isinstance(event.get("response_id"), str)
                            else None
                        )
                        reasoning_signature = (
                            event.get("reasoning_signature")
                            if isinstance(event.get("reasoning_signature"), str)
                            else None
                        )
                        response_event_types = [
                            str(item)
                            for item in event.get("event_types", [])
                            if isinstance(item, str)
                        ][:32]
            if not final_text.strip() and not tool_calls:
                tool_names = [
                    str(item.get("name"))
                    for item in tools or []
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                ]
                self.logger.warning(
                    "Codex transport returned no visible text or tool call: events=%s tools=%d names=%s",
                    ",".join(response_event_types[-12:]) or "none",
                    len(tools or []),
                    ",".join(tool_names[:16]) or "none",
                )
                raise TransportError("Codex transport 返回空白响应")
            usage_diagnostic = None
            if usage:
                try:
                    usage_diagnostic = await self.usage.record_turn_usage(
                        conversation_id=session_key,
                        thread_id=None,
                        turn_id=response_id,
                        model=selected_model,
                        reasoning_effort=self._effort,
                        usage={
                            "input_tokens": usage.get("input_tokens"),
                            "cached_input_tokens": usage.get("cached_input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                            "reasoning_tokens": usage.get("reasoning_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                            "cache_write_input_tokens": usage.get("cache_write_input_tokens"),
                        },
                    )
                except Exception as exc:
                    self.logger.warning("Unable to persist transport usage: %s", type(exc).__name__)
            self._last_usage = {"total": usage, "source": "responses.completed"} if usage else None
            self._last_turn = {
                "backend": "transport",
                "model": selected_model,
                "reasoning_effort": self._effort,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
                "usage": usage,
                "usage_diagnostic": usage_diagnostic,
                "thread_id": None,
                "response_id": response_id,
                "response_event_types": response_event_types,
                "reasoning_signature": bool(reasoning_signature),
                "previous_response_id": bool(previous_response_id),
                "context_history_replayed": not bool(previous_response_id),
                "continuation_items": len(continuation_contexts),
                "context_diagnostics": context_diagnostics,
            }
            await self.sessions.put(
                session_key,
                f"__transport__:{session_key}",
                bootstrapped=True,
                model=selected_model,
                prompt_version=self.prompt_version(instructions),
                response_id=None,
                increment_turn=True,
            )
            if tool_calls:
                yield {
                    "kind": "tool_call",
                    "tool_calls": tool_calls,
                    "reasoning_signature": reasoning_signature,
                }
            else:
                yield {
                    "kind": "final",
                    "text": final_text,
                    "reasoning_signature": reasoning_signature,
                }

    async def _stream_app_server_turn(
        self,
        *,
        session_key: str,
        prompt: str | None,
        contexts: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        extra_user_content_parts: list[Any] | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        del tool_calls_result, tools
        timeout = max(30.0, min(3600.0, float(self.config.get("turn_timeout", 600))))
        async with self._turn_slots, self.sessions.lock_for(session_key):
            await self.usage.initialize()
            selected_model = model or self._default_model
            if selected_model == "auto":
                cached = await self.list_models()
                if cached:
                    selected_model = cached[0].id
            developer_instructions = self._instructions_from_contexts(
                system_prompt,
                contexts,
            )[-40000:]
            prompt_version = self.prompt_version(developer_instructions)
            bootstrap = ""
            context_text = self._context_text(contexts)
            if context_text:
                bootstrap += "<astrbot_context>\n" + context_text + "\n</astrbot_context>\n"
            current_parts: list[Any] = []
            current_attachment_markers: list[str] = []
            user_text = (prompt or "").strip()
            if not user_text:
                for message in reversed(contexts or []):
                    if not isinstance(message, dict) or message.get("role") != "user":
                        continue
                    raw_content = message.get("content")
                    current_parts = (
                        raw_content if isinstance(raw_content, list) else [raw_content]
                    )
                    user_text = self._content_text(raw_content).strip()
                    try:
                        from .transport.responses import _attachment_marker

                        for raw_part in current_parts:
                            if isinstance(raw_part, dict):
                                marker = _attachment_marker(raw_part)
                                if marker:
                                    current_attachment_markers.append(marker)
                    except ImportError:
                        pass
                    break
            if current_attachment_markers:
                user_text = "\n".join(
                    [user_text, *current_attachment_markers]
                ).strip()
            user_text = user_text or "(The user sent an empty message.)"
            extra_text = self._extra_text(extra_user_content_parts)
            if extra_text:
                user_text += (
                    "\n\n<astrbot_dynamic_context>\n"
                    + extra_text[-40000:]
                    + "\n</astrbot_dynamic_context>"
                )
            record = await self.sessions.get(session_key)
            is_bootstrapped = bool(record and record.get("bootstrapped"))
            if not is_bootstrapped:
                user_text = (
                    bootstrap
                    + "\n<astrbot_latest_user_message>\n"
                    + user_text
                    + "\n</astrbot_latest_user_message>"
                )
            tool_schema = self._tool_schema_json()
            context_diagnostics = {
                "kind": "estimated_context_composition",
                "harness_mode": self.harness_mode,
                "harness_chars": len(self._base_instructions() or ""),
                "system_persona_chars": len(developer_instructions),
                "conversation_history_chars": len(context_text) if not is_bootstrapped else 0,
                "dynamic_context_chars": len(extra_text),
                "latest_user_chars": len(user_text),
                "tool_schema_json_bytes": len(tool_schema.encode("utf-8")),
                "tool_router": self.tool_router_mode,
                "tools_enabled": len(self.tool_bridge.dynamic_tools())
                if (
                    self.tool_router_mode != "none"
                    and self.tool_bridge.enabled
                    and self.config.get("enable_local_codex_tools", False)
                )
                else 0,
                "exact_token_count": False,
                "history_forwarded_to_codex": not is_bootstrapped,
            }
            thread_id, _ = await self._thread_for(
                session_key,
                model=selected_model,
                developer_instructions=developer_instructions,
                prompt_version=prompt_version,
            )
            queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
            turn_id: str | None = None
            public_types = {"agentMessage", "commandExecution", "fileChange", "mcpToolCall"}

            async def on_event(method: str, params: dict[str, Any]) -> None:
                # Subscribe before turn/start so no early notification is lost, but
                # defer turn filtering until the request returns its authoritative id.
                if params.get("threadId") != thread_id:
                    return
                await queue.put((method, params))

            rpc = await self._connect()
            unsubscribers = [
                rpc.subscribe(method, on_event)
                for method in (
                    "item/agentMessage/delta",
                    "item/started",
                    "item/completed",
                    "error",
                    "turn/completed",
                    "thread/tokenUsage/updated",
                )
            ]
            completed = False
            terminal = False
            completed_agent_items: list[dict[str, Any]] = []
            final_text = ""
            last_turn_snapshot: UsageSnapshot | None = None
            retry_count = 0
            turn_started_at = time.monotonic()
            self._turn_starting_threads.add(thread_id)
            try:
                params: dict[str, Any] = {
                    "threadId": thread_id,
                    "clientUserMessageId": str(uuid.uuid4()),
                    "input": self._app_server_input_items(
                        user_text,
                        image_urls=image_urls,
                        audio_urls=audio_urls,
                        extra_user_content_parts=[
                            *current_parts,
                            *(extra_user_content_parts or []),
                        ],
                    ),
                    "cwd": str(self.data_dir),
                    "approvalPolicy": "on-request",
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                }
                if selected_model != "auto":
                    params["model"] = selected_model
                if self._effort != "auto":
                    params["effort"] = self._effort
                result = await rpc.request("turn/start", params, timeout=30)
                turn = result.get("turn") if isinstance(result, dict) else None
                if isinstance(turn, dict):
                    turn_id = turn.get("id")
                if not isinstance(turn_id, str) or not turn_id:
                    raise CodexPluginError("Codex did not return a turn id")
                self._active_turns[turn_id] = thread_id
                self._turn_starting_threads.discard(thread_id)
                await self.sessions.put(
                    session_key,
                    thread_id,
                    bootstrapped=True,
                    model=selected_model,
                    prompt_version=prompt_version,
                )
                deadline = time.monotonic() + timeout
                while not completed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexTimeoutError("Codex turn timed out")
                    try:
                        method, event_params = await asyncio.wait_for(queue.get(), remaining)
                    except TimeoutError as exc:
                        raise CodexTimeoutError("Codex turn timed out") from exc
                    if self._notification_turn_id(event_params) != turn_id:
                        continue
                    if method == "item/agentMessage/delta":
                        # Deltas cannot be retracted after an upstream reconnect. Buffering
                        # until item/completed prevents replayed text from reaching AstrBot.
                        continue
                    if method == "item/completed":
                        item = event_params.get("item")
                        if isinstance(item, dict) and item.get("type") == "agentMessage":
                            completed_agent_items.append(item)
                        elif self.config.get("show_tool_status", False) and isinstance(item, dict):
                            item_type = item.get("type")
                            if item_type in public_types:
                                yield {"kind": "status", "text": f"[{item_type} completed]"}
                        continue
                    if method == "item/started":
                        item = event_params.get("item")
                        if (
                            self.config.get("show_tool_status", False)
                            and isinstance(item, dict)
                            and item.get("type") in public_types
                        ):
                            yield {"kind": "status", "text": f"[{item.get('type')} started]"}
                        continue
                    if method == "error":
                        if bool(event_params.get("willRetry")):
                            retry_count += 1
                            if self.config.get("show_tool_status", False):
                                yield {"kind": "status", "text": "[Codex reconnecting]"}
                            continue
                        error = event_params.get("error")
                        error_text = (
                            f"{error.get('message', error)} {error.get('codexErrorInfo', '')}"
                            if isinstance(error, dict)
                            else str(error or "Codex turn failed")
                        )
                        raise classify_rpc_error(CodexRPCError(None, safe_error(error_text)))
                    if method == "thread/tokenUsage/updated":
                        _, event_turn_id, snapshot = parse_usage_snapshot_event(event_params)
                        if snapshot is not None and event_turn_id == turn_id:
                            last_turn_snapshot = snapshot
                        continue
                    if method == "turn/completed":
                        turn = event_params.get("turn")
                        if not isinstance(turn, dict):
                            continue
                        terminal = True
                        status = turn.get("status")
                        error = turn.get("error")
                        if status != "completed":
                            if isinstance(error, dict):
                                error_text = (
                                    f"{error.get('message', error)} "
                                    f"{error.get('codexErrorInfo', '')}"
                                )
                            else:
                                error_text = f"Codex turn ended with status {status}"
                            raise classify_rpc_error(CodexRPCError(None, safe_error(error_text)))
                        final_text = self._final_agent_text(turn.get("items"))
                        if not final_text:
                            final_text = self._final_agent_text(completed_agent_items)
                        completed = True
                await self.sessions.put(
                    session_key,
                    thread_id,
                    bootstrapped=True,
                    model=selected_model,
                    prompt_version=prompt_version,
                    increment_turn=True,
                )
                snapshot = last_turn_snapshot or self._usage_by_turn.pop(turn_id, None)
                usage_diagnostic = None
                if snapshot is not None:
                    try:
                        usage_diagnostic = await self.usage.record_turn_snapshot(
                            conversation_id=session_key,
                            thread_id=thread_id,
                            turn_id=turn_id,
                            model=selected_model,
                            reasoning_effort=self._effort,
                            snapshot=snapshot,
                        )
                    except Exception as exc:  # Usage must never break a completed answer.
                        self.logger.warning(
                            "Unable to persist Codex usage delta: %s", type(exc).__name__
                        )
                else:
                    self.logger.warning(
                        "Codex turn completed without thread token usage: thread=%s turn=%s",
                        self._safe_identifier(thread_id),
                        self._safe_identifier(turn_id),
                    )
                self._last_turn = {
                    "thread_reused": bool(self._thread_reused.get(session_key, False)),
                    "model": selected_model,
                    "reasoning_effort": self._effort,
                    "retry_count": retry_count,
                    "latency_ms": round((time.monotonic() - turn_started_at) * 1000, 1),
                    "usage": snapshot.as_dict() if snapshot else None,
                    "usage_diagnostic": usage_diagnostic,
                    "context_diagnostics": context_diagnostics,
                    "prompt_version": prompt_version,
                }
                yield {"kind": "final", "text": final_text}
            finally:
                if turn_id:
                    self._active_turns.pop(turn_id, None)
                    self._usage_by_turn.pop(turn_id, None)
                self._turn_starting_threads.discard(thread_id)
                if turn_id and not terminal:
                    with contextlib.suppress(Exception):
                        await rpc.request(
                            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10
                        )
                for unsubscribe in unsubscribers:
                    unsubscribe()

    async def run_turn(self, **kwargs: Any) -> str:
        parts: list[str] = []
        async for event in self.stream_turn(**kwargs):
            if event.get("kind") == "delta" or event.get("kind") == "final" and not parts:
                parts.append(str(event.get("text", "")))
        return "".join(parts)

    async def close(self) -> None:
        if self._rpc:
            await self._rpc.close()
            self._rpc = None
        await self.manager.stop()
        self._active_threads.clear()
        self._thread_sessions.clear()
        await self.sessions.close()
        await self.usage.close()
