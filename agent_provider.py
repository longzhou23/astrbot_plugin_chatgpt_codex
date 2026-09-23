"""Thin AstrBot Provider adapter; all orchestration remains in CodexService."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from .codex_errors import CodexCapabilityError
from .codex_service import CodexService
from .transport.responses import openai_tools_to_responses
from .transport.types import TransportError

try:
    from astrbot.api.provider import Provider
    from astrbot.core.agent.message import ContentPart, Message
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.entities import LLMResponse
    from astrbot.core.provider.entities import TokenUsage as AstrBotTokenUsage
    from astrbot.core.provider.register import (
        provider_cls_map,
        provider_registry,
        register_provider_adapter,
    )

    _ASTRBOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ASTRBOT_AVAILABLE = False

    class AstrBotTokenUsage:  # type: ignore[no-redef]
        def __init__(self, input_other: int = 0, input_cached: int = 0, output: int = 0) -> None:
            self.input_other = input_other
            self.input_cached = input_cached
            self.output = output

    class LLMResponse:  # type: ignore[no-redef]
        """Small host-free contract double used by adapter unit tests."""

        def __init__(
            self,
            role: str,
            completion_text: str | None = None,
            tools_call_args: list[dict[str, Any]] | None = None,
            tools_call_name: list[str] | None = None,
            tools_call_ids: list[str] | None = None,
            reasoning_signature: str | None = None,
            is_chunk: bool = False,
            usage: AstrBotTokenUsage | None = None,
        ) -> None:
            self.role = role
            self.completion_text = completion_text or ""
            self.tools_call_args = tools_call_args or []
            self.tools_call_name = tools_call_name or []
            self.tools_call_ids = tools_call_ids or []
            self.reasoning_signature = reasoning_signature
            self.is_chunk = is_chunk
            self.usage = usage


_SERVICE: CodexService | None = None

_SUPPORTED_MODALITIES = ("text", "image", "audio", "tool_use")
_SUPPORTED_REQUEST_OPTIONS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_output_tokens",
    "response_format",
    "parallel_tool_calls",
    "stop",
}


def bind_service(service: CodexService) -> None:
    global _SERVICE
    _SERVICE = service


def _astrbot_token_usage(value: Any) -> AstrBotTokenUsage | None:
    """Map one server turn without counting reasoning as output a second time."""

    if not isinstance(value, dict):
        return None

    def token(name: str) -> int | None:
        item = value.get(name)
        return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None

    input_tokens = token("input_tokens")
    cached_tokens = token("cached_input_tokens")
    output_tokens = token("output_tokens")
    if input_tokens is None and cached_tokens is None and output_tokens is None:
        return None
    cached = (
        min(cached_tokens or 0, input_tokens) if input_tokens is not None else (cached_tokens or 0)
    )
    return AstrBotTokenUsage(
        input_other=max(0, (input_tokens or 0) - cached),
        input_cached=cached,
        output=output_tokens or 0,
    )


def _normalize_response_format(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodexCapabilityError("response_format 必须是 OpenAI 兼容的对象")
    kind = value.get("type")
    if kind in {"text", "json_object"}:
        return {"type": kind}
    if kind == "json_schema":
        schema = value.get("json_schema")
        if not isinstance(schema, dict):
            raise CodexCapabilityError("response_format.json_schema 必须是对象")
        normalized = {"type": "json_schema"}
        for key in ("name", "description", "schema", "strict"):
            if key in schema:
                normalized[key] = schema[key]
        if not isinstance(normalized.get("name"), str) or not isinstance(
            normalized.get("schema"), dict
        ):
            raise CodexCapabilityError("json_schema response_format 需要 name 和 schema")
        return normalized
    raise CodexCapabilityError(f"不支持的 response_format 类型：{kind or 'missing'}")


def _normalize_request_options(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Validate the small Responses parameter subset this provider preserves."""

    unknown = sorted(set(kwargs) - _SUPPORTED_REQUEST_OPTIONS)
    if unknown:
        raise CodexCapabilityError("不支持的模型调用参数：" + ", ".join(unknown))
    if kwargs.get("stop") not in (None, [], ""):
        raise CodexCapabilityError("Codex Responses Transport 当前不支持 stop 参数")

    options: dict[str, Any] = {}
    for key, lower, upper in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        value = kwargs.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodexCapabilityError(f"{key} 必须是数字")
        if not lower <= float(value) <= upper:
            raise CodexCapabilityError(f"{key} 必须位于 {lower:g} 到 {upper:g} 之间")
        options[key] = value

    max_tokens = kwargs.get("max_output_tokens")
    alias_tokens = kwargs.get("max_tokens")
    if max_tokens is not None and alias_tokens is not None and max_tokens != alias_tokens:
        raise CodexCapabilityError("max_tokens 与 max_output_tokens 不能设置为不同值")
    max_tokens = max_tokens if max_tokens is not None else alias_tokens
    if max_tokens is not None:
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise CodexCapabilityError("max_output_tokens 必须是正整数")
        options["max_output_tokens"] = max_tokens

    parallel = kwargs.get("parallel_tool_calls")
    if parallel is not None:
        if not isinstance(parallel, bool):
            raise CodexCapabilityError("parallel_tool_calls 必须是布尔值")
        options["parallel_tool_calls"] = parallel

    response_format = kwargs.get("response_format")
    if response_format is not None:
        options["text"] = {"format": _normalize_response_format(response_format)}
    return options


