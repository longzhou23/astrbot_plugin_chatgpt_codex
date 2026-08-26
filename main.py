from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .agent_provider import bind_service
from .codex_errors import safe_error
from .codex_service import CodexService

try:
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star
    from astrbot.api.web import request as web_request
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:  # pragma: no cover
    logger = None
    AstrMessageEvent = Any  # type: ignore[misc,assignment]
    Context = Any  # type: ignore[misc,assignment]
    Star = object  # type: ignore[misc,assignment]
    filter = None
    web_request = None
    get_astrbot_plugin_data_path = lambda: Path("data/plugin_data")  # type: ignore[assignment]


def _data_dir() -> Path:
    return Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_chatgpt_codex"


CONFIG_DEFAULTS: dict[str, Any] = {
    "codex_path": "codex",
    "backend_mode": "transport",
    "transport_proxy": "",
    "use_system_proxy": True,
    "login_mode": "browser",
    "default_model": "auto",
    "reasoning_effort": "auto",
    "harness_mode": "lightweight",
    "tool_router": "minimal",
    "streaming": True,
    "show_tool_status": False,
    "max_concurrent_turns": 2,
    "turn_timeout": 600,
    "max_thread_turns": 100,
    "thread_idle_ttl": 604800,
    "thread_max_age": 2592000,
    "force_http_transport": True,
    "enable_local_codex_tools": False,
    "usage_timezone": "Asia/Shanghai",
    "usage_retention_days": 365,
    "usage_debug": False,
}

CONFIG_RANGES: dict[str, tuple[int, int]] = {
    "max_concurrent_turns": (1, 32),
    "turn_timeout": (30, 3600),
    "max_thread_turns": (0, 10000),
    "thread_idle_ttl": (0, 31536000),
    "thread_max_age": (0, 31536000),
    "usage_retention_days": (0, 3650),
}


