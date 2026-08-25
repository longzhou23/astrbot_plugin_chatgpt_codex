from __future__ import annotations

from typing import Any


def rate_limits_from_headers(headers: Any) -> dict[str, Any]:
    """Read only numeric rate headers; never retain arbitrary response headers."""

    result: dict[str, Any] = {}
    names = {
        "x-ratelimit-limit-requests": "requestLimit",
        "x-ratelimit-remaining-requests": "requestRemaining",
        "x-ratelimit-reset-requests": "requestReset",
        "x-ratelimit-limit-tokens": "tokenLimit",
        "x-ratelimit-remaining-tokens": "tokenRemaining",
        "x-ratelimit-reset-tokens": "tokenReset",
        "x-codex-rate-limit": "codexRateLimit",
        "x-codex-rate-limit-remaining": "codexRateLimitRemaining",
        "x-codex-rate-limit-reset": "codexRateLimitReset",
    }
    for source, target in names.items():
        try:
            value = headers.get(source)
        except AttributeError:
            value = None
        if value is None:
            continue
        text = str(value).strip()
        if len(text) <= 128:
            result[target] = text
    return result
