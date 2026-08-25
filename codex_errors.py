from __future__ import annotations

from typing import Any

from .codex_security import safe_error


class CodexPluginError(Exception):
    """Base error exposed by the plugin."""


class CodexProcessError(CodexPluginError):
    pass


class CodexTransportError(CodexPluginError):
    pass


class CodexTimeoutError(CodexPluginError):
    pass


class CodexQuotaError(CodexPluginError):
    pass


class CodexRPCError(CodexPluginError):
    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        self.code = code
        self.message = safe_error(message)
        self.data = data
        super().__init__(self.message)

    @property
    def is_quota(self) -> bool:
        haystack = f"{self.message} {self.data!s}".lower()
        markers = (
            "usagelimitexceeded",
            "rate limit",
            "rate_limit",
            "quota",
            "credits exhausted",
            "usage limit",
            "too many requests",
        )
        return self.code == 429 or any(marker in haystack for marker in markers)


def classify_rpc_error(error: CodexRPCError) -> CodexPluginError:
    return CodexQuotaError(error.message) if error.is_quota else error
