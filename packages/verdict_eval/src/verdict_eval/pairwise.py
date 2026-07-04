"""Pairwise judge — direct A-vs-B comparison with position-swap consistency.

This is the methodology Arena-Hard-Auto and MT-Bench use, and it's what we
should be using for cross-LLM ranking + judge–human alignment, NOT the
independent-rubric-then-compare approach.

The judge sees both responses to the same prompt and returns one of:
    A_BETTER | B_BETTER | TIE

To kill position bias we run the judge twice with positions swapped, and
accept the verdict only if both runs agree. Inconsistent → tie.

See Wang et al. 2023 (arXiv:2305.17926) for the position-bias literature and
the pairwise methodology notes in `docs/adrs/002-judge-methodology.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from verdict_eval.providers import CompletionRequest, LLMProvider


class PairwiseVerdict(str, Enum):
    A_BETTER = "A"
    B_BETTER = "B"
    TIE = "tie"
    INCONSISTENT = "inconsistent"   # position swap disagreed → treat as tie


@dataclass
class PairwiseJudgment:
    """Result of one pairwise comparison after position-swap reconciliation."""
    verdict: PairwiseVerdict
    raw_verdict_ab: PairwiseVerdict     # what the judge said with A first
    raw_verdict_ba: PairwiseVerdict     # what the judge said with B first (mapped back to A/B)
    reasoning_ab: str = ""
    reasoning_ba: str = ""
    judge_model: str = ""
    component_judgments: list["PairwiseJudgment"] = field(default_factory=list)


SYSTEM_PROMPT = """You are an impartial evaluator comparing two AI assistant responses
to the same user query. Your job is to pick the better response.

You will use one of three verdicts: [[A]], [[B]], or [[C]] (a tie).

CRITICAL: Be decisive. Use [[C]] (tie) ONLY when you genuinely cannot identify
EVEN ONE concrete advantage one response has over the other. If you can name
any specific way one response is better — even minor — you must pick that side.
Examples of valid tie-breakers:
- one response is more factually accurate on a specific claim
- one response addresses a part of the question the other ignored
- one response uses a more appropriate format given the query
- one response makes a specific reasoning step the other gets wrong
If you find yourself wanting to say "they're both pretty good" — that's a sign
you haven't looked hard enough. Look harder.

Ignore these factors when comparing:
- Response length. A short correct response is not worse than a long one.
- Markdown / formatting style, unless the user explicitly asked for a format.
- Order of presentation. Do not favor A or B based on position.

What to focus on:
- Did the response correctly and helpfully address the user's actual question?
- Is it factually accurate?
- Does it follow any constraints the user stated?
- Is its reasoning sound?

