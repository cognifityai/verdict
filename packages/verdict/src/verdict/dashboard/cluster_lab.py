"""Explicit dashboard boundary for immutable cluster-registry actions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TENANT = "__verdict_local__"
ACTOR = "dashboard-user"


def _instant(value: object, *, default_now: bool = False) -> datetime:
    if value is None and default_now:
        return datetime.now(timezone.utc)
    if not isinstance(value, str) or len(value.encode("utf-8")) > 128:
        raise ValueError("invalid cutoff")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cutoff must include an offset")
    return parsed.astimezone(timezone.utc)


def _bounded_text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"invalid {name}")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"invalid {name}")
    return value


def _service(storage: object, payload: dict[str, Any]):
    from verdict_eval.cluster_registry import ClusterRegistryService

    model_path = payload.get("modelPath")
    if model_path is None:
        return ClusterRegistryService(storage)
    path_text = _bounded_text(model_path, "model path", 4096)
    path = Path(path_text).expanduser()
    if path.is_symlink() or not path.is_dir():
        raise ValueError("model path must be an approved local directory")

    def factory():
        from verdict_eval.clustering import FrozenMiniLMEmbedder
        return FrozenMiniLMEmbedder(path)

    return ClusterRegistryService(storage, embedder_factory=factory)


def execute_cluster_action(
    storage: object, *, action: str, payload: dict[str, Any]
) -> dict[str, object]:
    """Execute one bounded, auditable registry transition."""
    if action not in {"fit", "refit", "validate", "activate", "replay", "rollback", "rename"}:
        raise ValueError("unsupported cluster action")
    service = _service(storage, payload)
    if action == "fit":
        from verdict_eval.clustering_strategies import FitConfig

        strategy = payload.get("strategy", "explicit")
        if strategy not in {"explicit", "semantic", "hybrid"}:
            raise ValueError("invalid cluster strategy")
        if strategy == "explicit" and payload.get("modelPath") is not None:
            raise ValueError("explicit clustering does not use a model")
        if strategy != "explicit" and payload.get("modelPath") is None:
            raise ValueError("semantic clustering requires an approved local model")
        workload = payload.get("targetWorkload")
        if workload is not None:
            workload = _bounded_text(workload, "target workload", 64)
        version = service.fit(
            TENANT,
            actor=ACTOR,
            strategy=strategy,
            cutoff=_instant(payload.get("cutoff"), default_now=True),
            config=FitConfig(strategy=strategy, target_workload=workload),
        )
        return {"action": action, "versionId": version.version_id, "status": "candidate"}
    if action == "refit":
        version = service.refit(
            TENANT, actor=ACTOR,
            cutoff=_instant(payload.get("cutoff"), default_now=True),
        )
        return {"action": action, "versionId": version.version_id, "status": "candidate"}

    if action == "rename":
        cluster_id = _bounded_text(payload.get("clusterId"), "cluster id")
        display_name = _bounded_text(payload.get("displayName"), "display name", 80)
        service.rename(TENANT, cluster_id, display_name, actor=ACTOR)
        return {"action": action, "clusterId": cluster_id}

    version_id = _bounded_text(payload.get("versionId"), "version id")
    if action == "validate":
        report = service.validate(TENANT, version_id, actor=ACTOR)
        return {"action": action, "versionId": version_id, "report": report}
    if action == "replay":
        assigned = service.assign(
            TENANT, version_id,
            through_cutoff=_instant(payload.get("throughCutoff"), default_now=True),
        )
        return {"action": action, "versionId": version_id, "assigned": assigned}
    generation = payload.get("expectedGeneration")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("invalid expected generation")
    if action == "activate":
        pointer = service.activate(
            TENANT, version_id, expected_generation=generation, actor=ACTOR,
        )
    else:
        pointer = service.rollback(
            TENANT, version_id, expected_generation=generation, actor=ACTOR,
            through_cutoff=(
                _instant(payload["throughCutoff"])
                if payload.get("throughCutoff") is not None else None
            ),
        )
    return {
        "action": action,
        "versionId": pointer.version_id,
        "generation": pointer.generation,
        "status": "active",
    }
