#!/usr/bin/env python3
"""Verify the pinned Verdict source checkout used by an installed POC skill."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

TARGET_TAG = "v0.1.0a4"
TARGET_VERSION = "0.1.0a4"
TARGET_COMMIT = "49eae0a67d471b087d7c146c5abbd215e723f3ad"
RUNTIME_PATHS = (
    "packages/verdict",
    "packages/verdict_eval",
    "scripts/run_drift_pipeline.py",
    "scripts/run_probes.py",
    "ui/server.py",
)
REQUIRED_FILES = (*RUNTIME_PATHS, "pyproject.toml")


@dataclass(frozen=True)
class Check:
    check_id: str
    ok: bool
    detail: str


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def verify(root: Path) -> dict[str, object]:
    checks: list[Check] = []
    commit: str | None = None

    missing = [relative for relative in REQUIRED_FILES if not (root / relative).exists()]
    checks.append(
        Check(
            "required-files",
            not missing,
            "all required source files are present"
            if not missing
            else "required source files are missing",
        )
    )

    version = None
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and pyproject.stat().st_size <= 1_000_000:
        match = re.search(
            r"(?m)^version\s*=\s*[\"']([^\"']+)[\"']\s*$",
            pyproject.read_text(encoding="utf-8", errors="replace"),
        )
        version = match.group(1) if match else None
    checks.append(
        Check(
            "workspace-version",
            version == TARGET_VERSION,
            f"workspace version is {TARGET_VERSION}"
            if version == TARGET_VERSION
            else f"workspace version does not equal {TARGET_VERSION}",
        )
    )

    git_commit = _run(["git", "rev-parse", "HEAD"], cwd=root)
    if git_commit is not None and git_commit.returncode == 0:
        candidate = git_commit.stdout.strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
            commit = candidate.lower()
    checks.append(
        Check(
            "git-commit",
            commit is not None,
            "exact source commit recorded"
            if commit is not None
            else "could not record an exact Git commit",
        )
    )

    tag = _run(
        ["git", "rev-parse", "--verify", f"refs/tags/{TARGET_TAG}^{{commit}}"],
        cwd=root,
    )
    has_tag = (
        tag is not None
        and tag.returncode == 0
        and tag.stdout.strip().lower() == TARGET_COMMIT
    )
    checks.append(
        Check(
            "target-tag",
            has_tag,
            f"target tag {TARGET_TAG} resolves to the expected release commit"
            if has_tag
            else f"target tag {TARGET_TAG} is unavailable or has unexpected identity",
        )
    )

    runtime_matches = False
    if has_tag:
        diff = _run(
            ["git", "diff", "--quiet", TARGET_COMMIT, "--", *RUNTIME_PATHS],
            cwd=root,
        )
        runtime_matches = diff is not None and diff.returncode == 0
    checks.append(
        Check(
            "released-runtime-match",
            runtime_matches,
            "runtime paths match the target tag"
            if runtime_matches
            else "runtime paths differ from the target tag or could not be compared",
        )
    )

    worktree = _run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *RUNTIME_PATHS],
        cwd=root,
    )
    runtime_clean = (
        worktree is not None
        and worktree.returncode == 0
        and not worktree.stdout.strip()
    )
    checks.append(
        Check(
            "runtime-worktree-clean",
            runtime_clean,
            "runtime paths have no local changes"
            if runtime_clean
            else "runtime paths have local or untracked changes",
        )
    )

    pipeline = root / "scripts" / "run_drift_pipeline.py"
    help_result = None
    if pipeline.is_file():
        help_result = _run([sys.executable, str(pipeline), "--help"], cwd=root)
    help_text = help_result.stdout if help_result is not None else ""
    parser_ok = (
        help_result is not None
        and help_result.returncode == 0
        and "--storage" in help_text
        and "--trust-existing-clusters" in help_text
        and "--yes-spend" not in help_text
        and "--max-spend-usd" not in help_text
    )
    checks.append(
        Check(
            "drift-parser-contract",
            parser_ok,
            "drift parser matches the a4 skill contract"
            if parser_ok
            else "drift parser does not match the a4 skill contract",
        )
    )

    return {
        "schema_version": 1,
        "root": str(root),
        "target_tag": TARGET_TAG,
        "target_commit": TARGET_COMMIT,
        "target_version": TARGET_VERSION,
        "commit": commit,
        "ready": all(check.ok for check in checks),
        "checks": [asdict(check) for check in checks],
        "limitations": [
            "This verifies source identity and parser shape, not installed provider SDK behavior.",
            "Run the skill's capture, privacy, clustering, judge, dashboard, and rollback gates separately.",
        ],
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the Verdict 0.1.0a4 source checkout used by the POC skill."
    )
    parser.add_argument("root", type=Path, help="absolute Verdict source checkout")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    return parser.parse_args(argv)


def render_text(report: dict[str, object]) -> str:
    lines = [
        f"Verdict checkout: {report['root']}",
        f"Commit: {report['commit'] or '<unavailable>'}",
        f"Ready: {report['ready']}",
    ]
    for check in report["checks"]:  # type: ignore[union-attr]
        marker = "PASS" if check["ok"] else "FAIL"
        lines.append(f"{marker:>4}  {check['check_id']} — {check['detail']}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: Verdict checkout is not a directory: {root}", file=sys.stderr)
        return 2

    report = verify(root)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
