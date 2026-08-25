from __future__ import annotations

import json
from typing import Any

from .types import TransportResponse, TransportToolCall, TransportUsage


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def build_input_items(
    contexts: list[dict[str, Any]] | None,
    prompt: str | None,
    extra_user_content_parts: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert AstrBot messages to the Responses input-item shape.

    System/developer messages belong in ``instructions`` and are intentionally
    excluded here.  No Codex-specific thread IDs or internal metadata are sent.
    """

    result: list[dict[str, Any]] = []
    for message in contexts or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"user", "assistant", "developer"}:
            continue
        text = _content_text(message.get("content"))
        if not text:
            continue
        content_type = "output_text" if role == "assistant" else "input_text"
        result.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": content_type, "text": text}],
            }
        )
    latest = (prompt or "").strip() or "(The user sent an empty message.)"
    extra = _content_text(extra_user_content_parts)
    if extra:
        latest += "\n\n<astrbot_dynamic_context>\n" + extra[-40000:] + "\n</astrbot_dynamic_context>"
    result.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": latest}],
        }
    )
    return result


def response_request(
    *,
    model: str,
    instructions: str,
    input_items: list[dict[str, Any]],
    effort: str = "auto",
    tools: list[dict[str, Any]] | None = None,
    prompt_cache_key: str | None = None,
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
        "include": [],
    }
    if effort and effort != "auto":
        payload["reasoning"] = {"effort": effort, "summary": "auto"}
    if tools:
        payload["tools"] = tools
    if prompt_cache_key:
        payload["prompt_cache_key"] = prompt_cache_key
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
        if isinstance(item, dict) and item.get("type") == "function_call":
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