def _validated_tool_choice(value: Any, tools: list[dict[str, Any]]) -> str:
    choice = value or "auto"
    if choice not in {"auto", "required"}:
        raise CodexCapabilityError("tool_choice 只能是 auto 或 required")
    if choice == "required" and not tools:
        raise CodexCapabilityError("tool_choice=required 时必须提供至少一个 AstrBot 工具")
    return str(choice)


def _ensure_supported_modalities(provider_config: dict[str, Any]) -> list[str]:
    """Migrate provider records created by older beta releases.

    AstrBot's Agent Runner removes every function tool before calling a provider
    when its persisted ``modalities`` list does not contain ``tool_use``. Early
    plugin builds saved text/image-only lists, so merely changing the provider
    template does not repair an existing installation.
    """

    configured = provider_config.get("modalities")
    values = (
        [str(item) for item in configured if isinstance(item, str)]
        if isinstance(configured, list)
        else []
    )
    for modality in _SUPPORTED_MODALITIES:
        if modality not in values:
            values.append(modality)
    provider_config["modalities"] = values
    return values


def _conversation_key(session_id: str | None) -> str:
    """Resolve a conversation id without ever falling back to a shared thread.

    AstrBot's ``Context.llm_generate`` helper does not provide a conversation
    id for background/plugin-owned calls. Those calls still need to work, but
    must not reuse a normal chat thread. Give each such invocation a unique
    ephemeral key; callers that provide AstrBot's unified id retain the durable
    conversation mapping.
    """

    key = (session_id or "").strip()
    return key or f"__astrbot_ephemeral__:{uuid.uuid4().hex}"


def _is_ephemeral_session(session_id: str | None) -> bool:
    return not bool((session_id or "").strip())


def _normalize_request_inputs(
    prompt: str | None,
    contexts: list[Message] | list[dict] | None,
) -> tuple[str | None, list[dict]]:
    """Split AstrBot's latest user message from its historical contexts.

    AstrBot 4.27 normally passes the current user message as the final context
    entry and leaves ``prompt`` unset.  Forwarding that shape unchanged makes
    Codex receive an empty latest message and, after the first turn, lose every
    subsequent user message because the historical bootstrap is intentionally
    sent only once.
    """

    plain = [
        item.model_dump() if hasattr(item, "model_dump") else item
        for item in (contexts or [])
        if isinstance(item, dict) or hasattr(item, "model_dump")
    ]
    latest = (prompt or "").strip()
    if plain and isinstance(plain[-1], dict) and plain[-1].get("role") == "user":
        content = plain[-1].get("content")
        candidate = CodexService._content_text(content).strip()
        if _has_non_text_content(content):
            # A multimodal current message must stay in contexts. Removing it
            # here would discard its image/audio/reply/file/video parts before
            # the Responses adapter can translate them. Avoid duplicating its
            # text as a separate latest prompt as well.
            if not latest or latest == candidate:
                latest = None
        else:
            if not latest:
                latest = candidate
        if candidate and candidate == latest and not _has_non_text_content(content):
            plain.pop()
    return latest or None, plain


