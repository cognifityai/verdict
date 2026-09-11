from __future__ import annotations

import asyncio
import inspect
import logging
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import verdict
from verdict.client import VerdictClient
from verdict.evidence import (
    AgentEventType,
    EvidenceState,
    ExecutionStatus,
    PrivacyClassification,
)
from verdict.instrumentors.base import apply_routing_context, safe_persist_trace
from verdict.read_port import StorageVerdictReadPort
from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage


@pytest.fixture(autouse=True)
def _reset_verdict() -> None:
    verdict.shutdown()
    yield
    verdict.shutdown()


def _completed_trace(provider: str = "openai") -> Trace:
    now = datetime.now(timezone.utc)
    return Trace(
        provider=provider,
        request_model="test-model",
        response_model="test-model",
        started_at=now,
        ended_at=now,
        prompt_redacted="question",
        response_redacted="answer",
        input_tokens=4,
        output_tokens=2,
        latency_ms=12.5,
        finish_reason="stop",
    )


def test_full_agent_context_persists_typed_timeline_and_one_trace_owner() -> None:
    storage = InMemoryStorage()
    client = verdict.init(
        storage=storage,
        tenant_id="tenant-a",
        service_name="support-service",
        environment="test",
        instrumentors=[],
    )

    with verdict.agent_run(
        name="support-agent",
        version="v2",
        session_id="conversation-7",
        external_id="customer-run-7",
    ) as run:
        with run.turn(user_input="email alice@example.com") as turn:
            with turn.tool("search", arguments={"query": "alice@example.com"}) as tool:
                tool.set_output({"document_count": 2})
            with verdict.model_call_context() as correlation_id:
                trace = _completed_trace()
                apply_routing_context(client, trace)
                safe_persist_trace(client, trace)
            turn.record_command(command="pytest -q", exit_code=0, cwd="/private/repo")
            turn.record_test(command="pytest -q", exit_code=0, passed=3, failed=0)
            turn.record_artifact(path="/private/repo/result.json", action="created")
            turn.record_instruction(name="policy", text="do not email alice@example.com")
            turn.record_context(name="account", value={"owner": "alice@example.com"})
            turn.record_retry(reason="rate limit", attempt=1)
            turn.record_handoff(agent_name="reviewer", child_run_id="child-1")
            turn.record_feedback(kind="thumbs_up")
            turn.set_output("done for alice@example.com")
        run.record_business_outcome("ticket_resolved", True)

    bundles = storage.list_agent_run_bundles("tenant-a")
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.run.status is ExecutionStatus.COMPLETED
    assert bundle.run.session_id == "conversation-7"
    assert bundle.run.agent_name == "support-agent"
    assert bundle.run.service_name == "support-service"
    assert len(bundle.turns) == 1
    assert bundle.turns[0].status is ExecutionStatus.COMPLETED
    assert bundle.turns[0].request_state is EvidenceState.PRESENT
    assert bundle.turns[0].user_request_redacted == "email <EMAIL>"
    assert bundle.turns[0].final_response_redacted == "done for <EMAIL>"
    assert {event.event_type for event in bundle.events} == {
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.MODEL_CALL,
        AgentEventType.COMMAND,
        AgentEventType.TEST_RESULT,
        AgentEventType.ARTIFACT,
        AgentEventType.INSTRUCTION,
        AgentEventType.CONTEXT,
        AgentEventType.RETRY,
        AgentEventType.SUBAGENT,
        AgentEventType.FEEDBACK,
        AgentEventType.OUTCOME,
    }
    model_event = next(
        event for event in bundle.events if event.event_type is AgentEventType.MODEL_CALL
    )
    context_event = next(
        event for event in bundle.events if event.event_type is AgentEventType.CONTEXT
    )
    assert model_event.trace_id == trace.trace_id
    assert trace.trace_id == correlation_id
    assert context_event.privacy_classification.value == "redacted"
    assert "question" not in repr(model_event.attributes)
    assert "answer" not in repr(model_event.attributes)
    assert storage.get_trace(trace.trace_id).response_redacted == "answer"  # type: ignore[union-attr]
    read = StorageVerdictReadPort(storage).get_agent_run(
        tenant_id="tenant-a", run_id=bundle.run.run_id
    )
    assert read is not None
    assert read.model_calls[0].trace_id == correlation_id


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("broken"), ExecutionStatus.FAILED),
        (TimeoutError("late"), ExecutionStatus.TIMED_OUT),
        (asyncio.CancelledError(), ExecutionStatus.CANCELLED),
    ],
)
def test_turn_and_run_terminal_status_preserve_application_exception(
    error: BaseException,
    expected: ExecutionStatus,
) -> None:
    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    with pytest.raises(type(error)):
        with verdict.agent_run(name="agent") as run:
            with run.turn(user_input="hello"):
                raise error

    bundle = storage.list_agent_run_bundles("tenant-a")[0]
    assert bundle.run.status is expected
    assert bundle.turns[0].status is expected


