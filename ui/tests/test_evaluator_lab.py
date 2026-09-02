import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from verdict.dashboard.evaluator_lab import (
    execute_calibration,
    execute_evaluation,
    preview_calibration,
    preview_evaluation,
)
from verdict.schema import Judgment, Trace
from verdict.storage import InMemoryStorage
from verdict_eval.providers import CompletionResponse


class CountingProvider:
    name = "anthropic"
    supports_temperature = False

    def __init__(self):
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return CompletionResponse(
            text=(
                '{"relevance":{"reasoning":"direct","verdict":"PASS"},'
                '"completeness":{"reasoning":"complete","verdict":"PASS"}}'
            ),
            input_tokens=10,
            output_tokens=8,
        )


def _trace(trace_id, *, prompt="question", response="answer"):
    return Trace(
        trace_id=trace_id,
        started_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        provider="anthropic",
        request_model="claude-test",
        response_model="claude-test",
        prompt_redacted=prompt,
        response_redacted=response,
        tenant_id="local",
        cluster_id="all",
    )


def _config():
    return {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "maxCalls": 10,
        "maxOutputTokens": 256,
        "rubric": {
            "name": "poc",
            "version": "1",
            "dimensions": [
                {"name": "relevance", "description": "Directly answers the question."},
                {"name": "completeness", "description": "Covers requested elements."},
            ],
        },
    }


def _approved_config(storage, config):
    preview = preview_evaluation(storage, tenant_id="local", config=config)
    return {
        **config,
        "planFingerprint": preview["planFingerprint"],
        "plannedTraces": preview["plannedTraces"],
    }


def test_preview_separates_evidence_eligibility_before_any_judge_call():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    storage.insert_trace(_trace("missing-response", response=None))

    preview = preview_evaluation(storage, tenant_id="local", config=_config())

    assert preview["eligible"] == 1
    assert preview["notEvaluable"] == 1
    assert preview["plannedCalls"] == 1
    assert preview["notEvaluableReasons"] == {"response_not_captured": 1}
    assert preview["estimatedMaximumCostUsd"] is not None
    assert preview["externalEgressRequired"] is True


def test_execute_judges_only_eligible_traces_and_persists_identity():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    storage.insert_trace(_trace("missing-prompt", prompt=None))
    provider = CountingProvider()
    config = _approved_config(storage, _config())

    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert provider.calls == 1
    assert result["completed"] == 1
    assert result["notEvaluable"] == 1
    assert len(result["evaluatorId"]) == 20
    [judgment] = storage.list_judgments_for_cluster("all", limit=10)
    assert judgment.trace_id == "eligible"
    assert judgment.evaluator_identity_complete is True
    assert [score.name for score in judgment.dimensions] == ["relevance", "completeness"]

    repeated = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )
    assert provider.calls == 1
    assert repeated["plannedCalls"] == 0
    assert repeated["alreadyJudged"] == 1

    repeated_preview = preview_evaluation(
        storage, tenant_id="local", config=_config()
    )
    assert repeated_preview["plannedCalls"] == 0
    assert repeated_preview["alreadyJudged"] == 1


def test_matching_evaluation_is_not_repeated_when_newer_evaluators_exceed_page_limit():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    provider = CountingProvider()
    config = _approved_config(storage, _config())

    execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )
    for index in range(101):
        storage.insert_judgment(Judgment(
            judgment_id=f"newer-{index:03d}",
            trace_id="eligible",
            created_at=datetime(2030, 1, 1, 0, 0, index % 60, tzinfo=timezone.utc),
            evaluator_fingerprint=f"other-{index}",
        ))

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    repeated = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert preview["plannedCalls"] == 0
    assert preview["alreadyJudged"] == 1
    assert repeated["plannedCalls"] == 0
    assert provider.calls == 1


def test_concurrent_equivalent_execution_makes_one_paid_call():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(CountingProvider):
        def complete(self, request):
            started.set()
            assert release.wait(timeout=5)
            return super().complete(request)

    provider = BlockingProvider()
    config = _approved_config(storage, _config())

    def run():
        return execute_evaluation(
            storage,
            tenant_id="local",
            config=config,
            provider=provider,
            confirm_external_egress=True,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run)
        assert started.wait(timeout=5)
        second = executor.submit(run)
        release.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert provider.calls == 1
    assert sorted(result["completed"] for result in results) == [0, 1]
    assert sorted(result["alreadyJudged"] for result in results) == [0, 1]


def test_execute_requires_explicit_external_egress_confirmation():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("eligible"))

    try:
        execute_evaluation(
            storage,
            tenant_id="local",
            config=_config(),
            provider=CountingProvider(),
            confirm_external_egress=False,
        )
    except ValueError as error:
        assert str(error) == "external judge egress was not confirmed"
    else:
        raise AssertionError("expected explicit egress confirmation")


