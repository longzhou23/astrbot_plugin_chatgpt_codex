from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

NUMERIC_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cache_write_input_tokens",
)


def _sum(rows: Iterable[dict[str, Any]], field: str) -> int | None:
    values = [row.get(field) for row in rows if isinstance(row.get(field), int)]
    return sum(values) if values else None


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {field: _sum(rows, field) for field in NUMERIC_FIELDS}
    result["requests"] = sum(int(row.get("request_count") or 0) for row in rows)
    result["records"] = len(rows)
    input_tokens = result["input_tokens"]
    cached = result["cached_input_tokens"]
    result["cache_ratio"] = round(cached / input_tokens, 4) if input_tokens and cached is not None else None
    return result


def aggregate_by(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        value = row.get(key) or "unknown"
        grouped[str(value)].append(row)
    result = []
    for value, group in grouped.items():
        item = aggregate_rows(group)
        item[key] = value
        result.append(item)
    result.sort(key=lambda item: (item.get("total_tokens") or 0, item["requests"]), reverse=True)
    return result


def heat_level(value: int, positive_values: list[int]) -> int:
    """Return 0..5 using adaptive P20/P40/P60/P80 thresholds."""

    if value <= 0 or not positive_values:
        return 0
    ordered = sorted(positive_values)
    if len(ordered) == 1:
        return 5

    def percentile(percent: float) -> float:
        index = (len(ordered) - 1) * percent
        lower, upper = int(index), min(len(ordered) - 1, int(index) + 1)
        fraction = index - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    thresholds = [percentile(p) for p in (0.2, 0.4, 0.6, 0.8)]
    return 1 + sum(value > threshold for threshold in thresholds)
