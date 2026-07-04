"""User-signal correlator runner (Layer 5).

The missing orchestration around `verdict_eval.correlator.UserSignalCorrelator`.
It reads persisted judgments + user signals from storage, joins each user
signal's trace_id to that trace's overall judge verdict, runs the correlator,
and prints the resulting CorrelationReport (agreement, Gwet AC2, disagreement
examples, interpretation).

Overall verdict per trace = PASS if a majority of the judgment's dimensions are
PASS among the PASS/FAIL dimensions (UNCLEAR ignored), else FAIL. Traces with no
judgment are skipped.

Pipeline:

    stored user signals  +  stored judgments
        → join on trace_id (skip signals whose trace has no judgment)
        → overall PASS/FAIL per trace (majority of PASS/FAIL dimensions)
        → CorrelationPair per (signal, verdict)
        → UserSignalCorrelator.correlate → CorrelationReport (printed)

Usage (live storage):
    python scripts/run_correlator.py --storage sqlite:///./verdict.db

Usage (offline demo — zero setup, deterministic):
    python scripts/run_correlator.py --demo

    --demo seeds an InMemoryStorage with synthetic traces + judgments + user
    signals (some agreeing, some not), so the printed report is non-trivial.
    correlator.py is pure stdlib, so this path runs anywhere.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))


# --------------------------------------------------------------------------- #
# Overall verdict from a Judgment
# --------------------------------------------------------------------------- #

def _overall_verdict(judgment) -> str | None:
    """PASS / FAIL / None for a judgment.

    Majority of the PASS/FAIL dimensions (UNCLEAR ignored). None when no
    dimension is PASS or FAIL (nothing to vote on). Ties → PASS (>= half).
    """
    from verdict.schema import Verdict

    n_pass = sum(1 for d in judgment.dimensions if d.verdict == Verdict.PASS)
    n_fail = sum(1 for d in judgment.dimensions if d.verdict == Verdict.FAIL)
    decided = n_pass + n_fail
    if decided == 0:
        return None
    return "PASS" if n_pass * 2 >= decided else "FAIL"


# --------------------------------------------------------------------------- #
# Build CorrelationPairs from storage
# --------------------------------------------------------------------------- #

def _collect_judgments_by_trace(storage, traces) -> dict[str, object]:
    """Map trace_id → its (latest) Judgment, pulled per cluster.

    Storage exposes judgments per cluster (`list_judgments_for_cluster`), so we
    walk the distinct cluster_ids of the known traces and index by trace_id.
    """
    by_trace: dict[str, object] = {}
    cluster_ids = {t.cluster_id for t in traces if t.cluster_id}
    # Traces with no cluster_id can still have judgments keyed under "" in some
    # adapters; include the empty bucket defensively.
    cluster_ids.add("")
    for cid in cluster_ids:
        try:
            for j in storage.list_judgments_for_cluster(cid, limit=10**9):
                by_trace[j.trace_id] = j
        except Exception:
            continue
    return by_trace


def _build_pairs(storage):
    """Join persisted user signals to overall judge verdicts → CorrelationPairs."""
    from verdict_eval.correlator import CorrelationPair

    traces = storage.list_traces(limit=10**9)
    trace_by_id = {t.trace_id: t for t in traces}
    judgments_by_trace = _collect_judgments_by_trace(storage, traces)

    signals = storage.list_user_signals(limit=10**9)

    pairs: list[CorrelationPair] = []
    n_no_judgment = 0
    n_no_verdict = 0
    for sig in signals:
        judgment = judgments_by_trace.get(sig.trace_id)
        if judgment is None:
            n_no_judgment += 1
            continue
        verdict = _overall_verdict(judgment)
        if verdict is None:
            n_no_verdict += 1
            continue
        trace = trace_by_id.get(sig.trace_id)
        prompt_preview = (trace.prompt_redacted or "")[:160] if trace else ""
        response_preview = (trace.response_redacted or "")[:160] if trace else ""
        pairs.append(CorrelationPair(
            trace_id=sig.trace_id,
            judge_verdict=verdict,        # "PASS" | "FAIL"
            user_signal=sig.kind,
            prompt_preview=prompt_preview,
            response_preview=response_preview,
        ))
    return pairs, {
        "signals_total": len(signals),
        "skipped_no_judgment": n_no_judgment,
        "skipped_no_verdict": n_no_verdict,
        "pairs_built": len(pairs),
    }


# --------------------------------------------------------------------------- #
# Demo seeding
# --------------------------------------------------------------------------- #

def _seed_demo_storage():
    """Seed an InMemoryStorage with synthetic traces + judgments + signals.

    Construction is deterministic. Designed so the correlator finds:
      * mostly agreement (judge PASS ↔ user positive, judge FAIL ↔ user negative)
      * a non-trivial chunk of judge-PASS / user-negative disagreement
        (the lenient-judge case worth surfacing)
    """
    from verdict.schema import (
        DimensionScore,
        Judgment,
        Trace,
        UserSignalRecord,
        Verdict,
    )
    from verdict.storage.memory import InMemoryStorage

    storage = InMemoryStorage()
    cluster = "c0001"
    now = datetime.now(timezone.utc)

    def _add(idx: int, dim_verdicts: list[Verdict], signal_kind: str,
             prompt: str, response: str) -> None:
        tid = f"trace-{idx:03d}"
        storage.insert_trace(Trace(
            trace_id=tid,
            cluster_id=cluster,
            request_model="demo-model",
            response_model="demo-model",
            prompt_redacted=prompt,
            response_redacted=response,
            started_at=now,
        ))
        storage.insert_judgment(Judgment(
            trace_id=tid,
            dimensions=[
                DimensionScore(name=f"dim{k}", verdict=v, judge_model="demo-judge")
                for k, v in enumerate(dim_verdicts)
            ],
        ))
        storage.insert_user_signal(UserSignalRecord(trace_id=tid, kind=signal_kind))

    P, F, U = Verdict.PASS, Verdict.FAIL, Verdict.UNCLEAR
    idx = 0

    # 1) Agreement: judge PASS, user positive (thumbs_up). 18 cases.
    for _ in range(18):
        _add(idx, [P, P, P], "thumbs_up",
             "How do I reset my password?",
             "Go to Settings → Security → Reset Password and follow the steps.")
        idx += 1

    # 2) Agreement: judge FAIL, user negative (thumbs_down). 12 cases.
    for _ in range(12):
        _add(idx, [F, F, P], "thumbs_down",
             "What's my account balance?",
             "I'm not able to access your account information.")
        idx += 1

    # 3) Disagreement: judge PASS, user negative (regenerate/abandon). 8 cases.
    #    The judge calls it good; users reject it. This is the signal worth
    #    surfacing when judge behavior is too lenient for a workload.
    for k in range(8):
        _add(idx, [P, P, U], "regenerate" if k % 2 == 0 else "abandon",
             "Draft a polite follow-up email to a client.",
             "Here is an email. (Tone is too formal for this customer.)")
        idx += 1

    # 4) Disagreement: judge FAIL, user positive (copy/accept). 3 cases.
    for k in range(3):
        _add(idx, [F, P, F], "copy" if k % 2 == 0 else "accept",
             "Give me a quick bash one-liner to count files.",
             "ls -1 | wc -l")
        idx += 1

    # 5) Some signals with NO usable label (skipped by the correlator).
    for _ in range(4):
        _add(idx, [P, P, P], "follow_up_question",
             "Explain recursion.",
             "Recursion is when a function calls itself.")
        idx += 1

    # 6) A user signal whose trace has NO judgment (skipped by the runner join).
    storage.insert_trace(Trace(
        trace_id="trace-unjudged",
        cluster_id=cluster,
        prompt_redacted="orphan", response_redacted="orphan", started_at=now,
    ))
    storage.insert_user_signal(UserSignalRecord(trace_id="trace-unjudged", kind="thumbs_up"))

    return storage


# --------------------------------------------------------------------------- #
# Report printing
# --------------------------------------------------------------------------- #

def _print_report(report, join_stats: dict) -> None:
    print("\n=== User-signal correlation report ===")
    print(f"Signals in storage:        {join_stats['signals_total']}")
    print(f"  skipped (no judgment):   {join_stats['skipped_no_judgment']}")
    print(f"  skipped (no verdict):    {join_stats['skipped_no_verdict']}")
    print(f"  pairs built:             {join_stats['pairs_built']}")
    print(f"  skipped (no usable label): {report.n_skipped_no_label}")
    print(f"Usable label pairs:        {report.n_pairs}")
    print()
    print("Confusion matrix (judge × user):")
    print(f"  judge PASS / user POS:   {report.judge_pos_user_pos}")
    print(f"  judge PASS / user NEG:   {report.judge_pos_user_neg}")
    print(f"  judge FAIL / user POS:   {report.judge_neg_user_pos}")
    print(f"  judge FAIL / user NEG:   {report.judge_neg_user_neg}")
    print()
    print(f"Raw agreement:             {report.raw_agreement:.3f}")
    print(f"Cohen's kappa:             {report.cohens_kappa:.3f}")
    print(f"Gwet's AC2:                {report.gwet_ac2:.3f}")
    print(f"Judge positive rate:       {report.judge_positive_rate:.3f}")
    print(f"User positive rate:        {report.user_positive_rate:.3f}")

    if report.examples_judge_pass_user_neg:
        print("\nDisagreement — judge PASS but user rejected:")
        for ex in report.examples_judge_pass_user_neg:
            print(f"  [{ex.trace_id}] signal={ex.user_signal}")
            if ex.prompt_preview:
                print(f"      prompt:   {ex.prompt_preview}")
            if ex.response_preview:
                print(f"      response: {ex.response_preview}")

    if report.examples_judge_fail_user_pos:
        print("\nDisagreement — judge FAIL but user accepted:")
        for ex in report.examples_judge_fail_user_pos:
            print(f"  [{ex.trace_id}] signal={ex.user_signal}")
            if ex.prompt_preview:
                print(f"      prompt:   {ex.prompt_preview}")
            if ex.response_preview:
                print(f"      response: {ex.response_preview}")

    print(f"\nInterpretation:\n  {report.interpretation}")


def main() -> int:
    p = argparse.ArgumentParser(description="User-signal correlator runner.")
    p.add_argument("--storage", default="sqlite:///./verdict.db",
                   help="Storage URL (sqlite/memory/postgres). Ignored with --demo.")
    p.add_argument("--demo", action="store_true",
                   help="Seed an offline InMemoryStorage with synthetic data "
                        "and run the report. Zero setup, deterministic.")
    p.add_argument("--max-examples", type=int, default=5,
                   help="Max disagreement examples per category.")
    args = p.parse_args()

    if args.demo:
        print("Mode: --demo (synthetic InMemoryStorage)")
        storage = _seed_demo_storage()
    else:
        from verdict.client import _resolve_storage
        print(f"Storage: {args.storage}")
        storage = _resolve_storage(args.storage)

    pairs, join_stats = _build_pairs(storage)

    from verdict_eval.correlator import UserSignalCorrelator
    correlator = UserSignalCorrelator(max_examples_per_disagreement=args.max_examples)
    report = correlator.correlate(pairs)

    _print_report(report, join_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
