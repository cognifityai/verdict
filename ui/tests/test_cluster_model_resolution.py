from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from verdict import cluster_runtime
from verdict.dashboard import cluster_lab
from verdict.storage import InMemoryStorage


def _snapshot(cache_root):
    path = (
        cache_root
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / cluster_runtime.MINILM_REVISION
    )
    path.mkdir(parents=True)
    return path


def test_semantic_model_cache_honors_huggingface_environment_precedence(
    monkeypatch, tmp_path
):
    hub_cache = tmp_path / "hub-cache"
    expected = _snapshot(hub_cache)
    monkeypatch.setenv("HF_HUB_CACHE", str(hub_cache))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "ignored-home"))
    monkeypatch.setattr(cluster_runtime.Path, "home", lambda: tmp_path / "ignored-user")

    assert cluster_runtime.resolve_cluster_model_path(
        None, allow_download=False,
    ) == expected


def test_semantic_model_cache_uses_hf_home_hub_directory(monkeypatch, tmp_path):
    hf_home = tmp_path / "hf-home"
    expected = _snapshot(hf_home / "hub")
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.setattr(cluster_runtime.Path, "home", lambda: tmp_path / "ignored-user")

    assert cluster_runtime.resolve_cluster_model_path(
        None, allow_download=False,
    ) == expected


def test_semantic_model_download_uses_the_pinned_revision(monkeypatch, tmp_path):
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    calls = []
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = lambda repository, **kwargs: (
        calls.append((repository, kwargs)) or str(downloaded)
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(cluster_runtime.Path, "home", lambda: tmp_path / "empty-home")

    assert cluster_runtime.resolve_cluster_model_path(
        None, allow_download=True,
    ) == downloaded
    assert calls == [(
        "sentence-transformers/all-MiniLM-L6-v2",
        {"revision": cluster_runtime.MINILM_REVISION},
    )]


def test_semantic_model_download_failure_is_bounded(monkeypatch, tmp_path):
    class HubHTTPError(Exception):
        pass

    def fail(*_args, **_kwargs):
        raise HubHTTPError("private remote response")

    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = fail
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(cluster_runtime.Path, "home", lambda: tmp_path / "empty-home")

    with pytest.raises(ValueError, match=r"^model_unavailable$"):
        cluster_runtime.resolve_cluster_model_path(None, allow_download=True)


def test_cluster_version_retains_an_explicit_local_model_override(tmp_path):
    model = tmp_path / "approved-model"
    model.mkdir()
    storage = InMemoryStorage()
    service = cluster_runtime.cluster_registry_service(
        storage, model_path=str(model), strategy="semantic",
    )
    version = SimpleNamespace(
        fit_definition_json=json.dumps({"model": {"local_path": str(model)}})
    )

    assert service.model_locator == str(model.resolve())
    assert cluster_runtime.cluster_model_path_for_version(version) == str(model)
    storage.close()


def test_explicit_fit_and_rename_do_not_resolve_an_active_semantic_model(monkeypatch):
    calls = []
    storage = SimpleNamespace(
        cluster_trace_time_bounds=lambda *_args, **_kwargs: (1, 0, 0),
        get_active_cluster_registry=lambda *_args: SimpleNamespace(
            version_id="retired-semantic-version"
        ),
        get_cluster_registry_version=lambda *_args: SimpleNamespace(
            fit_definition_json="{}"
        ),
    )
    service = SimpleNamespace(
        fit=lambda *_args, **_kwargs: SimpleNamespace(version_id="explicit-candidate"),
        rename=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        cluster_lab,
        "cluster_model_path_for_version",
        lambda _version: pytest.fail("explicit actions resolved a semantic model"),
    )
    monkeypatch.setattr(
        cluster_lab,
        "cluster_registry_service",
        lambda _storage, **kwargs: calls.append(kwargs) or service,
    )

    fitted = cluster_lab.execute_cluster_action(
        storage, action="fit", payload={"strategy": "explicit"}
    )
    renamed = cluster_lab.execute_cluster_action(
        storage,
        action="rename",
        payload={"clusterId": "billing", "displayName": "Billing"},
    )

    assert fitted["versionId"] == "explicit-candidate"
    assert renamed["clusterId"] == "billing"
    assert calls == [
        {"model_path": None, "allow_download": False, "strategy": "explicit"},
        {"strategy": "explicit"},
    ]
