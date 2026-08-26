"""Small, conservative redaction helpers used by every log boundary."""

from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS = (
    re.compile(r"(?i)data:[^;\s]+;base64,[A-Za-z0-9+/=_-]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(access[_ -]?token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(refresh[_ -]?token\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(user[_ -]?code\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(device[_ -]?code\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)([?&](?:code|state|error|error_description)=)[^&#\s]+"),
)


def redact_text(value: str) -> str:
    """Redact common credentials without attempting to parse or persist them."""

    result = value
    for index, pattern in enumerate(_SECRET_PATTERNS):
        replacement = "<redacted-data-uri>" if index == 0 else r"\1<redacted>"
        result = pattern.sub(replacement, result)
    return result


def safe_error(value: Any, limit: int = 500) -> str:
    """Make an exception or protocol error safe for a user-facing message/log."""

    return redact_text(str(value).replace("\x00", " "))[:limit]
