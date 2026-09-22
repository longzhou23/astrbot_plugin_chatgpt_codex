from __future__ import annotations

from typing import Any

from .codex_security import safe_error


class CodexPluginError(Exception):
    """Base error exposed by the plugin."""


class CodexProcessError(CodexPluginError):
    pass


class CodexTransportError(CodexPluginError):
    pass


class CodexCapabilityError(CodexPluginError):
    """The selected backend cannot preserve a requested AstrBot capability."""


class CodexAuthError(CodexPluginError):
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

    @property
    def is_auth(self) -> bool:
        haystack = f"{self.message} {self.data!s}".lower()
        markers = (
            "unauthorized",
            "authentication",
            "invalid token",
            "token is invalid",
            "token expired",
            "login required",
        )
        return self.code in {401, 403} or any(marker in haystack for marker in markers)


def classify_rpc_error(error: CodexRPCError) -> CodexPluginError:
    if error.is_quota:
        return CodexQuotaError(error.message)
    if error.is_auth:
        return CodexAuthError(error.message)
    return error
