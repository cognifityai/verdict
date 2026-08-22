#!/usr/bin/env python3
"""Reject private environment and review files from release archives."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

BANNED_PREFIXES = ("_review", "_sync", "_verdict_src")


def _private_segment(segment: str, *, terminal: bool) -> bool:
    lowered = segment.lower()
    if lowered == ".env.example":
        return not (terminal and segment == ".env.example")
    return (
        ".env" in lowered
        or lowered == ".direnv"
        or lowered.startswith(BANNED_PREFIXES)
    )


def private_members(members: Iterable[str]) -> list[str]:
    """Return archive members that must never ship in a release artifact."""
    rejected = []
    for member in members:
        path = PurePosixPath(member.replace("\\", "/"))
        segments = [segment for segment in path.parts if segment not in {"/", "."}]
        if any(
            _private_segment(segment, terminal=index == len(segments) - 1)
            for index, segment in enumerate(segments)
        ):
            rejected.append(member)
    return rejected


def scan_artifact(artifact: Path) -> list[str]:
    """Return prohibited members from a supported wheel or source archive."""
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return private_members(archive.namelist())
    if artifact.name.endswith(".tar.gz"):
        with tarfile.open(artifact, "r:gz") as archive:
            return private_members(archive.getnames())
    return []


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path("dist")
    artifacts = sorted(
        artifact
        for artifact in root.glob("*/*")
        if artifact.suffix == ".whl" or artifact.name.endswith(".tar.gz")
    )
    if not artifacts:
        print(f"{root} contains no wheel or source archives", file=sys.stderr)
        return 2
    for artifact in artifacts:
        rejected = scan_artifact(artifact)
        if rejected:
            print(f"{artifact} contains private files: {rejected}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