def _has_non_text_content(content: Any) -> bool:
    """Tell whether a provider message contains media or attachment parts."""

    if isinstance(content, dict):
        parts = [content]
    elif isinstance(content, list):
        parts = content
    else:
        return False
    for part in parts:
        value = part.model_dump() if hasattr(part, "model_dump") else part
        if not isinstance(value, dict):
            continue
        if value.get("type") not in {"text", "input_text", "output_text"}:
            return True
    return False


def _is_title_generation_request(
    prompt: str | None,
    contexts: list[Message] | list[dict] | None,
    system_prompt: str | None = None,
) -> bool:
    """Recognize AstrBot ChatUI's internal, sessionless title-only request."""

    candidates: list[str] = []
    if prompt:
        candidates.append(prompt)
    if system_prompt:
        candidates.append(system_prompt)
    for item in contexts or []:
        value = item.model_dump() if hasattr(item, "model_dump") else item
        if not isinstance(value, dict) or value.get("role") not in ("system", "developer"):
            continue
        candidates.append(CodexService._content_text(value.get("content")))
    instruction = "\n".join(candidates).lower()
    return (
        "conversation title generator" in instruction and "generate a concise title" in instruction
    )


async def _stream_frames(
    events: AsyncGenerator[dict[str, Any], None],
) -> AsyncGenerator[tuple[str, bool], None]:
    """Translate service events into AstrBot chunks plus one terminal response."""

    emitted_text = False
    async for event in events:
        if event.get("kind") not in ("delta", "final", "status"):
            continue
        text = str(event.get("text", ""))
        if event.get("kind") == "final":
            if not emitted_text:
                yield text, True
        else:
            if text:
                emitted_text = True
                yield text, True
    # The terminal marker must carry no text: AstrBot uses it to close the
    # Agent Runner step, while repeating the answer here would render it twice.
    yield "", False


async def _stream_provider_responses(
    events: AsyncGenerator[dict[str, Any], None],
) -> AsyncGenerator[LLMResponse, None]:
    """Keep transport deltas and hand function calls to AstrBot's Agent Runner."""

    accumulated_text: list[str] = []
    final_text = ""
    emitted_text = False
    reasoning_signature: str | None = None
    terminal_usage: AstrBotTokenUsage | None = None
    saw_tool_call = False
    async for event in events:
        kind = event.get("kind")
        candidate_signature = event.get("reasoning_signature")
        if isinstance(candidate_signature, str):
            reasoning_signature = candidate_signature
        if kind == "delta":
            text = str(event.get("text", ""))
            if text:
                accumulated_text.append(text)
                emitted_text = True
                yield LLMResponse(role="assistant", completion_text=text, is_chunk=True)
        elif kind == "tool_call":
            terminal_usage = _astrbot_token_usage(event.get("usage"))
            calls = event.get("tool_calls") if isinstance(event.get("tool_calls"), list) else []
            args: list[dict[str, Any]] = []
            names: list[str] = []
            ids: list[str] = []
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                    continue
                raw = call.get("arguments", "{}")
                try:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                except (TypeError, ValueError):
                    value = {}
                args.append(value if isinstance(value, dict) else {})
                names.append(call["name"])
                ids.append(str(call.get("call_id", "")))
            if names:
                saw_tool_call = True
                yield LLMResponse(
                    role="tool",
                    tools_call_args=args,
                    tools_call_name=names,
                    tools_call_ids=ids,
                    reasoning_signature=reasoning_signature,
                    is_chunk=False,
                    usage=terminal_usage,
                )
        elif kind == "final":
            final_text = str(event.get("text", ""))
            terminal_usage = _astrbot_token_usage(event.get("usage"))
            if final_text and not emitted_text:
                yield LLMResponse(role="assistant", completion_text=final_text, is_chunk=True)

    # A tool-call response is already the terminal Agent Runner step. Emitting
    # another empty assistant response here makes AstrBot report an empty
    # assistant message and can cause duplicate/replayed turns.
    if saw_tool_call:
        return
    # AstrBot 4.27 requires a non-chunk response to close the Agent Runner
    # step. It must carry the completed text; an empty sentinel is interpreted
    # as an empty assistant message and produces a warning (and can lose the
    # response when no streaming delta reached the outer pipeline).
    completed_text = final_text or "".join(accumulated_text)
    if not completed_text.strip():
        raise TransportError("Codex transport 返回空白响应")
    yield LLMResponse(
        role="assistant",
        completion_text=completed_text,
        reasoning_signature=reasoning_signature,
        is_chunk=False,
        usage=terminal_usage,
    )


