from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1]
INSPECTOR_PATH = SKILL_ROOT / "scripts" / "inspect_environment.py"

SPEC = importlib.util.spec_from_file_location("verdict_environment_inspector", INSPECTOR_PATH)
assert SPEC is not None and SPEC.loader is not None
INSPECTOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INSPECTOR
SPEC.loader.exec_module(INSPECTOR)


class Distribution:
    def __init__(self, version: str, *, editable: bool = False) -> None:
        self.version = version
        self._editable = editable

    def read_text(self, name: str) -> str | None:
        if name != "direct_url.json" or not self._editable:
            return None
        return json.dumps({"dir_info": {"editable": True}})


def inspect(
    monkeypatch: pytest.MonkeyPatch,
    versions: dict[str, Distribution],
    *,
    storage: str | None = None,
) -> dict[str, object]:
    def distribution(name: str) -> Distribution:
        try:
            return versions[name]
        except KeyError as exc:
            raise INSPECTOR.metadata.PackageNotFoundError(name) from exc

    monkeypatch.setattr(INSPECTOR.metadata, "distribution", distribution)
    monkeypatch.setattr(INSPECTOR.shutil, "which", lambda *_args, **_kwargs: None)
    if storage is None:
        monkeypatch.delenv("VERDICT_STORAGE", raising=False)
    else:
        monkeypatch.setenv("VERDICT_STORAGE", storage)
    return INSPECTOR.inspect_environment()


def synchronized(version: str, *, editable: bool = False) -> dict[str, Distribution]:
    return {
        name: Distribution(version, editable=editable) for name in INSPECTOR.REQUIRED_DISTRIBUTIONS
    }


def test_absent_environment_requires_install(monkeypatch: pytest.MonkeyPatch) -> None:
    report = inspect(monkeypatch, {})

    assert report["state"] == "absent"
    assert report["action"] == "install"
    assert "verdict-cluster" in report["commands"]


def test_a5_editable_environment_requires_synchronized_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = inspect(monkeypatch, synchronized("0.1.0a5", editable=True))

    assert report["state"] == "upgrade"
    assert report["action"] == "upgrade"
    packages = report["packages"]
    assert isinstance(packages, dict)
    assert all(package["editable"] for package in packages.values())


def test_a6_environment_requires_synchronized_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    report = inspect(monkeypatch, synchronized("0.1.0a6"))

    assert report["state"] == "upgrade"
    assert report["action"] == "upgrade"


def test_a7_environment_requires_synchronized_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = inspect(monkeypatch, synchronized("0.1.0a7"))

    assert report["state"] == "upgrade"
    assert report["action"] == "upgrade"


def test_a8_environment_requires_synchronized_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = inspect(monkeypatch, synchronized("0.1.0a8"))

    assert report["state"] == "upgrade"
    assert report["action"] == "upgrade"


def test_a9_environment_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    report = inspect(monkeypatch, synchronized("0.1.0a9"))

    assert report["state"] == "current"
    assert report["action"] == "none"


def test_mixed_versions_fail_closed_to_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = synchronized("0.1.0a9")
    versions["cognifity-verdict-eval"] = Distribution("0.1.0a5")

    report = inspect(monkeypatch, versions)

    assert report["state"] == "repair"
    assert report["action"] == "repair"


def test_unrelated_verdict_distribution_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = synchronized("0.1.0a9")
    versions["verdict"] = Distribution("1.0.0")

    report = inspect(monkeypatch, versions)

    assert report["state"] == "conflict"
    assert report["action"] == "remove-conflict"


@pytest.mark.parametrize(
    ("storage", "backend"),
    [
        ("sqlite:////private/data/verdict.db", "sqlite"),
        ("postgresql://user:secret@db.example/verdict", "postgresql"),
        ("postgres://user:secret@db.example/verdict", "postgresql"),
        ("postgresql://[malformed-secret", "other"),
    ],
)
def test_storage_report_exposes_backend_but_never_secret(
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
    backend: str,
) -> None:
    report = inspect(monkeypatch, synchronized("0.1.0a9"), storage=storage)

    assert report["storage_backend"] == backend
    assert "secret" not in json.dumps(report)
