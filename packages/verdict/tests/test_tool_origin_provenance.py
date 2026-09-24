from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from verdict.agent_judgment import (
    TurnToolCounts,
    classify_tool_origin,
    tool_counts_from_rows,
    turn_evidence_fingerprint,
)
from verdict.evidence import AgentTurn, EvidenceState, ExecutionStatus
from verdict.storage.turn_tool_evidence import tool_origin_code_sql


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"tool_origin": "mcp"}, "mcp"),
        ({"tool_origin": "application"}, "application"),
        ({"tool_origin": "provider_hosted"}, "provider_hosted"),
        ({}, "not_captured"),
        ({"tool_origin": None}, "unusable"),
        ({"tool_origin": ""}, "unusable"),
        ({"tool_origin": "unknown"}, "unusable"),
        ({"tool_origin": ["mcp"]}, "unusable"),
        ("malformed-json", "unusable"),
    ],
)
def test_tool_origin_classification_is_fixed_and_content_free(
    attributes: object, expected: str,
) -> None:
    assert classify_tool_origin(attributes) == expected


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('{"tool_origin":"mcp"}', "mcp"),
        ("{}", "not_captured"),
        ("[]", "unusable"),
        ("null", "unusable"),
        ('"scalar"', "unusable"),
        ("7", "unusable"),
        ('{"tool_origin":"mcp","tool_origin":"application"}', "unusable"),
    ],
)
def test_sqlite_origin_projection_rejects_nonobjects_and_duplicate_keys(
    stored: str,
    expected: str,
) -> None:
    expression = tool_origin_code_sql(postgres=False)
    with sqlite3.connect(":memory:") as connection:
        row = connection.execute(
            f"SELECT {expression} FROM (SELECT ? AS attributes_json)",
            (stored,),
        ).fetchone()

    assert row is not None
    assert row[0] == expected


def test_tool_counts_partition_every_call_and_warn_about_unobserved_boundaries() -> None:
    counts = tool_counts_from_rows([
        ("tool_call", "completed", None, "mcp"),
        ("tool_call", "completed", None, "application"),
        ("tool_call", "completed", None, "provider_hosted"),
        ("tool_call", "completed", None, "not_captured"),
        ("tool_call", "completed", None, "unusable"),
        ("tool_result", "completed", False, "not_captured"),
    ])

    assert counts.calls == 5
    assert (
        counts.mcp_calls
        + counts.application_calls
        + counts.provider_hosted_calls
        + counts.origin_not_captured_calls
        + counts.origin_unusable_calls
    ) == counts.calls
    assert (
        counts.mcp_calls,
        counts.application_calls,
        counts.provider_hosted_calls,
        counts.origin_not_captured_calls,
        counts.origin_unusable_calls,
    ) == (1, 1, 1, 1, 1)
    prompt = counts.prompt_block()
    assert "Directly recorded MCP dispatches: 1" in prompt
    assert "Origin not captured: 1" in prompt
    assert "Origin unusable: 1" in prompt
    assert "unobserved downstream service or protocol" in prompt
    assert "MCP was not used" not in prompt


def test_legacy_tool_count_construction_maps_calls_to_origin_not_captured() -> None:
    counts = TurnToolCounts(event_count=1, calls=1)

    assert counts.origin_not_captured_calls == 1


@pytest.mark.parametrize(
    "counts",
    [
        {"event_count": True},
        {"event_count": -1},
        {"event_count": 1, "calls": 1, "mcp_calls": 2},
        {"event_count": 0, "calls": 0, "origin_unusable_calls": 1},
    ],
)
def test_tool_counts_reject_invalid_values_or_origin_partitions(
    counts: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        TurnToolCounts(**counts)


def test_application_wrapper_never_infers_mcp_from_private_content() -> None:
    canaries = {
        "tool_name": "mcp_private_wrapper",
        "arguments": {"url": "https://private.example/mcp"},
        "result": "MCP call completed with PRIVATE_RESULT",
        "tool_origin": "application",
    }

    classification = classify_tool_origin(canaries)
    counts = tool_counts_from_rows([
        ("tool_call", "completed", None, classification),
    ])
    prompt = counts.prompt_block()

    assert counts.application_calls == 1
    assert counts.mcp_calls == 0
    for canary in ("mcp_private_wrapper", "private.example", "PRIVATE_RESULT"):
        assert canary not in prompt
    assert "MCP was not used" not in prompt


def test_all_origin_buckets_bind_turn_evidence_currentness() -> None:
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    turn = AgentTurn(
        turn_id="turn-1",
        run_id="run-1",
        sequence=0,
        started_at=now,
        ended_at=now,
        status=ExecutionStatus.COMPLETED,
        user_request_redacted="question",
        final_response_redacted="answer",
        request_state=EvidenceState.PRESENT,
        response_state=EvidenceState.PRESENT,
    )
    base = TurnToolCounts(event_count=1, calls=1, origin_not_captured_calls=1)
    base_fingerprint = turn_evidence_fingerprint(turn, base)

    for field in (
        "mcp_calls",
        "application_calls",
        "provider_hosted_calls",
        "origin_unusable_calls",
    ):
        changed = replace(
            base,
            origin_not_captured_calls=0,
            **{field: 1},
        )
        assert turn_evidence_fingerprint(turn, changed) != base_fingerprint
