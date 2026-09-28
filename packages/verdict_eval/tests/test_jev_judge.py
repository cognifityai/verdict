"""Jev's real SDK against a local HTTP transport, through Verdict's judge."""

from __future__ import annotations

import json

import httpx2
import pytest
import typesafe_sdk
from verdict.schema import Verdict
from verdict_eval.cli.pipeline import build_parser, main
from verdict_eval.jev_judge import JevJudge
from verdict_eval.judge import DEFAULT_RUBRIC


def _wire_judge(monkeypatch, *, labels=None, response_model="jev-1.13.0"):
    requests = []
    actual_client = typesafe_sdk.TypeSafeClient

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        answers = {
            name: {
                "type": "choice",
                "choice": (labels or {}).get(name, "pass"),
                "confidence": 0.8,
                "probabilities": {"pass": 0.8, "fail": 0.1, "unclear": 0.1},
            }
            for name in body["questions"]
        }
        if labels and "__omit__" in labels:
            answers.pop(labels["__omit__"])
        if labels and "__extra__" in labels:
            answers["unexpected"] = answers[next(iter(answers))]
        return httpx2.Response(200, json={
            "model": response_model,
            "answers": answers,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })

    def client(**kwargs):
        return actual_client(
            api_key="ts_test_key",
            transport=httpx2.MockTransport(handle),
            **kwargs,
        )

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", client)
    return requests


def test_jev_choice_labels_become_complete_verdict_judgment(monkeypatch):
    requests = _wire_judge(monkeypatch, labels={"safety": "fail"})
    judge = JevJudge()

    judgment = judge.judge(
        query="What is the refund policy?",
        response="A response.",
        context="Refunds within 30 days.",
        trace_id="trace-1",
    )

    assert judgment.trace_id == "trace-1"
    assert judgment.evaluator_provider == "jev"
    assert judgment.judge_models == ["jev-1.13.0"]
    assert judgment.evaluator_fingerprint == judge.evaluator_identity()["evaluator_fingerprint"]
    assert [score.name for score in judgment.dimensions] == [
        dimension.name for dimension in DEFAULT_RUBRIC.dimensions
    ]
    assert {score.name: score.verdict for score in judgment.dimensions}["safety"] == Verdict.FAIL
    assert all(score.reasoning == "" for score in judgment.dimensions)
    assert requests[0]["state"]["retrieved_context"] == "Refunds within 30 days."
    assert set(requests[0]["questions"]) == set(judgment.expected_dimensions)


@pytest.mark.parametrize("labels,model", [
    ({"__omit__": "safety"}, "jev-1.13.0"),
    ({"__extra__": True}, "jev-1.13.0"),
    ({"safety": "maybe"}, "jev-1.13.0"),
    ({}, "jev-other-version"),
])
def test_jev_rejects_incomplete_or_unexpected_api_result(monkeypatch, labels, model):
    _wire_judge(monkeypatch, labels=labels, response_model=model)
    with pytest.raises(ValueError):
        JevJudge().judge(query="Question", response="Answer")


def test_jev_omits_context_dimension_when_requested_and_fingerprints_it(monkeypatch):
    requests = _wire_judge(monkeypatch)
    judge = JevJudge(skip_context_dependent_when_missing=True)

    judgment = judge.judge(query="Question", response="Answer")

    assert "groundedness" not in judgment.expected_dimensions
    assert "groundedness" not in requests[0]["questions"]
    assert judgment.evaluator_fingerprint != judge.evaluator_identity("Context")["evaluator_fingerprint"]
    assert judge.evaluator_identity("  ")["expected_dimensions"] == judgment.expected_dimensions


def test_pipeline_accepts_jev_but_rejects_unavailable_telemetry_capture(capsys):
    parsed = build_parser().parse_args(["--judge-provider", "jev", "--judge-model", "jev-1.13.0"])
    assert parsed.judge_provider == "jev"
    assert main(["--judge-provider", "jev", "--capture-judge-telemetry"]) == 2
    assert "unavailable" in capsys.readouterr().out


def test_pipeline_rejects_missing_jev_key_before_opening_storage(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "missing-key.db"
    assert main([
        "--storage", f"sqlite:///{path}",
        "--judge-provider", "jev", "--judge-model", "jev-1.13.0",
    ]) == 2
    assert not path.exists()
