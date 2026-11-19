"""Reply check: judge each agent reply by what the user wrote next.

A model judge reads the user's request, the agent's final response, and the
user's next request in the same session, then answers four fixed questions.
Nobody labels anything; the next message is evidence the transcript already
holds. Runs reuse the Evaluator Lab's Agent Turn path, so results are stored,
kept current, and shown like any other Turn evaluation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Callable
from typing import Any, TextIO

from verdict.agent_judgment import NEXT_MESSAGE_MODE
from verdict.monitor_inputs import LOCAL_TENANT
from verdict.pricing import compute_cost_usd
from verdict.schema import JudgmentStatus

# The wording names both what is and what is not a FAIL. Without the "not"
# list, judges count questions and confusion as corrections; with only a
# narrow "said it was wrong" rule, they miss plain pushback such as "no, ...".
REPLY_CHECK_RUBRIC: dict[str, Any] = {
    "name": "reply_check",
    "version": "1",
    "dimensions": [
        {"name": "understood_reply", "description": (
            "FAIL only if the user's next message says or clearly shows they could not "
            "follow the response, for example by saying they do not understand or asking "
            "for it to be explained again, simplified, or summarized. A follow-up question "
            "about details or a new topic is not by itself a FAIL. PASS if the next message "
            "acts on or builds on the response. UNCLEAR if it gives no sign either way."
        )},
        {"name": "no_correction_needed", "description": (
            "FAIL if the user's next message pushes back on the response: says it is wrong, "
            "does not work, or is not what they wanted; rejects or objects to something it "
            "did or proposed, including short rejections such as 'no, ...'; questions a step "
            "in a way that shows they disagree with it; or repeats a request the response "
            "did not meet. Not a FAIL: saying they do not understand the response, a neutral "
            "follow-up question about details, or starting an unrelated new task. PASS if "
            "the user accepts the response or builds on it. UNCLEAR if the next message "
            "gives no sign either way."
        )},
        {"name": "stayed_in_scope", "description": (
            "PASS if the user's next message continues without objecting to what the "
            "assistant did. FAIL if it shows the assistant did more than asked, changed "
            "something the user did not want changed, or took an action the user had to "
            "undo. UNCLEAR if the next message gives no sign either way."
        )},
        {"name": "done_claim_held", "description": (
            "If the response says work is done, fixed, or passing: PASS if the user's next "
            "message proceeds as if it was, FAIL if it shows it was not. UNCLEAR if the "
            "response makes no such claim or the next message gives no sign."
        )},
    ],
}
FAIL_MEANS = {
    "understood_reply": "user could not follow the reply",
    "no_correction_needed": "user had to correct the agent",
    "stayed_in_scope": "agent went beyond what was asked",
    "done_claim_held": "a 'done' or 'fixed' claim did not hold",
}
SKIP_MEANS = {
    "next_message_unavailable": "no next message (last reply in the session)",
    "next_message_truncated": "next message was cut off at capture",
    "next_message_from_agent": "sub-agent reply; the next message came from another agent",
    "redacted_evidence": "redaction would change the text sent to the judge",
    "turn_not_completed": "the agent's turn did not complete",
    "request_unavailable": "the request was not captured",
    "response_unavailable": "the reply was not captured",
    "evidence_truncated": "the request or reply was cut off at capture",
}
MAX_OUTPUT_TOKENS = 1024
_FIRST_BATCH = 5
# Measured batches are projected from the largest output seen and the observed
# ratio of real to estimated input tokens, with this margin on top.
_COST_MARGIN = 1.5
_STALE_PLAN_RETRIES = 3


class _MeteredProvider:
    """Measure real token usage per call; the call itself is unchanged."""

    def __init__(self, inner, model: str) -> None:
        self._inner = inner
        self._model = model
        self.calls = 0
        self.spent_usd = 0.0
        self.input_tokens = 0
        self.max_output_tokens = 0
        # A call without reported usage cannot be priced, so the cap is void.
        self.unmeasured = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete(self, request):
        response = self._inner.complete(request)
        self.calls += 1
        if response.input_tokens is None or response.output_tokens is None:
            self.unmeasured = True
            return response
        self.input_tokens += response.input_tokens
        self.max_output_tokens = max(self.max_output_tokens, response.output_tokens)
        self.spent_usd += compute_cost_usd(
            self._model, response.input_tokens, response.output_tokens,
        ) or 0.0
        return response


def _config(provider: str, model: str, **extra) -> dict[str, Any]:
    return {
        "unit": "agent_turn", "provider": provider, "model": model,
        "maxOutputTokens": MAX_OUTPUT_TOKENS, "nextMessageEvidence": NEXT_MESSAGE_MODE,
        "rubric": REPLY_CHECK_RUBRIC, "scanLimit": 100, **extra,
    }


def scan(storage, *, tenant_id: str, provider: str, model: str) -> dict[str, Any]:
    """Count what a run would judge; nothing leaves the machine."""
    from verdict.dashboard.evaluator_lab import preview_evaluation

    pending = already = 0
    reasons: Counter[str] = Counter()
    cursor = None
    while True:
        page = preview_evaluation(storage, tenant_id=tenant_id, config=_config(
            provider, model, maxCalls="all", **({"before": cursor} if cursor else {}),
        ))
        pending += page["plannedCalls"]
        already = page["alreadyJudged"] + already
        reasons.update(page["notEvaluableReasons"])
        if not page["hasMore"]:
            break
        cursor = page["nextCursor"]
    return {"pending": pending, "alreadyJudged": already, "skipped": dict(sorted(reasons.items())),
            "destination": page["destination"]}


def judge(
    storage, *, tenant_id: str, provider: str, model: str, budget_usd: float,
    provider_factory: Callable[[str], Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Judge pending replies newest first until done or the budget is reached."""
    from verdict.dashboard import evaluator_lab

    if compute_cost_usd(model, 1_000, 1_000) is None:
        raise ValueError(f"no list price for {model}; the spend cap cannot be enforced")
    meter = _MeteredProvider((provider_factory or evaluator_lab._provider)(provider), model)
    completed = errors = 0
    fingerprint = None
    stopped = None
    input_ratio = 1.0
    cursor = None
    stale_retries = 0

    def projected_usd(preview) -> float:
        if meter.calls == 0:
            # Nothing measured yet: the static bound assumes the full output allowance.
            return preview["upperStaticListPriceUsd"] or math.inf
        return _COST_MARGIN * (compute_cost_usd(
            model, math.ceil(preview["estimatedInputTokens"] * input_ratio),
            preview["plannedCalls"] * meter.max_output_tokens,
        ) or math.inf)

    while stopped is None:
        allowed = _FIRST_BATCH if meter.calls == 0 else 100
        config = _config(provider, model, maxCalls=allowed,
                         **({"before": cursor} if cursor else {}))
        preview = evaluator_lab.preview_evaluation(storage, tenant_id=tenant_id, config=config)
        # Halve the batch until its projected cost fits what is left of the budget.
        while preview["plannedCalls"] and projected_usd(preview) > budget_usd - meter.spent_usd:
            if preview["plannedCalls"] == 1:
                stopped = "budget"
                break
            config["maxCalls"] = preview["plannedCalls"] // 2
            preview = evaluator_lab.preview_evaluation(
                storage, tenant_id=tenant_id, config=config)
        if stopped:
            break
        result = None
        if preview["plannedCalls"]:
            before_input = meter.input_tokens
            try:
                result = evaluator_lab.execute_evaluation(
                    storage, tenant_id=tenant_id, provider=meter, confirm_external_egress=True,
                    config={**config, "planFingerprint": preview["planFingerprint"],
                            "plannedTurns": preview["plannedTurns"]},
                )
            except ValueError as exc:
                # A live session gained a Turn between preview and run; preview again.
                if "no longer current" not in str(exc) or stale_retries >= _STALE_PLAN_RETRIES:
                    raise
                stale_retries += 1
                continue
            stale_retries = 0
            completed += result["completed"]
            errors += result["errors"]
            fingerprint = result["evaluatorFingerprint"]
            if preview["estimatedInputTokens"]:
                input_ratio = max(input_ratio, (meter.input_tokens - before_input)
                                  / preview["estimatedInputTokens"])
            if progress:
                progress({"completed": completed, "errors": errors,
                          "spentUsd": meter.spent_usd})
            if meter.unmeasured:
                stopped = "usage_unreported"
                break
            if meter.spent_usd >= budget_usd:
                stopped = "budget"
                break
        left_on_page = preview["eligible"] - preview["alreadyJudged"] - preview["plannedCalls"]
        # Stay on a page the budget split, but only while it is making progress,
        # so a reply whose judge call keeps failing cannot hold the run in place.
        if left_on_page > 0 and result is not None and result["completed"] > 0:
            continue
        if not preview["hasMore"]:
            break
        cursor = preview["nextCursor"]
    return {"completed": completed, "errors": errors, "spentUsd": meter.spent_usd,
            "calls": meter.calls, "stopped": stopped,
            "stoppedForBudget": stopped == "budget", "evaluatorFingerprint": fingerprint}


