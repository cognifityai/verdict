"""Explicit dashboard boundary for immutable cluster-registry actions."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TENANT = "__verdict_local__"
ACTOR = "dashboard-user"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MINILM_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


def _model_path(value: object, *, allow_download: bool) -> Path | None:
    configured = value
    if configured is None:
        configured = os.environ.get("VERDICT_CLUSTER_MODEL_PATH")
    candidates: list[Path] = []
    if configured is not None:
        candidates.append(Path(_bounded_text(configured, "model path", 4096)).expanduser())
    candidates.append(
        Path.home()
        / ".cache/huggingface/hub"
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / _MINILM_REVISION
    )
    for path in candidates:
        if not path.is_symlink() and path.is_dir():
            return path
    if configured is not None:
        raise ValueError("configured semantic model directory is unavailable")
    if allow_download:
        try:
            from huggingface_hub import snapshot_download
            from huggingface_hub.utils import HfHubError
        except ImportError as exc:
            raise ValueError("model_unavailable") from exc

        try:
            downloaded = Path(
                snapshot_download(
                    "sentence-transformers/all-MiniLM-L6-v2",
                    revision=_MINILM_REVISION,
                )
            )
        except (OSError, HfHubError) as exc:
            raise ValueError("model_unavailable") from exc
        if not downloaded.is_symlink() and downloaded.is_dir():
            return downloaded
    return None


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


def _service(storage: object, payload: dict[str, Any], *, allow_download: bool = False):
    from verdict_eval.cluster_registry import ClusterRegistryService

    path = _model_path(payload.get("modelPath"), allow_download=allow_download)
    if path is None:
        return ClusterRegistryService(storage)

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
    if action == "fit":
        from verdict_eval.clustering_strategies import FitConfig

        strategy = payload.get("strategy", "explicit")
        if strategy not in {"explicit", "semantic", "hybrid"}:
            raise ValueError("invalid cluster strategy")
        if strategy == "explicit" and payload.get("modelPath") is not None:
            raise ValueError("explicit clustering does not use a model")
        service = _service(storage, payload, allow_download=strategy != "explicit")
        workload = payload.get("targetWorkload")
        if workload is not None:
            workload = _bounded_text(workload, "target workload", 64)
        cutoff_value = payload.get("cutoff")
        if cutoff_value is None:
            count, _earliest_us, latest_us = storage.cluster_trace_time_bounds(
                TENANT, target_workload=workload
            )
            if count == 0 or latest_us is None:
                raise ValueError("no eligible traces are available for clustering")
            cutoff = _EPOCH + timedelta(microseconds=latest_us + 1)
        else:
            cutoff = _instant(cutoff_value)
        version = service.fit(
            TENANT,
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
    service = _service(storage, payload)
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
