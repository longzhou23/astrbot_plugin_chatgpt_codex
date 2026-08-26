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


def _safe_label(value: Any, default: str = "附件") -> str:
    """Return a short human-readable attachment label without dumping metadata."""

    if not isinstance(value, str):
        return default
    label = " ".join(value.split()).strip()
    return label[:256] or default


def _media_ref(value: Any) -> str | None:
    """Extract a transportable URL/data URI from an AstrBot/OpenAI media value."""

    if isinstance(value, str):
        value = value.strip()
        return value or None
    if not isinstance(value, dict):
        return None
    for key in ("url", "data", "image_url", "audio_url", "file_url"):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            candidate = candidate.get("url") or candidate.get("data")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


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
    if part_type in {"input_image", "image", "local_image", "localImage"}:
        image_url = _media_ref(part.get("image_url") or part.get("url"))
        if not image_url or not image_url.startswith(("data:", "http://", "https://")):
            return None
        detail = part.get("detail", "auto")
    elif part_type == "image_url":
        image = part.get("image_url")
        image_url = _media_ref(image)
        if not image_url:
            return None
        detail = image.get("detail", "auto") if isinstance(image, dict) else "auto"
    else:
        return None
    if detail not in {"low", "high", "auto"}:
        detail = "auto"
    return {"type": "input_image", "detail": detail, "image_url": image_url}


def _audio_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Map Codex/AstrBot audio parts to Codex's ``audio_url`` content item."""

    audio_url = _media_ref(part.get("audio_url") or part.get("url"))
    if not audio_url:
        return None
    if not audio_url.startswith(("data:audio/", "http://", "https://")):
        return None
    return {"type": "input_audio", "audio_url": audio_url}


def _attachment_marker(part: dict[str, Any]) -> str | None:
    """Preserve platform attachments unsupported by Codex's ContentItem union."""

    part_type = str(part.get("type") or "").lower()
    if part_type in {"reply", "quote", "quoted_message"}:
        quoted = part.get("message_str") or part.get("text") or part.get("content")
        if isinstance(quoted, str) and quoted.strip():
            return f"[引用消息]\n{quoted.strip()[:4000]}"
        return "[引用消息]"
    if part_type in {"file", "input_file", "file_url", "file_attachment", "document"}:
        filename = (
            part.get("filename")
            or part.get("file_name")
            or part.get("name")
            or part.get("title")
        )
        return f"[文件附件：{_safe_label(filename)}]"
    if part_type in {"video", "input_video", "video_url"}:
        filename = part.get("filename") or part.get("file_name") or part.get("name")
        return f"[视频附件：{_safe_label(filename)}]"
    if part_type in {"image", "local_image", "localimage", "input_image", "image_url"}:
        label = part.get("filename") or part.get("file_name") or part.get("name")
        return f"[图片附件：{_safe_label(label)}]"
    if part_type in {
        "audio",
        "record",
        "voice",
        "input_audio",
        "audio_url",
    }:
        label = part.get("filename") or part.get("file_name") or part.get("name")
        return f"[音频附件：{_safe_label(label)}]"
    if part_type in {"at", "mention"}:
        target = part.get("name") or part.get("qq") or part.get("target")
        return f"[@{_safe_label(target, '成员')}]"
    if part_type in {"face", "emoji", "sticker"}:
        label = part.get("name") or part.get("id") or part.get("face_id")
        return f"[表情：{_safe_label(label)}]"
    if part_type in {"location", "geo"}:
        label = part.get("title") or part.get("address") or part.get("name")
        return f"[位置：{_safe_label(label)}]"
    if part_type in {"forward", "forward_message", "node"}:
        return "[转发消息]"
    if part_type in {"json", "xml", "share", "contact", "music", "markdown"}:
        value = part.get("text") or part.get("title") or part.get("data")
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, str) and value.strip():
            return f"[{part_type}消息]\n{value.strip()[:4000]}"
        return f"[{part_type}消息]"
    text = part.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:4000]
    return None


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
        if part_type in {"audio_url", "input_audio", "audio", "record", "voice"}:
            audio = _audio_part(part) if role != "assistant" else None
            if audio is not None:
                content.append(audio)
                continue
        marker = _attachment_marker(part)
        if marker:
            if role == "assistant":
                output_text.append(marker)
            else:
                content.append({"type": "input_text", "text": marker})

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
        if call_dict.get("type") in {"function_call", "custom_tool_call"}:
            call_id = call_dict.get("call_id") or call_dict.get("id")
            name = call_dict.get("name")
            arguments = call_dict.get("arguments")
            if arguments is None:
                arguments = call_dict.get("input", "{}")
            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
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


