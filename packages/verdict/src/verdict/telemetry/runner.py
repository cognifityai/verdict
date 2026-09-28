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
            f"stored={summary.stored}, skipped={summary.skipped} ({self.cause_type})"
        )


def import_into_storage(results: Iterable[MappingResult], storage: Storage) -> ImportSummary:
    """Synchronously persist every mapped trace and account for every result."""
    summary = ImportSummary()
    iterator = iter(results)
    while True:
        try:
            result = next(iterator)
        except StopIteration:
            return summary
        except Exception as exc:
            raise ImportRunError("source", summary, exc) from exc
        summary.seen += 1
        if result.session is not None:
            try:
                storage.save_session(result.session)
            except Exception as exc:
                raise ImportRunError("session_storage", summary, exc) from exc
            summary.sessions_stored += 1
        if result.trace is None:
            if result.skip_reason is not None:
                summary.add_skip(result.skip_reason)
            continue
        try:
            if result.trace.tags.get("verdict.source") == "voice":
                if not storage.insert_voice_trace_if_coherent(result.trace):
                    summary.add_skip("voice_reply_revision_retained")
                    continue
            else:
                storage.insert_trace(result.trace)
        except Exception as exc:
            raise ImportRunError("storage", summary, exc) from exc
        summary.stored += 1
