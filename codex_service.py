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
from .model_catalog import CodexModel, ModelCatalog, parse_models
from .process_manager import CodexProcessManager
from .session_store import SessionStore
from .tool_bridge import ToolBridge
from .usage.models import parse_token_usage_event
from .usage.service import UsageService


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
        self.manager = CodexProcessManager(
            str(config.get("codex_path", "codex")),
            self.codex_home,
            logger=self.logger,
            force_http_transport=bool(config.get("force_http_transport", True)),
        )
        self.catalog = ModelCatalog(self.data_dir / "models.json")
        self.sessions = SessionStore(self.data_dir / "sessions.sqlite3")
        self.tool_bridge = ToolBridge()
        self.usage = UsageService(
            self.data_dir / "usage.db",
            timezone_name=str(config.get("usage_timezone", "Asia/Shanghai") or "Asia/Shanghai"),
            retention_days=int(config.get("usage_retention_days", 365) or 0),
        )
        self._rpc: JsonlRpcClient | None = None
        self._rpc_lock = asyncio.Lock()
        self._turn_slots = asyncio.Semaphore(
            max(1, min(32, int(config.get("max_concurrent_turns", 2))))
        )
        self._account: dict[str, Any] | None = None
        self._rate_limits: dict[str, Any] | None = None
        self._login_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._active_threads: dict[str, str] = {}
        self._thread_sessions: dict[str, str] = {}
        self._thread_reused: dict[str, bool] = {}
        self._usage_by_session: dict[str, dict[str, Any]] = {}
        self._last_usage: dict[str, Any] | None = None
        self._last_turn: dict[str, Any] | None = None
        self._usage_by_turn: dict[str, dict[str, Any] | None] = {}
        # Only an ephemeral listener port is retained for browser OAuth.  The
        # authorization URL and its code/state query values are never stored.
        self._browser_callback_port: int | None = None
        self._default_model = str(config.get("default_model", "auto") or "auto")
        self._effort = str(config.get("reasoning_effort", "auto") or "auto")
        self._state_path = self.data_dir / "runtime_settings.json"
        self._load_runtime_settings()

    async def initialize(self) -> None:
        """Initialize local usage storage without starting the Codex process."""

        await self.usage.initialize()

    def update_usage_config(self) -> None:
        self.usage.timezone_name = str(
            self.config.get("usage_timezone", "Asia/Shanghai") or "Asia/Shanghai"
        )
        try:
            from zoneinfo import ZoneInfo

            self.usage.zone = ZoneInfo(self.usage.timezone_name)
        except Exception:
            self.usage.timezone_name = "Asia/Shanghai"
            from zoneinfo import ZoneInfo

            self.usage.zone = ZoneInfo("Asia/Shanghai")
        self.usage.retention_days = max(0, int(self.config.get("usage_retention_days", 365) or 0))

    def _load_runtime_settings(self) -> None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                self._default_model = str(state.get("model", self._default_model) or "auto")
                self._effort = str(state.get("effort", self._effort) or "auto")
        except (OSError, ValueError, TypeError):
            return

    def _persist_runtime_settings(self) -> None:
        self._state_path.write_text(
            json.dumps(
                {"model": self._default_model, "effort": self._effort}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )

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
            await self._login_events.put(
                {
                    "kind": "login",
                    "success": bool(params.get("success")),
                    "error": safe_error(params.get("error")) if params.get("error") else None,
                }
            )
        elif method == "account/rateLimits/updated":
            value = params.get("rateLimits")
            if isinstance(value, dict):
                self._rate_limits = value
        elif method == "thread/tokenUsage/updated":
            usage = self._safe_usage(params.get("tokenUsage"))
            _, turn_id, _ = parse_token_usage_event(params)
            if turn_id:
                self._usage_by_turn[turn_id] = usage
            if usage:
                thread_id = params.get("threadId")
                session_key = (
                    self._thread_sessions.get(thread_id) if isinstance(thread_id, str) else None
                )
                if session_key:
                    self._usage_by_session[session_key] = usage
                self._last_usage = usage

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
                            "version": "0.1.0",
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
            with contextlib.suppress(Exception):
                await self.manager.stop()
            raise
        except CodexRPCError as exc:
            if exc.is_quota:
                raise classify_rpc_error(exc) from exc
            raise

    async def account_read(self, refresh: bool = False) -> dict[str, Any]:
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
        result = await self._request("account/login/start", {"type": login_type}, timeout=30)
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
        result = await self._request("account/rateLimits/read", {}, timeout=30)
        if isinstance(result, dict):
            self._rate_limits = (
                result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else None
            )
            # This is a display snapshot, not a credential or raw response archive.
            return {
                key: result.get(key)
                for key in (
                    "rateLimits",
                    "individualLimit",
                    "spendControlReached",
                    "rateLimitResetCredits",
                )
                if key in result
            }
        return {}

    async def list_models(self, *, refresh: bool = False) -> list[CodexModel]:
        if self.catalog.is_fresh() and not refresh:
            return self.catalog.models
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

    def _tool_schema_json(self) -> str:
        tools = []
        if self.tool_bridge.enabled and self.config.get("enable_local_codex_tools", False):
            tools = self.tool_bridge.dynamic_tools()
        return json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def prompt_version(self, system_prompt: str | None) -> str:
        """Hash only deterministic prompt inputs, never per-turn metadata."""

        payload = {
            "template": "astrbot-codex-prompt-v1",
            "system_prompt": self._stable_system_prompt(system_prompt),
            "tool_schema": self._tool_schema_json(),
            "local_tools": bool(self.config.get("enable_local_codex_tools", False)),
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_usage(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        result: dict[str, Any] = {}
        for scope in ("last", "total"):
            breakdown = value.get(scope)
            if not isinstance(breakdown, dict):
                continue
            safe = {
                key: breakdown[key]
                for key in (
                    "cachedInputTokens",
                    "inputTokens",
                    "outputTokens",
                    "reasoningOutputTokens",
                    "totalTokens",
                    "cacheWriteInputTokens",
                )
                if isinstance(breakdown.get(key), int) and breakdown[key] >= 0
            }
            if safe:
                result[scope] = safe
        if isinstance(value.get("modelContextWindow"), int):
            result["modelContextWindow"] = value["modelContextWindow"]
        last = result.get("last")
        if isinstance(last, dict):
            # cachedInputTokens is a breakdown of input accounting, not an
            # additional quantity. Never add it to the authoritative total.
            denominator = last.get("inputTokens", 0)
            if denominator:
                result["cacheRatio"] = last.get("cachedInputTokens", 0) / denominator
        return result or None

    async def status(self) -> dict[str, Any]:
        account = await self.account_read(refresh=False)
        stale_removed = await self.sessions.cleanup(
            idle_ttl=float(self.config.get("thread_idle_ttl", 604800)),
            max_age=float(self.config.get("thread_max_age", 2592000)),
        )
        return {
            "process": self.manager.status,
            "account": account,
            "model": self._default_model,
            "effort": self._effort,
            "cached_models": len(self.catalog.models),
            "browser_login_pending": self._browser_callback_port is not None,
            "active_threads": len(self._active_threads),
            "stale_mappings_removed": stale_removed,
            "last_usage": self._last_usage,
            "last_turn": self._last_turn,
            "local_tools": bool(self.config.get("enable_local_codex_tools", False)),
            "last_error": self.manager.last_error,
        }

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in ("text", "input_text"):
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
                "system",
                "user",
                "assistant",
            ):
                continue
            text = cls._content_text(message.get("content"))
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
            if text:
                result.append(text)
        return "\n".join(result)

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
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        if model != "auto":
            params["model"] = model
        if self.tool_bridge.enabled and self.config.get("enable_local_codex_tools", False):
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
        model: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        timeout = max(30.0, min(3600.0, float(self.config.get("turn_timeout", 600))))
        async with self._turn_slots, self.sessions.lock_for(session_key):
            await self.usage.initialize()
            selected_model = model or self._default_model
            if selected_model == "auto":
                cached = await self.list_models()
                if cached:
                    selected_model = cached[0].id
            developer_instructions = self._stable_system_prompt(system_prompt)[-40000:]
            prompt_version = self.prompt_version(developer_instructions)
            bootstrap = ""
            context_text = self._context_text(contexts)
            if context_text:
                bootstrap += "<astrbot_context>\n" + context_text + "\n</astrbot_context>\n"
            user_text = (prompt or "").strip() or "(The user sent an empty message.)"
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
            last_turn_usage: dict[str, Any] | None = None
            retry_count = 0
            turn_started_at = time.monotonic()
            try:
                params: dict[str, Any] = {
                    "threadId": thread_id,
                    "clientUserMessageId": str(uuid.uuid4()),
                    "input": [{"type": "text", "text": user_text}],
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
                        last_turn_usage = self._safe_usage(event_params.get("tokenUsage"))
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
                usage = last_turn_usage or self._usage_by_turn.pop(turn_id, None)
                try:
                    await self.usage.collector.record_turn_usage(
                        conversation_id=session_key,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        model=selected_model,
                        reasoning_effort=self._effort,
                        usage=usage.get("last") if isinstance(usage, dict) else None,
                    )
                except Exception as exc:  # Usage must never break a completed answer.
                    self.logger.warning("Unable to persist Codex usage: %s", type(exc).__name__)
                self._last_turn = {
                    "thread_reused": bool(self._thread_reused.get(session_key, False)),
                    "model": selected_model,
                    "reasoning_effort": self._effort,
                    "retry_count": retry_count,
                    "latency_ms": round((time.monotonic() - turn_started_at) * 1000, 1),
                    "usage": usage,
                }
                yield {"kind": "final", "text": final_text}
            finally:
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

