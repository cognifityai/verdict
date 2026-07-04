"""Binary-rubric alignment — judge PASS/FAIL vs YOUR PASS/FAIL (the drift path).

This measures the number that actually decides Verdict's CORE product. The
drift detector runs the judge in BINARY mode: for one response, does each rubric
dimension PASS or FAIL? That is a different, easier, more objective task than the
pairwise MT-Bench RANKING measured by verify_judge_alignment.py. A weak ranking
number does NOT condemn this path — you must measure it directly.

It answers: on a set of real (query, response) examples that YOU labeled PASS/FAIL
per dimension, how often does the judge's PASS/FAIL agree with yours? Reported as
per-dimension and pooled Cohen's κ / Gwet's AC2 with 95% bootstrap CIs.

Why the bar here can be lower than "κ ≥ 0.6 vs humans": drift measures CHANGE vs a
baseline, so a judge that is consistently biased but STABLE still detects a real
regression (the baseline absorbs the bias). Absolute agreement still matters, but
consistency matters as much.

--------------------------------------------------------------------------------
LABELED DATA FORMAT (JSONL, one example per line):
    {"query": "...", "response": "...", "context": "optional retrieved context",
     "labels": {"groundedness": "PASS", "relevance": "FAIL", "completeness": "PASS",
                "safety": "PASS", "instruction_following": null}}
  - Use "PASS" / "FAIL" for dimensions you judged; null or omit ones you didn't.
  - "context" is optional (needed for a fair groundedness judgment).

USAGE:
    # 1. make a labeling template from raw (query, response) examples:
    python scripts/verify_rubric_alignment.py --make-template raw.jsonl labels.jsonl
    #    raw.jsonl lines: {"query": "...", "response": "...", "context": "..."}
    #    then fill in the "labels" PASS/FAIL yourself.

    # 2. offline wiring check (no API):
    python scripts/verify_rubric_alignment.py --offline

    # 3. score your labels against a real judge:
    python scripts/verify_rubric_alignment.py --labeled labels.jsonl \
        --provider anthropic --judge-model claude-haiku-4-5-20251001
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))

# Reuse the tested metric functions from the ranking harness (single source).
from verify_judge_alignment import bootstrap_ci, cohens_kappa, gwets_ac2  # noqa: E402

DIMENSIONS = ["groundedness", "relevance", "completeness", "safety", "instruction_following"]


def _bit(label) -> int | None:
    """PASS -> 1, FAIL -> 0, everything else (UNCLEAR/None/missing) -> None."""
    if label is None:
        return None
    s = str(getattr(label, "value", label)).upper().strip()
    if s == "PASS":
        return 1
    if s == "FAIL":
        return 0
    return None


def _load_labeled(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def make_template(in_path: str, out_path: str) -> int:
    raw = _load_labeled(in_path)
    with open(out_path, "w") as f:
        for ex in raw:
            f.write(json.dumps({
                "query": ex.get("query", ""),
                "response": ex.get("response", ""),
                "context": ex.get("context", ""),
                "labels": {d: None for d in DIMENSIONS},
            }) + "\n")
    print(f"Wrote {len(raw)} rows to {out_path}. Fill in each 'labels' value with "
          "PASS or FAIL (leave null to skip a dimension), then run --labeled.")
    return 0


def _report(per_dim_pairs: dict[str, list[tuple[int, int]]]) -> int:
    """per_dim_pairs: dimension -> list of (human_bit, judge_bit). Print report."""
    pooled: list[tuple[int, int]] = []
    for pairs in per_dim_pairs.values():
        pooled.extend(pairs)

    if not pooled:
        print("No comparable (human PASS/FAIL vs judge PASS/FAIL) pairs. Did you "
              "fill in labels, and did the judge return PASS/FAIL?")
        return 1

    def _line(name: str, pairs: list[tuple[int, int]]) -> str:
        n = len(pairs)
        if n == 0:
            return f"  {name:22s}  n=0   (no labeled pairs)"
        h = [p[0] for p in pairs]
        j = [p[1] for p in pairs]
        agree = sum(1 for a, b in pairs if a == b) / n
        ac2 = gwets_ac2(h, j, 2)
        lo, hi = bootstrap_ci(pairs, gwets_ac2, 2)
        return (f"  {name:22s}  n={n:<4d}  agree={agree:.3f}  "
                f"AC2={ac2:.3f} [95% CI {lo:.3f}, {hi:.3f}]")

    print("\nPer-dimension agreement (judge PASS/FAIL vs your PASS/FAIL):")
    for dim in DIMENSIONS:
        if dim in per_dim_pairs:
            print(_line(dim, per_dim_pairs[dim]))

    print("\nPooled (all dimensions):")
    print(_line("ALL", pooled))

    # Pooled confusion + verdict off the pooled AC2 CI lower bound.
    tp = sum(1 for a, b in pooled if a == 1 and b == 1)
    tn = sum(1 for a, b in pooled if a == 0 and b == 0)
    fp = sum(1 for a, b in pooled if a == 0 and b == 1)   # judge PASS, you FAIL
    fn = sum(1 for a, b in pooled if a == 1 and b == 0)   # judge FAIL, you PASS
    print("\n  Pooled confusion (rows = you, cols = judge):")
    print("             judge:PASS  judge:FAIL")
    print(f"    you:PASS   {tp:>7d}     {fn:>7d}")
    print(f"    you:FAIL   {fp:>7d}     {tn:>7d}")

    lo, hi = bootstrap_ci(pooled, gwets_ac2, 2)
    if lo >= 0.60:
        verdict = "STRONG — CI lower bound ≥ 0.60. Trustworthy binary signal."
    elif lo >= 0.40:
        verdict = "USABLE for drift — CI lower bound ≥ 0.40. Consistent enough to detect change."
    elif hi < 0.40:
        verdict = "WEAK — even the CI upper bound < 0.40. The binary judge needs work."
    else:
        verdict = "INCONCLUSIVE — CI straddles 0.40; too few labels. Label more examples."

    print(f"\nVERDICT (pooled, read the CI not the dot):\n  {verdict}")
    print(textwrap_notes())
    return 0 if lo >= 0.40 else 1


def textwrap_notes() -> str:
    import textwrap
    return textwrap.dedent("""
        Honesty notes:
          - This is the number that matters for DRIFT (binary rubric), unlike the
            pairwise-ranking number in verify_judge_alignment.py.
          - Measure it on YOUR data. A public number would not be your number.
          - Small label sets give wide CIs. "Cleared the bar" = CI lower bound
            cleared it, not the point estimate. Aim for >= ~50-100 labeled pairs
            per dimension for a tight interval.
          - For drift specifically, consistency matters as much as absolute
            agreement; even a CI lower bound in the 0.40s can power useful drift
            detection because the baseline absorbs steady bias.""").rstrip()


def run_offline() -> int:
    """Wiring check: run the REAL Judge with a FakeProvider over synthetic labeled
    examples, plus a controlled metric sanity check. No API, no real numbers."""
    import random
    from verdict_eval.judge import DEFAULT_RUBRIC, Judge
    from verdict_eval.providers import FakeProvider

    print("Offline wiring check (FakeProvider judge — numbers are NOT meaningful).")

    # 1. Exercise the real Judge -> parse -> compare pipeline on 3 examples.
    fake_json = json.dumps({d.name: {"reasoning": "x", "verdict": "PASS"}
                            for d in DEFAULT_RUBRIC.dimensions})
    judge = Judge(provider=FakeProvider(fake_json), model="fake", rubric=DEFAULT_RUBRIC)
    examples = [
        {"query": "What is 2+2?", "response": "4",
         "labels": {"relevance": "PASS", "completeness": "PASS"}},
        {"query": "Return JSON", "response": "not json",
         "labels": {"instruction_following": "FAIL"}},
        {"query": "Capital of France?", "response": "Paris",
         "labels": {"relevance": "PASS", "safety": "PASS"}},
    ]
    per_dim: dict[str, list[tuple[int, int]]] = {}
    for ex in examples:
        j = judge.judge(query=ex["query"], response=ex["response"],
                        context=ex.get("context") or None)
        jv = {d.name: d.verdict for d in j.dimensions}
        for dim, human in ex["labels"].items():
            hb, jb = _bit(human), _bit(jv.get(dim))
            if hb is not None and jb is not None:
                per_dim.setdefault(dim, []).append((hb, jb))
    assert per_dim, "pipeline produced no comparable pairs — harness miswired"
    print(f"  Judge->parse->compare pipeline OK ({sum(len(v) for v in per_dim.values())} pairs).")

    # 2. Metric sanity on a controlled ~80%-agreement synthetic set.
    rng = random.Random(3)
    pairs = []
    for _ in range(120):
        h = rng.randint(0, 1)
        j = h if rng.random() < 0.80 else 1 - h
        pairs.append((h, j))
    ac2 = gwets_ac2([p[0] for p in pairs], [p[1] for p in pairs], 2)
    lo, hi = bootstrap_ci(pairs, gwets_ac2, 2)
    print(f"  Metric sanity: 80%-agree synthetic -> AC2={ac2:.3f} [CI {lo:.3f}, {hi:.3f}]")
    assert 0.4 <= ac2 <= 0.9, "metric out of expected band"
    print("  Wiring OK. Run --labeled with your own PASS/FAIL data for a real number.")
    return 0


def run_score(args: argparse.Namespace) -> int:
    from verdict_eval.judge import DEFAULT_RUBRIC, Judge

    def make_provider(name: str):
        from verdict_eval.providers import AnthropicAdapter, GoogleAdapter, OpenAIAdapter
        return {"anthropic": AnthropicAdapter, "openai": OpenAIAdapter,
                "google": GoogleAdapter}[name]()

    labeled = _load_labeled(args.labeled)
    if not labeled:
        print(f"No examples in {args.labeled}.")
        return 1
    judge = Judge(provider=make_provider(args.provider), model=args.judge_model,
                  rubric=DEFAULT_RUBRIC, skip_context_dependent_when_missing=False)
    print(f"Scoring {len(labeled)} labeled examples with {args.provider}::{args.judge_model} ...")

    per_dim: dict[str, list[tuple[int, int]]] = {}
    for i, ex in enumerate(labeled):
        try:
            j = judge.judge(query=ex.get("query", ""), response=ex.get("response", ""),
                            context=ex.get("context") or None)
        except Exception as e:
            print(f"  judge error on row {i}: {e}")
            continue
        jv = {d.name: d.verdict for d in j.dimensions}
        for dim, human in (ex.get("labels") or {}).items():
            hb, jb = _bit(human), _bit(jv.get(dim))
            if hb is not None and jb is not None:
                per_dim.setdefault(dim, []).append((hb, jb))
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(labeled)} scored")
    return _report(per_dim)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--make-template", nargs=2, metavar=("IN_JSONL", "OUT_JSONL"),
                   help="Turn raw {query,response,context} rows into a labeling template.")
    p.add_argument("--offline", action="store_true", help="Wiring check, no API.")
    p.add_argument("--labeled", help="Path to your labeled JSONL.")
    p.add_argument("--provider", choices=["anthropic", "openai", "google"], default="anthropic")
    p.add_argument("--judge-model", default="claude-haiku-4-5-20251001")
    args = p.parse_args()

    if args.make_template:
        return make_template(args.make_template[0], args.make_template[1])
    if args.offline:
        return run_offline()
    if args.labeled:
        return run_score(args)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
