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


def clone_repo(destination: Path, *, shallow: bool) -> Path:
    command = ["git", "clone", "--quiet", "--no-tags"]
    if shallow:
        command.extend(["--depth", "1"])
    command.extend([REPO_ROOT.as_uri(), str(destination)])
    subprocess.run(command, check=True, capture_output=True, text=True)
    if not shallow:
        shutil.rmtree(destination / ".git")
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "add", "."],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Verifier Test",
                "-c",
                "user.email=verifier@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test fixture",
            ],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
    return destination


def check(report: dict[str, object], check_id: str) -> dict[str, object]:
    return next(item for item in report["checks"] if item["check_id"] == check_id)


def test_current_a5_checkout_is_rejected_by_the_historical_a4_verifier() -> None:
    report = VERIFIER.verify(REPO_ROOT)

    assert report["ready"] is False
    assert report["schema_version"] == 2
    assert report["target_tag"] == "v0.1.0a4"
    assert report["target_commit"] == "49eae0a67d471b087d7c146c5abbd215e723f3ad"
    assert len(report["commit"]) == 40
    assert check(report, "workspace-version")["ok"] is False


def test_native_historical_verifier_rejects_current_a5_checkout(tmp_path: Path) -> None:
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

    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout)["ready"] is False


def test_real_shallow_a5_checkout_does_not_impersonate_a4(tmp_path: Path) -> None:
    checkout = clone_repo(tmp_path / "shallow", shallow=True)
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "true"

    report = VERIFIER.verify(checkout)

    assert report["ready"] is False
    assert report["identity_mode"] is None
    target_check = check(report, "target-release-identity")
    assert target_check["ok"] is False


def test_tagless_non_shallow_checkout_fails(tmp_path: Path) -> None:
    checkout = clone_repo(tmp_path / "full", shallow=False)
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "false"

    report = VERIFIER.verify(checkout)

    assert report["ready"] is False
    assert report["identity_mode"] is None
    target_check = check(report, "target-release-identity")
    assert "non-shallow checkout" in target_check["detail"]


def test_wrong_present_tag_fails_even_when_runtime_manifest_matches(monkeypatch) -> None:
    original_run = VERIFIER._run

    def run_with_wrong_tag(args, *, cwd, timeout=15.0):
        if args == [
            "git",
            "tag",
            "--list",
            "v0.1.0a4",
        ]:
            return subprocess.CompletedProcess(args, 0, "v0.1.0a4\n", "")
        if args == [
            "git",
            "rev-parse",
            "--verify",
            "refs/tags/v0.1.0a4^{commit}",
        ]:
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return original_run(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(VERIFIER, "_run", run_with_wrong_tag)
    report = VERIFIER.verify(REPO_ROOT)

    assert report["ready"] is False
    target_check = check(report, "target-release-identity")
    assert target_check["ok"] is False


def test_changed_runtime_object_fails_with_valid_tag(monkeypatch) -> None:
    original_run = VERIFIER._run

    def run_with_changed_runtime(args, *, cwd, timeout=15.0):
        if args == [
            "git",
            "rev-parse",
            "--verify",
            "HEAD:packages/verdict",
        ]:
            return subprocess.CompletedProcess(args, 0, "0" * 40 + "\n", "")
        return original_run(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(VERIFIER, "_run", run_with_changed_runtime)
    report = VERIFIER.verify(REPO_ROOT)

    assert report["ready"] is False
    runtime_check = check(report, "released-runtime-match")
    assert runtime_check["ok"] is False


def test_hidden_index_flags_and_changed_bytes_fail(
    tmp_path: Path,
) -> None:
    for flag in ("--assume-unchanged", "--skip-worktree"):
        checkout = clone_repo(tmp_path / flag.removeprefix("--"), shallow=True)
        runtime_file = checkout / "scripts" / "run_probes.py"
        subprocess.run(
            ["git", "update-index", flag, "scripts/run_probes.py"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        runtime_file.write_text(
            runtime_file.read_text(encoding="utf-8") + "\n# hidden mutation\n",
            encoding="utf-8",
        )

        report = VERIFIER.verify(checkout)

        assert report["ready"] is False
        assert check(report, "runtime-index-flags")["ok"] is False
        assert check(report, "runtime-worktree-content")["ok"] is False


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