def test_all_selection_previews_and_judges_every_eligible_trace_beyond_old_default():
    storage = InMemoryStorage()
    for index in range(12):
        storage.insert_trace(_trace(f"eligible-{index}"))
    storage.insert_trace(_trace("missing-response", response=None))
    config = {**_config(), "maxCalls": "all"}

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    config = {
        **config,
        "planFingerprint": preview["planFingerprint"],
        "plannedTraces": preview["plannedTraces"],
    }

    assert preview["availableTraces"] == 13
    assert preview["eligible"] == 12
    assert preview["notEvaluable"] == 1
    assert preview["plannedCalls"] == 12
    assert preview["maximumCalls"] == "all"

    provider = CountingProvider()
    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert provider.calls == 12
    assert result["availableTraces"] == 13
    assert result["eligible"] == 12
    assert result["plannedCalls"] == 12
    assert result["completed"] == 12


def test_execution_is_pinned_to_the_approved_preview_plan():
    storage = InMemoryStorage()
    storage.insert_trace(_trace("approved"))
    provider = CountingProvider()
    config = _approved_config(storage, {**_config(), "maxCalls": "all"})
    storage.insert_trace(_trace("arrived-after-preview-a"))
    storage.insert_trace(_trace("arrived-after-preview-b"))

    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert result["eligible"] == 3
    assert result["plannedCalls"] == 1
    assert result["completed"] == 1
    assert provider.calls == 1

    try:
        execute_evaluation(
            storage,
            tenant_id="local",
            config={**config, "maxCalls": 2},
            provider=provider,
            confirm_external_egress=True,
        )
    except ValueError as error:
        assert str(error) == "evaluator plan does not match the approved preview"
    else:
        raise AssertionError("expected changed evaluator plan rejection")

    changed = InMemoryStorage()
    changed.insert_trace(_trace("changed"))
    changed_config = _approved_config(changed, _config())
    changed.insert_trace(_trace("changed", response="different response"))
    try:
        execute_evaluation(
            changed,
            tenant_id="local",
            config=changed_config,
            provider=provider,
            confirm_external_egress=True,
        )
    except ValueError as error:
        assert str(error) == "evaluator preview plan is no longer current"
    else:
        raise AssertionError("expected changed trace evidence rejection")


def test_numeric_selection_cap_remains_available_and_invalid_modes_fail_closed():
    storage = InMemoryStorage()
    for index in range(12):
        storage.insert_trace(_trace(f"eligible-{index}"))

    preview = preview_evaluation(storage, tenant_id="local", config=_config())
    assert preview["plannedCalls"] == 10
    assert preview["maximumCalls"] == 10

    for invalid in ("everything", 0, 10_001, True):
        try:
            preview_evaluation(
                storage,
                tenant_id="local",
                config={**_config(), "maxCalls": invalid},
            )
        except ValueError as error:
            assert str(error) == "invalid evaluator budget"
        else:
            raise AssertionError(f"expected invalid maxCalls rejection: {invalid!r}")


def test_numeric_cap_reports_all_existing_evaluation_coverage():
    storage = InMemoryStorage()
    provider = CountingProvider()
    for index in range(12):
        storage.insert_trace(_trace(f"judged-{index}"))
    all_config = _approved_config(storage, {**_config(), "maxCalls": "all"})
    execute_evaluation(
        storage,
        tenant_id="local",
        config=all_config,
        provider=provider,
        confirm_external_egress=True,
    )
    storage.insert_trace(_trace("new-a"))
    storage.insert_trace(_trace("new-b"))
    config = {**_config(), "maxCalls": 2}

    preview = preview_evaluation(storage, tenant_id="local", config=config)
    config = {
        **config,
        "planFingerprint": preview["planFingerprint"],
        "plannedTraces": preview["plannedTraces"],
    }
    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=config,
        provider=provider,
        confirm_external_egress=True,
    )

    assert preview["eligible"] == 14
    assert preview["plannedCalls"] == 2
    assert preview["alreadyJudged"] == 12
    assert result["completed"] == 2
    assert result["alreadyJudged"] == 12
    assert provider.calls == 14


def test_customer_label_set_can_be_previewed_and_calibrated(tmp_path):
    label_set = tmp_path / "labels.jsonl"
    label_set.write_text(
        '{"set_name":"customer-v1"}\n'
        '{"sentinel_id":"one","query":"q","response":"a",'
        '"labels":{"relevance":"pass","completeness":"pass"}}\n'
    )
    preview = preview_calibration(path=label_set, config=_config())
    provider = CountingProvider()
    storage = InMemoryStorage()

    result = execute_calibration(
        storage,
        path=label_set,
        config=_config(),
        provider=provider,
        confirm_external_egress=True,
        minimum_examples=1,
        agreement_threshold=0.5,
    )

    assert preview["setName"] == "customer-v1"
    assert preview["examples"] == 1
    assert preview["labelCounts"] == {"completeness": 1, "relevance": 1}
    assert provider.calls == 1
    assert result["status"] == "degraded"  # Wilson lower bound is below .5 at n=1
    [health] = storage.list_evaluator_health(limit=10)
    assert health.sentinel_set_name == "customer-v1"
