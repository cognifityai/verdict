"""Jev's real SDK against a local HTTP transport, through Verdict's judge."""

from __future__ import annotations

import json

import httpx2
import pytest
import typesafe_sdk
from typesafe_sdk.constants import DEFAULT_BASE_URL
from verdict.schema import Verdict
from verdict_eval.cli.pipeline import build_parser, main
from verdict_eval.jev_judge import JevJudge
from verdict_eval.judge import DEFAULT_RUBRIC


def _wire_judge(monkeypatch, *, labels=None, response_model="jev-1.13.0"):
    requests = []
    actual_client = typesafe_sdk.TypeSafeClient

    def handle(request):
        body = json.loads(request.content)
        body["__request_url__"] = str(request.url)
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


def test_pipeline_accepts_an_explicit_versioned_jev_model(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key")
    from verdict_eval.cli import pipeline

    selected = []
    monkeypatch.setattr(pipeline, "_run", lambda args: selected.append(args.judge_model) or 0)
    assert main(["--judge-provider", "jev", "--judge-model", "jev-1.14.0"]) == 0
    assert selected == ["jev-1.14.0"]


def test_jev_versioned_model_is_sent_and_identified(monkeypatch):
    requests = _wire_judge(monkeypatch, response_model="jev-1.14.0")
    judge = JevJudge(model="jev-1.14.0")
    result = judge.judge(query="Question", response="Answer")
    assert requests[0]["model"] == "jev-1.14.0"
    assert result.judge_models == ["jev-1.14.0"]
    assert result.evaluator_fingerprint != JevJudge().evaluator_identity()[
        "evaluator_fingerprint"
    ]


@pytest.mark.parametrize("model", ["jev-latest", "jev", "", "jev-1.14.0?key=x"])
def test_jev_rejects_alias_or_invalid_model_before_provider_call(monkeypatch, model):
    requests = _wire_judge(monkeypatch)
    with pytest.raises(ValueError, match="versioned Jev model"):
        JevJudge(model=model)
    assert requests == []


def test_pipeline_rejects_missing_jev_key_before_opening_storage(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "missing-key.db"
    assert main([
        "--storage", f"sqlite:///{path}",
        "--judge-provider", "jev", "--judge-model", "jev-1.13.0",
    ]) == 2
    assert not path.exists()


def test_jev_default_and_custom_endpoint_are_part_of_identity(monkeypatch):
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    default = JevJudge()
    assert default.evaluator_identity()["evaluator_config"]["base_url"] == DEFAULT_BASE_URL
    monkeypatch.setenv("TYPESAFE_BASE_URL", "  https://judge-a.example/api/  ")
    custom = JevJudge()
    assert custom.evaluator_identity()["evaluator_config"]["base_url"] == (
        "https://judge-a.example/api"
    )
    assert custom.evaluator_identity()["evaluator_fingerprint"] != (
        default.evaluator_identity()["evaluator_fingerprint"]
    )


def test_jev_call_keeps_the_endpoint_captured_before_environment_change(monkeypatch):
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-a.example/api")
    requests = _wire_judge(monkeypatch)
    judge = JevJudge()
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://judge-b.example/api")
    judge.judge(query="Question", response="Answer")
    assert requests[0]["__request_url__"].startswith("https://judge-a.example/api/")


@pytest.mark.parametrize("endpoint", [
    "https://user:password@judge.example",
    "https://judge.example/?api_key=secret",
    "https://judge.example/#fragment",
    "file:///tmp/socket",
    "https://judge.example:invalid",
    "https://judge.example\\@elsewhere.example",
    pytest.param("https://judge.example/" + "a" * 2050, id="oversized"),
])
def test_jev_rejects_endpoint_values_that_cannot_be_safely_disclosed(monkeypatch, endpoint):
    monkeypatch.setenv("TYPESAFE_BASE_URL", endpoint)
    with pytest.raises(ValueError, match="Jev endpoint"):
        JevJudge()
