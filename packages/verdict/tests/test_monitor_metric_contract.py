"""Deterministic trace metrics: definitions, versioning and re-bootstrap."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from verdict.monitor_inputs import advance_monitor
from verdict.monitoring import (
    METRIC_CONTRACT_VERSION,
    MonitorPolicy,
    MonitorRebootstrapRequired,
    compare_manifest,
    monitor_requires_rebootstrap,
    monitor_snapshot_from_json,
    monitor_snapshot_to_json,
    plan_historical_manifest,
    trace_monitor_units,
)
from verdict.schema import Trace
from verdict.storage import InMemoryStorage
from verdict.trace_facts import RESPONSE_TOOL_CALLS_TAG, response_is_blank

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
CALLS = RESPONSE_TOOL_CALLS_TAG


@pytest.mark.parametrize(
    ("response", "tags", "finish_reason", "expected"),
    [
        ("Here is the answer.", {}, None, False),
        ("", {CALLS: "2"}, "completed", False),
        ("", {}, "tool_calls", False),
        ("", {}, "tool_use", False),
        ("", {}, "tool_call", False),
        # A recorded zero count settles the question.
        ("", {CALLS: "0"}, "completed", True),
        ("  \n", {CALLS: "0"}, None, True),
        # A provider stop reason does too.
        ("", {}, "stop", True),
        ("", {}, "end_turn", True),
        ("", {}, "length", True),
        # Without either, a tool call cannot be ruled out.
        ("", {}, "completed", None),
        ("", {}, None, None),
        ("", {CALLS: "not-a-count"}, "completed", None),
        (None, {CALLS: "0"}, "stop", None),
    ],
)
def test_blank_reply_classification(response, tags, finish_reason, expected) -> None:
    assert response_is_blank(
        response=response, tags=tags, finish_reason=finish_reason,
    ) is expected


def _trace(index: int, **fields) -> Trace:
    started = NOW + timedelta(minutes=index)
    return Trace(trace_id=f"t-{index:03d}", started_at=started, ended_at=started,
                 provider="openai", request_model="gpt-5-mini", **fields)


def test_failed_calls_measure_no_response_metric() -> None:
    [unit] = trace_monitor_units([
        _trace(0, error="rate_limit (HTTP 429)", response_redacted="I can't help: limit hit",
               tags={CALLS: "0"}),
    ])
    assert unit.metrics == {"provider_error": True}


def _policy() -> MonitorPolicy:
    return MonitorPolicy("contract", "tenant:application:trace", reference_ratio=0.5,
                         minimum_reference=30, minimum_current=30)


def test_capture_upgrade_mid_store_is_not_reported_as_drift() -> None:
    """Adversarial: a tool-calling app captured before and after tool-call counting.

    Every reply is a tool call. Older Responses traces have no count and a
    status-only finish reason; newer ones record their calls. Treating the
    older empty replies as blank would report a fall in blank replies that
    is only a capture change.
    """
    traces = [
        _trace(index, response_redacted="", finish_reason="completed")
        for index in range(60)
    ] + [
        _trace(index, response_redacted="", finish_reason="completed", tags={CALLS: "1"})
        for index in range(60, 120)
    ]
    units = trace_monitor_units(traces)
    policy = _policy()
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=1))
    comparison = compare_manifest(units, manifest, policy)

    assert not any(metric.alert for metric in comparison.metrics)
    blank = [metric for metric in comparison.metrics if metric.metric == "response_empty"]
    assert all(metric.reference_n == 0 for metric in blank)


def test_real_blank_replies_still_alert() -> None:
    traces = [
        _trace(index, response_redacted="Done.", tags={CALLS: "0"}, finish_reason="stop")
        for index in range(60)
    ] + [
        _trace(index, response_redacted="", tags={CALLS: "0"}, finish_reason="stop")
        for index in range(60, 120)
    ]
    units = trace_monitor_units(traces)
    policy = _policy()
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=1))
    comparison = compare_manifest(units, manifest, policy)

    [blank] = [metric for metric in comparison.metrics if metric.metric == "response_empty"]
    assert (blank.reference_value, blank.current_value, blank.alert) == (0.0, 1.0, True)


def _snapshot():
    traces = [_trace(index, response_redacted="ok", tags={CALLS: "0"}) for index in range(80)]
    units = trace_monitor_units(traces)
    policy = _policy()
    manifest = plan_historical_manifest(units, policy, cutoff=NOW + timedelta(days=1))
    return policy, manifest, compare_manifest(units, manifest, policy)


def test_new_manifests_carry_the_current_contract_through_json() -> None:
    policy, manifest, comparison = _snapshot()
    assert manifest.metric_contract_version == METRIC_CONTRACT_VERSION
    restored, _ = monitor_snapshot_from_json(monitor_snapshot_to_json(manifest, comparison))
    assert restored.metric_contract_version == METRIC_CONTRACT_VERSION
    assert monitor_requires_rebootstrap(policy, restored) is False


def test_manifests_stored_before_versioning_require_rebootstrap() -> None:
    policy, manifest, comparison = _snapshot()
    payload = json.loads(monitor_snapshot_to_json(manifest, comparison))
    del payload["manifest"]["metric_contract_version"]
    legacy, _ = monitor_snapshot_from_json(json.dumps(payload))

    assert legacy.metric_contract_version == 0
    assert monitor_requires_rebootstrap(policy, legacy) is True


def test_worker_refuses_to_advance_a_legacy_reference() -> None:
    policy, manifest, comparison = _snapshot()
    legacy = replace(manifest, metric_contract_version=0)
    storage = InMemoryStorage()
    storage.save_monitor_candidate(policy, legacy, comparison)
    storage.activate_monitor_policy(
        policy.scope_key, policy.policy_id, expected_active_policy_id=None,
    )

    with pytest.raises(MonitorRebootstrapRequired):
        advance_monitor(storage, policy, tenant_id="tenant")


def test_unknown_contract_version_is_rejected() -> None:
    _policy_value, manifest, _comparison = _snapshot()
    with pytest.raises(ValueError, match="metric contract version"):
        replace(manifest, metric_contract_version=METRIC_CONTRACT_VERSION + 1)
