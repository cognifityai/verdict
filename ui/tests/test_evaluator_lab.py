from datetime import datetime, timezone

from verdict.dashboard.evaluator_lab import (
    execute_calibration,
    execute_evaluation,
    preview_calibration,
    preview_evaluation,
)
from verdict.schema import Trace
from verdict.storage import InMemoryStorage
from verdict_eval.providers import CompletionResponse


class CountingProvider:
    name = "test"

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

    result = execute_evaluation(
        storage,
        tenant_id="local",
        config=_config(),
        provider=provider,
        confirm_external_egress=True,
    )

    assert provider.calls == 1
    assert result["completed"] == 1
    assert result["notEvaluable"] == 1
    [judgment] = storage.list_judgments_for_cluster("all", limit=10)
    assert judgment.trace_id == "eligible"
    assert judgment.evaluator_identity_complete is True
    assert [score.name for score in judgment.dimensions] == ["relevance", "completeness"]

    repeated = execute_evaluation(
        storage,
        tenant_id="local",
        config=_config(),
        provider=provider,
        confirm_external_egress=True,
    )
    assert provider.calls == 1
    assert repeated["plannedCalls"] == 0
    assert repeated["alreadyJudged"] == 1


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
