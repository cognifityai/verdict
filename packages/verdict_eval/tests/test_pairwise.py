"""Tests for the pairwise judge (Arena-Hard methodology).

Verifies that position-swap consistency works correctly, including the
critical case where the judge flips its answer under position swap →
treated as TIE.
"""

from __future__ import annotations

from verdict_eval.pairwise import (
    PairwiseJudge,
    PairwiseJudgeEnsemble,
    PairwiseJudgment,
    PairwiseVerdict,
    _parse_verdict,
)
from verdict_eval.providers import CompletionResponse, FakeProvider


class _StubJudge:
    """Duck-typed judge whose compare() returns a preset reconciled verdict.

    Lets us drive the ensemble vote directly without simulating position
    swaps. Provides the `.model` attribute the ensemble reads.
    """

    def __init__(self, verdict: PairwiseVerdict, model: str = "stub") -> None:
        self._verdict = verdict
        self.model = model

    def compare(self, *, query, response_a, response_b) -> PairwiseJudgment:
        return PairwiseJudgment(
            verdict=self._verdict,
            raw_verdict_ab=self._verdict,
            raw_verdict_ba=self._verdict,
            judge_model=self.model,
        )


def _make_provider(text: str) -> FakeProvider:
    return FakeProvider(text)


def test_parse_verdict_finds_double_bracket_marker():
    v, _ = _parse_verdict("Reasoning here.\n[[A]]")
    assert v == PairwiseVerdict.A_BETTER
    v, _ = _parse_verdict("Some reasoning\n[[B]]")
    assert v == PairwiseVerdict.B_BETTER
    v, _ = _parse_verdict("They're about the same.\n[[C]]")
    assert v == PairwiseVerdict.TIE


def test_parse_verdict_handles_whitespace():
    v, _ = _parse_verdict("text\n[[ A ]]")
    assert v == PairwiseVerdict.A_BETTER


def test_parse_verdict_defaults_to_tie_when_marker_absent():
    v, _ = _parse_verdict("I don't know which is better honestly.")
    assert v == PairwiseVerdict.TIE


def test_position_swap_consistent_judge_returns_clear_winner():
    """Judge says A is better; under swap (B-first), judge says B is better
    again (which maps to A in A/B space) → consistent → A wins."""
    # Round 1 (A first): judge says [[A]]
    # Round 2 (B first → swap): judge says [[B]] (which maps to A in A/B space)
    # We simulate this with a callable that toggles based on call count
    state = {"calls": 0}

    def responder(req):
        state["calls"] += 1
        # First call: A first → judge picks first → [[A]]
        # Second call: B first → judge picks first → [[A]] (which is B in A/B space → flipped → B_BETTER mapped)
        # Wait we need a judge that consistently prefers a particular RESPONSE not POSITION.
        # The first call sees A in position 1; says [[A]] → response_a wins
        # The second call sees B in position 1; says [[B]] → because response_a (now in position 2) wins
        # So both round 1 and round 2 should say the position WITH response_a in it.
        msg = req.messages[-1]["content"]
        # response_a contains "RIGHT", response_b contains "WRONG"
        # We make the judge always pick the response containing "RIGHT" regardless of position.
        # Easiest: introspect the prompt and find which position contains RIGHT.
        idx_a = msg.find("Assistant A's Response")
        idx_b = msg.find("Assistant B's Response")
        a_segment = msg[idx_a:idx_b]
        b_segment = msg[idx_b:]
        if "RIGHT" in a_segment:
            return CompletionResponse(text="A is correct.\n[[A]]")
        if "RIGHT" in b_segment:
            return CompletionResponse(text="B is correct.\n[[B]]")
        return CompletionResponse(text="[[C]]")

    class FuncProvider:
        name = "func"
        def complete(self, req):
            return responder(req)

    judge = PairwiseJudge(provider=FuncProvider(), model="func-judge")
    j = judge.compare(query="Q", response_a="this is RIGHT", response_b="this is WRONG")
    assert j.verdict == PairwiseVerdict.A_BETTER
    assert j.raw_verdict_ab == PairwiseVerdict.A_BETTER
    assert j.raw_verdict_ba == PairwiseVerdict.A_BETTER


def test_position_swap_flipping_judge_treated_as_inconsistent():
    """Judge always prefers position 1 (position bias). Under swap, it flips.
    Pairwise should detect this as INCONSISTENT."""
    class FirstPositionProvider:
        name = "bias"
        def complete(self, req):
            return CompletionResponse(text="The first one looks better.\n[[A]]")

    judge = PairwiseJudge(provider=FirstPositionProvider(), model="biased")
    j = judge.compare(query="Q", response_a="alpha", response_b="beta")
    # Round 1: A first, judge says [[A]] → A_BETTER
    # Round 2: B first, judge says [[A]] (still first position) → maps to B_BETTER in A/B
    # Inconsistent
    assert j.verdict == PairwiseVerdict.INCONSISTENT
    assert j.raw_verdict_ab == PairwiseVerdict.A_BETTER
    assert j.raw_verdict_ba == PairwiseVerdict.B_BETTER


