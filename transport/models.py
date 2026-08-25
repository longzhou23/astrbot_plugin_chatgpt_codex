from __future__ import annotations

from typing import Any

from ..model_catalog import CodexModel


def parse_transport_models(payload: Any) -> list[CodexModel]:
    """Parse the open-source Codex models endpoint and tolerant aliases."""

    entries = payload.get("models", payload.get("data", [])) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return []
    models: list[CodexModel] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        model_id = item.get("slug", item.get("id", item.get("model")))
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        efforts = (
            item.get("supported_reasoning_efforts")
            or item.get("supportedReasoningEfforts")
            or item.get("supported_reasoning_levels")
            or item.get("supportedReasoningLevels")
            or item.get("reasoning_efforts")
            or item.get("reasoningEfforts")
            or []
        )
        if not isinstance(efforts, list):
            efforts = []
        effort_names = []
        for effort in efforts:
            if isinstance(effort, str) and effort:
                effort_names.append(effort)
            elif isinstance(effort, dict) and isinstance(effort.get("effort"), str):
                effort_names.append(effort["effort"])
        models.append(
            CodexModel(
                id=model_id,
                display_name=str(
                    item.get("display_name", item.get("displayName", item.get("name", "")))
                    or ""
                ),
                reasoning_efforts=tuple(dict.fromkeys(effort_names)),
                hidden=bool(item.get("hidden", False)),
                raw=item,
            )
        )
        seen.add(model_id)
    return models
