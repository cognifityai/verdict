"""Shared, storage-neutral dashboard presentation helpers.

The analytics bundle and paginated Trace Explorer must agree on evaluator
identity and per-trace judgment semantics.  This module owns that projection;
it does not open storage or perform writes.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from verdict.metrics import ScoreCounts, verdict_label
from verdict.redaction import redact

_KNOWN_PROVIDERS = {"anthropic", "openai", "google"}


def row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a column from current and pre-migration rows."""
    return row[key] if key in row.keys() else default


def json_value(raw: object, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def json_column(row: Mapping[str, Any], name: str, default: Any) -> Any:
    """Read one logical JSON field from either adapter's physical schema."""
    raw = row_value(row, f"{name}_json", row_value(row, name))
    return json_value(raw, default)


def evaluator_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the dashboard's stable evaluator discriminator."""
    models = json_column(row, "judge_models", [])
    config = json_column(row, "evaluator_config", {})
    expected_dimensions = json_column(row, "expected_dimensions", [])
    provider = row_value(row, "evaluator_provider", "") or ""
    fingerprint = row_value(row, "evaluator_fingerprint", "") or ""
    canonical = {
        "provider": provider,
        "models": models if isinstance(models, list) else [],
        "rubricName": row_value(row, "rubric_name", "default") or "default",
        "rubricVersion": row_value(row, "rubric_version", "1") or "1",
        "config": config if isinstance(config, dict) else {},
        "expectedDimensions": (
            expected_dimensions if isinstance(expected_dimensions, list) else []
        ),
        "fingerprint": fingerprint,
    }
    complete = bool(
        provider
        and fingerprint
        and canonical["models"]
        and canonical["expectedDimensions"]
    )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    identity_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]
    model_label = "+".join(str(model) for model in canonical["models"]) or "unknown judge"
    rubric_label = f"{canonical['rubricName']} v{canonical['rubricVersion']}"
    identity_suffix = (
        f" · fp {fingerprint[:8]}"
        if complete
        else " · historical identity incomplete"
    )
    return {
        "id": identity_id,
        "provider": provider or None,
        "models": canonical["models"],
        "rubricName": canonical["rubricName"],
        "rubricVersion": canonical["rubricVersion"],
        "fingerprint": fingerprint or None,
        "config": canonical["config"],
        "expectedDimensions": canonical["expectedDimensions"],
        "complete": complete,
        "label": f"{model_label} · {rubric_label}{identity_suffix}",
    }


def provider_key(provider: object) -> str:
    """Return the shared chart-safe provider key without erasing its value."""
    if provider in _KNOWN_PROVIDERS:
        return str(provider)
    encoded = json.dumps(
        {"type": type(provider).__name__, "value": provider},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return f"provider_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _score_rate(counts: Counter[str]) -> float | None:
    rate = ScoreCounts(
        passed=counts.get("pass", 0),
        failed=counts.get("fail", 0),
        unclear=counts.get("unclear", 0),
    ).pass_rate
    return round(100 * rate, 1) if rate is not None else None


def judgment_presentation(
    row: Mapping[str, Any],
    *,
    reasoning_limit: int | None = None,
) -> dict[str, Any] | None:
    """Project one completed persisted judgment into the canonical UI shape."""
    status = str(row_value(row, "status", "completed") or "completed").lower()
    if status == "error":
        return None
    dimensions = json_column(row, "dimensions", [])
    dimensions = dimensions if isinstance(dimensions, list) else []
    expected = json_column(row, "expected_dimensions", [])
    if not isinstance(expected, list) or not expected:
        expected = [
            dimension.get("name")
            for dimension in dimensions
            if isinstance(dimension, dict) and dimension.get("name")
        ]
    normalized_dimensions: list[dict[str, str]] = []
    present_names: set[str] = set()
    nameless_unclear = 0
    for dimension in dimensions:
        if not isinstance(dimension, dict) or not dimension.get("name"):
            nameless_unclear += 1
            continue
        name = str(dimension["name"])
        present_names.add(name)
        reasoning = str(dimension.get("reasoning", ""))
        if reasoning_limit is not None:
            reasoning = reasoning[:reasoning_limit]
        normalized_dimensions.append({
            "name": name,
            "verdict": verdict_label(dimension.get("verdict", "unclear")).lower(),
            "reasoning": redact(reasoning) or "",
        })
    counts = Counter(
        dimension["verdict"] for dimension in normalized_dimensions
    )
    missing = len(set(expected) - present_names)
    trace_status = (
        "fail" if counts["fail"]
        else "unclear" if counts["unclear"] or nameless_unclear or missing
        else "pass" if counts["pass"]
        else "unavailable"
    )
    return {
        "judges": json_column(row, "judge_models", []),
        "dims": normalized_dimensions,
        "summary": {
            "status": trace_status,
            "pass": counts["pass"],
            "fail": counts["fail"],
            "unclear": counts["unclear"] + nameless_unclear,
            "missing": missing,
            "passRate": _score_rate(counts),
        },
    }
