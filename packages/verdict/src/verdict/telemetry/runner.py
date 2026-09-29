"""Synchronous import runner: mapping result to existing Storage port."""

from __future__ import annotations

from collections.abc import Iterable

from verdict.storage.base import Storage
from verdict.telemetry.model import ImportSummary, MappingResult


class ImportRunError(RuntimeError):
    """Source or storage failure with safe progress counters."""

    def __init__(self, stage: str, summary: ImportSummary, cause: BaseException) -> None:
        self.stage = stage
        self.summary = summary
        self.cause_type = type(cause).__name__
        super().__init__(
            f"telemetry import {stage} failed after seen={summary.seen}, "
            f"stored={summary.stored}, conversations_stored={summary.conversations_stored}, "
            f"skipped={summary.skipped} ({self.cause_type})"
        )


def import_into_storage(results: Iterable[MappingResult], storage: Storage) -> ImportSummary:
    """Synchronously persist mapped records through a direct storage adapter."""
    summary = ImportSummary()
    from verdict.storage.buffered import BufferedStorage

    if isinstance(storage, BufferedStorage):
        raise ImportRunError(
            "requires_direct_storage", summary,
            RuntimeError("buffered writes cannot confirm import persistence"),
        )
    iterator = iter(results)
    while True:
        try:
            result = next(iterator)
        except StopIteration:
            return summary
        except Exception as exc:
            raise ImportRunError("source", summary, exc) from exc
        summary.seen += 1
        if result.conversation is not None:
            try:
                storage.save_conversation(result.conversation)
            except Exception as exc:
                raise ImportRunError("conversation_storage", summary, exc) from exc
            summary.conversations_stored += 1
        if result.trace is None:
            summary.add_skip(result.skip_reason or "unknown_skip")
            continue
        try:
            storage.insert_trace(result.trace)
        except Exception as exc:
            raise ImportRunError("storage", summary, exc) from exc
        summary.stored += 1