@pytest.mark.asyncio
async def test_async_tasks_do_not_leak_agent_or_turn_identity() -> None:
    storage = InMemoryStorage()
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    async def worker(label: str) -> str:
        async with verdict.agent_run(name=label, external_id=label) as run:
            async with run.turn(user_input=label):
                await asyncio.sleep(0)
                trace = _completed_trace(label)
                apply_routing_context(client, trace)
                safe_persist_trace(client, trace)
                return trace.trace_id

    trace_ids = await asyncio.gather(worker("alpha"), worker("beta"))
    bundles = storage.list_agent_run_bundles("tenant-a")
    assert len(bundles) == 2
    linked = {
        bundle.run.agent_name: next(
            event.trace_id
            for event in bundle.events
            if event.event_type is AgentEventType.MODEL_CALL
        )
        for bundle in bundles
    }
    assert linked == {"alpha": trace_ids[0], "beta": trace_ids[1]}


def test_run_sampling_is_all_or_nothing_and_standalone_traces_are_unchanged() -> None:
    storage = InMemoryStorage()
    client = verdict.init(
        storage=storage,
        tenant_id="tenant-a",
        sample_rate=0.0,
        instrumentors=[],
    )
    with verdict.agent_run(name="omitted") as run:
        with run.turn(user_input="hello"):
            trace = _completed_trace()
            apply_routing_context(client, trace)
            safe_persist_trace(client, trace)

    assert storage.list_agent_run_bundles("tenant-a") == []
    assert storage.get_trace(trace.trace_id) is None

    standalone = _completed_trace()
    apply_routing_context(client, standalone)
    safe_persist_trace(client, standalone)
    assert storage.get_trace(standalone.trace_id) is not None


def test_sampled_run_controls_the_real_provider_wrapper_sampling_decision() -> None:
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    storage = InMemoryStorage()
    client = verdict.init(
        storage=storage,
        tenant_id="tenant-a",
        sample_rate=1.0,
        instrumentors=[],
    )
    instrumentor = AnthropicInstrumentor(client)
    instrumentor._should_sample = lambda: False  # type: ignore[method-assign]

    def provider_call(*args, **kwargs):
        return SimpleNamespace(
            model="claude-test",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="provider answer")],
        )

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="provider question") as turn:
            response = instrumentor._wrap_create_sync(
                provider_call,
                None,
                (),
                {"model": "claude-test", "max_tokens": 20, "messages": []},
            )
            turn.set_output(response.content[0].text)

    [bundle] = storage.list_agent_run_bundles("tenant-a")
    [model_event] = [
        event for event in bundle.events if event.event_type is AgentEventType.MODEL_CALL
    ]
    assert storage.get_trace(model_event.trace_id) is not None  # type: ignore[arg-type]


def test_agent_stream_failure_preserves_later_provider_traces_standalone() -> None:
    class FailingSecondAppend(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.append_calls = 0

        def append_agent_capture(self, batch, traces=()) -> None:
            self.append_calls += 1
            if self.append_calls == 2:
                raise RuntimeError("transient append failure")
            super().append_agent_capture(batch, traces)

    storage = FailingSecondAppend()
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="hello") as turn:
            for _ in range(2):
                trace = _completed_trace()
                apply_routing_context(client, trace)
                safe_persist_trace(client, trace)
            turn.set_output("done")

    assert storage.append_calls == 2
    assert len(storage.list_traces(limit=10)) == 2
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert bundle.events == ()


