#!/usr/bin/env python3
"""Report installed Verdict and storage state without exposing secrets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from importlib import metadata
from typing import Any
from urllib.parse import urlsplit

TARGET_VERSION = "0.1.0a6"
UPGRADE_VERSIONS = {"0.1.0a5"}
REQUIRED_DISTRIBUTIONS = (
    "cognifity-verdict",
    "cognifity-verdict-eval",
    "cognifity-verdict-inspect",
)
COMMANDS = (
    "verdict-dashboard",
    "verdict-pipeline",
    "verdict-probes",
    "verdict-inspect",
)


def _package(name: str) -> dict[str, str | bool | None]:
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return {"version": None, "editable": False}

    editable = False
    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        try:
            editable = json.loads(direct_url).get("dir_info", {}).get("editable") is True
        except (AttributeError, json.JSONDecodeError):
            pass
    return {"version": distribution.version, "editable": editable}


def _storage_backend(value: str | None) -> str:
    if not value:
        return "unset"
    try:
        scheme = urlsplit(value).scheme.lower().split("+", 1)[0]
    except ValueError:
        return "other"
    if scheme in {"postgres", "postgresql"}:
        return "postgresql"
    if scheme == "sqlite":
        return "sqlite"
    return "other"


def _state(
    packages: dict[str, dict[str, str | bool | None]],
    *,
    unrelated_verdict: bool,
) -> tuple[str, str]:
    if unrelated_verdict:
        return "conflict", "remove-conflict"

    versions = [packages[name]["version"] for name in REQUIRED_DISTRIBUTIONS]
    if all(version is None for version in versions):
        return "absent", "install"
    if versions == [TARGET_VERSION] * len(REQUIRED_DISTRIBUTIONS):
        return "current", "none"
    if len(set(versions)) == 1 and versions[0] in UPGRADE_VERSIONS:
        return "upgrade", "upgrade"
    return "repair", "repair"


def inspect_environment() -> dict[str, Any]:
    packages = {name: _package(name) for name in REQUIRED_DISTRIBUTIONS}
    unrelated = _package("verdict")["version"] is not None
    state, action = _state(packages, unrelated_verdict=unrelated)
    scripts = os.path.dirname(sys.executable)
    return {
        "schema_version": 1,
        "target_version": TARGET_VERSION,
        "state": state,
        "action": action,
        "packages": packages,
        "unrelated_verdict_installed": unrelated,
        "commands": {
            command: shutil.which(command, path=scripts) is not None for command in COMMANDS
        },
        "storage_backend": _storage_backend(os.environ.get("VERDICT_STORAGE")),
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect the active Python environment for Verdict 0.1.0a6."
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report = inspect_environment()
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Verdict environment: {report['state']} (action: {report['action']})")
        print(f"Target: {report['target_version']}")
        print(f"Storage backend: {report['storage_backend']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
