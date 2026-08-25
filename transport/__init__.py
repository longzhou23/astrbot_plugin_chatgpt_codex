"""Experimental direct Codex Responses transport.

This package intentionally contains no thread/turn orchestration.  The host
plugin remains responsible for conversation history, persona, tools and the
Agent Runner loop.
"""

from .client import CodexTransportClient
from .types import TransportError, TransportModeError, TransportResponse

__all__ = [
    "CodexTransportClient",
    "TransportError",
    "TransportModeError",
    "TransportResponse",
]
