from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodexModel:
    id: str
    display_name: str = ""
    reasoning_efforts: tuple[str, ...] = field(default_factory=tuple)
    hidden: bool = False
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


def parse_models(payload: dict[str, Any]) -> list[CodexModel]:
    entries = payload.get("models", payload.get("data", []))
    if not isinstance(entries, list):
        return []
    result: list[CodexModel] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id", item.get("model"))
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            continue
        efforts = item.get("supportedReasoningEfforts", item.get("reasoningEfforts", []))
        if not isinstance(efforts, list):
            efforts = []
        result.append(
            CodexModel(
                id=model_id,
                display_name=str(item.get("displayName", item.get("display_name", "")) or ""),
                reasoning_efforts=tuple(str(x) for x in efforts if isinstance(x, str)),
                hidden=bool(item.get("hidden", False)),
                raw=item,
            )
        )
        seen.add(model_id)
    return result


class ModelCatalog:
    def __init__(self, path: Path, ttl_seconds: int = 300) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self.models: list[CodexModel] = []
        self.loaded_at = 0.0
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self.models = parse_models(payload)
            self.loaded_at = float(payload.get("loadedAt", 0))
        except (OSError, ValueError, TypeError):
            self.models = []
            self.loaded_at = 0.0

    def replace(self, models: list[CodexModel]) -> None:
        self.models = models
        self.loaded_at = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "loadedAt": self.loaded_at,
            "models": [
                {
                    "id": model.id,
                    "displayName": model.display_name,
                    "supportedReasoningEfforts": list(model.reasoning_efforts),
                    "hidden": model.hidden,
                }
                for model in models
            ],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def is_fresh(self) -> bool:
        return bool(self.models) and time.time() - self.loaded_at < self.ttl_seconds

    def choose(self, model_id: str | None) -> CodexModel | None:
        visible = [model for model in self.models if not model.hidden]
        if model_id and model_id != "auto":
            return next((model for model in visible if model.id == model_id), None)
        return visible[0] if visible else None