def evaluator_fingerprint(provider: str, model: str) -> str:
    from verdict.dashboard.evaluator_lab import _IdentityOnlyProvider, _judge, _validated_config

    _, _, _, max_output, rubric = _validated_config(_config(provider, model))
    return _judge(_IdentityOnlyProvider(provider), model, rubric, max_output,
                  next_message=True).evaluator_identity(context=None)["evaluator_fingerprint"]


def _wilson(k: int, n: int) -> tuple[float, float]:
    if not n:
        return 0.0, 0.0
    p, z = k / n, 1.96
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(storage, *, tenant_id: str, fingerprint: str) -> dict[str, Any]:
    """Rates over current completed results; a stale result is never counted."""
    current = set()
    before = None
    while True:
        rows, has_more = storage.list_agent_turn_evaluation_candidates(
            tenant_id, fingerprint, limit=100, before=before, next_message=True,
        )
        # Storage reports COMPLETED only for a result bound to the current next message.
        current.update((turn.run_id, turn.turn_id) for turn, status, _message in rows
                       if status is JudgmentStatus.COMPLETED)
        if not has_more or not rows:
            break
        last = rows[-1][0]
        before = (last.started_at, last.run_id, last.turn_id)
    verdicts = {name: Counter() for name in FAIL_MEANS}
    judged = 0
    after = None
    while True:
        page, after = storage.list_agent_turn_judgments(
            tenant_id, fingerprint, limit=500, after=after)
        for judgment in page:
            if (judgment.run_id, judgment.turn_id) not in current:
                continue
            judged += 1
            for score in judgment.dimensions:
                if score.name in verdicts:
                    verdicts[score.name][score.verdict.value] += 1
        if after is None:
            break
    rates = {}
    for name, counts in verdicts.items():
        low, high = _wilson(counts["fail"], judged)
        rates[name] = {"fail": counts["fail"], "pass": counts["pass"],
                       "unclear": counts["unclear"], "failRate": counts["fail"] / judged
                       if judged else None, "failRate95": [low, high]}
    return {"judged": judged, "rates": rates}


