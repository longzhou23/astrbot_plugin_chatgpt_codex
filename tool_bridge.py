"""Reserved boundary for a future AstrBot-tools -> Codex dynamicTools bridge."""

from __future__ import annotations

from typing import Any


class ToolBridge:
    """MVP intentionally returns no tools; this prevents a double Agent Loop."""

    enabled = False

    def dynamic_tools(self) -> list[dict[str, Any]]:
        return []

    async def handle_call(self, _name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Codex dynamic tool bridge is disabled in this MVP")