def test_one_tie_one_preference_resolves_to_tie():
    """If one round is a clear A and the other round is a tie, we treat the
    overall as TIE rather than letting one round dominate."""
    state = {"calls": 0}

    class Provider:
        name = "p"
        def complete(self, req):
            state["calls"] += 1
            if state["calls"] == 1:
                return CompletionResponse(text="A is slightly better.\n[[A]]")
            return CompletionResponse(text="They're roughly equal.\n[[C]]")

    judge = PairwiseJudge(provider=Provider(), model="p")
    j = judge.compare(query="Q", response_a="a", response_b="b")
    assert j.verdict == PairwiseVerdict.TIE


def test_ensemble_majority_vote():
    """Three judges, two say A_BETTER consistently, one says B → A wins."""
    class AlwaysAJudge:
        name = "a"
        def complete(self, req):
            return CompletionResponse(text="A.\n[[A]]")

    class AlwaysBJudge:
        name = "b"
        def complete(self, req):
            return CompletionResponse(text="B.\n[[B]]")

    # We need judges whose position-swap behavior is CONSISTENT for the
    # A_BETTER preference to survive position swap. So we need judges that
    # vote for the same response regardless of position.
    class ConsistentForFirstResponseJudge:
        """Always prefer whichever response is response_a in the original call,
        regardless of presentation order."""
        name = "cfa"
        def complete(self, req):
            msg = req.messages[-1]["content"]
            idx_a = msg.find("Assistant A's Response")
            idx_b = msg.find("Assistant B's Response")
            a_seg = msg[idx_a:idx_b]
            # Original response_a contains the marker "ALPHA_MARK"
            return CompletionResponse(
                text="[[A]]" if "ALPHA_MARK" in a_seg else "[[B]]"
            )

    judges = [
        PairwiseJudge(provider=ConsistentForFirstResponseJudge(), model=f"j{i}")
        for i in range(3)
    ]
    ensemble = PairwiseJudgeEnsemble(judges)
    j = ensemble.compare(query="Q", response_a="ALPHA_MARK content", response_b="other")
    assert j.verdict == PairwiseVerdict.A_BETTER
    assert len(j.component_judgments) == 3


def test_ensemble_inconsistent_does_not_win():
    """2 INCONSISTENT votes + 1 A_BETTER must NOT collapse to INCONSISTENT.

    INCONSISTENT and TIE both mean 'no usable preference', so they're merged.
    The lone A_BETTER is the only winnable vote → A wins (or at minimum, the
    result is never INCONSISTENT)."""
    judges = [
        _StubJudge(PairwiseVerdict.INCONSISTENT, "j0"),
        _StubJudge(PairwiseVerdict.INCONSISTENT, "j1"),
        _StubJudge(PairwiseVerdict.A_BETTER, "j2"),
    ]
    ensemble = PairwiseJudgeEnsemble(judges)
    j = ensemble.compare(query="Q", response_a="a", response_b="b")
    assert j.verdict != PairwiseVerdict.INCONSISTENT
    assert j.verdict == PairwiseVerdict.A_BETTER
    assert sum(
        1 for vote in j.component_judgments
        if vote.verdict == PairwiseVerdict.INCONSISTENT
    ) == 2


def test_ensemble_clear_a_plurality_wins():
    """A strict plurality of A over B wins even with TIE/INCONSISTENT noise."""
    judges = [
        _StubJudge(PairwiseVerdict.A_BETTER, "j0"),
        _StubJudge(PairwiseVerdict.A_BETTER, "j1"),
        _StubJudge(PairwiseVerdict.B_BETTER, "j2"),
        _StubJudge(PairwiseVerdict.TIE, "j3"),
        _StubJudge(PairwiseVerdict.INCONSISTENT, "j4"),
    ]
    ensemble = PairwiseJudgeEnsemble(judges)
    j = ensemble.compare(query="Q", response_a="a", response_b="b")
    assert j.verdict == PairwiseVerdict.A_BETTER


def test_ensemble_one_a_one_b_is_tie():
    """1 A_BETTER + 1 B_BETTER → no strict plurality → TIE."""
    judges = [
        _StubJudge(PairwiseVerdict.A_BETTER, "j0"),
        _StubJudge(PairwiseVerdict.B_BETTER, "j1"),
    ]
    ensemble = PairwiseJudgeEnsemble(judges)
    j = ensemble.compare(query="Q", response_a="a", response_b="b")
    assert j.verdict == PairwiseVerdict.TIE
