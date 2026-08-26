from __future__ import annotations

import json
from typing import Any

from .types import TransportResponse, TransportToolCall, TransportUsage

_REASONING_STATE_TYPE = "openai_responses_reasoning"


def _as_dict(value: Any) -> dict[str, Any] | None:
    """Convert AstrBot/Pydantic content objects without invoking ``repr``."""

    if isinstance(value, dict):
        return value
    for method_name in ("model_dump_for_context", "model_dump"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except Exception:
                return None
            return dumped if isinstance(dumped, dict) else None
    return None


def _content_text(value: Any) -> str:
    """Extract visible text from strings, dicts, and AstrBot content parts."""

    if isinstance(value, str):
        return value
    value_dict = _as_dict(value)
    if value_dict is not None:
        text = value_dict.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(value, list):
        return "".join(_content_text(item) for item in value)
    return ""


def _reasoning_items(part: dict[str, Any]) -> list[dict[str, Any]]:
    """Restore opaque Responses reasoning items, never plaintext reasoning."""

    if part.get("type") != "think":
        return []
    encrypted = part.get("encrypted")
    if not isinstance(encrypted, str):
        return []
    try:
        state = json.loads(encrypted)
    except (TypeError, ValueError):
        return []
    if not isinstance(state, dict) or state.get("type") != _REASONING_STATE_TYPE:
        return []
    items = state.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Map AstrBot/OpenAI image parts to a Responses input image."""

    part_type = part.get("type")
    if part_type == "input_image":
        image_url = part.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            return None
        detail = part.get("detail", "auto")
    elif part_type == "image_url":
        image = part.get("image_url")
        image_url = image.get("url") if isinstance(image, dict) else image
        if not isinstance(image_url, str) or not image_url:
            return None
        detail = image.get("detail", "auto") if isinstance(image, dict) else "auto"
    else:
        return None
    if detail not in {"low", "high", "auto"}:
        detail = "auto"
    return {"type": "input_image", "detail": detail, "image_url": image_url}


def _content_parts(value: Any, *, role: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map one message's content and return (visible parts, opaque reasoning)."""

    if isinstance(value, str):
        content_type = "output_text" if role == "assistant" else "input_text"
        return ([{"type": content_type, "text": value}] if value else []), []

    value_dict = _as_dict(value)
    if value_dict is not None:
        value = [value_dict]
    if not isinstance(value, list):
        return [], []

    content: list[dict[str, Any]] = []
    reasoning: list[dict[str, Any]] = []
    output_text: list[str] = []
    for raw_part in value:
        part = _as_dict(raw_part)
        if part is None:
            continue
        part_type = part.get("type")
        reasoning.extend(_reasoning_items(part))
        if part_type in {"think", "reasoning"}:
            continue
        if part_type in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if not isinstance(text, str) or not text:
                continue
            if role == "assistant":
                output_text.append(text)
            else:
                content.append({"type": "input_text", "text": text})
            continue
        image = _image_part(part) if role != "assistant" else None
        if image is not None:
            content.append(image)
            continue
        if part_type in {"audio_url", "input_audio"}:
            # AstrBot's native Responses provider degrades audio history to a
            # marker because GPT-5.6 Codex does not accept Chat Completions'
            # audio part shape directly here.
            if role == "assistant":
                output_text.append("[Audio]")
            else:
                content.append({"type": "input_text", "text": "[Audio]"})

    if role == "assistant" and output_text:
        content = [{"type": "output_text", "text": "".join(output_text)}]
    return content, reasoning


def _function_call_items(tool_calls: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(tool_calls, list):
        return result
    for call in tool_calls:
        call_dict = _as_dict(call)
        if call_dict is None:
            continue
        function = call_dict.get("function")
        if not isinstance(function, dict):
            continue
        call_id = call_dict.get("id") or call_dict.get("call_id")
        name = function.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, default=str)
        result.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return result


def _tool_result_messages(value: Any) -> list[dict[str, Any]]:
    """Extract OpenAI-shaped messages from AstrBot ToolCallsResult objects."""

    if value is None:
        return []
    entries = value if isinstance(value, list) else [value]
    result: list[dict[str, Any]] = []
    for entry in entries:
        converter = getattr(entry, "to_openai_messages", None)
        if callable(converter):
            try:
                messages = converter()
            except Exception:
                messages = []
            if isinstance(messages, list):
                result.extend(message for message in messages if isinstance(message, dict))
            continue
        if isinstance(entry, dict) and entry.get("role") in {"assistant", "tool"}:
            result.append(entry)
    return result


def build_input_items(
    contexts: list[dict[str, Any]] | None,
    prompt: str | None,
    extra_user_content_parts: list[Any] | None = None,
    image_urls: list[str] | None = None,
    audio_urls: list[str] | None = None,
    tool_calls_result: Any = None,
    include_latest: bool = True,
) -> list[dict[str, Any]]:
    """Convert AstrBot messages to the Responses input-item shape.

    System/developer messages belong in ``instructions`` and are intentionally
    excluded here.  No Codex-specific thread IDs or internal metadata are sent.
    """

    result: list[dict[str, Any]] = []
    for message in contexts or []:
        message = _as_dict(message)
        if message is None:
            continue
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                output = message.get("content", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False, default=str)
                result.append({"type": "function_call_output", "call_id": call_id, "output": output})
            continue
        if role not in {"user", "assistant", "developer"}:
            continue
        content, reasoning = _content_parts(message.get("content"), role=role)
        result.extend(reasoning)
        if content:
            result.append({"type": "message", "role": role, "content": content})
        if role == "assistant":
            result.extend(_function_call_items(message.get("tool_calls")))

    latest_content: list[dict[str, Any]] = []
    latest = (prompt or "").strip() or "(The user sent an empty message.)"
    if include_latest:
        latest_content.append({"type": "input_text", "text": latest})
    extras = list(extra_user_content_parts or [])
    if extras:
        latest_content.append({"type": "input_text", "text": "<astrbot_dynamic_context>"})
        for part in extras:
            content, _ = _content_parts(part, role="user")
            latest_content.extend(content)
        latest_content.append({"type": "input_text", "text": "</astrbot_dynamic_context>"})
    for image_url in image_urls or []:
        if isinstance(image_url, str) and image_url:
            image = _image_part({"type": "image_url", "image_url": {"url": image_url}})
            if image is not None:
                latest_content.append(image)
    for _audio_url in audio_urls or []:
        # Preserve that an audio attachment existed without sending an invalid
        # Responses payload for a model that does not accept this audio shape.
        latest_content.append({"type": "input_text", "text": "[Audio]"})
    if latest_content:
        result.append(
            {
                "type": "message",
                "role": "user",
                "content": latest_content,
            }
        )
    for message in _tool_result_messages(tool_calls_result):
        role = message.get("role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                output = message.get("content", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False, default=str)
                result.append({"type": "function_call_output", "call_id": call_id, "output": output})
        elif role == "assistant":
            result.extend(_function_call_items(message.get("tool_calls")))
    return result


def response_request(
    *,
    model: str,
    instructions: str,
    input_items: list[dict[str, Any]],
    effort: str = "auto",
    tools: list[dict[str, Any]] | None = None,
    prompt_cache_key: str | None = None,
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    """Build a direct Responses request without Codex thread metadata."""

    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "store": False,
        "stream": True,
        # Required to replay opaque reasoning items when the caller keeps
        # ``store`` disabled. These items are kept out of visible output.
        "include": ["reasoning.encrypted_content"],
    }
    if effort and effort != "auto":
        payload["reasoning"] = {"effort": effort, "summary": "auto"}
    if tools:
        payload["tools"] = tools
    if prompt_cache_key:
        payload["prompt_cache_key"] = prompt_cache_key
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    return payload


def openai_tools_to_responses(tools: Any) -> list[dict[str, Any]]:
    """Convert AstrBot's OpenAI-shaped function schemas to Responses tools."""

    if tools is None:
        return []
    try:
        source = tools.openai_schema()
    except AttributeError:
        source = tools if isinstance(tools, list) else []
    result = []
    for item in source:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        value: dict[str, Any] = {
            "type": "function",
            "name": name,
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        }
        if function.get("description"):
            value["description"] = str(function["description"])
        result.append(value)
    return result


def _text_from_item(item: Any) -> str:
    if not isinstance(item, dict) or item.get("type") not in {"message", "agent_message"}:
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if (
            isinstance(part, dict)
            and part.get("type") in {"output_text", "text", "input_text"}
            and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
    return "".join(parts)


def parse_sse_data(data: str, result: TransportResponse) -> bool:
    """Apply one Responses SSE data object; return True at terminal completion."""

    if data.strip() in {"", "[DONE]"}:
        return data.strip() == "[DONE]"
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict):
        return False
    result.event_count += 1
    kind = event.get("type")
    response = event.get("response") if isinstance(event.get("response"), dict) else {}
    if isinstance(response.get("id"), str):
        result.response_id = response["id"]
    if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
        result.text += event["delta"]
    elif kind == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            # Keep the opaque item available for the next Responses turn. It is
            # never copied into visible assistant text or logs.
            items = [item]
            if result.reasoning_signature:
                try:
                    previous_state = json.loads(result.reasoning_signature)
                except (TypeError, ValueError):
                    previous_state = None
                if (
                    isinstance(previous_state, dict)
                    and previous_state.get("type") == _REASONING_STATE_TYPE
                    and isinstance(previous_state.get("items"), list)
                ):
                    items = [
                        *[old for old in previous_state["items"] if isinstance(old, dict)],
                        item,
                    ]
            state = {"type": _REASONING_STATE_TYPE, "items": items}
            result.reasoning_signature = json.dumps(
                state, ensure_ascii=False, separators=(",", ":")
            )
        elif isinstance(item, dict) and item.get("type") == "function_call":
            call_id = item.get("call_id") or item.get("id")
            if isinstance(call_id, str) and isinstance(item.get("name"), str):
                arguments = item.get("arguments", "")
                result.tool_calls.append(
                    TransportToolCall(call_id, item["name"], arguments if isinstance(arguments, str) else json.dumps(arguments))
                )
        elif not result.text:
            result.text = _text_from_item(item)
    elif kind == "response.completed":
        result.usage = TransportUsage.from_response(response.get("usage"))
        return True
    elif kind in {"response.failed", "response.incomplete", "error"}:
        return True
    return False
