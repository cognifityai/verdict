"""Bounded adapter from dashboard uploads to the existing Inspect package."""

from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

MAX_INSPECT_BYTES = 4 * 1024 * 1024
MAX_INSPECT_TURNS = 10_000
FORMATS = {"auto", "chatgpt", "claude_ai", "cowork", "openai_jsonl"}
_LOCK = threading.Lock()


def inspect_export(
    content: bytes,
    *,
    format_name: str,
    enable_semantic: bool,
    enable_judge: bool,
    judge_model: str,
    confirm_external_egress: bool,
) -> dict[str, Any]:
    """Analyze one export without retaining its source text or report."""
    if not content or len(content) > MAX_INSPECT_BYTES:
        raise ValueError("inspect input must be between 1 byte and 4 MiB")
    if format_name not in FORMATS:
        raise ValueError("unsupported inspect format")
    if not judge_model or len(judge_model.encode("utf-8")) > 256:
        raise ValueError("invalid judge model")
    if enable_judge and not confirm_external_egress:
        raise ValueError("external judge egress was not confirmed")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("inspect input must be UTF-8") from exc
    if not text.strip() or "\x00" in text:
        raise ValueError("inspect input is empty or invalid")
    if not _LOCK.acquire(blocking=False):
        raise RuntimeError("another inspect analysis is already running")

    try:
        from verdict_inspect.parsers.autodetect import detect_format, parse
        from verdict_inspect.pipeline import run_inspect

        suffix = ".jsonl" if format_name in {"cowork", "openai_jsonl"} else ".json"
        with TemporaryDirectory(prefix="verdict-inspect-") as directory:
            path = Path(directory) / f"upload{suffix}"
            path.write_text(text, encoding="utf-8")
            resolved_format = detect_format(path) if format_name == "auto" else format_name
            conversations = parse(path, resolved_format)
        if not conversations:
            raise ValueError("inspect input contains no supported conversations")
        if sum(conversation.n_turns for conversation in conversations) > MAX_INSPECT_TURNS:
            raise ValueError("inspect input contains too many turns")

        report = run_inspect(
            conversations,
            format_name=resolved_format,
            judge_model=judge_model,
            enable_semantic=enable_semantic,
            enable_judge=enable_judge,
        )
        return {"schema": "verdict-inspect-dashboard-v1", "report": asdict(report)}
    finally:
        _LOCK.release()
