#!/usr/bin/env python3
"""Verify the pinned Verdict source checkout used by an installed POC skill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
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
RUNTIME_OBJECT_IDS = {
    "packages/verdict": "e5e0a7a32c372a254c003d0c53e5ba6d1dde40ea",
    "packages/verdict_eval": "b8904fb8ca85c4bb9655c90cbf06184fd329f968",
    "scripts/run_drift_pipeline.py": "a9537bd474acd41fc6e5fcaccfc2dd2773c3c674",
    "scripts/run_probes.py": "2e000292e5fb991be2964228d574bafd69ee19be",
    "ui/server.py": "114c3f5e749c190be911b91796898a3ebf72ee1e",
}
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


def _blob_object_id(path: Path, mode: str) -> str | None:
    """Hash the checked-out bytes using Git's blob object format."""

    try:
        before = path.lstat()
        if mode == "120000":
            if not stat.S_ISLNK(before.st_mode):
                return None
            data = os.fsencode(os.readlink(path))
            digest = hashlib.sha1(usedforsecurity=False)
            digest.update(f"blob {len(data)}\0".encode())
            digest.update(data)
            return digest.hexdigest()
        if not stat.S_ISREG(before.st_mode):
            return None
        digest = hashlib.sha1(usedforsecurity=False)
        digest.update(f"blob {before.st_size}\0".encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.lstat()
    except (OSError, ValueError):
        return None
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        return None
    return digest.hexdigest()


def _worktree_bytes_match_head(root: Path) -> bool:
    """Compare actual runtime bytes with the committed blobs, ignoring index hints."""

    tree = _run(
        ["git", "ls-tree", "-r", "-z", "HEAD", "--", *RUNTIME_PATHS],
        cwd=root,
    )
    if tree is None or tree.returncode != 0:
        return False
    records = [record for record in tree.stdout.split("\0") if record]
    if not records:
        return False
    for record in records:
        try:
            metadata, relative = record.split("\t", 1)
            mode, object_type, expected = metadata.split(" ", 2)
        except ValueError:
            return False
        if object_type != "blob" or not re.fullmatch(r"[0-9a-f]{40}", expected):
            return False
        if _blob_object_id(root / relative, mode) != expected:
            return False
    return True


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

    runtime_object_ids: dict[str, str] = {}
    for path in RUNTIME_PATHS:
        runtime_object = _run(
            ["git", "rev-parse", "--verify", f"HEAD:{path}"],
            cwd=root,
        )
        if runtime_object is None or runtime_object.returncode != 0:
            continue
        object_id = runtime_object.stdout.strip().lower()
        if re.fullmatch(r"[0-9a-f]{40}", object_id):
            runtime_object_ids[path] = object_id
    runtime_matches = runtime_object_ids == RUNTIME_OBJECT_IDS

    shallow = _run(["git", "rev-parse", "--is-shallow-repository"], cwd=root)
    is_shallow = (
        shallow is not None
        and shallow.returncode == 0
        and shallow.stdout.strip() == "true"
    )

    tag_ref = _run(
        ["git", "tag", "--list", TARGET_TAG],
        cwd=root,
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
    tag_present = (
        tag_ref is not None
        and tag_ref.returncode == 0
        and tag_ref.stdout.strip() == TARGET_TAG
    )
    tag_absent = (
        tag_ref is not None
        and tag_ref.returncode == 0
        and not tag_ref.stdout.strip()
    )
    target_identity_ok = has_tag or (tag_absent and is_shallow and runtime_matches)
    identity_mode = (
        "tag"
        if has_tag
        else "shallow-runtime-manifest"
        if target_identity_ok
        else None
    )
    checks.append(
        Check(
            "target-release-identity",
            target_identity_ok,
            f"target tag {TARGET_TAG} resolves to the expected release commit"
            if has_tag
            else (
                "target tag is absent in this shallow checkout; immutable runtime "
                "object identities match the release"
                if target_identity_ok
                else (
                    f"target tag {TARGET_TAG} has unexpected identity"
                    if tag_present
                    else (
                        f"target tag {TARGET_TAG} is absent from a non-shallow checkout"
                        if tag_absent and not is_shallow
                        else f"target tag {TARGET_TAG} could not be verified"
                    )
                )
            ),
        )
    )

    checks.append(
        Check(
            "released-runtime-match",
            runtime_matches,
            "runtime paths match the immutable release object manifest"
            if runtime_matches
            else "runtime paths differ from the release manifest or could not be compared",
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
    index = _run(
        ["git", "ls-files", "-v", "-z", "--", *RUNTIME_PATHS],
        cwd=root,
    )
    index_records = (
        [record for record in index.stdout.split("\0") if record]
        if index is not None and index.returncode == 0
        else []
    )
    index_flags_clean = bool(index_records) and all(
        not record[0].islower() and record[0] != "S" for record in index_records
    )
    checks.append(
        Check(
            "runtime-index-flags",
            index_flags_clean,
            "runtime paths do not use assume-unchanged or skip-worktree flags"
            if index_flags_clean
            else "runtime paths use hidden index flags or could not be enumerated",
        )
    )

    worktree_bytes_match = _worktree_bytes_match_head(root)
    checks.append(
        Check(
            "runtime-worktree-content",
            worktree_bytes_match,
            "checked-out runtime bytes match the committed blobs"
            if worktree_bytes_match
            else "checked-out runtime bytes differ from committed blobs",
        )
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
        "schema_version": 2,
        "root": str(root),
        "target_tag": TARGET_TAG,
        "target_commit": TARGET_COMMIT,
        "target_version": TARGET_VERSION,
        "commit": commit,
        "identity_mode": identity_mode,
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
