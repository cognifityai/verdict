"""Explicit dashboard boundary for immutable cluster-registry actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.cluster_runtime import (
    cluster_model_path_for_version,
    cluster_registry_service,
)

TENANT = "__verdict_local__"
ACTOR = "dashboard-user"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


def _service(
    storage: object,
    payload: dict[str, Any],
    *,
    tenant_id: str = TENANT,
    version_id: str | None = None,
    strategy: str | None = None,
    allow_download: bool = False,
):
    model_path = payload.get("modelPath")
    version = (
        storage.get_cluster_registry_version(tenant_id, version_id)
        if version_id is not None
        else None
    )
    if version is not None and strategy is None:
        strategy = version.strategy
    if strategy == "explicit":
        model_path = None
    elif model_path is None and version is not None:
        model_path = cluster_model_path_for_version(version)
    return cluster_registry_service(
        storage,
        model_path=model_path,
        allow_download=allow_download,
        strategy=strategy,
    )


def execute_cluster_action(
    storage: object,
    *,
    action: str,
    payload: dict[str, Any],
    tenant_id: str = TENANT,
) -> dict[str, object]:
    """Execute one bounded, auditable registry transition."""
    if action not in {"fit", "refit", "validate", "activate", "replay", "rollback", "rename"}:
        raise ValueError("unsupported cluster action")
    if action == "fit":
        from verdict_eval.clustering_strategies import FitConfig

        strategy = payload.get("strategy", "explicit")
        if strategy not in {"explicit", "semantic", "hybrid"}:
            raise ValueError("invalid cluster strategy")
        if strategy == "explicit" and payload.get("modelPath") is not None:
            raise ValueError("explicit clustering does not use a model")
        service = _service(
            storage,
            payload,
            tenant_id=tenant_id,
            strategy=strategy,
            allow_download=strategy != "explicit",
        )
        workload = payload.get("targetWorkload")
        if workload is not None:
            workload = _bounded_text(workload, "target workload", 64)
        cutoff_value = payload.get("cutoff")
        if cutoff_value is None:
            count, _earliest_us, latest_us = storage.cluster_trace_time_bounds(
                tenant_id, target_workload=workload
            )
            if count == 0 or latest_us is None:
                raise ValueError("no eligible traces are available for clustering")
            cutoff = _EPOCH + timedelta(microseconds=latest_us + 1)
        else:
            cutoff = _instant(cutoff_value)
        version = service.fit(
            tenant_id,
            actor=ACTOR,
            strategy=strategy,
            cutoff=cutoff,
            config=FitConfig(
                strategy=strategy,
                target_workload=workload,
                lookback_days=payload.get("lookbackDays", 90),
            ),
        )
        return {"action": action, "versionId": version.version_id, "status": "candidate"}
    if action == "refit":
        active = storage.get_active_cluster_registry(tenant_id)
        service = _service(
            storage,
            payload,
            tenant_id=tenant_id,
            version_id=active.version_id if active is not None else None,
        )
        version = service.refit(
            tenant_id, actor=ACTOR,
            cutoff=_instant(payload.get("cutoff"), default_now=True),
        )
        return {"action": action, "versionId": version.version_id, "status": "candidate"}

    if action == "rename":
        cluster_id = _bounded_text(payload.get("clusterId"), "cluster id")
        display_name = _bounded_text(payload.get("displayName"), "display name", 80)
        service = cluster_registry_service(storage, strategy="explicit")
        service.rename(tenant_id, cluster_id, display_name, actor=ACTOR)
        return {"action": action, "clusterId": cluster_id}

    version_id = _bounded_text(payload.get("versionId"), "version id")
    service = _service(storage, payload, tenant_id=tenant_id, version_id=version_id)
    if action == "validate":
        report = service.validate(tenant_id, version_id, actor=ACTOR)
        return {"action": action, "versionId": version_id, "report": report}
    if action == "replay":
        assigned = service.assign(
            tenant_id, version_id,
            through_cutoff=_instant(payload.get("throughCutoff"), default_now=True),
        )
        return {"action": action, "versionId": version_id, "assigned": assigned}
    generation = payload.get("expectedGeneration")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("invalid expected generation")
    if action == "activate":
        pointer = service.activate(
            tenant_id, version_id, expected_generation=generation, actor=ACTOR,
        )
    else:
        pointer = service.rollback(
            tenant_id, version_id, expected_generation=generation, actor=ACTOR,
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
