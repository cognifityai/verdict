"""Regression tests for redaction budget reporting and capture-path cost.

Both properties here previously failed silently:
  * an over-budget structure wiped every sibling attribute and left no evidence
  * the EMAIL pattern was quadratic on whitespace-free text
"""

from __future__ import annotations

import time

from verdict.redaction import redact, sanitize_span, sanitize_trace
from verdict.schema import SpanRecord, Trace


def test_over_budget_span_attributes_report_a_marker_instead_of_vanishing():
    record = SpanRecord(
        name="retrieve",
        attributes={
            "k": 5,
            "index": "prod",
            "latency_ms": 12.0,
            # Exceeds _MAX_STRUCTURE_NODES.
            "chunk_ids": list(range(10_050)),
        },
    )
    sanitize_span(record)

    assert record.attributes != {}, "over-budget attributes vanished without a trace"
    assert "verdict.redaction_status" in record.attributes


def test_over_budget_trace_tags_report_a_marker_instead_of_vanishing():
    trace = Trace(tags={"env": "prod"})
    trace.tags = {"env": "prod", "ids": list(range(10_050))}  # type: ignore[dict-item]
    sanitize_trace(trace)

    assert trace.tags != {}
    assert "verdict.redaction_status" in trace.tags


def test_within_budget_attributes_are_untouched():
    record = SpanRecord(
        name="retrieve",
        attributes={"k": 5, "index": "prod", "note": "ping ops@corp.com"},
    )
    sanitize_span(record)

    assert record.attributes["k"] == 5
    assert record.attributes["index"] == "prod"
    assert record.attributes["note"] == "ping <EMAIL>"
    assert "verdict.redaction_status" not in record.attributes


def test_redaction_of_whitespace_free_text_stays_within_the_overhead_budget():
    """CJK prose is one unbroken word-character run; the EMAIL pattern was
    quadratic over it (466 ms for ~8.8k chars against a <2 ms p99 budget)."""
    text = "患者は胸の圧迫感を訴えています" * 600  # ~8_800 chars, no whitespace
    start = time.perf_counter()
    redact(text)
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert elapsed_ms < 25.0, f"redact() took {elapsed_ms:.1f} ms on 8.8k CJK chars"


def test_malformed_email_candidates_stay_within_the_overhead_budget():
    for text in ("a" * 8_800 + "@", "患" * 8_800 + "@"):
        start = time.perf_counter()
        redact(text)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert elapsed_ms < 25.0, (
            f"redact() took {elapsed_ms:.1f} ms on malformed email candidate"
        )


def test_emails_are_still_redacted_in_whitespace_free_text():
    """The fast path must not skip a real address."""
    assert redact("連絡先ops@corp.comまで") == "<EMAIL>"  # CJK is \\w, so the local part over-matches (pre-existing)
    assert redact("a@b.com") == "<EMAIL>"
    assert "@" not in redact("mail me at first.last+tag@sub.example.co.uk please")