def test_invalid_agent_api_arguments_fail_before_writing() -> None:
    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    with pytest.raises(ValueError, match="agent name"):
        with verdict.agent_run(name=""):
            pass
    assert storage.list_agent_run_bundles("tenant-a") == []


def test_invalid_typed_helper_arguments_fail_at_the_call_site() -> None:
    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="hello") as turn:
            with pytest.raises(ValueError, match="feedback kind"):
                turn.record_feedback(kind="thumbsup")
            with pytest.raises(ValueError, match="output must be text"):
                turn.set_output(42)
            with pytest.raises(ValueError, match="instruction name"):
                turn.record_instruction(name="", text="policy")
            with pytest.raises(ValueError, match="available"):
                turn.record_instruction(name="policy", text="text", available="yes")  # type: ignore[arg-type]
            turn.record_feedback(kind="thumbs_up")
            turn.record_outcome("score", math.nan)
            turn.set_output("done")

    bundle = storage.list_agent_run_bundles("tenant-a")[0]
    outcome = next(event for event in bundle.events if event.event_type is AgentEventType.OUTCOME)
    assert outcome.attributes["value"] == "<REDACTED>"
    feedback = next(event for event in bundle.events if event.event_type is AgentEventType.FEEDBACK)
    assert feedback.sequence == 0


def test_unrepresentable_nested_evidence_is_omitted_without_breaking_sequence() -> None:
    class BadMapping(dict):
        def items(self):
            raise RuntimeError("mapping unavailable")

    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="hello") as turn:
            turn.record_context(name="record", value=BadMapping(secret="value"))
            turn.record_feedback(kind="thumbs_up")
            turn.set_output("done")

    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert [event.sequence for event in bundle.events] == [0, 1]
    assert bundle.events[0].attributes["value"] == "<OMITTED:invalid>"


def test_turn_without_output_distinguishes_missing_from_disabled_capture() -> None:
    for capture_content, expected in (
        (True, EvidenceState.MISSING),
        (False, EvidenceState.NOT_CAPTURED),
    ):
        storage = InMemoryStorage()
        verdict.init(
            storage=storage,
            tenant_id="tenant-a",
            capture_content=capture_content,
            instrumentors=[],
        )
        with verdict.agent_run(name="agent") as run:
            with run.turn(user_input="hello"):
                pass
        [bundle] = storage.list_agent_run_bundles("tenant-a")
        assert bundle.turns[0].response_state is expected
        verdict.shutdown()


def test_metadata_only_agent_capture_omits_every_content_field() -> None:
    storage = InMemoryStorage()
    verdict.init(
        storage=storage,
        tenant_id="tenant-a",
        capture_content=False,
        instrumentors=[],
    )

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="private request") as turn:
            turn.record_instruction(name="policy", text="private instruction")
            turn.record_context(name="record", value={"private": "context"})
            with turn.tool("lookup", arguments={"private": "argument"}) as tool:
                tool.set_output({"private": "result"})
            turn.record_command(
                command="private command",
                exit_code=1,
                stdout="private stdout",
                stderr="private stderr",
            )
            turn.record_test(
                command="private test",
                exit_code=1,
                failed=1,
                output="private output",
            )
            turn.record_retry(reason="private reason", attempt=1)
            turn.record_feedback(kind="thumbs_down", value=True)
            turn.set_output("private response")
        run.record_business_outcome("resolved", True)

    bundle = storage.list_agent_run_bundles("tenant-a")[0]
    assert bundle.turns[0].request_state is EvidenceState.NOT_CAPTURED
    assert bundle.turns[0].response_state is EvidenceState.NOT_CAPTURED
    assert all(
        not (
            {
                "text",
                "value",
                "arguments",
                "result",
                "command",
                "stdout",
                "stderr",
                "output",
                "reason",
            }
            & event.attributes.keys()
        )
        for event in bundle.events
    )
    assert all(
        event.privacy_classification is PrivacyClassification.OMITTED
        and event.omission_reason == "content_capture_disabled"
        for event in bundle.events
    )