def _tool_output_value(value: Any) -> str | list[dict[str, Any]]:
    """Keep structured public media in tool output when Codex supports it."""

    if isinstance(value, str):
        return value
    parts, _ = _content_parts(value, role="user")
    if parts:
        return parts
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


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
                output = _tool_output_value(message.get("content", ""))
                result.append({"type": "function_call_output", "call_id": call_id, "output": output})
            continue
        # System/developer messages are promoted to the top-level
        # ``instructions`` field by CodexService. Forwarding developer here as
        # well would send the same persona/instructions twice.
        if role not in {"user", "assistant"}:
            continue
        content, reasoning = _content_parts(message.get("content"), role=role)
        result.extend(reasoning)
        if content:
            result.append({"type": "message", "role": role, "content": content})
        if role == "assistant":
            result.extend(_function_call_items(message.get("tool_calls")))

    extras = list(extra_user_content_parts or [])
    latest_content: list[dict[str, Any]] = []
    latest = (prompt or "").strip()
    if include_latest and latest:
        latest_content.append({"type": "input_text", "text": latest})
    elif include_latest and not contexts and not extras and not image_urls and not audio_urls:
        # Preserve an explicitly empty first message without inventing an
        # empty user turn during a tool continuation or a context-only call.
        latest_content.append({"type": "input_text", "text": "(The user sent an empty message.)"})
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
    for audio_url in audio_urls or []:
        if isinstance(audio_url, str) and audio_url:
            audio = _audio_part({"type": "input_audio", "audio_url": audio_url})
            if audio is not None:
                latest_content.append(audio)
            else:
                latest_content.append({"type": "input_text", "text": "[音频附件]"})
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
                output = _tool_output_value(message.get("content", ""))
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
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"output_text", "text", "input_text", "refusal"}:
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _consume_reasoning_item(result: TransportResponse, item: dict[str, Any]) -> None:
    """Retain encrypted reasoning items for replay while deduplicating events."""

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
            previous_items = [old for old in previous_state["items"] if isinstance(old, dict)]
            item_id = item.get("id")
            if isinstance(item_id, str) and any(old.get("id") == item_id for old in previous_items):
                return
            items = [*previous_items, item]
    state = {"type": _REASONING_STATE_TYPE, "items": items}
    result.reasoning_signature = json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def _consume_output_item(result: TransportResponse, item: dict[str, Any]) -> None:
    """Consume a complete public Responses item without exposing reasoning."""

    if item.get("type") == "reasoning":
        _consume_reasoning_item(result, item)
    elif item.get("type") in {"function_call", "custom_tool_call"}:
        _consume_function_call_item(result, item)
    elif not result.text:
        text = _text_from_item(item)
        if text:
            result.text = text


def _update_function_call(
    result: TransportResponse,
    *,
    item_id: Any = None,
    call_id: Any = None,
    name: Any = None,
    arguments: Any = None,
    append_arguments: bool = False,
    finalize: bool = False,
) -> None:
    """Accumulate and finalize a function call spread across SSE events."""

    state_key = item_id if isinstance(item_id, str) and item_id else call_id
    if not isinstance(state_key, str) or not state_key:
        return
    state = result.function_call_state.setdefault(state_key, {})
    if isinstance(call_id, str) and call_id:
        state["call_id"] = call_id
    if isinstance(name, str) and name:
        state["name"] = name
    if isinstance(arguments, str):
        if append_arguments:
            state["arguments"] = state.get("arguments", "") + arguments
        elif arguments or not state.get("arguments"):
            state["arguments"] = arguments
    if not finalize:
        return

    # A few Responses-compatible streams omit call_id in the argument events
    # and only provide the output item id. That id is stable for this turn and
    # is the safest fallback AstrBot can use for its tool-result continuation.
    final_call_id = state.get("call_id") or state_key
    final_name = state.get("name")
    if not final_call_id or not final_name:
        return
    call = TransportToolCall(final_call_id, final_name, state.get("arguments", ""))
    for index, existing in enumerate(result.tool_calls):
        if existing.call_id == final_call_id:
            result.tool_calls[index] = call
            break
    else:
        result.tool_calls.append(call)


