from __future__ import annotations

import sys
from types import ModuleType

import pytest
from verdict.dashboard import cluster_lab


def _snapshot(cache_root):
    path = (
        cache_root
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
        / cluster_lab._MINILM_REVISION
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
    monkeypatch.setattr(cluster_lab.Path, "home", lambda: tmp_path / "ignored-user")

    assert cluster_lab._model_path(None, allow_download=False) == expected


def test_semantic_model_cache_uses_hf_home_hub_directory(monkeypatch, tmp_path):
    hf_home = tmp_path / "hf-home"
    expected = _snapshot(hf_home / "hub")
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(hf_home))
    monkeypatch.setattr(cluster_lab.Path, "home", lambda: tmp_path / "ignored-user")

    assert cluster_lab._model_path(None, allow_download=False) == expected


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
    monkeypatch.setattr(cluster_lab.Path, "home", lambda: tmp_path / "empty-home")

    assert cluster_lab._model_path(None, allow_download=True) == downloaded
    assert calls == [(
        "sentence-transformers/all-MiniLM-L6-v2",
        {"revision": cluster_lab._MINILM_REVISION},
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
    monkeypatch.setattr(cluster_lab.Path, "home", lambda: tmp_path / "empty-home")

    with pytest.raises(ValueError, match=r"^model_unavailable$"):
        cluster_lab._model_path(None, allow_download=True)
