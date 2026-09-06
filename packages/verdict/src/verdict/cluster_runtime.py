"""Shared construction of the pinned cluster-registry runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path

MINILM_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


def resolve_cluster_model_path(
    value: object = None,
    *,
    allow_download: bool,
) -> Path | None:
    configured = value if value is not None else os.environ.get("VERDICT_CLUSTER_MODEL_PATH")
    candidates: list[Path] = []
    if configured is not None:
        if not isinstance(configured, str) or not configured or "\x00" in configured:
            raise ValueError("invalid model path")
        if len(configured.encode("utf-8")) > 4096:
            raise ValueError("invalid model path")
        candidates.append(Path(configured).expanduser())
    hub_cache = os.environ.get("HF_HUB_CACHE")
    if hub_cache is None:
        hf_home = os.environ.get("HF_HOME")
        hub_cache = str(Path(hf_home) / "hub") if hf_home else None
    cache_root = (
        Path(hub_cache).expanduser() if hub_cache else Path.home() / ".cache/huggingface/hub"
    )
    candidates.append(
        cache_root
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / MINILM_REVISION
    )
    for path in candidates:
        if not path.is_symlink() and path.is_dir():
            return path
    if configured is not None:
        raise ValueError("configured semantic model directory is unavailable")
    if allow_download:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ValueError("model_unavailable") from exc
        try:
            downloaded = Path(
                snapshot_download(
                    "sentence-transformers/all-MiniLM-L6-v2",
                    revision=MINILM_REVISION,
                )
            )
        except Exception as exc:
            raise ValueError("model_unavailable") from exc
        if not downloaded.is_symlink() and downloaded.is_dir():
            return downloaded
    return None


def cluster_registry_service(
    storage: object,
    *,
    model_path: object = None,
    allow_download: bool = False,
    strategy: str | None = None,
):
    from verdict_eval.cluster_registry import ClusterRegistryService

    if strategy == "explicit":
        return ClusterRegistryService(storage)
    path = resolve_cluster_model_path(model_path, allow_download=allow_download)
    if path is None:
        return ClusterRegistryService(storage)

    def factory():
        from verdict_eval.clustering import FrozenMiniLMEmbedder

        return FrozenMiniLMEmbedder(path)

    return ClusterRegistryService(
        storage,
        embedder_factory=factory,
        model_locator=str(path.resolve()) if model_path is not None else None,
    )


def cluster_model_path_for_version(version: object) -> str | None:
    """Read a persisted local model override from immutable version metadata."""
    try:
        definition = json.loads(version.fit_definition_json)
        model = definition.get("model")
        value = model.get("local_path") if isinstance(model, dict) else None
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid fit definition") from exc
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > 4096
    ):
        raise ValueError("invalid model locator")
    return value
