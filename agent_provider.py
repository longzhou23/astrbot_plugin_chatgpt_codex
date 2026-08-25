"""Thin AstrBot Provider adapter; all orchestration remains in CodexService."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from .codex_service import CodexService
from .transport.responses import openai_tools_to_responses

try:
    from astrbot.api.provider import Provider
    from astrbot.core.agent.message import ContentPart, Message
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.provider.entities import LLMResponse
    from astrbot.core.provider.register import register_provider_adapter

    _ASTRBOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ASTRBOT_AVAILABLE = False


_SERVICE: CodexService | None = None


def bind_service(service: CodexService) -> None:
    global _SERVICE
    _SERVICE = service


def _conversation_key(session_id: str | None) -> str:
    """Use AstrBot's unified conversation id; never fall back to a global thread."""

    key = (session_id or "").strip()
    if not key:
        raise RuntimeError(
            "AstrBot did not provide a conversation session id; refusing to share a Codex thread"
        )
    return key


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
        if not latest:
            latest = candidate
        if candidate and candidate == latest:
            plain.pop()
    return latest or None, plain


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

    final_text = ""
    emitted_text = False
    async for event in events:
        kind = event.get("kind")
        if kind == "delta":
            text = str(event.get("text", ""))
            if text:
                emitted_text = True
                yield LLMResponse(role="assistant", completion_text=text, is_chunk=True)
        elif kind == "tool_call":
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
                yield LLMResponse(
                    role="assistant",
                    tools_call_args=args,
                    tools_call_name=names,
                    tools_call_ids=ids,
                    is_chunk=False,
                )
        elif kind == "final":
            final_text = str(event.get("text", ""))
            if final_text and not emitted_text:
                yield LLMResponse(role="assistant", completion_text=final_text, is_chunk=True)
    yield LLMResponse(role="assistant", completion_text="", is_chunk=False)


if _ASTRBOT_AVAILABLE:

    @register_provider_adapter(
        "chatgpt_codex",
        "Official Codex App Server bridge using the current ChatGPT account login",
        default_config_tmpl={
            "type": "chatgpt_codex",
            "id": "chatgpt_codex",
            "provider": "ChatGPT Codex Subscription",
            "provider_type": "chat_completion",
            "enable": False,
            "key": ["chatgpt-subscription"],
            "model": "auto",
        },
        provider_display_name="ChatGPT Codex Subscription",
    )
    class CodexProvider(Provider):
        def __init__(self, provider_config: dict, provider_settings: dict) -> None:
            super().__init__(provider_config, provider_settings)
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
            return [model.id for model in await self._service().list_models() if not model.hidden]

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
            **kwargs: Any,
        ) -> LLMResponse:
            del image_urls, audio_urls, tool_calls_result, kwargs
            if session_id is None and _is_title_generation_request(prompt, contexts, system_prompt):
                return LLMResponse(role="assistant", completion_text="<None>")
            latest_prompt, historical_contexts = _normalize_request_inputs(prompt, contexts)
            text = await self._service().run_turn(
                session_key=_conversation_key(session_id),
                prompt=latest_prompt,
                contexts=historical_contexts,
                system_prompt=system_prompt,
                extra_user_content_parts=extra_user_content_parts,
                model=model or self.model_name,
                tools=openai_tools_to_responses(func_tool),
            )
            return LLMResponse(role="assistant", completion_text=text)

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
            **kwargs: Any,
        ) -> AsyncGenerator[LLMResponse, None]:
            del image_urls, audio_urls, tool_calls_result, kwargs
            if session_id is None and _is_title_generation_request(prompt, contexts, system_prompt):
                yield LLMResponse(role="assistant", completion_text="<None>", is_chunk=False)
                return
            latest_prompt, historical_contexts = _normalize_request_inputs(prompt, contexts)
            events = self._service().stream_turn(
                session_key=_conversation_key(session_id),
                prompt=latest_prompt,
                contexts=historical_contexts,
                system_prompt=system_prompt,
                extra_user_content_parts=extra_user_content_parts,
                model=model or self.model_name,
                tools=openai_tools_to_responses(func_tool),
            )
            async for response in _stream_provider_responses(events):
                # AstrBot's Agent Runner requires the final non-chunk response to
                # transition the step to DONE. Tool calls are returned as structured
                # fields so AstrBot, rather than Codex, executes the next loop step.
                yield response

else:  # pragma: no cover
    CodexProvider = None  # type: ignore[assignment,misc]
