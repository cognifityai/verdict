"""Real Agent context to SQLite regressions for bounded tool results."""

from __future__ import annotations

import asyncio
import itertools
import logging

import pytest
import verdict
from verdict.evidence import AgentEventType, ExecutionStatus, PrivacyClassification
from verdict.storage.sqlite import SQLiteStorage


@pytest.fixture(autouse=True)
def reset_verdict():
    verdict.shutdown()
    yield
    verdict.shutdown()


@pytest.mark.parametrize(
    "output",
    [
        {"pages": [{"body": {"sections": [{"text": "safe"}]}}]},
        {f"page_{n}": list(range(24)) for n in range(24)},
        {"text": "x" * 30_000},
        {"invalid": object()},
        {3: "invalid key"},
    ],
)
@pytest.mark.asyncio
async def test_async_tool_has_terminal_sqlite_event_for_bounded_output(tmp_path, output):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    async with verdict.agent_run(name="agent") as run:
        async with run.turn(user_input="search") as turn:
            async with turn.tool("web_fetch", arguments={"url": "https://example.invalid"}) as tool:
                tool.set_output(output)
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    calls = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_CALL]
    results = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert len(calls) == len(results) == 1
    assert results[0].attributes["call_id"] == calls[0].attributes["call_id"]
    assert results[0].privacy_classification in {
        PrivacyClassification.REDACTED, PrivacyClassification.OMITTED
    }
    if results[0].privacy_classification is PrivacyClassification.OMITTED:
        assert results[0].omission_reason
    storage.close()


@pytest.mark.asyncio
async def test_tool_cycles_secrets_and_oversize_arguments_reach_sqlite(tmp_path, caplog):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    cyclic: dict[str, object] = {"email": "reader@example.com"}
    cyclic["self"] = cyclic
    large_arguments = {f"arg_{n}": list(range(24)) for n in range(24)}
    with caplog.at_level(logging.WARNING, logger="verdict.agent"):
        async with verdict.agent_run(name="agent") as run:
            async with run.turn(user_input="search") as turn:
                async with turn.tool("web_fetch", arguments=large_arguments) as tool:
                    tool.set_output({"token": "private-value", "cycle": cyclic})
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    [call] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_CALL]
    [result] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert call.attributes["call_id"] == result.attributes["call_id"]
    assert "private-value" not in repr(bundle) + caplog.text
    assert "reader@example.com" not in repr(bundle) + caplog.text
    assert "<REDACTED>" in repr(result)
    if call.privacy_classification is PrivacyClassification.OMITTED:
        assert call.omission_reason == "tool_call_arguments_exceeds_event_limit"
        assert "tool_call_arguments_exceeds_event_limit" in caplog.text
    storage.close()


@pytest.mark.asyncio
async def test_nested_tool_result_redacts_secret_and_email_in_sqlite(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    async with verdict.agent_run(name="agent") as run:
        async with run.turn(user_input="search") as turn:
            async with turn.tool("web_fetch") as tool:
                tool.set_output({"pages": [{"contact": "reader@example.com", "api_key": "private-value"}]})
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    [result] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert "reader@example.com" not in repr(bundle)
    assert "private-value" not in repr(bundle)
    assert "<EMAIL>" in repr(result)
    assert "<SECRET>" in repr(result)
    storage.close()


@pytest.mark.asyncio
async def test_aggregate_node_overflow_records_omission_reason_without_content(tmp_path, caplog):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    output = {f"page_{n}": list(range(24)) for n in range(24)}
    output["api_key"] = "never-log-this-value"
    with caplog.at_level(logging.WARNING, logger="verdict.agent"):
        async with verdict.agent_run(name="agent") as run:
            async with run.turn(user_input="search") as turn:
                async with turn.tool("web_fetch") as tool:
                    tool.set_output(output)
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    [result] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert result.privacy_classification is PrivacyClassification.OMITTED
    assert result.omission_reason == "tool_result_exceeds_event_limit"
    assert "result" not in result.attributes
    assert "tool_result_exceeds_event_limit" in caplog.text
    assert "never-log-this-value" not in repr(bundle) + caplog.text
    storage.close()


@pytest.mark.asyncio
async def test_redaction_budget_overflow_keeps_tool_result_identity(tmp_path, caplog):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with caplog.at_level(logging.WARNING, logger="verdict.agent"):
        async with verdict.agent_run(name="agent") as run:
            async with run.turn(user_input="search") as turn:
                async with turn.tool("web_fetch") as tool:
                    tool.set_output({"body": "x" * 1_000_000})
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    [call] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_CALL]
    [result] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert result.attributes["call_id"] == call.attributes["call_id"]
    assert result.privacy_classification is PrivacyClassification.OMITTED
    assert result.omission_reason == "tool_result_exceeds_redaction_limit"
    assert "result" not in result.attributes
    assert "tool_result_exceeds_redaction_limit" in caplog.text
    storage.close()


@pytest.mark.parametrize("call_id", ["bad\x00id", "\ud800", 7])
def test_invalid_call_id_is_rejected_before_tool_starts(tmp_path, call_id):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="search") as turn:
            with pytest.raises(ValueError, match="call_id"):
                turn.tool("web_fetch", call_id=call_id)
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert bundle.events == ()
    storage.close()