def test_direct_verdict_client_remains_a_supported_trace_sink() -> None:
    storage = InMemoryStorage()
    client = VerdictClient(storage=storage)
    trace = _completed_trace()
    safe_persist_trace(client, trace)
    assert storage.get_trace(trace.trace_id) is not None


def test_verdict_client_preserves_published_positional_field_order() -> None:
    storage = InMemoryStorage()
    client = VerdictClient(
        "",
        "service",
        "production",
        True,
        "redact",
        None,
        1.0,
        "tenant-a",
        storage,
        ["openai"],
    )

    assert client.storage is storage
    assert client.enabled_instrumentors == ["openai"]
    assert client.transport == "storage"
    assert list(inspect.signature(VerdictClient).parameters)[:12] == [
        "api_key",
        "service_name",
        "environment",
        "capture_content",
        "redaction_mode",
        "redaction_secret",
        "sample_rate",
        "tenant_id",
        "storage",
        "enabled_instrumentors",
        "_instrumentors",
        "_initialized",
    ]


def test_agent_capture_failure_and_logging_failure_do_not_escape() -> None:
    class CaptureFailure(RuntimeError):
        pass

    class ApplicationFailure(RuntimeError):
        pass

    class FailingSink:
        def capture_agent(self, batch: object, traces: object = ()) -> None:
            raise CaptureFailure("private failure detail")

        def capture_trace(self, trace: object) -> None:
            raise CaptureFailure("private failure detail")

        def close(self) -> None:
            pass

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise RuntimeError("logging failed")

    client = verdict.init(storage=InMemoryStorage(), tenant_id="tenant-a", instrumentors=[])
    client._capture_sink = FailingSink()  # type: ignore[assignment]
    handler = FailingHandler()
    logger = logging.getLogger("verdict.agent")
    logger.addHandler(handler)
    failure = ApplicationFailure("application failure")
    try:
        with pytest.raises(ApplicationFailure) as caught:
            with verdict.agent_run(name="agent"):
                raise failure
    finally:
        logger.removeHandler(handler)

    assert caught.value is failure


def test_tool_exception_remains_authoritative_when_stringification_fails() -> None:
    class OriginalFailure(RuntimeError):
        def __str__(self) -> str:
            raise RuntimeError("stringification failed")

    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    failure = OriginalFailure()
    with pytest.raises(OriginalFailure) as caught:
        with verdict.agent_run(name="agent") as run:
            with run.turn(user_input="hello") as turn:
                with turn.tool("lookup"):
                    raise failure

    assert caught.value is failure
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    result = next(
        event for event in bundle.events if event.event_type is AgentEventType.TOOL_RESULT
    )
    assert result.status is ExecutionStatus.FAILED
    assert result.attributes["result"]["error_type"] == "OriginalFailure"
    assert result.attributes["result"]["message"] == "<UNAVAILABLE>"


def test_tool_exception_remains_authoritative_when_message_is_not_utf8() -> None:
    class OriginalFailure(RuntimeError):
        def __str__(self) -> str:
            return "\ud800"

    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    failure = OriginalFailure()
    with pytest.raises(OriginalFailure) as caught:
        with verdict.agent_run(name="agent") as run:
            with run.turn(user_input="hello") as turn:
                with turn.tool("lookup"):
                    raise failure

    assert caught.value is failure
    [bundle] = storage.list_agent_run_bundles("tenant-a")
    result = next(
        event for event in bundle.events if event.event_type is AgentEventType.TOOL_RESULT
    )
    assert result.attributes["result"]["message"] == "<UNAVAILABLE>"


def test_multiple_turns_append_without_rewriting_the_run() -> None:
    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="agent") as run:
        for value in ("first", "second"):
            with run.turn(user_input=value) as turn:
                turn.record_feedback(kind="thumbs_up")
                turn.set_output(value)

    bundle = storage.list_agent_run_bundles("tenant-a")[0]
    assert [turn.sequence for turn in bundle.turns] == [0, 1]
    assert [event.sequence for event in bundle.events] == [0, 0]


