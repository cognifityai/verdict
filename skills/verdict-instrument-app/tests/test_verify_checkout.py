from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]
VERIFIER_PATH = SKILL_ROOT / "scripts" / "verify_verdict_checkout.py"

SPEC = importlib.util.spec_from_file_location("verdict_checkout_verifier", VERIFIER_PATH)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFIER
SPEC.loader.exec_module(VERIFIER)


def test_current_checkout_matches_the_pinned_a4_runtime() -> None:
    report = VERIFIER.verify(REPO_ROOT)

    assert report["ready"] is True
    assert report["target_tag"] == "v0.1.0a4"
    assert report["target_commit"] == "49eae0a67d471b087d7c146c5abbd215e723f3ad"
    assert len(report["commit"]) == 40
    assert all(check["ok"] for check in report["checks"])


def test_native_skill_copy_resolves_repo_from_explicit_argument(tmp_path: Path) -> None:
    installed = tmp_path / "native" / "verdict-instrument-app" / "scripts"
    installed.mkdir(parents=True)
    copied_verifier = installed / "verify_verdict_checkout.py"
    shutil.copyfile(VERIFIER_PATH, copied_verifier)

    result = subprocess.run(
        [
            sys.executable,
            str(copied_verifier),
            str(REPO_ROOT),
            "--format",
            "json",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ready"] is True


def test_unrelated_directory_fails_closed_without_file_contents(
    tmp_path: Path,
) -> None:
    canary = "checkout-verifier-private-canary"
    (tmp_path / "pyproject.toml").write_text(
        f"# {canary}\nversion = '0.1.0a4'\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(VERIFIER_PATH),
            str(tmp_path),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["ready"] is False
    assert canary not in result.stdout
    assert any(not check["ok"] for check in report["checks"])
