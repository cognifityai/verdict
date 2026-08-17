from __future__ import annotations

import json

import pytest
from verdict.schema import EvaluatorHealthStatus, Verdict
from verdict_eval.judge import Judge, Rubric, RubricDimension
from verdict_eval.judge_health import (
    SentinelExample,
    evaluate_judge_health,
    load_sentinel_set,
    sentinel_set_fingerprint,
)
from verdict_eval.providers import FakeProvider

RUBRIC = Rubric(
    name="health",
    version="1",
    dimensions=(RubricDimension("relevance", "The answer addresses the query."),),
)


def _payload(verdict: str) -> str:
    return json.dumps({
        "relevance": {"reasoning": "fixed test output", "verdict": verdict}
    })


def _example(index: int, expected: Verdict = Verdict.PASS) -> SentinelExample:
    return SentinelExample(
        sentinel_id=f"s-{index}",
        query=f"anchor-{index}",
        response="anchored answer",
        labels={"relevance": expected},
    )


def test_judge_health_marks_sufficient_high_agreement_healthy():
    judge = Judge(provider=FakeProvider(_payload("PASS")), model="judge-a", rubric=RUBRIC)

    record = evaluate_judge_health(
        judge,
        [_example(index) for index in range(30)],
        set_name="support-v1",
    )

    assert record.status == EvaluatorHealthStatus.HEALTHY
    assert record.correct_examples == record.total_examples == 30
    assert record.example_agreement == 1.0
    assert record.correct_labels == record.total_labels == 30
    assert record.label_agreement == 1.0
    assert record.example_confidence_low is not None
    assert record.example_confidence_high is not None
    assert record.example_confidence_low < record.example_confidence_high
    assert record.example_confidence_high == pytest.approx(1.0)
    assert record.evaluator_fingerprint == judge.evaluator_identity()[
        "evaluator_fingerprint"
    ]