class ChatgptCodexPlugin(Star):
    """AstrBot provider, management page, and headless fallback commands."""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # Keep AstrBotConfig identity when AstrBot supplies it so WebUI saves
        # are persisted by the same configuration object used by the service.
        self.config = config if isinstance(config, dict) else {}
        self.service = CodexService(_data_dir(), self.config, logger=logger)
        bind_service(self.service)

    async def initialize(self) -> None:
        """Make the adapter visible and loadable by AstrBot's core provider manager.

        AstrBot 4.27 registers plugin provider adapters before the core provider
        manager initializes, but it does not create a provider config entry for a
        plugin automatically.  Keeping the entry in ``cmd_config.json`` makes the
        provider appear in the WebUI and lets the normal provider manager instantiate
        it on the next part of the same startup sequence.  Existing user settings
        are never overwritten.
        """
        config = self.context.get_config()
        providers = config.setdefault("provider", [])
        if not isinstance(providers, list):
            providers = []
            config["provider"] = providers

        provider_sources = config.get("provider_sources", [])
        has_current_source = isinstance(provider_sources, list) and any(
            isinstance(item, dict) and item.get("type") == "chatgpt_codex"
            for item in provider_sources
        )

        provider_id = "chatgpt_codex"
        if has_current_source:
            if logger:
                logger.info("ChatGPT Codex provider source already present")
        elif not any(
            isinstance(item, dict) and item.get("id") == provider_id for item in providers
        ):
            providers.append(
                {
                    "id": provider_id,
                    "provider": "ChatGPT Codex Subscription",
                    "type": "chatgpt_codex",
                    "provider_type": "chat_completion",
                    "enable": True,
                    "key": ["chatgpt-subscription"],
                    "model": "auto",
                }
            )
            save_config = getattr(config, "save_config", None)
            if callable(save_config):
                save_config()
            if logger:
                logger.info(
                    "ChatGPT Codex provider configuration added: %s",
                    provider_id,
                )
        elif logger:
            logger.info(
                "ChatGPT Codex provider configuration already present: %s",
                provider_id,
            )

        # The primary account-management UX is the authenticated plugin page.
        # Commands below remain useful for headless deployments and diagnostics.
        web_prefix = "/astrbot_plugin_chatgpt_codex"
        self.context.register_web_api(
            f"{web_prefix}/status",
            self._web_status,
            ["GET"],
            "Read non-secret Codex account and process status",
        )
        self.context.register_web_api(
            f"{web_prefix}/login/start",
            self._web_login_start,
            ["POST"],
            "Start ChatGPT browser or device-code login",
        )
        self.context.register_web_api(
            f"{web_prefix}/login/callback",
            self._web_login_callback,
            ["POST"],
            "Forward a pasted browser OAuth localhost callback to local Codex",
        )
        self.context.register_web_api(
            f"{web_prefix}/logout",
            self._web_logout,
            ["POST"],
            "Log out of the Codex ChatGPT account",
        )
        self.context.register_web_api(
            f"{web_prefix}/models",
            self._web_models,
            ["GET"],
            "Refresh the server-advertised Codex model catalog",
        )
        self.context.register_web_api(
            f"{web_prefix}/quota",
            self._web_quota,
            ["GET"],
            "Read the non-secret Codex account rate limits",
        )
        self.context.register_web_api(
            f"{web_prefix}/config",
            self._web_config,
            ["GET", "POST"],
            "Read or update the validated non-secret plugin settings",
        )
        self.context.register_web_api(
            f"{web_prefix}/usage/summary",
            self._web_usage_summary,
            ["GET"],
            "Read locally collected Codex token usage summary",
        )
        self.context.register_web_api(
            f"{web_prefix}/usage/daily",
            self._web_usage_daily,
            ["GET"],
            "Read locally collected daily Codex token usage",
        )
        self.context.register_web_api(
            f"{web_prefix}/usage/models",
            self._web_usage_models,
            ["GET"],
            "Read local Codex token usage grouped by model",
        )
        self.context.register_web_api(
            f"{web_prefix}/usage/rate-limits",
            self._web_usage_rate_limits,
            ["GET"],
            "Read official Codex account rate limits separately from local usage",
        )
        self.context.register_web_api(
            f"{web_prefix}/usage/turns",
            self._web_usage_turns,
            ["GET"],
            "Read recent redacted per-turn Codex usage diagnostics",
        )
        await self.service.initialize()

    @staticmethod
    def _web_ok(data: Any) -> dict[str, Any]:
        return {"status": "ok", "data": data}

    @staticmethod
    def _web_error(exc: Exception) -> dict[str, Any]:
        if logger:
            logger.warning("ChatGPT Codex WebUI request failed: %s", safe_error(exc))
        return {"status": "error", "message": safe_error(exc), "data": {}}

    async def _web_status(self) -> dict[str, Any]:
        try:
            return self._web_ok(await self.service.status())
        except Exception as exc:
            return self._web_error(exc)

    async def _web_login_start(self) -> dict[str, Any]:
        try:
            mode = str(self.config.get("login_mode", "browser") or "browser")
            if web_request is not None and web_request.method == "POST":
                body = await web_request.json({})
                if isinstance(body, dict) and body.get("mode") in {"browser", "device_code"}:
                    mode = str(body["mode"])
            return self._web_ok(await self.service.login_start(mode))
        except Exception as exc:
            return self._web_error(exc)

    async def _web_login_callback(self) -> dict[str, Any]:
        try:
            body = await web_request.json({}) if web_request is not None else {}
            callback_url = body.get("callbackUrl") if isinstance(body, dict) else None
            if not isinstance(callback_url, str):
                raise TypeError("请粘贴完整的 localhost 回调地址。")
            return self._web_ok(await self.service.submit_browser_callback(callback_url))
        except Exception as exc:
            return self._web_error(exc)

    async def _web_logout(self) -> dict[str, Any]:
        try:
            await self.service.logout()
            return self._web_ok({"loggedOut": True})
        except Exception as exc:
            return self._web_error(exc)

    async def _web_models(self) -> dict[str, Any]:
        try:
            models = await self.service.list_models(refresh=True)
            return self._web_ok(
                {
                    "models": [
                        {
                            "id": model.id,
                            "displayName": model.display_name,
                            "reasoningEfforts": list(model.reasoning_efforts),
                            "hidden": model.hidden,
                        }
                        for model in models
                        if not model.hidden
                    ],
                    "selectedModel": self.service.default_model,
                    "selectedEffort": self.service.reasoning_effort,
                }
            )
        except Exception as exc:
            return self._web_error(exc)

    async def _web_quota(self) -> dict[str, Any]:
        try:
            return self._web_ok(await self.service.read_quota())
        except Exception as exc:
            return self._web_error(exc)

    @staticmethod
    def _query_int(name: str, default: int) -> int:
        if web_request is None:
            return default
        query = getattr(web_request, "query", None) or getattr(web_request, "query_params", None)
        value = query.get(name) if hasattr(query, "get") else None
        try:
            return int(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    async def _web_usage_summary(self) -> dict[str, Any]:
        try:
            return self._web_ok(await self.service.usage.summary(self._query_int("days", 30)))
        except Exception as exc:
            return self._web_error(exc)

    async def _web_usage_daily(self) -> dict[str, Any]:
        try:
            days = max(1, min(3660, self._query_int("days", 180)))
            return self._web_ok({"days": days, "daily": await self.service.usage.daily(days)})
        except Exception as exc:
            return self._web_error(exc)

    async def _web_usage_models(self) -> dict[str, Any]:
        try:
            days = max(1, min(3660, self._query_int("days", 30)))
            return self._web_ok({"days": days, "models": await self.service.usage.by_model(days), "efforts": await self.service.usage.by_effort(days)})
        except Exception as exc:
            return self._web_error(exc)

    async def _web_usage_rate_limits(self) -> dict[str, Any]:
        try:
            return self._web_ok(await self.service.read_quota())
        except Exception as exc:
            return self._web_error(exc)

    async def _web_usage_turns(self) -> dict[str, Any]:
        try:
            limit = max(1, min(200, self._query_int("limit", 20)))
            return self._web_ok({"turns": await self.service.usage.recent_turns(limit)})
        except Exception as exc:
            return self._web_error(exc)

    def _config_snapshot(self) -> dict[str, Any]:
        return {
            key: self.config.get(key, default)
            for key, default in CONFIG_DEFAULTS.items()
        }

    @staticmethod
    def _validate_config_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("设置数据必须是对象。")
        values: dict[str, Any] = {}
        string_fields = {
            "codex_path",
            "transport_proxy",
            "default_model",
            "reasoning_effort",
            "harness_mode",
            "tool_router",
            "usage_timezone",
        }
        bool_fields = {
            "streaming",
            "show_tool_status",
            "force_http_transport",
            "enable_local_codex_tools",
            "usage_debug",
            "use_system_proxy",
        }
        for key, value in payload.items():
            if key not in CONFIG_DEFAULTS:
                continue
            if key == "login_mode":
                if value not in {"browser", "device_code"}:
                    raise ValueError("登录方式只能是 browser 或 device_code。")
                values[key] = value
            elif key == "backend_mode":
                if value not in {"app_server", "transport", "auto"}:
                    raise ValueError("backend_mode 只能是 app_server、transport 或 auto。")
                values[key] = value
            elif key == "harness_mode":
                if value not in {"lightweight", "codex"}:
                    raise ValueError("harness_mode 只能是 lightweight 或 codex。")
                values[key] = value
            elif key == "tool_router":
                if value not in {"none", "minimal", "all"}:
                    raise ValueError("tool_router 只能是 none、minimal 或 all。")
                values[key] = value
            elif key == "transport_proxy":
                if not isinstance(value, str) or len(value.strip()) > 512:
                    raise ValueError("Transport 代理必须是 512 个字符以内的文本。")
                proxy = value.strip()
                if proxy:
                    parsed = urlsplit(proxy)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                        raise ValueError("Transport 代理必须是 http:// 或 https:// 地址。")
                    if parsed.username or parsed.password:
                        raise ValueError("Transport 代理地址不能包含用户名或密码。")
                    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                        raise ValueError("Transport 代理只支持主机和端口，不支持路径或查询参数。")
                values[key] = proxy
            elif key in string_fields:
                if not isinstance(value, str) or len(value.strip()) > 512:
                    raise ValueError(f"{key} 必须是 512 个字符以内的文本。")
                values[key] = value.strip() or CONFIG_DEFAULTS[key]
            elif key in bool_fields:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} 必须是布尔值。")
                values[key] = value
            elif key in CONFIG_RANGES:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{key} 必须是整数。")
                low, high = CONFIG_RANGES[key]
                if not low <= value <= high:
                    raise ValueError(f"{key} 必须在 {low} 到 {high} 之间。")
                values[key] = value
        return values

    async def _web_config(self) -> dict[str, Any]:
        try:
            if web_request is not None:
                body = await web_request.json({})
                if isinstance(body, dict) and body.get("config") is not None:
                    values = self._validate_config_payload(body.get("config"))
                    if values:
                        save_config = getattr(self.config, "save_config", None)
                        if callable(save_config):
                            save_config(values)
                        else:
                            self.config.update(values)
                        if "default_model" in values:
                            self.service.set_model(str(values["default_model"]))
                        if "reasoning_effort" in values:
                            self.service.set_effort(str(values["reasoning_effort"]))
                        if "harness_mode" in values:
                            self.service.set_harness_mode(str(values["harness_mode"]))
                        if "tool_router" in values:
                            self.service.set_tool_router_mode(str(values["tool_router"]))
                        self.service.manager.codex_path = str(
                            self.config.get("codex_path", "codex")
                        )
                        self.service.manager.force_http_transport = bool(
                            self.config.get("force_http_transport", True)
                        )
                        if "transport_proxy" in values or "use_system_proxy" in values:
                            await self.service.set_network_proxy(
                                str(self.config.get("transport_proxy", "") or ""),
                                use_system_proxy=bool(self.config.get("use_system_proxy", True)),
                            )
                        if (
                            "usage_timezone" in values
                            or "usage_retention_days" in values
                            or "usage_debug" in values
                        ):
                            self.service.update_usage_config()
                    restart_required = any(
                        key in values
                        for key in (
                            "codex_path",
                            "force_http_transport",
                            "max_concurrent_turns",
                        )
                    )
                    return self._web_ok(
                        {
                            "config": self._config_snapshot(),
                            "restartRequired": restart_required,
                        }
                    )
            return self._web_ok({"config": self._config_snapshot()})
        except Exception as exc:
            return self._web_error(exc)

    async def terminate(self) -> None:
        await self.service.close()

    @staticmethod
    def _fmt(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    @filter.command_group("gpt")
    def gpt(self):
        """ChatGPT Codex subscription bridge commands."""

    @gpt.command("status")
    async def gpt_status(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(
                "ChatGPT Codex 状态\n" + self._fmt(await self.service.status())
            )
        except Exception as exc:
            yield event.plain_result(f"状态读取失败：{safe_error(exc)}")

    @gpt.command("login")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_login(self, event: AstrMessageEvent):
        try:
            result = await self.service.login_start(str(self.config.get("login_mode", "browser")))
            if result.get("type") == "chatgptDeviceCode":
                # The code is intentionally shown only to the requesting admin and never logged/persisted.
                yield event.plain_result(
                    "请在浏览器打开以下地址并输入一次性验证码（不会写入插件日志）：\n"
                    f"{result.get('verificationUrl', '')}\n验证码：{result.get('userCode', '')}"
                )
            else:
                yield event.plain_result(
                    "请在浏览器完成 ChatGPT 登录。授权地址（不会写入插件日志）：\n"
                    + str(result.get("authUrl", "未返回授权地址"))
                )
        except Exception as exc:
            yield event.plain_result(f"登录启动失败：{safe_error(exc)}")

    @gpt.command("logout")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_logout(self, event: AstrMessageEvent):
        try:
            await self.service.logout()
            yield event.plain_result("已退出 Codex ChatGPT 登录。")
        except Exception as exc:
            yield event.plain_result(f"退出登录失败：{safe_error(exc)}")

    @gpt.command("models")
    async def gpt_models(self, event: AstrMessageEvent):
        try:
            models = await self.service.list_models(refresh=True)
            if not models:
                yield event.plain_result("服务端没有返回可用模型。请先 /gpt login。")
                return
            lines = [
                f"- {model.id}"
                + (f" ({model.display_name})" if model.display_name else "")
                + (
                    f" | effort: {', '.join(model.reasoning_efforts)}"
                    if model.reasoning_efforts
                    else ""
                )
                for model in models
                if not model.hidden
            ]
            yield event.plain_result("服务端 model/list 模型：\n" + "\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"模型读取失败：{safe_error(exc)}")

    @gpt.command("model")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_model(self, event: AstrMessageEvent, model_id: str):
        try:
            models = await self.service.list_models()
            selected = next(
                (model for model in models if model.id == model_id and not model.hidden), None
            )
            if not selected:
                yield event.plain_result(
                    "模型不在当前服务端 model/list 列表中，请使用 /gpt models 查看。"
                )
                return
            self.service.set_model(model_id)
            yield event.plain_result(f"已选择模型：{model_id}")
        except Exception as exc:
            yield event.plain_result(f"模型设置失败：{safe_error(exc)}")

    @gpt.command("effort")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_effort(self, event: AstrMessageEvent, level: str):
        try:
            if level != "auto":
                await self.service.list_models()
                selected = self.service.catalog.choose(self.service.default_model)
                if (
                    selected
                    and selected.reasoning_efforts
                    and level not in selected.reasoning_efforts
                ):
                    yield event.plain_result(
                        f"当前模型不支持 effort={level}。可选：{', '.join(selected.reasoning_efforts)}"
                    )
                    return
            self.service.set_effort(level)
            yield event.plain_result(f"已设置 reasoning effort：{level}")
        except Exception as exc:
            yield event.plain_result(f"effort 设置失败：{safe_error(exc)}")

    @gpt.command("harness")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_harness(self, event: AstrMessageEvent, mode: str):
        if mode not in {"lightweight", "codex"}:
            yield event.plain_result("harness 只能是 lightweight 或 codex。")
            return
        self.service.set_harness_mode(mode)
        yield event.plain_result(
            f"已切换 Harness：{mode}。当前会话将在下一轮按新配置创建或恢复 thread。"
        )

    @gpt.command("prompt-debug")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_prompt_debug(self, event: AstrMessageEvent):
        """Show redacted prompt composition diagnostics, never raw prompt text."""

        try:
            yield event.plain_result(
                "Codex Prompt 诊断（仅长度/指纹，不含原文）\n"
                + self._fmt(await self.service.prompt_debug())
            )
        except Exception as exc:
            yield event.plain_result(f"Prompt 诊断读取失败：{safe_error(exc)}")

    @gpt.command("quota")
    async def gpt_quota(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(
                "Codex ChatGPT 配额：\n" + self._fmt(await self.service.read_quota())
            )
        except Exception as exc:
            yield event.plain_result(f"配额读取失败：{safe_error(exc)}")

    @gpt.command("benchmark")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def gpt_benchmark(self, event: AstrMessageEvent, backend: str = "transport"):
        """Run one explicit real hello benchmark; never runs automatically."""

        if backend not in {"transport", "app_server"}:
            yield event.plain_result("用法：/gpt benchmark transport 或 /gpt benchmark app_server")
            return
        try:
            result = await self.service.benchmark_backend(backend)
            yield event.plain_result(
                f"已完成一次真实 {backend} hello benchmark（会产生一次实际用量）：\n"
                + self._fmt(result)
            )
        except Exception as exc:
            yield event.plain_result(f"benchmark 失败：{safe_error(exc)}")

    @gpt.command("usage")
    async def gpt_usage(self, event: AstrMessageEvent, period: str = "30d"):
        """Show local token usage; it is intentionally separate from /gpt quota."""

        value = str(period or "30d").lower()
        if value == "reset":
            checker = getattr(event, "is_admin", None)
            if not callable(checker) or not checker():
                yield event.plain_result("/gpt usage reset 仅管理员可用。")
                return
            try:
                result = await self.service.usage.reset()
                yield event.plain_result(
                    "已重置当前 Usage 统计。\n"
                    f"删除当前记录：{result.get('records', 0)}，快照：{result.get('snapshots', 0)}。\n"
                    "历史 v1 记录未删除，仅保留为迁移审计数据。"
                )
            except Exception as exc:
                yield event.plain_result(f"Usage 重置失败：{safe_error(exc)}")
            return
        if value == "debug":
            checker = getattr(event, "is_admin", None)
            if not callable(checker) or not checker():
                yield event.plain_result("/gpt usage debug 仅管理员可用。")
                return
            try:
                debug = await self.service.usage.debug()
                yield event.plain_result("Codex 本地 Usage 调试状态：\n" + self._fmt(debug))
            except Exception as exc:
                yield event.plain_result(f"Usage 调试读取失败：{safe_error(exc)}")
            return
        days = 30
        if value == "today":
            days = 1
        elif value.endswith("d") and value[:-1].isdigit():
            days = max(1, min(3660, int(value[:-1])))
        try:
            summary = await self.service.usage.summary(days)
            window = summary["today"] if value == "today" else summary["window"]
            lines = [
                "ChatGPT Codex 本地 Usage（不是官方配额）",
                f"时间范围：{summary['startDate']} 至 {summary['endDate']}（{summary['timezone']}）",
                f"Processed total：{window.get('total_tokens') if window.get('total_tokens') is not None else 'Unavailable'}",
                f"Input：{window.get('input_tokens') if window.get('input_tokens') is not None else 'Unavailable'}",
                f"Cached input：{window.get('cached_input_tokens') if window.get('cached_input_tokens') is not None else 'Unavailable'}",
                f"Output：{window.get('output_tokens') if window.get('output_tokens') is not None else 'Unavailable'}",
                f"Reasoning：{window.get('reasoning_tokens') if window.get('reasoning_tokens') is not None else 'Unavailable'}",
                f"Requests：{window.get('requests', 0)}",
                "说明：Cached input 是 Input 的子集；Reasoning 是 Output 的分项，不会重复计入 Processed total。",
            ]
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            yield event.plain_result(f"Usage 读取失败：{safe_error(exc)}")

    @gpt.command("reset")
    async def gpt_reset(self, event: AstrMessageEvent):
        session_key = (
            getattr(event, "unified_msg_origin", None)
            or getattr(event, "session_id", None)
            or "astrbot:default"
        )
        try:
            removed = await self.service.reset_session(str(session_key))
            yield event.plain_result(
                "已重置当前会话的 Codex thread。"
                if removed
                else "当前会话没有已保存的 Codex thread。"
            )
        except Exception as exc:
            yield event.plain_result(f"会话重置失败：{safe_error(exc)}")