@pytest.mark.parametrize("call_id", ["", "c" * 256, "c" * 257, "c" * 1024, "c" * 1_000_000])
def test_published_call_id_boundary_still_correlates_events(tmp_path, call_id):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="search") as turn:
            with turn.tool("web_fetch", call_id=call_id) as tool:
                tool.set_output("ok")
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert len(bundle.events) == 2
    assert bundle.events[0].attributes["call_id"] == bundle.events[1].attributes["call_id"]
    assert bundle.events[0].attributes["call_id"]
    storage.close()


def test_semantic_secret_name_redacts_value_before_metadata_bounding(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="search") as turn:
            turn.record_context(name="x" * 1024 + "_api_key", value="private-value")
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert "private-value" not in repr(bundle)
    storage.close()


def test_json_string_tool_arguments_redact_nested_secret_in_sqlite(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="search") as turn:
            with turn.tool("web_fetch", arguments='{"nested":{"api_key":"private-value"}}') as tool:
                tool.set_output("ok")
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert "private-value" not in repr(bundle)
    [call] = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_CALL]
    assert "<SECRET>" in repr(call.attributes)
    storage.close()


def test_failing_logging_handler_cannot_drop_omitted_result(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    class FailingHandler(logging.Handler):
        def emit(self, record):
            raise RuntimeError("logging unavailable")

    logger = logging.getLogger("verdict.agent")
    handler = FailingHandler()
    logger.addHandler(handler)
    try:
        with verdict.agent_run(name="agent") as run:
            with run.turn(user_input="search") as turn:
                with turn.tool("web_fetch") as tool:
                    tool.set_output({"body": "x" * 1_000_000})
    finally:
        logger.removeHandler(handler)
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert [e.event_type for e in bundle.events] == [
        AgentEventType.TOOL_CALL, AgentEventType.TOOL_RESULT
    ]
    assert bundle.events[-1].omission_reason == "tool_result_exceeds_redaction_limit"
    storage.close()


@pytest.mark.parametrize("order", tuple(itertools.permutations(("deep", "wide", "large"))))
def test_tool_order_variants_keep_one_terminal_result_per_call(tmp_path, order):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    outputs = {
        "deep": {"a": [{"b": {"c": ["safe"]}}]},
        "wide": {f"page_{n}": list(range(24)) for n in range(24)},
        "large": {"body": "x" * 1_000_000},
    }
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="search") as turn:
            for name in order:
                with turn.tool(name, call_id=f"call_{name}") as tool:
                    tool.set_output(outputs[name])
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    calls = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_CALL]
    results = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert [e.attributes["call_id"] for e in calls] == [f"call_{name}" for name in order]
    assert [e.attributes["call_id"] for e in results] == [f"call_{name}" for name in order]
    assert len(calls) == len(results) == 3
    storage.close()


@pytest.mark.asyncio
async def test_tool_result_survives_shutdown_and_reopen(tmp_path):
    path = str(tmp_path / "events.db")
    storage = SQLiteStorage(path)
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    async with verdict.agent_run(name="agent") as run:
        async with run.turn(user_input="search") as turn:
            async with turn.tool("web_fetch") as tool:
                tool.set_output({"body": {"nested": ["safe"]}})
    verdict.shutdown()
    storage = SQLiteStorage(path)
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert [e.event_type for e in bundle.events] == [
        AgentEventType.TOOL_CALL, AgentEventType.TOOL_RESULT
    ]
    storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    [(RuntimeError("api_key=hidden"), ExecutionStatus.FAILED),
     (asyncio.CancelledError(), ExecutionStatus.CANCELLED)],
)
async def test_tool_failure_or_cancellation_still_has_result(tmp_path, failure, status):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with pytest.raises(type(failure)):
        async with verdict.agent_run(name="agent") as run:
            async with run.turn(user_input="search") as turn:
                async with turn.tool("web_fetch") as tool:
                    tool.set_output({"private": "api_key=hidden"})
                    raise failure
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    results = [e for e in bundle.events if e.event_type is AgentEventType.TOOL_RESULT]
    assert len(results) == 1
    assert results[0].status is status
    assert "api_key=hidden" not in repr(bundle)
    storage.close()


@pytest.mark.asyncio
async def test_failed_sqlite_write_after_tool_completion_is_observable(tmp_path, monkeypatch, caplog):
    storage = SQLiteStorage(str(tmp_path / "events.db"))
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    original = storage.append_agent_capture

    def fail_result(batch, traces=()):
        if any(e.event_type is AgentEventType.TOOL_RESULT for e in batch.events):
            raise OSError("api_key=never-log-this")
        return original(batch, traces)

    monkeypatch.setattr(storage, "append_agent_capture", fail_result)
    with caplog.at_level(logging.WARNING, logger="verdict.agent"):
        async with verdict.agent_run(name="agent") as run:
            async with run.turn(user_input="search") as turn:
                async with turn.tool("web_fetch") as tool:
                    tool.set_output({"secret": "api_key=never-log-this"})
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert sum(e.event_type is AgentEventType.TOOL_CALL for e in bundle.events) == 1
    assert sum(e.event_type is AgentEventType.TOOL_RESULT for e in bundle.events) == 0
    assert client.runtime_metrics.snapshot(storage)["capture"]["dropped_records"] >= 1
    assert "api_key=never-log-this" not in caplog.text
    assert "agent evidence" in caplog.text
    storage.close()
