"""Canonical local-agent import orchestration."""

from __future__ import annotations

import os
from pathlib import Path

from verdict.client import _resolve_storage
from verdict.storage.base import Storage
from verdict.telemetry.model import ImportContext, ImportSummary
from verdict.telemetry.runner import import_into_storage
from verdict.telemetry.sources.claude_code import iter_claude_history
from verdict.telemetry.sources.codex import iter_codex_history


def capture_local_history(
    storage: Storage,
    *,
    codex_root: Path,
    claude_root: Path,
    source: str = "all",
    tenant_id: str | None = None,
    home: Path | None = None,
) -> dict[str, ImportSummary]:
    """Import local agent turns through Verdict's one persistence runner."""
    if source not in {"all", "codex", "claude"}:
        raise ValueError("source must be all, codex, or claude")
    summaries: dict[str, ImportSummary] = {}
    if source in {"all", "codex"}:
        context = ImportContext(
            adapter="codex",
            source_scope=str(codex_root.expanduser().resolve()),
            tenant_id=tenant_id,
        )
        summaries["codex"] = import_into_storage(
            iter_codex_history(codex_root, context=context, home=home), storage
        )
    if source in {"all", "claude"}:
        context = ImportContext(
            adapter="claude",
            source_scope=str(claude_root.expanduser().resolve()),
            tenant_id=tenant_id,
        )
        summaries["claude"] = import_into_storage(
            iter_claude_history(claude_root, context=context, home=home), storage
        )
    return summaries


def capture_local_history_url(
    storage_url: str,
    *,
    codex_root: Path,
    claude_root: Path,
    source: str = "all",
    tenant_id: str | None = None,
    home: Path | None = None,
) -> dict[str, ImportSummary]:
    """Open configured storage once and run the canonical local import."""
    if storage_url.startswith("memory://") or storage_url == "sqlite:///:memory:":
        raise ValueError("local agent capture requires durable SQLite or PostgreSQL storage")
    _prepare_private_sqlite(storage_url)
    storage = _resolve_storage(storage_url)
    try:
        return capture_local_history(
            storage,
            codex_root=codex_root,
            claude_root=claude_root,
            source=source,
            tenant_id=tenant_id,
            home=home,
        )
    finally:
        storage.close()


def _prepare_private_sqlite(storage_url: str) -> Path | None:
    if not storage_url.startswith("sqlite:///"):
        return None
    path = Path(storage_url[len("sqlite:///") :]).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    return path
