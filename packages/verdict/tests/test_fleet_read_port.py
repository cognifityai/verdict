from __future__ import annotations

import inspect
import json
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from typing import get_type_hints

import pytest
from verdict.fleet_read_port import (
    VERDICT_FLEET_READ_SCHEMA_VERSION,
    TraceContributionReadV1,
    TraceWindowReadV1,
    VerdictFleetReadError,
    VerdictFleetReadPortV1,
    trace_window_read_to_json,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)


def _contribution(**changes: object) -> TraceContributionReadV1:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "trace_id": "trace-a",
        "started_at": NOW,
        "ended_at": NOW + timedelta(seconds=1),
        "request_status": "succeeded",
        "provider": "openai",
        "request_model": "gpt-request",
        "response_model": "gpt-response",
        "service_name": "checkout",
        "environment": "preview",
        "input_tokens": 10,
        "output_tokens": 20,
        "latency_us": 1_250,
        "cost_micro_usd": 500,
        "parent_span_id": "span-a",
        "agent_link_state": "exact",
        "agent_event_id": "event-a",
        "agent_turn_id": "turn-a",
        "agent_run_id": "run-a",
    }
    values.update(changes)
    return TraceContributionReadV1(**values)  # type: ignore[arg-type]


def _window(**changes: object) -> TraceWindowReadV1:
    items = (_contribution(),)
    values: dict[str, object] = {
        "schema_version": VERDICT_FLEET_READ_SCHEMA_VERSION,
        "tenant_id": "tenant-a",
        "window_start": NOW,
        "window_end": NOW + timedelta(minutes=15),
        "read_started_at": NOW + timedelta(minutes=15),
        "read_completed_at": NOW + timedelta(minutes=15, milliseconds=1),
        "item_count": len(items),
        "items": items,
    }
    values.update(changes)
    return TraceWindowReadV1(**values)  # type: ignore[arg-type]


def test_public_fleet_v1_contract_has_exact_fields_and_signatures() -> None:
    assert [field.name for field in fields(TraceContributionReadV1)] == [
        "tenant_id",
        "trace_id",
        "started_at",
        "ended_at",
        "request_status",
        "provider",
        "request_model",
        "response_model",
        "service_name",
        "environment",
        "input_tokens",
        "output_tokens",
        "latency_us",
        "cost_micro_usd",
        "parent_span_id",
        "agent_link_state",
        "agent_event_id",
        "agent_turn_id",
        "agent_run_id",
    ]
    assert [field.name for field in fields(TraceWindowReadV1)] == [
        "schema_version",
        "tenant_id",
        "window_start",
        "window_end",
        "read_started_at",
        "read_completed_at",
        "item_count",
        "items",
    ]
    assert get_type_hints(TraceContributionReadV1)["ended_at"] == datetime | None
    assert get_type_hints(TraceWindowReadV1)["items"] == tuple[TraceContributionReadV1, ...]
    signature = inspect.signature(VerdictFleetReadPortV1.read_trace_window)
    assert list(signature.parameters) == ["self", "tenant_id", "window_start", "window_end"]
    assert signature.parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(trace_window_read_to_json).parameters) == ["value"]
    assert VERDICT_FLEET_READ_SCHEMA_VERSION == "verdict.trace-window-read.v1"


def test_fleet_read_error_has_stable_public_shape() -> None:
    for code in (
        "invalid_query",
        "unsupported_backend",
        "unsupported_version",
        "read_unavailable",
        "invalid_read_model",
        "window_too_dense",
        "response_limit_exceeded",
    ):
        error = VerdictFleetReadError(code)
        assert error.code == code
        assert str(error) == code
        assert error.args == (code,)

    with pytest.raises(ValueError, match="unsupported Verdict fleet read error code"):
        VerdictFleetReadError("database-password-canary")
    with pytest.raises(ValueError, match="unsupported Verdict fleet read error code"):
        VerdictFleetReadError(False)  # type: ignore[arg-type]


