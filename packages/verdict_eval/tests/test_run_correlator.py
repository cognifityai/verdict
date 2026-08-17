from __future__ import annotations

from datetime import datetime, timezone

import pytest
from verdict.schema import (
    DimensionScore,
    Judgment,
    JudgmentStatus,
    Trace,
    UserSignalRecord,
    Verdict,
)
from verdict.storage.memory import InMemoryStorage
from verdict_eval.correlator import UserSignalCorrelator

from scripts.run_correlator import _build_pairs, _judgment_identity


def _judgment(trace_id: str, *, fingerprint: str, model: str, verdict: Verdict):
    return Judgment(
        trace_id=trace_id,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        rubric_name="quality",
        rubric_version="1",
        judge_models=[model],
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint=fingerprint,
        expected_dimensions=["quality"],
        dimensions=[DimensionScore(name="quality", verdict=verdict)],
    )


def test_correlator_refuses_mixed_evaluators_without_selection():
    storage = InMemoryStorage()
    trace = Trace(trace_id="t1", cluster_id="c1")
    storage.insert_trace(trace)
    first = _judgment(
        trace.trace_id, fingerprint="fingerprint-a", model="judge-a", verdict=Verdict.PASS
    )
    second = _judgment(
        trace.trace_id, fingerprint="fingerprint-b", model="judge-b", verdict=Verdict.FAIL
    )
    storage.insert_judgment(first)
    storage.insert_judgment(second)
    storage.insert_user_signal(UserSignalRecord(
        trace_id=trace.trace_id, kind="thumbs_up"
    ))

    with pytest.raises(ValueError, match="multiple evaluator identities"):
        _build_pairs(storage)

    selected_id, _ = _judgment_identity(first)
    pairs, stats = _build_pairs(storage, evaluator_id=selected_id)
    assert stats["selected_evaluator_id"] == selected_id
    assert len(pairs) == 1
    assert pairs[0].judge_verdict == "PASS"


def test_correlator_latest_judgment_tie_break_is_stable():
    storage = InMemoryStorage()
    trace = Trace(trace_id="t1", cluster_id="c1")
    storage.insert_trace(trace)
    earlier_id = _judgment(
        trace.trace_id,
        fingerprint="fingerprint-a",
        model="judge-a",
        verdict=Verdict.PASS,
    )
    earlier_id.judgment_id = "a"
    later_id = _judgment(
        trace.trace_id,
        fingerprint="fingerprint-a",
        model="judge-a",
        verdict=Verdict.FAIL,
    )
    later_id.judgment_id = "z"
    storage.insert_judgment(later_id)
    storage.insert_judgment(earlier_id)
    storage.insert_user_signal(UserSignalRecord(
        trace_id=trace.trace_id, kind="thumbs_down"
    ))

    pairs, _ = _build_pairs(storage)

    assert len(pairs) == 1
    assert pairs[0].judge_verdict == "FAIL"


def test_correlator_conflicting_tied_signals_are_excluded_and_content_is_redacted():
    storage = InMemoryStorage()
    trace = Trace(trace_id="t1", cluster_id="c1")
    storage.insert_trace(trace)
    # Simulate a row persisted before storage-boundary redaction existed.
    storage._traces[trace.trace_id].prompt_redacted = "contact legacy@example.com"
    storage.insert_judgment(_judgment(
        trace.trace_id,
        fingerprint="fingerprint-a",
        model="judge-a",
        verdict=Verdict.PASS,
    ))
    created_at = datetime(2026, 8, 2, tzinfo=timezone.utc)
    storage.insert_user_signal(UserSignalRecord(
        signal_id="a", trace_id=trace.trace_id, kind="thumbs_up", created_at=created_at
    ))
    storage.insert_user_signal(UserSignalRecord(
        signal_id="z", trace_id=trace.trace_id, kind="thumbs_down", created_at=created_at
    ))

    pairs, _ = _build_pairs(storage)
    report = UserSignalCorrelator(minimum_pairs=1).correlate(pairs)

    assert [pair.user_signal for pair in pairs] == ["thumbs_down", "thumbs_up"]
    assert "legacy@example.com" not in pairs[0].prompt_preview
    assert report.n_pairs == 0
    assert report.n_skipped_conflicting_trace == 1
    assert report.n_skipped_duplicate_trace == 1
    assert report.judge_pos_user_neg == 0


def test_correlator_does_not_reuse_completed_score_after_newer_error():
    storage = InMemoryStorage()
    trace = Trace(trace_id="t1", cluster_id="c1")
    storage.insert_trace(trace)
    completed = _judgment(
        trace.trace_id,
        fingerprint="fingerprint-a",
        model="judge-a",
        verdict=Verdict.PASS,
    )
    error = _judgment(
        trace.trace_id,
        fingerprint="fingerprint-a",
        model="judge-a",
        verdict=Verdict.PASS,
    )
    error.created_at = completed.created_at.replace(day=2)
    error.status = JudgmentStatus.ERROR
    error.dimensions = []
    error.error = "provider unavailable"
    storage.insert_judgment(completed)
    storage.insert_judgment(error)
    storage.insert_user_signal(UserSignalRecord(
        trace_id=trace.trace_id, kind="thumbs_up"
    ))

    pairs, stats = _build_pairs(storage)

    assert pairs == []
    assert stats["skipped_no_judgment"] == 1
