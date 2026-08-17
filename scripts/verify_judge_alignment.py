"""Judge-human pairwise alignment check on MT-Bench.

Uses PROPER pairwise judging (Arena-Hard methodology) with position-swap
consistency, not independent-rubric-then-compare. Use this as one validation
input, not as proof that a judge is calibrated for every workload.

Modes:
  --mode offline           : synthetic data path, verifies the harness wiring
  --mode online            : real lmsys/mt_bench_human_judgments

Usage:
    python scripts/verify_judge_alignment.py --mode offline

    python scripts/verify_judge_alignment.py --mode online \\
        --provider anthropic --judge-model claude-haiku-4-5 --n 100 \\
        --json-output alignment.json

    # Reproduce the old extraction for A/B comparison:
    python scripts/verify_judge_alignment.py --mode online \\
        --provider anthropic --judge-model claude-haiku-4-5 --n 100 \\
        --context-mode legacy

Interpretation (Landis & Koch 1977; see docs/adrs/002-judge-methodology.md):
    κ ≥ 0.80   strong agreement
    κ 0.60-0.80   acceptable — use rankings with CIs
    κ 0.40-0.60   preliminary — gather more data
    κ < 0.40    unreliable — do not rely on rankings for this judge
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))

MT_BENCH_DATASET = "lmsys/mt_bench_human_judgments"
MT_BENCH_DATASET_REVISION = "f7d2896d2cc5d80f8b55c2bbc722613555233c25"
# This floor only rejects degenerate reports; it is not a claim that 50 pairs
# guarantee a narrow interval. The CI gate below still decides usability.
MIN_ALIGNMENT_PAIRS = 50


def cohens_kappa(y_a: list[int], y_b: list[int], n_categories: int = 3) -> float:
    """Cohen's kappa for two annotators on `n_categories`-level categorical labels.

    Known issue: the "kappa paradox" — when both raters agree on most cases
    (high marginal pass rate), κ can be artificially deflated even with high
    raw agreement. This is exactly our situation with skewed PASS-heavy data.
    Use `gwets_ac2` alongside this for a less paradox-prone number.
    """
    if len(y_a) != len(y_b) or not y_a:
        return 0.0
    n = len(y_a)
    agree = sum(1 for a, b in zip(y_a, y_b, strict=True) if a == b)
    po = agree / n
    # Expected agreement: sum over categories of P(annot_a = c) * P(annot_b = c)
    pe = 0.0
    for c in range(n_categories):
        p_a_c = sum(1 for x in y_a if x == c) / n
        p_b_c = sum(1 for x in y_b if x == c) / n
        pe += p_a_c * p_b_c
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1.0 - pe)


def gwets_ac2(y_a: list[int], y_b: list[int], n_categories: int = 3) -> float:
    """Gwet's AC2 (2008) — agreement coefficient that fixes the kappa paradox.

    Unlike Cohen's κ, Gwet's AC2 doesn't penalize agreement when the marginal
    distribution is skewed (e.g. most things are PASS). For our use case —
    judges that pass most responses — AC2 is methodologically more
    appropriate than κ and gives a more honest picture of agreement.

    Reference: Gwet, K. L. (2008). "Computing inter-rater reliability and
    its variance in the presence of high agreement." British Journal of
    Mathematical and Statistical Psychology, 61(1), 29-48.

    Formula:
        AC2 = (P_o - P_e) / (1 - P_e)
    where:
        P_o = observed agreement
        P_e = chance agreement, computed differently than κ:
              P_e = Σ_c [π_c * (1 - π_c)] / (n_categories - 1)
              with π_c = (p_a_c + p_b_c) / 2  (averaged marginals)
    """
    if len(y_a) != len(y_b) or not y_a:
        return 0.0
    n = len(y_a)
    agree = sum(1 for a, b in zip(y_a, y_b, strict=True) if a == b)
    po = agree / n
    if n_categories < 2:
        return 1.0 if po == 1.0 else 0.0
    pe = 0.0
    for c in range(n_categories):
        p_a_c = sum(1 for x in y_a if x == c) / n
        p_b_c = sum(1 for x in y_b if x == c) / n
        pi_c = (p_a_c + p_b_c) / 2.0
        pe += pi_c * (1 - pi_c)
    pe = pe / (n_categories - 1)
    if pe >= 1.0:
        return 1.0
    return (po - pe) / (1.0 - pe)


def bootstrap_ci(pairs: list[tuple[int, int]], metric_fn, n_categories: int,
                 *, iters: int = 2000, seed: int = 0) -> tuple[float, float]:
    """95% bootstrap confidence interval for an agreement metric.

    Resamples the (human, judge) pairs WITH replacement `iters` times, recomputes
    the metric each time, and returns the 2.5th / 97.5th percentiles. A wide
    interval means the point estimate is noise — do not over-read it. The judge
    "clears" a threshold only if the CI LOWER bound is at/above it, not just the
    point estimate.
    """
    import random as _random
    n = len(pairs)
    if n < 2:
        return (0.0, 0.0)
    rng = _random.Random(seed)
    vals: list[float] = []
    for _ in range(iters):
        sample = [pairs[rng.randrange(n)] for _ in range(n)]
        a = [p[0] for p in sample]
        b = [p[1] for p in sample]
        vals.append(metric_fn(a, b, n_categories))
    vals.sort()
    lo = vals[max(0, int(0.025 * len(vals)))]
    hi = vals[min(len(vals) - 1, int(0.975 * len(vals)))]
    return (lo, hi)


def _ci_payload(ci: tuple[float, float] | None) -> list[float] | None:
    return list(ci) if ci is not None else None


def _format_ci(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "not computed"
    return f"{ci[0]:.3f}, {ci[1]:.3f}"


def _alignment_verdict(
    ac2_ci: tuple[float, float] | None,
) -> tuple[str, str, bool]:
    """Return the serialized status, explanation, and evidence-gate outcome."""
    if ac2_ci is None:
        return (
            "unreliable",
            "UNRELIABLE — no binarized confidence interval could be computed; "
            "do not rely on rankings.",
            False,
        )
    lo, hi = ac2_ci
    if lo >= 0.60:
        return (
            "acceptable",
            "ACCEPTABLE — CI lower bound ≥ 0.60; use rankings with CIs.",
            True,
        )
    if lo >= 0.40:
        return (
            "preliminary",
            "PRELIMINARY — CI lower bound ≥ 0.40 but < 0.60; gather more data.",
            True,
        )
    if hi < 0.40:
        return (
            "unreliable",
            "UNRELIABLE — even the CI UPPER bound is < 0.40; do not rely on rankings.",
            False,
        )
    return (
        "inconclusive",
        "INCONCLUSIVE — CI straddles 0.40; sample too small to tell. "
        "Increase --n before concluding.",
        False,
    )


def _binarized_ac2(pairs3: list[tuple[int, int]]) -> tuple[float, tuple[float, float], int]:
    """Binarized AC2 for a set of 3-way (human, judge) pairs.

    Drops ties (label 2) from BOTH sides, then computes 2-category AC2 plus its
    bootstrap CI. Returns (ac2, (ci_lo, ci_hi), n_binarized). n < 2 yields zeros.
    Used both for the overall headline and per-category breakdown so the two
    numbers are computed the same way.
    """
    bin_pairs = [(h, jl) for h, jl in pairs3 if h != 2 and jl != 2]
    if len(bin_pairs) < 2:
        return (0.0, (0.0, 0.0), len(bin_pairs))
    bin_h = [p[0] for p in bin_pairs]
    bin_j = [p[1] for p in bin_pairs]
    ac2 = gwets_ac2(bin_h, bin_j, n_categories=2)
    ci = bootstrap_ci(bin_pairs, gwets_ac2, 2)
    return (ac2, ci, len(bin_pairs))


def _tie_detection_stats(pairs3: list[tuple[int, int]]) -> dict:
    """Treat "is this a tie?" as a binary detection problem over ALL pairs.

    Positive class = human label is TIE (2). We report precision/recall/F1 of the
    JUDGE detecting HUMAN ties, plus the raw confusion counts:
      tp = human tie AND judge tie   (ties the judge caught)
      fp = judge tie BUT human non-tie (judge cried tie when humans had a winner)
      fn = human tie BUT judge non-tie (human ties the judge missed)
      tn = neither is a tie
    """
    tp = fp = fn = tn = 0
    for h, jl in pairs3:
        h_tie = (h == 2)
        j_tie = (jl == 2)
        if h_tie and j_tie:
            tp += 1
        elif j_tie and not h_tie:
            fp += 1
        elif h_tie and not j_tie:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
        "n_human_ties": tp + fn, "n_judge_ties": tp + fp,
    }


# MT-Bench category layout: question_ids 81..160, grouped in blocks of 10 in a
# fixed order (see the MT-Bench question file, lmsys/FastChat). We use this to
# recover a category when an example doesn't carry one explicitly.
_MT_BENCH_CATEGORIES = [
    "writing",       # 81-90
    "roleplay",      # 91-100
    "reasoning",     # 101-110
    "math",          # 111-120
    "coding",        # 121-130
    "extraction",    # 131-140
    "stem",          # 141-150
    "humanities",    # 151-160
]


def _mt_bench_category_from_qid(question_id: object) -> str | None:
    """Derive the MT-Bench category from a question_id (81..160), or None."""
    try:
        qid = int(question_id)
    except (TypeError, ValueError):
        return None
    if not (81 <= qid <= 160):
        return None
    return _MT_BENCH_CATEGORIES[(qid - 81) // 10]


def _example_category(ex: dict) -> str:
    """Best-effort category for an MT-Bench example.

    Prefers an explicit `category` field; else derives from `question_id`;
    else falls back to "unknown".
    """
    cat = ex.get("category")
    if cat:
        return str(cat)
    derived = _mt_bench_category_from_qid(ex.get("question_id"))
    if derived:
        return derived
    return "unknown"


# Label encoding for kappa: 0 = A wins, 1 = B wins, 2 = tie
def _encode_pairwise(verdict_str: str) -> int:
    from verdict_eval.pairwise import PairwiseVerdict
    if verdict_str == PairwiseVerdict.A_BETTER:
        return 0
    if verdict_str == PairwiseVerdict.B_BETTER:
        return 1
    return 2  # tie / inconsistent


def _encode_human(winner: str) -> int:
    if winner == "model_a":
        return 0
    if winner == "model_b":
        return 1
    return 2  # tie


def _message_role(msg: object, idx: int) -> str:
    if isinstance(msg, dict):
        raw_role = str(msg.get("role") or msg.get("from") or "").lower()
        if raw_role in {"user", "human"}:
            return "user"
        if raw_role in {"assistant", "gpt", "model"}:
            return "assistant"
    return "user" if idx % 2 == 0 else "assistant"


def _message_content(msg: object) -> str:
    if isinstance(msg, dict):
        for key in ("content", "value", "text"):
            val = msg.get(key)
            if val is not None:
                return str(val).strip()
        return ""
    return str(msg).strip()


def _split_turns(conversation: object) -> tuple[list[str], list[str]]:
    if not isinstance(conversation, list):
        raise ValueError("conversation is not a list")

    user_turns: list[str] = []
    assistant_turns: list[str] = []
    for idx, msg in enumerate(conversation):
        content = _message_content(msg)
        if not content:
            continue
        role = _message_role(msg, idx)
        if role == "user":
            user_turns.append(content)
        elif role == "assistant":
            assistant_turns.append(content)

    if not user_turns or not assistant_turns:
        raise ValueError("conversation is missing user or assistant turns")
    return user_turns, assistant_turns


def _render_turns(label: str, turns: list[str]) -> str:
    return "\n\n".join(
        f"{label} turn {idx}:\n{turn}"
        for idx, turn in enumerate(turns, start=1)
    )


def _same_turns(left: list[str], right: list[str]) -> bool:
    return [x.strip() for x in left] == [x.strip() for x in right]


def _build_full_context_pair(ex: dict) -> tuple[str, str, str]:
    """Render the whole MT-Bench multi-turn transcript for pairwise judging.

    The previous harness used the first user message plus the final assistant
    answer. On two-turn MT-Bench examples that asks the judge to evaluate a
    turn-2 answer against a turn-1 question, which depresses agreement and
    inflates position-swap inconsistency.
    """
    users_a, assistants_a = _split_turns(ex["conversation_a"])
    users_b, assistants_b = _split_turns(ex["conversation_b"])
    if not _same_turns(users_a, users_b):
        raise ValueError("conversation_a and conversation_b have different user turns")

    query = (
        "Compare the two assistants over the full multi-turn conversation. "
        "The user requests, in order, were:\n\n"
        f"{_render_turns('User', users_a)}"
    )
    response_a = _render_turns("Assistant A response", assistants_a)
    response_b = _render_turns("Assistant B response", assistants_b)
    return query, response_a, response_b


def _build_legacy_pair(ex: dict) -> tuple[str, str, str]:
    """Reproduce the original first-user/final-answer extraction exactly."""
    return (
        _message_content(ex["conversation_a"][0]),
        _message_content(ex["conversation_a"][-1]),
        _message_content(ex["conversation_b"][-1]),
    )


def _write_json_report(path: str | None, report: dict) -> None:
    """Atomically persist the stable machine-readable alignment report."""
    if path is None:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _report_payload(
    args: argparse.Namespace,
    *,
    mode: str,
    pairs_available: int,
    pairs_scored: int,
    inconsistent_count: int,
    agree3: float,
    kappa3: float,
    ac2_3: float,
    ac2_3_ci: tuple[float, float] | None,
    bin_pairs: list[tuple[int, int]],
    agree_bin: float,
    kappa_bin: float,
    kappa_bin_ci: tuple[float, float] | None,
    ac2_bin: float,
    ac2_bin_ci: tuple[float, float] | None,
    human_nontie_count: int,
    agree_human_nontie: float,
    verdict_status: str,
    verdict_message: str,
) -> dict:
    return {
        "schemaVersion": 1,
        "mode": mode,
        "dataset": (
            {
                "name": MT_BENCH_DATASET,
                "revision": MT_BENCH_DATASET_REVISION,
            }
            if mode == "online"
            else {"name": "synthetic", "revision": None}
        ),
        "judge": {
            "provider": args.provider,
            "model": args.judge_model,
        },
        "contextMode": args.context_mode,
        "pairs": {
            "available": pairs_available,
            "scored": pairs_scored,
        },
        "verdict": {
            "status": verdict_status,
            "message": verdict_message,
        },
        "metrics": {
            "threeWay": {
                "rawAgreement": agree3,
                "cohensKappa": kappa3,
                "gwetsAc2": ac2_3,
                "gwetsAc2Ci95": _ci_payload(ac2_3_ci),
            },
            "binarized": {
                "pairsKept": len(bin_pairs),
                "rawAgreement": agree_bin,
                "cohensKappa": kappa_bin,
                "cohensKappaCi95": _ci_payload(kappa_bin_ci),
                "gwetsAc2": ac2_bin,
                "gwetsAc2Ci95": _ci_payload(ac2_bin_ci),
            },
            "nonTiePairsKept": human_nontie_count,
            "nonTieAgreement": agree_human_nontie,
            "inconsistentCount": inconsistent_count,
        },
    }


def run_offline(args: argparse.Namespace) -> int:
    """Run with synthetic data to verify the harness end-to-end.

    The human labels are a realistic MIX of A/B/tie (not a degenerate all-one-
    class set — with zero human variance, κ is mathematically forced to 0
    regardless of agreement, which makes the wiring check meaningless). A noisy
    judge agrees ~80% of the time, so both κ and AC2 land in a sane range and
    the harness is actually exercised.
    """
    import random
    rng = random.Random(7)
    print("Offline mode — synthetic 120-pair MT-Bench-like sample (mixed labels).")
    # Realistic human distribution: ~45% A, ~40% B, ~15% tie.
    human = [(0 if rng.random() < 0.45 else (1 if rng.random() < 0.70 else 2)) for _ in range(120)]
    # Judge agrees ~80%; when it disagrees it picks one of the other two labels.
    judge = []
    for h in human:
        if rng.random() < 0.80:
            judge.append(h)
        else:
            judge.append(rng.choice([c for c in (0, 1, 2) if c != h]))
    agreement = sum(1 for h, j in zip(human, judge, strict=True) if h == j) / len(human)
    kappa = cohens_kappa(human, judge, n_categories=3)
    ac2 = gwets_ac2(human, judge, n_categories=3)
    pairs3 = list(zip(human, judge, strict=True))
    ac2_ci = bootstrap_ci(pairs3, gwets_ac2, 3)
    bin_pairs = [(h, jl) for h, jl in pairs3 if h != 2 and jl != 2]
    bin_h = [pair[0] for pair in bin_pairs]
    bin_j = [pair[1] for pair in bin_pairs]
    agree_bin = sum(1 for h, jl in bin_pairs if h == jl) / len(bin_pairs)
    kappa_bin = cohens_kappa(bin_h, bin_j, n_categories=2)
    ac2_bin = gwets_ac2(bin_h, bin_j, n_categories=2)
    kappa_bin_ci = bootstrap_ci(bin_pairs, cohens_kappa, 2)
    ac2_bin_ci = bootstrap_ci(bin_pairs, gwets_ac2, 2)
    human_nontie = [(h, jl) for h, jl in pairs3 if h != 2]
    agree_human_nontie = (
        sum(1 for h, jl in human_nontie if h == jl) / len(human_nontie)
    )
    print(f"  n={len(human)}  agreement={agreement:.3f}  Cohen's κ={kappa:.3f}  Gwet's AC2={ac2:.3f}")
    if not (0.4 <= kappa <= 0.9 and 0.4 <= ac2 <= 0.95):
        print("  WARNING: offline κ/AC2 outside expected band — harness may be miswired.")
        return 1
    print("  Harness wiring OK (κ and AC2 both in the expected band for ~80% agreement).")
    print("  NOTE: this is a WIRING check only. The real judge-vs-human number")
    print("  comes from `--mode online` against lmsys/mt_bench_human_judgments.")
    _write_json_report(
        args.json_output,
        _report_payload(
            args,
            mode="offline",
            pairs_available=len(pairs3),
            pairs_scored=len(pairs3),
            inconsistent_count=0,
            agree3=agreement,
            kappa3=kappa,
            ac2_3=ac2,
            ac2_3_ci=ac2_ci,
            bin_pairs=bin_pairs,
            agree_bin=agree_bin,
            kappa_bin=kappa_bin,
            kappa_bin_ci=kappa_bin_ci,
            ac2_bin=ac2_bin,
            ac2_bin_ci=ac2_bin_ci,
            human_nontie_count=len(human_nontie),
            agree_human_nontie=agree_human_nontie,
            verdict_status="synthetic",
            verdict_message=(
                "SYNTHETIC WIRING ONLY — no provider judge or public dataset was used."
            ),
        ),
    )
    return 0


def run_online(args: argparse.Namespace) -> int:
    try:
        from datasets import load_dataset
    except ImportError:
        print("Install the dataset loader:  uv pip install datasets")
        return 1

    from verdict_eval.pairwise import (
        PairwiseJudge,
        PairwiseJudgeEnsemble,
        PairwiseVerdict,
    )
    from verdict_eval.providers import (
        AnthropicAdapter,
        GoogleAdapter,
        OpenAIAdapter,
    )

    print(f"Loading {MT_BENCH_DATASET} @ {MT_BENCH_DATASET_REVISION}...")
    ds = load_dataset(
        MT_BENCH_DATASET,
        split="human",
        revision=MT_BENCH_DATASET_REVISION,
    )
    ds = ds.shuffle(seed=42).select(range(min(args.n, len(ds))))

    # Build the judge (or judge ensemble)
    def make_provider(name: str):
        if name == "anthropic":
            return AnthropicAdapter()
        if name == "openai":
            return OpenAIAdapter()
        if name == "google":
            return GoogleAdapter()
        raise ValueError(f"unknown provider {name}")

    judge_specs = [(args.provider, args.judge_model)]
    if args.ensemble:
        # Cross-family ensemble — never include same family as compared models
        # (MT-Bench compared models can be either, so we pick judges that aren't
        #  the same as `--provider`).
        extras = []
        if args.provider != "anthropic":
            extras.append(("anthropic", "claude-haiku-4-5"))
        if args.provider != "openai":
            extras.append(("openai", "gpt-4o-mini"))
        if args.provider != "google":
            extras.append(("google", "gemini-2.5-flash"))
        judge_specs.extend(extras[:2])

    judges = []
    for prov_name, model_name in judge_specs:
        try:
            provider = make_provider(prov_name)
            judges.append(PairwiseJudge(provider=provider, model=model_name))
            print(f"  Judge: {prov_name} :: {model_name}")
        except Exception as e:
            print(f"  Skipping {prov_name} judge: {e}")
    if not judges:
        print("No judges available.")
        return 1

    judge = PairwiseJudgeEnsemble(judges) if len(judges) > 1 else judges[0]
    context_desc = (
        "full multi-turn transcript"
        if args.context_mode == "full"
        else "legacy first-user/final-answer extraction"
    )
    print(f"  Context mode: {args.context_mode} ({context_desc})")
    print(f"\nEvaluating {len(ds)} pairs with pairwise + position-swap...\n")

    n_total = 0
    n_used = 0
    n_inconsistent = 0
    n_component_votes = 0
    n_component_inconsistent = 0
    human_labels: list[int] = []
    judge_labels: list[int] = []
    categories: list[str] = []

    for ex in ds:
        n_total += 1
        winner = ex.get("winner")
        if winner not in {"model_a", "model_b", "tie"}:
            continue

        try:
            if args.context_mode == "full":
                q, resp_a, resp_b = _build_full_context_pair(ex)
            else:
                q, resp_a, resp_b = _build_legacy_pair(ex)
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        try:
            j = judge.compare(query=q, response_a=resp_a, response_b=resp_b)
        except Exception as e:
            print(f"  judge error: {e}")
            continue

        if j.verdict == PairwiseVerdict.INCONSISTENT:
            n_inconsistent += 1
            # Treat as tie for kappa — but still counted as a sample
        component_judgments = getattr(j, "component_judgments", [])
        if component_judgments:
            n_component_votes += len(component_judgments)
            n_component_inconsistent += sum(
                1 for vote in component_judgments
                if vote.verdict == PairwiseVerdict.INCONSISTENT
            )
        n_used += 1
        human_labels.append(_encode_human(winner))
        judge_labels.append(_encode_pairwise(j.verdict.value))
        categories.append(_example_category(ex))

        # Live progress every 10
        if n_used % 10 == 0:
            agree_so_far = sum(1 for h, jl in zip(human_labels, judge_labels, strict=True) if h == jl) / len(human_labels)
            print(f"  {n_used}/{len(ds)} pairs scored  agree={agree_so_far:.3f}")

    if n_used != len(ds):
        print(
            f"Incomplete run: scored {n_used} of {len(ds)} selected pairs. "
            "Judge errors or unusable rows make this evidence invalid."
        )
        return 1

    if not human_labels:
        print("No usable comparisons.")
        return 1

    # 3-way kappa (includes ties as a real category)
    pairs3 = list(zip(human_labels, judge_labels, strict=True))
    kappa3 = cohens_kappa(human_labels, judge_labels, n_categories=3)
    ac2_3 = gwets_ac2(human_labels, judge_labels, n_categories=3)
    agree3 = sum(1 for h, jl in pairs3 if h == jl) / len(human_labels)
    ac2_3_ci = bootstrap_ci(pairs3, gwets_ac2, 3)

    # Binarized kappa (Arena-Hard / MT-Bench standard: drop ties from BOTH sides
    # before computing kappa). This is the number most papers publish.
    bin_pairs = [(h, jl) for h, jl in pairs3 if h != 2 and jl != 2]
    if bin_pairs:
        bin_h, bin_j = zip(*bin_pairs, strict=True)
        kappa_bin = cohens_kappa(list(bin_h), list(bin_j), n_categories=2)
        ac2_bin = gwets_ac2(list(bin_h), list(bin_j), n_categories=2)
        agree_bin = sum(1 for h, jl in bin_pairs if h == jl) / len(bin_pairs)
        ac2_bin_ci = bootstrap_ci(bin_pairs, gwets_ac2, 2)
        kappa_bin_ci = bootstrap_ci(bin_pairs, cohens_kappa, 2)
    else:
        kappa_bin = 0.0
        ac2_bin = 0.0
        agree_bin = 0.0
        ac2_bin_ci = None
        kappa_bin_ci = None

    # Non-tie agreement rate (drop only HUMAN ties; treat judge ties as wrong):
    # this is what some practitioners report — "of the cases where humans had
    # a clear winner, how often did the judge agree?"
    human_nontie = [(h, jl) for h, jl in zip(human_labels, judge_labels, strict=True) if h != 2]
    if human_nontie:
        agree_human_nontie = sum(1 for h, jl in human_nontie if h == jl) / len(human_nontie)
    else:
        agree_human_nontie = 0.0

    # B-ALIGN-5: tie handling as a first-class binary detection problem.
    tie_stats = _tie_detection_stats(pairs3)

    # B-ALIGN-4: per-category binarized AC2 breakdown. Group scored pairs by the
    # category captured during scoring, binarize within each category, and keep
    # categories with >= 5 binarized pairs. Offline mode never populates
    # `categories`, so this stays empty there (per-category is online-only).
    per_category: list[dict] = []
    skipped_categories: list[tuple[str, int]] = []
    if categories:
        by_cat: dict[str, list[tuple[int, int]]] = {}
        for cat, h, jl in zip(categories, human_labels, judge_labels, strict=True):
            by_cat.setdefault(cat, []).append((h, jl))
        for cat, cat_pairs in by_cat.items():
            ac2, ci, n_bin = _binarized_ac2(cat_pairs)
            if n_bin < 5:
                skipped_categories.append((cat, n_bin))
            else:
                per_category.append({
                    "category": cat, "ac2": ac2, "ci": ci,
                    "n_bin": n_bin, "n_total": len(cat_pairs),
                })
        per_category.sort(key=lambda r: r["n_bin"], reverse=True)
        skipped_categories.sort(key=lambda r: r[1], reverse=True)

    # Breakdown of judge verdicts vs human verdicts
    matrix: dict[tuple[int, int], int] = {}
    for h, jl in zip(human_labels, judge_labels, strict=True):
        matrix[(h, jl)] = matrix.get((h, jl), 0) + 1
    label = {0: "A", 1: "B", 2: "T"}
    print()
    print("Confusion matrix (rows = human, cols = judge):")
    print("          judge:A   judge:B   judge:T")
    for h in range(3):
        row = f"  human:{label[h]}   "
        for jl in range(3):
            row += f"  {matrix.get((h, jl), 0):>5d}   "
        print(row)

    # Honest verdict: a threshold is "cleared" only if the CI LOWER bound clears
    # it — not the point estimate. With small n the interval is wide on purpose.
    verdict_status, verdict, verdict_passed = _alignment_verdict(ac2_bin_ci)

    print(textwrap.dedent(f"""
        ─ Results ───────────────────────────────────────
        Context mode:           {args.context_mode} ({context_desc})
        Pairs available:        {n_total}
        Pairs scored:           {n_used}
        Inconsistent (swap):    {n_inconsistent} ({100*n_inconsistent/max(1,n_used):.1f}%)
        Component inconsistent: {n_component_inconsistent} / {n_component_votes}
                                ({100*n_component_inconsistent/max(1,n_component_votes):.1f}% of individual judge votes)

        3-way agreement (A/B/Tie):
          Raw agreement:        {agree3:.3f}
          Cohen's κ:            {kappa3:.3f}   (paradox-vulnerable on skewed marginals)
          Gwet's AC2:           {ac2_3:.3f}   [95% CI {ac2_3_ci[0]:.3f}, {ac2_3_ci[1]:.3f}]

        Binarized (Arena-Hard style — ties dropped from BOTH sides):
          Pairs kept:           {len(bin_pairs)}
          Raw agreement:        {agree_bin:.3f}
          Cohen's κ:            {kappa_bin:.3f}   [95% CI {_format_ci(kappa_bin_ci)}]
          Gwet's AC2:           {ac2_bin:.3f}   [95% CI {_format_ci(ac2_bin_ci)}]   ← headline

        Non-tie agreement (humans had clear winner, judge agreed):
          Pairs kept:           {len(human_nontie)}
          Agreement:            {agree_human_nontie:.3f}

        VERDICT (read the CI, not the dot):
          {verdict}

        Interpretation (Landis & Koch thresholds; apply to the CI LOWER bound):
          ≥ 0.80    strong
          0.60-0.80 acceptable — use rankings with CIs
          0.40-0.60 preliminary — gather more data
          < 0.40    unreliable — do not rely on rankings

        Honesty notes:
          - This is ONE public benchmark (MT-Bench), a generic worst case, not
            your workload-specific number. The useful claim needs your held-out,
            human-labeled task data.
          - This measures PAIRWISE-RANKING agreement — a harder, more subjective
            task than the binary PASS/FAIL rubric the drift detector uses. A weak
            number here does not by itself condemn the binary-rubric path.
          - "Cleared the bar" = CI lower bound cleared it. A point estimate from
            a small sample is noise.
        ──────────────────────────────────────────────────
    """).strip())

    # ── B-ALIGN-5: Tie handling ─────────────────────────────────────────────
    # Report the three tie-related views separately instead of folding ties away.
    print(textwrap.dedent(f"""
        ─ Tie handling (B-ALIGN-5) ───────────────────────
        Definition: positive class = HUMAN label is TIE. "Tie-detection" scores
        the judge as a detector of human ties over ALL {n_used} scored pairs.

        1) Clear-winner agreement (human picked A or B, judge matched):
             Pairs kept:          {len(human_nontie)}
             Agreement:           {agree_human_nontie:.3f}
             (same number as "Non-tie agreement" in Results, surfaced here)

        2) Tie-detection (human_is_tie vs judge_is_tie, over ALL pairs):
             Human ties:          {tie_stats['n_human_ties']}
             Judge ties:          {tie_stats['n_judge_ties']}
             Caught (tp):         {tie_stats['tp']}   human tie AND judge tie
             Missed (fn):         {tie_stats['fn']}   human tie BUT judge picked a winner
             False ties (fp):     {tie_stats['fp']}   judge tie BUT human picked a winner
             Neither (tn):        {tie_stats['tn']}
             Precision:           {tie_stats['precision']:.3f}   (of judge ties, share that were human ties)
             Recall:              {tie_stats['recall']:.3f}   (of human ties, share the judge caught)
             F1:                  {tie_stats['f1']:.3f}

        3) Binarized AC2 (ties dropped from both sides): {ac2_bin:.3f}
             [95% CI {_format_ci(ac2_bin_ci)}]  — see Results block above.
        ──────────────────────────────────────────────────
    """).strip())

    # ── B-ALIGN-4: Per-category binarized AC2 ───────────────────────────────
    print()
    print("─ Per-category (binarized AC2) (B-ALIGN-4) ───────")
    if not categories:
        print("  No category information on these examples — skipped.")
    elif not per_category and not skipped_categories:
        print("  No categories captured.")
    else:
        print(f"  {'category':<14}{'AC2':>7}{'95% CI':>18}{'n_bin':>7}{'n_all':>7}")
        for row in per_category:
            lo, hi = row["ci"]
            ci_str = f"[{lo:.2f},{hi:.2f}]"
            print(f"  {row['category']:<14}{row['ac2']:>7.3f}{ci_str:>18}"
                  f"{row['n_bin']:>7}{row['n_total']:>7}")
        for cat, n_bin in skipped_categories:
            print(f"  {cat:<14}{'—':>7}{'n<5, skipped':>18}{n_bin:>7}")
        print()
        print("  Honesty note: per-category n is usually small, so these CIs are")
        print("  WIDE — do not read a single category's point estimate as a verdict.")
        print("  Categories with < 5 binarized pairs are skipped, not zero-scored.")
    print("──────────────────────────────────────────────────")
    _write_json_report(
        args.json_output,
        _report_payload(
            args,
            mode="online",
            pairs_available=n_total,
            pairs_scored=n_used,
            inconsistent_count=n_inconsistent,
            agree3=agree3,
            kappa3=kappa3,
            ac2_3=ac2_3,
            ac2_3_ci=ac2_3_ci,
            bin_pairs=bin_pairs,
            agree_bin=agree_bin,
            kappa_bin=kappa_bin,
            kappa_bin_ci=kappa_bin_ci,
            ac2_bin=ac2_bin,
            ac2_bin_ci=ac2_bin_ci,
            human_nontie_count=len(human_nontie),
            agree_human_nontie=agree_human_nontie,
            verdict_status=verdict_status,
            verdict_message=verdict,
        ),
    )
    return 0 if verdict_passed else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["offline", "online"], default="offline")
    p.add_argument("--provider", choices=["anthropic", "openai", "google"], default="anthropic")
    p.add_argument("--judge-model", default="claude-haiku-4-5")
    p.add_argument("--n", type=int, default=100)
    p.add_argument("--context-mode", choices=["full", "legacy"], default="full",
                   help=("How to render MT-Bench examples. 'full' uses all user and "
                         "assistant turns; 'legacy' reproduces the old first-user/"
                         "final-answer extraction for comparison."))
    p.add_argument("--ensemble", action="store_true",
                   help="Use a 3-judge cross-family ensemble (more accurate, ~3x cost).")
    p.add_argument(
        "--json-output",
        help="Write a stable machine-readable result to this path on success.",
    )
    args = p.parse_args()
    if args.mode == "online" and args.n < MIN_ALIGNMENT_PAIRS:
        p.error(
            f"--n must be at least {MIN_ALIGNMENT_PAIRS} in online mode; "
            "smaller samples produce degenerate or unusably wide intervals"
        )
    if args.mode == "offline":
        return run_offline(args)
    return run_online(args)


if __name__ == "__main__":
    sys.exit(main())