def _consume_function_call_item(
    result: TransportResponse,
    item: dict[str, Any],
) -> None:
    item_type = item.get("type")
    if item_type not in {"function_call", "custom_tool_call"}:
        return
    # Some Codex-compatible Responses streams expose the output item id but
    # omit call_id. AstrBot still needs a stable id to execute the tool and
    # return its output, so retain the historical id fallback.
    call_id = item.get("call_id") or item.get("id")
    arguments = item.get("arguments")
    if arguments is None and item_type == "custom_tool_call":
        arguments = item.get("input", "")
    if arguments is None:
        arguments = ""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, default=str)
    _update_function_call(
        result,
        item_id=item.get("id"),
        call_id=call_id,
        name=item.get("name"),
        arguments=arguments,
        finalize=True,
    )


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
    if isinstance(kind, str) and len(result.event_types) < 32:
        result.event_types.append(kind)
    response = event.get("response") if isinstance(event.get("response"), dict) else {}
    if isinstance(response.get("id"), str):
        result.response_id = response["id"]
    if kind == "response.output_text.delta" and isinstance(event.get("delta"), str):
        result.text += event["delta"]
    elif kind == "response.refusal.delta" and isinstance(event.get("delta"), str):
        # Refusals are public assistant output. Reasoning deltas are deliberately
        # not handled here and therefore cannot leak into the chat.
        result.text += event["delta"]
    elif kind == "response.output_text.done" and isinstance(event.get("text"), str):
        # Some Responses-compatible transports omit text deltas and only send
        # the authoritative completed text event.
        if not result.text:
            result.text = event["text"]
    elif kind == "response.output_item.added":
        item = event.get("item")
        if isinstance(item, dict):
            _update_function_call(
                result,
                item_id=item.get("id"),
                call_id=item.get("call_id"),
                name=item.get("name"),
                arguments=item.get("arguments"),
            )
    elif kind == "response.function_call_arguments.delta":
        _update_function_call(
            result,
            item_id=event.get("item_id"),
            call_id=event.get("call_id"),
            name=event.get("name"),
            arguments=event.get("delta"),
            append_arguments=True,
        )
    elif kind == "response.function_call_arguments.done":
        _update_function_call(
            result,
            item_id=event.get("item_id"),
            call_id=event.get("call_id"),
            name=event.get("name"),
            arguments=event.get("arguments"),
            finalize=True,
        )
    elif kind == "response.custom_tool_call_input.delta":
        _update_function_call(
            result,
            item_id=event.get("item_id"),
            call_id=event.get("call_id"),
            name=event.get("name"),
            arguments=event.get("delta"),
            append_arguments=True,
        )
    elif kind == "response.custom_tool_call_input.done":
        _update_function_call(
            result,
            item_id=event.get("item_id"),
            call_id=event.get("call_id"),
            name=event.get("name"),
            arguments=event.get("input"),
            finalize=True,
        )
    elif kind == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            _consume_reasoning_item(result, item)
        elif isinstance(item, dict):
            _consume_output_item(result, item)
    elif kind == "response.completed":
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    _consume_output_item(result, item)
        result.usage = TransportUsage.from_response(response.get("usage"))
        return True
    elif kind in {"response.refusal.done", "response.failed", "response.incomplete", "error"}:
        if (
            kind == "response.refusal.done"
            and isinstance(event.get("text"), str)
            and not result.text
        ):
            result.text = event["text"]
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    _consume_output_item(result, item)
        if response:
            result.usage = TransportUsage.from_response(response.get("usage"))
        return True
    return False