def test_contribution_normalizes_utc_and_empty_source_strings() -> None:
    offset = timezone(timedelta(hours=-7))
    value = _contribution(
        started_at=NOW.astimezone(offset),
        ended_at=(NOW + timedelta(seconds=1)).astimezone(offset),
        provider="",
        request_model="",
        response_model="",
        service_name="",
        environment="",
    )

    assert value.started_at == NOW
    assert value.ended_at == NOW + timedelta(seconds=1)
    assert value.started_at.tzinfo is timezone.utc
    assert value.provider is None
    assert value.request_model is None
    assert value.response_model is None
    assert value.service_name is None
    assert value.environment is None


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "local"},
        {"tenant_id": "tenant with spaces"},
        {"tenant_id": "t\N{SNOWMAN}"},
        {"trace_id": ""},
        {"trace_id": "x" * 257},
        {"provider": "x" * 129},
        {"request_model": "x" * 257},
        {"parent_span_id": ""},
        {"input_tokens": True},
        {"input_tokens": -1},
        {"output_tokens": 2**63},
        {"latency_us": 86_400_000_001},
        {"cost_micro_usd": 10**15 + 1},
        {"request_status": "unknown"},
        {"request_status": "in_progress", "ended_at": NOW},
        {"request_status": "succeeded", "ended_at": None},
        {"ended_at": NOW - timedelta(microseconds=1)},
        {"ended_at": NOW + timedelta(hours=24, microseconds=1)},
        {"agent_link_state": "conflicting"},
        {"agent_link_state": "not_found"},
        {"agent_link_state": "exact", "agent_event_id": None},
    ],
)
def test_contribution_rejects_every_invalid_contract_shape(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _contribution(**changes)


def test_not_found_contribution_clears_all_agent_ids() -> None:
    value = _contribution(
        agent_link_state="not_found",
        agent_event_id=None,
        agent_turn_id=None,
        agent_run_id=None,
    )

    assert value.agent_link_state == "not_found"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "verdict.trace-window-read.v2"},
        {"tenant_id": "tenant-b"},
        {"window_end": NOW},
        {"window_end": NOW + timedelta(seconds=901)},
        {"read_completed_at": NOW - timedelta(microseconds=1)},
        {"item_count": True},
        {"item_count": 0},
        {"items": [_contribution()]},
        {"items": (_contribution(tenant_id="tenant-b"),)},
        {"items": (_contribution(started_at=NOW - timedelta(microseconds=1)),)},
        {"items": (_contribution(trace_id="trace-b"), _contribution(trace_id="trace-a"))},
        {"items": (_contribution(), _contribution())},
    ],
)
def test_window_rejects_invalid_bounds_counts_tenants_and_order(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _window(**changes)


def test_serializer_emits_exact_order_utc_and_no_omitted_fields() -> None:
    value = _window()

    encoded = trace_window_read_to_json(value)
    payload = json.loads(encoded)

    assert list(payload) == [
        "schema_version",
        "tenant_id",
        "window_start",
        "window_end",
        "read_started_at",
        "read_completed_at",
        "item_count",
        "items",
    ]
    assert list(payload["items"][0]) == [field.name for field in fields(TraceContributionReadV1)]
    assert payload["window_start"] == "2026-09-14T12:00:00Z"
    assert payload["items"][0]["ended_at"] == "2026-09-14T12:00:01Z"
    assert " " not in encoded
    assert "prompt" not in encoded
    assert "response_content" not in encoded
    assert "error" not in encoded


def test_serializer_revalidates_exact_nested_values_and_detaches_failure() -> None:
    item = _contribution()
    value = _window(items=(item,))
    object.__setattr__(item, "trace_id", "private-storage-canary\x00")

    with pytest.raises(VerdictFleetReadError) as raised:
        trace_window_read_to_json(value)

    assert raised.value.code == "invalid_read_model"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private-storage-canary" not in repr(raised.value)


def test_serializer_rejects_subclasses_and_overridden_serialization_hooks() -> None:
    class WindowProxy(TraceWindowReadV1):
        pass

    value = _window()
    proxy = WindowProxy(**{field.name: getattr(value, field.name) for field in fields(value)})

    with pytest.raises(VerdictFleetReadError, match="invalid_read_model"):
        trace_window_read_to_json(proxy)


def test_serializer_rejects_response_larger_than_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import verdict.fleet_read_port as fleet_read_port

    monkeypatch.setattr(fleet_read_port, "_MAX_RESPONSE_BYTES", 1)

    with pytest.raises(VerdictFleetReadError, match="response_limit_exceeded"):
        trace_window_read_to_json(_window())


def test_replacing_valid_window_with_mutated_exact_item_is_rejected() -> None:
    item = _contribution()
    object.__setattr__(item, "agent_link_state", "privacy-canary")

    with pytest.raises(ValueError):
        replace(_window(), items=(item,))
