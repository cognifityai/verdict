"""Bradley-Terry comparator runner.

The missing orchestration around `verdict_eval.compare.BradleyTerryComparator`.
It takes a set of prompts, generates a response from each of N models, judges
them PAIRWISE with position-swap (via `verdict_eval.pairwise.PairwiseJudge`),
collects `PairwiseResult`s, fits Bradley-Terry, and prints model ratings with
bootstrap confidence intervals.

Pipeline:

    prompts
        → each model generates a response          (provider.complete)
        → every unordered model pair, every prompt → PairwiseJudge.compare
          (position-swap consistent A/B/TIE)
        → PairwiseResult per comparison
        → BradleyTerryComparator.fit              → ModelRating + bootstrap CIs

Usage (offline, deterministic — no API keys, no network):
    python scripts/run_comparator.py --provider fake

    The fake path defines models "strong" and "weak". A FakeProvider makes
    "strong" emit responses that the fake judge prefers ~90% of the time, so
    Bradley-Terry recovers rating(strong) > rating(weak). Fully deterministic
    (seeded), so the recovered ordering is stable across runs.

Usage (live):
    export ANTHROPIC_API_KEY=...
    python scripts/run_comparator.py --provider anthropic \\
        --judge-model claude-haiku-4-5 \\
        --models claude-haiku-4-5 claude-sonnet-4-5

NOTE: `verdict_eval.compare` imports scikit-learn, so this script needs sklearn
installed to actually fit Bradley-Terry. The fake provider/judge paths are
deterministic and documented; everything except the final BT fit runs without
any optional heavy deps.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))


# Small built-in default prompt set (used when --prompts-file is not given).
DEFAULT_PROMPTS = [
    "Explain why the sky appears blue.",
    "Summarize the plot of Romeo and Juliet in two sentences.",
    "What are the trade-offs between TCP and UDP?",
    "Write a haiku about autumn leaves.",
    "How does a hash map achieve average O(1) lookup?",
    "Give two reasons unit tests are worth writing.",
    "Describe the difference between latency and throughput.",
    "What is the capital of Australia, and why is it not Sydney?",
]


# --------------------------------------------------------------------------- #
# Fake (offline) provider + judge
# --------------------------------------------------------------------------- #
#
# Determinism contract for `--provider fake`:
#   * Two models: "strong" and "weak".
#   * The generation FakeProvider tags each response with the model name so the
#     downstream fake judge can tell them apart: a "strong" answer literally
#     contains the token "[STRONG]" and a "weak" answer contains "[WEAK]".
#   * The fake judge prefers the [STRONG] side ~90% of the time. The 10% upset
#     is a deterministic function of (query, A-text) — no RNG — so the whole
#     run is reproducible and BradleyTerry recovers strong > weak with a CI
#     gap rather than a degenerate clean sweep.

FAKE_STRONG = "strong"
FAKE_WEAK = "weak"


def _fake_generation_provider():
    """A FakeProvider whose response depends on the model in the request.

    The Judge/generator calls provider.complete(req); req.model tells us which
    model is generating. We tag the text so the fake judge can score it.
    """
    from verdict_eval.providers import FakeProvider

    def respond(req) -> str:
        model = req.model
        # Pull the user's actual question out of the messages for flavor.
        user_msgs = [m["content"] for m in req.messages if m["role"] == "user"]
        q = user_msgs[-1] if user_msgs else ""
        if model == FAKE_STRONG:
            return f"[STRONG] A careful, complete answer to: {q}"
        return f"[WEAK] A vague, partial answer to: {q}"

    return FakeProvider(respond)


def _fake_judge_provider():
    """A FakeProvider standing in for the pairwise judge LLM.

    It reads the [User Query] / [Assistant A's Response] / [Assistant B's
    Response] block that PairwiseJudge builds, decides which side is stronger,
    and emits the [[A]]/[[B]]/[[C]] marker PairwiseJudge parses.

    Preference rule (deterministic, no RNG):
      * If exactly one side is tagged [STRONG], prefer it ~90% of the time.
      * The ~10% upset is keyed off a stable hash of the query text, so it's
        reproducible. This gives BradleyTerry a non-degenerate dataset.
    """
    import re

    from verdict_eval.providers import FakeProvider

    a_re = re.compile(r"\[Assistant A's Response\]\n(.*?)\n\n\[Assistant B's Response\]",
                      re.DOTALL)
    b_re = re.compile(r"\[Assistant B's Response\]\n(.*?)\n\n", re.DOTALL)
    q_re = re.compile(r"\[User Query\]\n(.*?)\n\n", re.DOTALL)

    def respond(req) -> str:
        prompt = ""
        for m in req.messages:
            if m["role"] == "user":
                prompt = m["content"]
        a_match = a_re.search(prompt)
        b_match = b_re.search(prompt)
        q_match = q_re.search(prompt)
        a_text = a_match.group(1) if a_match else ""
        b_text = b_match.group(1) if b_match else ""
        query = q_match.group(1) if q_match else ""

        a_strong = "[STRONG]" in a_text
        b_strong = "[STRONG]" in b_text

        if a_strong == b_strong:
            # Both strong or both weak — genuine tie.
            return "Both responses are comparable. [[C]]"

        # Exactly one side is strong. Prefer it, with a deterministic ~10% upset.
        # Hash query → [0, 99]; upset when bucket < 10.
        bucket = sum(ord(c) for c in query) % 100
        upset = bucket < 10
        strong_is_a = a_strong
        prefer_a = strong_is_a if not upset else (not strong_is_a)
        if prefer_a:
            return "Assistant A is more complete and accurate. [[A]]"
        return "Assistant B is more complete and accurate. [[B]]"

    return FakeProvider(respond)


# --------------------------------------------------------------------------- #
# Generation + judging
# --------------------------------------------------------------------------- #

def _generate(provider, model: str, prompt: str, max_tokens: int) -> str:
    from verdict_eval.providers import CompletionRequest

    req = CompletionRequest(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return provider.complete(req).text


def main() -> int:
    p = argparse.ArgumentParser(description="Bradley-Terry pairwise comparator runner.")
    p.add_argument("--provider", default="fake", choices=["fake", "anthropic"],
                   help="Generation+judge provider. 'fake' is offline & deterministic.")
    p.add_argument("--models", nargs="+", default=None,
                   help="Model ids to compare. Default for fake: strong weak.")
    p.add_argument("--judge-model", default="fake-judge",
                   help="Model id for the pairwise judge.")
    p.add_argument("--prompts-file", default=None,
                   help="Path to a newline-delimited prompts file. "
                        "Defaults to a small built-in set.")
    p.add_argument("--max-tokens", type=int, default=256,
                   help="max_tokens for generation calls.")
    p.add_argument("--bootstrap", type=int, default=1000,
                   help="Bradley-Terry bootstrap iterations (CI width).")
    p.add_argument("--seed", type=int, default=42, help="Bootstrap seed.")
    args = p.parse_args()

    # -- prompts -------------------------------------------------------------
    if args.prompts_file:
        prompts = [
            line.strip()
            for line in Path(args.prompts_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    else:
        prompts = list(DEFAULT_PROMPTS)
    print(f"Prompts: {len(prompts)}")

    # -- providers + models --------------------------------------------------
    if args.provider == "fake":
        gen_provider = _fake_generation_provider()
        judge_provider = _fake_judge_provider()
        models = args.models or [FAKE_STRONG, FAKE_WEAK]
        judge_model = args.judge_model
    else:  # anthropic (live)
        from verdict_eval.providers import AnthropicAdapter
        gen_provider = AnthropicAdapter()
        judge_provider = AnthropicAdapter()
        models = args.models or ["claude-haiku-4-5", "claude-sonnet-4-5"]
        judge_model = args.judge_model if args.judge_model != "fake-judge" else "claude-haiku-4-5"

    if len(models) < 2:
        print("Need at least 2 models to compare.")
        return 1
    print(f"Models: {models}")
    print(f"Judge:  {judge_model} (provider={args.provider})")

    # -- Step 1: generate one response per (model, prompt) -------------------
    print("Generating responses...")
    responses: dict[tuple[str, int], str] = {}
    for model in models:
        for i, prompt in enumerate(prompts):
            responses[(model, i)] = _generate(gen_provider, model, prompt, args.max_tokens)

    # -- Step 2: pairwise judge (position-swap consistent) -------------------
    from verdict_eval.compare import PairwiseResult
    from verdict_eval.pairwise import PairwiseJudge, PairwiseVerdict

    judge = PairwiseJudge(provider=judge_provider, model=judge_model,
                          temperature=0.0, max_tokens=256)

    print("Judging pairwise...")
    results: list[PairwiseResult] = []
    for model_a, model_b in itertools.combinations(models, 2):
        for i, prompt in enumerate(prompts):
            j = judge.compare(
                query=prompt,
                response_a=responses[(model_a, i)],
                response_b=responses[(model_b, i)],
            )
            # Map PairwiseVerdict → winner model id (or "tie").
            if j.verdict == PairwiseVerdict.A_BETTER:
                winner = model_a
            elif j.verdict == PairwiseVerdict.B_BETTER:
                winner = model_b
            else:
                winner = "tie"
            # INCONSISTENT means the position swap disagreed → not consistent.
            consistent = j.verdict != PairwiseVerdict.INCONSISTENT
            results.append(PairwiseResult(
                model_a=model_a,
                model_b=model_b,
                winner=winner,
                judge_model=judge_model,
                position_consistent=consistent,
            ))
    n_decisive = sum(1 for r in results if r.winner != "tie")
    print(f"  Collected {len(results)} comparison(s); {n_decisive} decisive.")

    # -- Step 3: fit Bradley-Terry ------------------------------------------
    from verdict_eval.compare import BradleyTerryComparator

    print("Fitting Bradley-Terry...")
    comparator = BradleyTerryComparator(
        bootstrap_iterations=args.bootstrap,
        seed=args.seed,
        anchor=models[-1],   # report win-rate vs the last model as the anchor
    )
    ratings = comparator.fit(results)
    ratings.sort(key=lambda r: r.rating, reverse=True)

    # -- Report --------------------------------------------------------------
    print("\nModel ratings (Bradley-Terry log-odds; higher is better):")
    print(f"  {'model':<24} {'rating':>8}  {'95% CI':>18}  {'win vs ' + models[-1]:>16}")
    for r in ratings:
        ci = f"[{r.rating_lo:+.3f}, {r.rating_hi:+.3f}]"
        wr = f"{r.win_rate_vs_anchor:.2%}" if r.win_rate_vs_anchor is not None else "—"
        print(f"  {r.model:<24} {r.rating:>+8.3f}  {ci:>18}  {wr:>16}")

    if args.provider == "fake" and ratings:
        top = ratings[0].model
        ok = top == FAKE_STRONG
        print(f"\nfake-path check: top model is '{top}' "
              f"({'PASS' if ok else 'UNEXPECTED'}; expected '{FAKE_STRONG}').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