def test_judge_health_reports_low_data_and_errors_without_leaking_examples():
    calls = 0

    def sometimes_errors(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("customer@example.com")
        return _payload("FAIL")

    judge = Judge(provider=FakeProvider(sometimes_errors), model="judge-a", rubric=RUBRIC)
    examples = [_example(0), _example(1)]

    record = evaluate_judge_health(judge, examples, set_name="tiny")

    assert record.status == EvaluatorHealthStatus.INSUFFICIENT_DATA
    assert record.correct_labels == 0
    assert record.total_labels == 1
    assert record.error_count == 1
    assert "customer@example.com" not in repr(record)


def test_judge_health_marks_sufficient_low_agreement_degraded():
    judge = Judge(provider=FakeProvider(_payload("FAIL")), model="judge-a", rubric=RUBRIC)

    record = evaluate_judge_health(
        judge,
        [_example(index) for index in range(30)],
        set_name="support-v1",
    )

    assert record.status == EvaluatorHealthStatus.DEGRADED
    assert record.example_agreement == 0.0


def test_complete_judge_outage_is_insufficient_data_not_degraded():
    def unavailable(_request):
        raise RuntimeError("provider unavailable")

    judge = Judge(provider=FakeProvider(unavailable), model="judge-a", rubric=RUBRIC)
    record = evaluate_judge_health(
        judge,
        [_example(index) for index in range(30)],
        set_name="support-v1",
    )

    assert record.status == EvaluatorHealthStatus.INSUFFICIENT_DATA
    assert record.total_labels == 0
    assert record.correct_labels == 0
    assert record.error_count == 30
    assert record.example_confidence_low is None
    assert record.example_confidence_high is None


def test_execution_errors_prevent_a_partial_sentinel_run_from_reporting_healthy():
    calls = 0

    def mostly_unavailable(_request):
        nonlocal calls
        calls += 1
        if calls <= 270:
            raise RuntimeError("provider unavailable")
        return _payload("PASS")

    judge = Judge(
        provider=FakeProvider(mostly_unavailable), model="judge-a", rubric=RUBRIC
    )
    record = evaluate_judge_health(
        judge,
        [_example(index) for index in range(300)],
        set_name="support-v1",
        minimum_examples=30,
    )

    assert record.correct_labels == record.total_labels == 30
    assert record.error_count == 270
    assert record.status == EvaluatorHealthStatus.DEGRADED


def test_confidence_interval_uses_independent_examples_not_correlated_labels():
    rubric = Rubric(
        name="multi",
        version="1",
        dimensions=tuple(
            RubricDimension(f"d{index}", "quality") for index in range(5)
        ),
    )
    payload = json.dumps({
        dimension.name: {"reasoning": "ok", "verdict": "PASS"}
        for dimension in rubric.dimensions
    })
    judge = Judge(provider=FakeProvider(payload), model="judge-a", rubric=rubric)
    examples = [
        SentinelExample(
            sentinel_id=f"s-{index}",
            query="q",
            response="r",
            labels={dimension.name: Verdict.PASS for dimension in rubric.dimensions},
        )
        for index in range(6)
    ]

    record = evaluate_judge_health(
        judge,
        examples,
        set_name="multi",
        minimum_examples=6,
        agreement_threshold=0.8,
    )

    assert record.total_labels == 30
    assert record.example_agreement == 1.0
    assert record.label_agreement == 1.0
    assert record.example_confidence_low < 0.8
    assert record.status == EvaluatorHealthStatus.DEGRADED


def test_label_rich_example_cannot_certify_a_judge_that_fails_most_examples():
    """The health gate's statistical unit is one independently judged example.

    One label-rich example must not outweigh many failed examples. This is the
    adversarial shape that previously reported a mostly-wrong judge as healthy.
    """
    dimensions = tuple(
        RubricDimension(f"d{index}", "quality") for index in range(200)
    )
    rubric = Rubric(name="unequal-labels", version="1", dimensions=dimensions)
    payload = json.dumps({
        dimension.name: {"reasoning": "fixed", "verdict": "PASS"}
        for dimension in dimensions
    })
    judge = Judge(provider=FakeProvider(payload), model="judge-a", rubric=rubric)
    examples = [
        SentinelExample(
            sentinel_id="label-rich-correct",
            query="q",
            response="r",
            labels={dimension.name: Verdict.PASS for dimension in dimensions},
        ),
        *[
            SentinelExample(
                sentinel_id=f"single-label-wrong-{index}",
                query="q",
                response="r",
                labels={"d0": Verdict.FAIL},
            )
            for index in range(29)
        ],
    ]

    record = evaluate_judge_health(
        judge,
        examples,
        set_name="unequal-labels",
        agreement_threshold=0.6,
    )

    assert record.status == EvaluatorHealthStatus.DEGRADED
    assert record.correct_examples == 1
    assert record.total_examples == 30
    assert record.example_agreement == pytest.approx(1 / 30)
    assert record.label_agreement == pytest.approx(200 / 229)


def test_health_threshold_must_be_cleared_by_confidence_lower_bound():
    judge = Judge(provider=FakeProvider(_payload("PASS")), model="judge-a", rubric=RUBRIC)
    examples = [
        _example(index, Verdict.PASS if index < 27 else Verdict.FAIL)
        for index in range(30)
    ]

    record = evaluate_judge_health(
        judge,
        examples,
        set_name="support-v1",
        agreement_threshold=0.8,
    )

    assert record.example_agreement == 0.9
    assert record.example_confidence_low < 0.8
    assert record.status == EvaluatorHealthStatus.DEGRADED


def test_sentinel_fingerprint_is_order_independent_and_includes_labels():
    first = _example(1)
    second = _example(2)

    assert sentinel_set_fingerprint([first, second]) == sentinel_set_fingerprint(
        [second, first]
    )
    assert sentinel_set_fingerprint([first]) != sentinel_set_fingerprint([
        _example(1, Verdict.FAIL)
    ])


def test_load_sentinel_set_rejects_duplicate_ids_and_unclear_labels(tmp_path):
    path = tmp_path / "anchors.jsonl"
    path.write_text(
        '\n'.join([
            '{"set_name":"support-v1"}',
            '{"sentinel_id":"same","query":"q","response":"r",'
            '"labels":{"relevance":"pass"}}',
            '{"sentinel_id":"same","query":"q2","response":"r2",'
            '"labels":{"relevance":"unclear"}}',
        ])
    )

    with pytest.raises(ValueError, match="PASS or FAIL"):
        load_sentinel_set(path)
