#!/usr/bin/env python3
"""Fail-closed PyPI publication guard for resumable multi-package releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path

PYPI_JSON_URL = "https://pypi.org/pypi/{project}/{version}/json"


class ReleaseStateError(RuntimeError):
    """The remote release state is unsafe to publish or resume."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_hashes(artifact_dir: Path) -> dict[str, str]:
    """Return hashes for exactly one reviewed wheel and one reviewed sdist."""
    try:
        artifacts = sorted(artifact_dir.iterdir())
    except OSError as exc:
        raise ReleaseStateError(f"cannot read local artifact directory {artifact_dir}") from exc
    wheels = [path for path in artifacts if path.is_file() and path.suffix == ".whl"]
    sdists = [
        path for path in artifacts if path.is_file() and path.name.endswith(".tar.gz")
    ]
    if (
        len(artifacts) != 2
        or len(wheels) != 1
        or len(sdists) != 1
        or any(path.is_symlink() for path in artifacts)
    ):
        raise ReleaseStateError(
            f"{artifact_dir} must contain exactly one wheel and one source archive"
        )
    try:
        return {artifact.name: _sha256(artifact) for artifact in (*wheels, *sdists)}
    except OSError as exc:
        raise ReleaseStateError(f"cannot hash local artifacts in {artifact_dir}") from exc


def remote_hashes(payload: Mapping[str, object] | object) -> dict[str, str]:
    """Read PyPI's filename-to-SHA-256 mapping from a version response."""
    if not isinstance(payload, Mapping):
        raise ReleaseStateError("PyPI version response is not an object")
    urls = payload.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ReleaseStateError("PyPI version response contains no release files")
    result: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ReleaseStateError("PyPI version response contains an invalid file entry")
        filename = item.get("filename")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, dict) else None
        if (
            not isinstance(filename, str)
            or not filename
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ReleaseStateError("PyPI version response omits a filename or SHA-256")
        if filename in result:
            raise ReleaseStateError(f"PyPI version response repeats {filename!r}")
        result[filename] = digest
    return result


def missing_artifacts(
    local: Mapping[str, str], remote: Mapping[str, str] | None
) -> tuple[str, ...]:
    """Return missing reviewed files after validating every existing remote file."""
    published = {} if remote is None else dict(remote)
    for filename, digest in published.items():
        if filename not in local:
            raise ReleaseStateError(f"PyPI contains unknown release file {filename!r}")
        if local[filename] != digest:
            raise ReleaseStateError(f"PyPI contains a changed digest for {filename!r}")
    return tuple(sorted(filename for filename in local if filename not in published))


def stage_artifacts(
    artifact_dir: Path,
    staging_dir: Path,
    expected_hashes: Mapping[str, str],
    filenames: tuple[str, ...],
) -> tuple[Path, ...]:
    """Copy only missing reviewed files into a new empty publisher directory."""
    try:
        if staging_dir.exists():
            if not staging_dir.is_dir() or any(staging_dir.iterdir()):
                raise ReleaseStateError(f"publisher staging directory is not empty: {staging_dir}")
        else:
            staging_dir.mkdir(parents=True)
        staged: list[Path] = []
        for filename in filenames:
            source = artifact_dir / filename
            destination = staging_dir / filename
            expected_digest = expected_hashes.get(filename)
            if expected_digest is None or _sha256(source) != expected_digest:
                raise ReleaseStateError(f"local artifact changed for {filename!r}")
            shutil.copyfile(source, destination)
            if _sha256(destination) != expected_digest:
                raise ReleaseStateError(f"staged artifact digest changed for {filename!r}")
            staged.append(destination)
    except ReleaseStateError:
        raise
    except OSError as exc:
        raise ReleaseStateError(f"cannot stage reviewed artifacts in {staging_dir}") from exc
    return tuple(staged)


def fetch_remote_hashes(project: str, version: str) -> dict[str, str] | None:
    """Return the published file hashes, or None only for an absent version."""
    url = PYPI_JSON_URL.format(
        project=urllib.parse.quote(project, safe=""),
        version=urllib.parse.quote(version, safe=""),
    )
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "verdict-release-guard/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseStateError(f"PyPI query failed with HTTP {exc.code}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseStateError("PyPI query failed") from exc
    if not isinstance(payload, dict):
        raise ReleaseStateError("PyPI version response is not an object")
    return remote_hashes(payload)


def write_github_output(publish: bool) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"publish={'true' if publish else 'false'}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish an absent PyPI version or skip an exact existing artifact set."
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--staging-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        local = artifact_hashes(args.artifact_dir)
        remote = fetch_remote_hashes(args.project, args.version)
        missing = missing_artifacts(local, remote)
        staged = (
            stage_artifacts(args.artifact_dir, args.staging_dir, local, missing)
            if missing
            else ()
        )
    except ReleaseStateError as exc:
        print(f"release guard rejected {args.project} {args.version}: {exc}", file=sys.stderr)
        return 1

    publish = bool(staged)
    write_github_output(publish)
    state = f"stage {len(staged)} missing reviewed file(s)" if publish else "already exact; skip"
    print(f"{args.project} {args.version}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
