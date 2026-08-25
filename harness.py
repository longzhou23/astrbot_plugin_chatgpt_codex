"""Prompt and capability policy for the AstrBot Codex bridge."""

from __future__ import annotations

from typing import Any

LIGHTWEIGHT_BASE_INSTRUCTIONS = (
    "You are the reasoning backend for an AstrBot agent. "
    "Follow the supplied persona and system instructions. "
    "Respond to the current user directly and concisely. "
    "Use only tools explicitly provided for this turn; do not assume access "
    "to a shell, filesystem, browser, repository, or local machine. "
    "Never expose hidden reasoning or internal state."
)


def normalize_harness_mode(value: Any) -> str:
    return "codex" if str(value or "lightweight").strip().lower() == "codex" else "lightweight"


def normalize_tool_router(value: Any) -> str:
    selected = str(value or "minimal").strip().lower()
    return selected if selected in {"none", "minimal", "all"} else "minimal"


def lightweight_config() -> dict[str, Any]:
    """Return current Codex config keys that remove optional prompt sources.

    These are passed as a thread-scoped ``config`` override. They do not touch
    the user's CODEX_HOME/config.toml or credentials. Core environment tools
    are a server capability and are reported separately when the server does
    not expose a tool-disable switch.
    """

    return {
        "include_permissions_instructions": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "include_skill_instructions": False,
        "include_environment_context": False,
        "project_doc_max_bytes": 0,
        "use_memories": False,
        "mcp_servers": {},
        "apps": {
            "_default": {
                "enabled": False,
                "default_tools_enabled": False,
                "open_world_enabled": False,
                "destructive_enabled": False,
            }
        },
        "tools": {
            "update_plan": {"enabled": False},
            "experimental_request_user_input": {"enabled": False},
        },
    }


def base_instructions_for(mode: Any) -> str | None:
    return LIGHTWEIGHT_BASE_INSTRUCTIONS if normalize_harness_mode(mode) == "lightweight" else None