Write 1–3 sentences of specific reasoning, then end with exactly one line containing:
[[A]]   if Assistant A's response is better in at least one concrete way
[[B]]   if Assistant B's response is better in at least one concrete way
[[C]]   ONLY if neither response has any identifiable advantage over the other"""


_VERDICT_PATTERNS = [
    re.compile(r"\[\[\s*(A|B|C)\s*\]\]"),                       # standard [[A]] / [[B]] / [[C]]
    re.compile(r"final\s+verdict[:\s]+\[\[\s*(A|B|C)\s*\]\]", re.IGNORECASE),
]


def _parse_verdict(text: str) -> tuple[PairwiseVerdict, str]:
    """Pull the [[A]] / [[B]] / [[C]] marker out of the judge's response.

    Returns (verdict, reasoning). Defaults to TIE if no marker found.
    """
    for pat in _VERDICT_PATTERNS:
        m = pat.search(text)
        if m:
            tok = m.group(1).upper()
            if tok == "A":
                return PairwiseVerdict.A_BETTER, text[: m.start()].strip()
            if tok == "B":
                return PairwiseVerdict.B_BETTER, text[: m.start()].strip()
            return PairwiseVerdict.TIE, text[: m.start()].strip()
    return PairwiseVerdict.TIE, text.strip()


def _user_prompt(query: str, response_a: str, response_b: str) -> str:
    return (
        f"[User Query]\n{query.strip()}\n\n"
        f"[Assistant A's Response]\n{response_a.strip()}\n\n"
        f"[Assistant B's Response]\n{response_b.strip()}\n\n"
        f"Compare A and B against the user query. End with [[A]], [[B]], or [[C]]."
    )


@dataclass
class PairwiseJudge:
    """Position-swap-consistent pairwise judge.

    Uses one underlying provider. For cross-family ensemble (the production
    setting), wrap several PairwiseJudge instances in PairwiseJudgeEnsemble.
    """

    provider: LLMProvider
    model: str
    temperature: float = 0.0
    max_tokens: int = 512

    def compare(self, *, query: str, response_a: str, response_b: str) -> PairwiseJudgment:
        """Run the judge twice with positions swapped, return reconciled verdict."""
        # Round 1: A then B
        verdict_ab, reasoning_ab = self._ask(query, response_a, response_b)
        # Round 2: B then A (swap positions)
        verdict_ba_raw, reasoning_ba = self._ask(query, response_b, response_a)

        # Re-map round-2's verdict back into A/B-space.
        # In round 2 the judge saw response_b as "A" and response_a as "B",
        # so if it said A_BETTER in round 2 that means response_b was better in A/B space.
        if verdict_ba_raw == PairwiseVerdict.A_BETTER:
            verdict_ba_mapped = PairwiseVerdict.B_BETTER
        elif verdict_ba_raw == PairwiseVerdict.B_BETTER:
            verdict_ba_mapped = PairwiseVerdict.A_BETTER
        else:
            verdict_ba_mapped = PairwiseVerdict.TIE

        # Consistency check
        if verdict_ab == verdict_ba_mapped:
            final = verdict_ab
        elif verdict_ab == PairwiseVerdict.TIE or verdict_ba_mapped == PairwiseVerdict.TIE:
            # One round said tie, the other had a preference → treat as tie
            final = PairwiseVerdict.TIE
        else:
            # The judge flipped under position swap → inconsistent → tie
            final = PairwiseVerdict.INCONSISTENT

        return PairwiseJudgment(
            verdict=final,
            raw_verdict_ab=verdict_ab,
            raw_verdict_ba=verdict_ba_mapped,
            reasoning_ab=reasoning_ab,
            reasoning_ba=reasoning_ba,
            judge_model=self.model,
        )

    def _ask(self, query: str, first: str, second: str) -> tuple[PairwiseVerdict, str]:
        req = CompletionRequest(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(query, first, second)},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        resp = self.provider.complete(req)
        return _parse_verdict(resp.text)


class PairwiseJudgeEnsemble:
    """Majority vote across multiple PairwiseJudges from different families.

    For cross-LLM ranking specifically: NEVER include a judge from the same
    family as either of the compared models. (Panickssery 2024 self-preference.)
    """

    def __init__(self, judges: list[PairwiseJudge]) -> None:
        if not judges:
            raise ValueError("Need at least one judge")
        self._judges = judges

    def compare(self, *, query: str, response_a: str, response_b: str) -> PairwiseJudgment:
        votes: list[PairwiseJudgment] = []
        for j in self._judges:
            try:
                votes.append(j.compare(query=query, response_a=response_a, response_b=response_b))
            except Exception:
                continue
        if not votes:
            return PairwiseJudgment(
                verdict=PairwiseVerdict.TIE,
                raw_verdict_ab=PairwiseVerdict.TIE,
                raw_verdict_ba=PairwiseVerdict.TIE,
                judge_model="+".join(j.model for j in self._judges),
            )
        # Majority on the final reconciled verdicts.
        #
        # Only A_BETTER and B_BETTER are *winnable* buckets — they encode a
        # usable preference. TIE and INCONSISTENT both mean "no usable
        # preference", so we MERGE INCONSISTENT into TIE before voting.
        # Counting INCONSISTENT as its own bucket let a handful of position-
        # flips out-vote a real plurality and collapse the result to a tie
        # (or, worse, surface INCONSISTENT as the ensemble verdict). The
        # ensemble result is the side with a *strict plurality* of A/B votes;
        # if neither A nor B strictly out-votes the other, the result is TIE.
        a_votes = sum(1 for v in votes if v.verdict == PairwiseVerdict.A_BETTER)
        b_votes = sum(1 for v in votes if v.verdict == PairwiseVerdict.B_BETTER)
        if a_votes > b_votes:
            chosen = PairwiseVerdict.A_BETTER
        elif b_votes > a_votes:
            chosen = PairwiseVerdict.B_BETTER
        else:
            chosen = PairwiseVerdict.TIE
        return PairwiseJudgment(
            verdict=chosen,
            raw_verdict_ab=votes[0].raw_verdict_ab,
            raw_verdict_ba=votes[0].raw_verdict_ba,
            reasoning_ab=" || ".join(f"[{v.judge_model}] {v.reasoning_ab}" for v in votes if v.reasoning_ab),
            reasoning_ba=" || ".join(f"[{v.judge_model}] {v.reasoning_ba}" for v in votes if v.reasoning_ba),
            judge_model="+".join(j.model for j in self._judges),
            component_judgments=votes,
        )