async def _collect_provider_response(
    events: AsyncGenerator[dict[str, Any], None],
) -> LLMResponse:
    """Collect one non-streaming response without discarding function calls.

    AstrBot uses ``text_chat`` for several plugin-owned and non-streaming Agent
    Runner paths.  Returning only ``run_turn()`` text loses a perfectly valid
    function-call response and makes AstrBot report an empty assistant message.
    Reuse the same terminal adapter as streaming so both paths have identical
    text, tool-call, reasoning-signature and empty-output semantics.
    """

    terminal: LLMResponse | None = None
    async for response in _stream_provider_responses(events):
        if not response.is_chunk:
            terminal = response
    if terminal is None:
        raise TransportError("Codex transport 未返回终态响应")
    return terminal


if _ASTRBOT_AVAILABLE:

    # AstrBot purges plugin modules during reload, but its provider adapter
    # registry is process-global and has no matching unregister hook. Remove
    # only a previous registration created by this exact module so a genuine
    # name collision with another plugin still raises from the decorator.
    _provider_type_name = "chatgpt_codex"
    _existing_provider = provider_cls_map.get(_provider_type_name)
    _existing_provider_cls = getattr(_existing_provider, "cls_type", None)
    if getattr(_existing_provider_cls, "__module__", None) == __name__:
        provider_cls_map.pop(_provider_type_name, None)
        provider_registry[:] = [
            metadata
            for metadata in provider_registry
            if not (
                getattr(metadata, "type", None) == _provider_type_name
                and getattr(getattr(metadata, "cls_type", None), "__module__", None)
                == __name__
            )
        ]

    @register_provider_adapter(
        _provider_type_name,
        "Official Codex App Server bridge using the current ChatGPT account login",
        default_config_tmpl={
            "type": "chatgpt_codex",
            "id": "chatgpt_codex",
            "provider": "ChatGPT Codex Subscription",
            "provider_type": "chat_completion",
            "enable": False,
            "key": ["chatgpt-subscription"],
            "model": "auto",
            "reasoning": True,
            "max_context_tokens": 1000000,
            "modalities": ["text", "image", "audio", "tool_use"],
        },
        provider_display_name="ChatGPT Codex Subscription",
    )
    class CodexProvider(Provider):
        def __init__(self, provider_config: dict, provider_settings: dict) -> None:
            super().__init__(provider_config, provider_settings)
            # Keep AstrBot's Agent Runner metadata aligned with what this
            # adapter actually forwards. Existing provider records created by
            # older beta builds may still contain reasoning=false, audio, or a
            # zero context window even though the Transport path now preserves
            # reasoning signatures and supports text/image/audio/tool input.
            self.provider_config["reasoning"] = True
            if not isinstance(self.provider_config.get("max_context_tokens"), int) or (
                self.provider_config["max_context_tokens"] <= 0
            ):
                self.provider_config["max_context_tokens"] = 1000000
            # Updating the registration template is insufficient for provider
            # records AstrBot has already persisted. In particular, an old list
            # without ``tool_use`` makes Agent Runner silently clear func_tool,
            # so star-map and every other AstrBot tool disappear for this model.
            _ensure_supported_modalities(self.provider_config)
            self.model_name = str(provider_config.get("model", "auto") or "auto")

        @staticmethod
        def _service() -> CodexService:
            if _SERVICE is None:
                raise RuntimeError("ChatGPT Codex plugin has not finished initializing")
            return _SERVICE

        def get_current_key(self) -> str:
            return "chatgpt-subscription"

        def set_key(self, key: str) -> None:
            del key

        async def test(self, timeout: float = 45.0) -> None:
            """Non-generative reachability check used by AstrBot's WebUI.

            The base Provider implementation sends a PONG prompt, which starts a
            second Codex thread and competes with the user's first turn.
            """

            async with asyncio.timeout(timeout):
                account = await self._service().account_read(refresh=False)
                if not account:
                    raise RuntimeError("ChatGPT account is not logged in")
                if not await self._service().list_models():
                    raise RuntimeError("Codex returned no available models")

        async def get_models(self) -> list[str]:
            # AstrBot calls this method when the user requests a model-list
            # refresh. Bypass the service TTL so newly advertised models are
            # visible immediately instead of waiting for the cache to expire.
            return [
                model.id
                for model in await self._service().list_models(refresh=True)
                if not model.hidden
            ]

        async def text_chat(
            self,
            prompt: str | None = None,
            session_id: str | None = None,
            image_urls: list[str] | None = None,
            audio_urls: list[str] | None = None,
            func_tool: ToolSet | None = None,
            contexts: list[Message] | list[dict] | None = None,
            system_prompt: str | None = None,
            tool_calls_result: Any = None,
            model: str | None = None,
            extra_user_content_parts: list[ContentPart] | None = None,
            tool_choice: str = "auto",
            request_max_retries: int | None = None,
            **kwargs: Any,
        ) -> LLMResponse:
            abort_signal = kwargs.pop("abort_signal", None)
            request_options = _normalize_request_options(kwargs)
            response_tools = openai_tools_to_responses(func_tool)
            selected_tool_choice = _validated_tool_choice(tool_choice, response_tools)
            if session_id is None and _is_title_generation_request(prompt, contexts, system_prompt):
                return LLMResponse(role="assistant", completion_text="<None>")
            session_key = _conversation_key(session_id)
            ephemeral = _is_ephemeral_session(session_id)
            latest_prompt, historical_contexts = _normalize_request_inputs(prompt, contexts)
            try:
                events = self._service().stream_turn(
                    session_key=session_key,
                    prompt=latest_prompt,
                    contexts=historical_contexts,
                    system_prompt=system_prompt,
                    extra_user_content_parts=extra_user_content_parts,
                    image_urls=image_urls,
                    audio_urls=audio_urls,
                    tool_calls_result=tool_calls_result,
                    model=model or self.model_name,
                    tools=response_tools,
                    tool_choice=selected_tool_choice,
                    request_max_retries=request_max_retries,
                    abort_signal=abort_signal,
                    request_options=request_options,
                )
                return await _collect_provider_response(events)
            finally:
                if ephemeral:
                    with contextlib.suppress(Exception):
                        await self._service().reset_session(session_key)

        async def text_chat_stream(
            self,
            prompt: str | None = None,
            session_id: str | None = None,
            image_urls: list[str] | None = None,
            audio_urls: list[str] | None = None,
            func_tool: ToolSet | None = None,
            contexts: list[Message] | list[dict] | None = None,
            system_prompt: str | None = None,
            tool_calls_result: Any = None,
            model: str | None = None,
            extra_user_content_parts: list[ContentPart] | None = None,
            tool_choice: str = "auto",
            request_max_retries: int | None = None,
            **kwargs: Any,
        ) -> AsyncGenerator[LLMResponse, None]:
            abort_signal = kwargs.pop("abort_signal", None)
            request_options = _normalize_request_options(kwargs)
            response_tools = openai_tools_to_responses(func_tool)
            selected_tool_choice = _validated_tool_choice(tool_choice, response_tools)
            if session_id is None and _is_title_generation_request(prompt, contexts, system_prompt):
                yield LLMResponse(role="assistant", completion_text="<None>", is_chunk=False)
                return
            session_key = _conversation_key(session_id)
            ephemeral = _is_ephemeral_session(session_id)
            latest_prompt, historical_contexts = _normalize_request_inputs(prompt, contexts)
            try:
                events = self._service().stream_turn(
                    session_key=session_key,
                    prompt=latest_prompt,
                    contexts=historical_contexts,
                    system_prompt=system_prompt,
                    extra_user_content_parts=extra_user_content_parts,
                    image_urls=image_urls,
                    audio_urls=audio_urls,
                    tool_calls_result=tool_calls_result,
                    model=model or self.model_name,
                    tools=response_tools,
                    tool_choice=selected_tool_choice,
                    request_max_retries=request_max_retries,
                    abort_signal=abort_signal,
                    request_options=request_options,
                )
                async for response in _stream_provider_responses(events):
                    # AstrBot's Agent Runner requires the final non-chunk response to
                    # transition the step to DONE. Tool calls are returned as structured
                    # fields so AstrBot, rather than Codex, executes the next loop step.
                    yield response
            finally:
                if ephemeral:
                    with contextlib.suppress(Exception):
                        await self._service().reset_session(session_key)

else:  # pragma: no cover
    CodexProvider = None  # type: ignore[assignment,misc]