def test_closed_contexts_reject_late_evidence_without_mutating_the_run() -> None:
    storage = InMemoryStorage()
    verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="first") as turn:
            with turn.tool("lookup") as tool:
                tool.set_output("done")
            turn.set_output("done")

        with pytest.raises(RuntimeError, match="active agent turn"):
            turn.record_retry(reason="late", attempt=1)
        with pytest.raises(RuntimeError, match="active agent turn"):
            with turn.tool("late"):
                pass

    with pytest.raises(RuntimeError, match="active agent run"):
        run.turn(user_input="late")
    with pytest.raises(RuntimeError, match="active agent run"):
        run.record_business_outcome("late", True)

    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert len(bundle.turns) == 1
    assert [event.event_type for event in bundle.events] == [
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
    ]


def test_provider_wrapper_creates_a_real_linked_trace_inside_agent_turn() -> None:
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    storage = InMemoryStorage()
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    instrumentor = AnthropicInstrumentor(client)

    def provider_call(*args, **kwargs):
        return SimpleNamespace(
            model="claude-test",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="provider answer")],
        )

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="provider question") as turn:
            response = instrumentor._wrap_create_sync(
                provider_call,
                None,
                (),
                {"model": "claude-test", "max_tokens": 20, "messages": []},
            )
            turn.set_output(response.content[0].text)

    bundle = storage.list_agent_run_bundles("tenant-a")[0]
    [event] = [event for event in bundle.events if event.event_type is AgentEventType.MODEL_CALL]
    linked = storage.get_trace(event.trace_id)  # type: ignore[arg-type]
    assert linked is not None
    assert linked.response_redacted == "provider answer"
    assert linked.tags["verdict.agent_run_id"] == bundle.run.run_id


def test_provider_context_is_snapshotted_when_call_begins() -> None:
    storage = InMemoryStorage()
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    trace = _completed_trace()

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="question") as turn:
            apply_routing_context(client, trace)
            turn.set_output("answer")
    safe_persist_trace(client, trace)

    bundle = storage.list_agent_run_bundles("tenant-a")[0]
    assert any(event.trace_id == trace.trace_id for event in bundle.events)


@pytest.mark.asyncio
async def test_provider_call_started_by_inherited_task_after_turn_closes_is_standalone() -> None:
    from verdict.instrumentors.anthropic import AnthropicInstrumentor

    storage = InMemoryStorage()
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    instrumentor = AnthropicInstrumentor(client)
    release = asyncio.Event()

    async def provider_call(*args, **kwargs):
        return SimpleNamespace(
            model="claude-test",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="late answer")],
        )

    async def background_call():
        await release.wait()
        return await instrumentor._wrap_create_async(
            provider_call,
            None,
            (),
            {"model": "claude-test", "max_tokens": 20, "messages": []},
        )

    with verdict.agent_run(name="agent") as run:
        with run.turn(user_input="question") as turn:
            task = asyncio.create_task(background_call())
            turn.set_output("parent answer")

    release.set()
    await task

    [bundle] = storage.list_agent_run_bundles("tenant-a")
    assert not any(event.event_type is AgentEventType.MODEL_CALL for event in bundle.events)
    [trace] = storage.list_traces(limit=10)
    assert "verdict.agent_run_id" not in trace.tags


def test_nested_agent_run_restores_parent_turn_context() -> None:
    storage = InMemoryStorage()
    client = verdict.init(storage=storage, tenant_id="tenant-a", instrumentors=[])
    with verdict.agent_run(name="parent") as parent:
        with parent.turn(user_input="parent") as parent_turn:
            with verdict.agent_run(name="child") as child:
                with child.turn(user_input="child") as child_turn:
                    child_trace = _completed_trace()
                    apply_routing_context(client, child_trace)
                    safe_persist_trace(client, child_trace)
                    child_turn.set_output("done")
            parent_trace = _completed_trace()
            apply_routing_context(client, parent_trace)
            safe_persist_trace(client, parent_trace)
            parent_turn.set_output("done")

    bundles = storage.list_agent_run_bundles("tenant-a")
    by_name = {bundle.run.agent_name: bundle for bundle in bundles}
    assert by_name["child"].run.parent_run_id == by_name["parent"].run.run_id
    assert any(event.trace_id == child_trace.trace_id for event in by_name["child"].events)
    assert any(event.trace_id == parent_trace.trace_id for event in by_name["parent"].events)