def _storage_url(value: str | None) -> str:
    from verdict.dashboard.app import resolve_db

    value = value or os.environ.get("VERDICT_STORAGE") or str(resolve_db())
    return value if "://" in value else f"sqlite:///{value}"


def _print_summary(summary: dict[str, Any], out: TextIO) -> None:
    judged = summary["judged"]
    print(f"\nReplies judged: {judged}", file=out)
    if not judged:
        return
    for name, rate in summary["rates"].items():
        low, high = rate["failRate95"]
        print(f"  {100 * rate['failRate']:5.1f}%  {FAIL_MEANS[name]}"
              f"  ({rate['fail']} of {judged}; 95% range {100 * low:.1f}-{100 * high:.1f}%;"
              f" {rate['unclear']} unclear)", file=out)


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None,
         out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verdict-reply-check",
        description=(
            "Judge each agent reply by the user's next message: did they understand it, "
            "correct it, object to its scope, or find a 'done' claim untrue?"
        ),
    )
    parser.add_argument("--storage", help="SQLite path/URL or PostgreSQL DSN "
                        "(default: the dashboard's store)")
    parser.add_argument("--tenant", default=LOCAL_TENANT)
    parser.add_argument("--provider", default="openai", choices=("openai", "anthropic", "google"))
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--budget", type=float, default=5.0,
                        help="USD cap: no batch starts if its projected cost would pass what is left")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--summary-only", action="store_true",
                        help="print rates from earlier runs without judging anything")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    if not math.isfinite(args.budget) or args.budget <= 0:
        parser.error("--budget must be a positive number")
    from verdict.client import _resolve_storage

    storage = None
    try:
        storage = _resolve_storage(_storage_url(args.storage))
        fingerprint = evaluator_fingerprint(args.provider, args.model)
        run = None
        if not args.summary_only:
            plan = scan(storage, tenant_id=args.tenant, provider=args.provider, model=args.model)
            if not args.json:
                print(f"{plan['pending']} replies to judge, {plan['alreadyJudged']} already "
                      "judged.", file=out)
                for reason, count in plan["skipped"].items():
                    print(f"  skipped {count}: {SKIP_MEANS.get(reason, reason)}", file=out)
            if plan["pending"]:
                if not args.yes:
                    print(f"\nEach request, reply and next message (already redacted) is sent to "
                          f"{plan['destination']} using {args.model}. Spend stops at "
                          f"${args.budget:.2f}. Continue? [y/N] ", end="", file=out, flush=True)
                    if stdin.readline().strip().lower() not in ("y", "yes"):
                        print("Nothing sent.", file=out)
                        return 1

                def progress(state):
                    if not args.json:
                        print(f"  judged {state['completed']}, errors {state['errors']}, "
                              f"spent ${state['spentUsd']:.2f}", file=out, flush=True)

                run = judge(storage, tenant_id=args.tenant, provider=args.provider,
                            model=args.model, budget_usd=args.budget, progress=progress)
                if run["stopped"] == "budget" and not args.json:
                    print(f"Stopped at the ${args.budget:.2f} budget; run again to continue.",
                          file=out)
                if run["stopped"] == "usage_unreported":
                    print("Stopped: the provider did not report token usage, so the spend cap "
                          "cannot be enforced.", file=sys.stderr)
        summary = summarize(storage, tenant_id=args.tenant, fingerprint=fingerprint)
        if args.json:
            print(json.dumps({"run": run, **summary}, sort_keys=True), file=out)
        else:
            _print_summary(summary, out)
        return 0
    except (OSError, ValueError, ImportError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if storage is not None:
            storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
