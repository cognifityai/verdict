"""Resumable real-judge execution with a no-call-over-budget contract."""

from __future__ import annotations

from dataclasses import dataclass

from verdict.redaction import redact
from verdict.schema import Judgment, JudgmentStatus, Trace

from verdict_eval.judge import DEFAULT_RUBRIC, Judge
from verdict_eval.providers import BudgetedProvider, LLMProvider


class JudgeBudgetError(ValueError):
    """Raised without provider I/O when the complete judgment batch cannot fit."""


@dataclass(frozen=True, slots=True)
class JudgeRunSummary:
    eligible: int
    reused: int
    planned: int
    completed: int
    errors: int
    preflight_upper_usd: float
    spent_usd: float
    input_tokens: int
    output_tokens: int
    evaluator_fingerprint: str


def judge_with_budget(
    storage: object,
    traces: list[Trace],
    *,
    provider: LLMProvider,
    model: str,
    budget_usd: float,
    input_usd_per_million: float,
    output_usd_per_million: float,
) -> JudgeRunSummary:
    """Judge all missing eligible traces or make no calls when preflight cannot fit."""
    guarded = BudgetedProvider(
        provider,
        budget_usd=budget_usd,
        input_usd_per_million=input_usd_per_million,
        output_usd_per_million=output_usd_per_million,
    )
    judge = Judge(
        provider=guarded,
        model=model,
        rubric=DEFAULT_RUBRIC,
        skip_context_dependent_when_missing=True,
    )
    identity = judge.evaluator_identity(context=None)
    eligible = [
        trace
        for trace in traces
        if trace.tags.get("verdict.workload") != "judge"
        and not trace.error
        and (trace.prompt_redacted or "").strip()
        and (trace.response_redacted or "").strip()
    ]
    latest: dict[str, Judgment] = {}
    for judgment in storage.list_judgments(limit=max(1_000, len(eligible) * 10)):
        if not _same_evaluator(judgment, identity):
            continue
        previous = latest.get(judgment.trace_id)
        if previous is None or (judgment.created_at, judgment.judgment_id) > (
            previous.created_at,
            previous.judgment_id,
        ):
            latest[judgment.trace_id] = judgment
    reusable = {
        trace_id
        for trace_id, judgment in latest.items()
        if judgment.status is JudgmentStatus.COMPLETED
    }
    planned = [trace for trace in eligible if trace.trace_id not in reusable]
    requests = [
        judge.completion_request(
            query=trace.prompt_redacted or "",
            response=trace.response_redacted or "",
        )
        for trace in planned
    ]
    preflight_upper = sum(guarded.upper_bound_usd(request) for request in requests)
    if preflight_upper > budget_usd:
        raise JudgeBudgetError(
            f"judge preflight upper bound ${preflight_upper:.4f} exceeds ${budget_usd:.2f} budget"
        )

    completed = errors = 0
    for trace in planned:
        try:
            storage.insert_judgment(
                judge.judge(
                    query=trace.prompt_redacted or "",
                    response=trace.response_redacted or "",
                    trace_id=trace.trace_id,
                )
            )
            completed += 1
        except Exception as exc:
            storage.insert_judgment(
                Judgment(
                    trace_id=trace.trace_id,
                    status=JudgmentStatus.ERROR,
                    error=redact(str(exc)) or "judge error",
                    **identity,
                )
            )
            errors += 1
    return JudgeRunSummary(
        eligible=len(eligible),
        reused=len(reusable & {trace.trace_id for trace in eligible}),
        planned=len(planned),
        completed=completed,
        errors=errors,
        preflight_upper_usd=preflight_upper,
        spent_usd=guarded.spent_usd,
        input_tokens=guarded.input_tokens,
        output_tokens=guarded.output_tokens,
        evaluator_fingerprint=identity["evaluator_fingerprint"],
    )


def _same_evaluator(judgment: Judgment, identity: dict[str, object]) -> bool:
    return (
        judgment.evaluator_provider == identity["evaluator_provider"]
        and judgment.evaluator_config == identity["evaluator_config"]
        and judgment.evaluator_fingerprint == identity["evaluator_fingerprint"]
        and judgment.expected_dimensions == identity["expected_dimensions"]
        and judgment.rubric_name == identity["rubric_name"]
        and judgment.rubric_version == identity["rubric_version"]
        and judgment.judge_models == identity["judge_models"]
    )
