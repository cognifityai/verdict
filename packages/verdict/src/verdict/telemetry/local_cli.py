"""Standalone local Claude/Codex history capture command."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from verdict.telemetry.local_agents import capture_local_history_url
from verdict.telemetry.runner import ImportRunError


def default_storage() -> str:
    return os.environ.get("VERDICT_STORAGE", "sqlite:///./verdict.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verdict-agent-capture",
        description="Import completed root Claude Code and Codex turns into Verdict.",
    )
    parser.add_argument("--storage", default=default_storage())
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--source", choices=["all", "codex", "claude"], default="all")
    parser.add_argument("--tenant-id")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summaries = capture_local_history_url(
            args.storage,
            codex_root=args.codex_root,
            claude_root=args.claude_root,
            source=args.source,
            tenant_id=args.tenant_id,
        )
    except Exception as exc:
        error = (
            str(exc)
            if isinstance(exc, ImportRunError)
            else f"local capture failed ({type(exc).__name__})"
        )
        print(f"ERROR: {error}")
        return 2
    sources = {name: asdict(summary) for name, summary in summaries.items()}
    payload = {
        "schema": "verdict-local-capture-v1",
        "stored": sum(summary.stored for summary in summaries.values()),
        "skipped": sum(summary.skipped for summary in summaries.values()),
        "sources": sources,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Imported {payload['stored']} completed local agent turn(s).")
        print(f"Skipped {payload['skipped']} incomplete or unsupported record(s).")
        print("Next: verdict-dashboard --storage <the same protected storage URL>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
