from __future__ import annotations

import json

from verdict.schema import Trace
from verdict.storage.memory import InMemoryStorage
from verdict_eval.judge import DEFAULT_RUBRIC
from verdict_eval.live_judging import judge_with_budget
from verdict_eval.providers import CompletionRequest, CompletionResponse


class _Provider:
    name = "test-live"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, req: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        return CompletionResponse(
            text=json.dumps(
                {
                    dimension.name: {"verdict": "PASS", "reasoning": "ok"}
                    for dimension in DEFAULT_RUBRIC.dimensions
                    if dimension.name != "groundedness"
                }
            ),
            input_tokens=100,
            output_tokens=20,
        )


def _traces() -> list[Trace]:
    return [
        Trace(
            trace_id=f"trace-{index}",
            prompt_redacted="question",
            response_redacted="answer",
        )
        for index in range(2)
    ]


def test_budget_preflight_makes_no_calls_when_batch_cannot_fit() -> None:
    storage = InMemoryStorage()
    provider = _Provider()

    try:
        judge_with_budget(
            storage,
            _traces(),
            provider=provider,
            model="test-model",
            budget_usd=0.000001,
            input_usd_per_million=1.0,
            output_usd_per_million=5.0,
        )
    except ValueError as exc:
        assert "preflight" in str(exc)
    else:
        raise AssertionError("preflight should have rejected the batch")
    assert provider.calls == 0
    assert storage.list_judgments() == []


def test_real_judgment_run_is_accounted_and_resumable() -> None:
    storage = InMemoryStorage()
    provider = _Provider()
    traces = _traces()

    first = judge_with_budget(
        storage,
        traces,
        provider=provider,
        model="test-model",
        budget_usd=1.0,
        input_usd_per_million=1.0,
        output_usd_per_million=5.0,
    )
    second = judge_with_budget(
        storage,
        traces,
        provider=provider,
        model="test-model",
        budget_usd=1.0,
        input_usd_per_million=1.0,
        output_usd_per_million=5.0,
    )

    assert first.completed == 2
    assert first.spent_usd == 0.0004
    assert second.reused == 2
    assert second.planned == 0
    assert provider.calls == 2
