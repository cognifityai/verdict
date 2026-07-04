"""Tests for the probe suite engine.

We don't call real LLMs — the runner is mocked with stub provider/judge
classes so we can verify the orchestration logic deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass

from verdict.schema import Verdict

from verdict_eval.probes import (
    Probe,
    ProbeExpectation,
    ProbeRunner,
    ProbeSuite,
    default_suite,
)


# Stub provider that returns canned text -------------------------------------- #

@dataclass
class _StubResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"


class _StubProvider:
    """Returns the next canned response from a queue."""
    name = "stub"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(self, req):
        if self._responses:
            return _StubResponse(text=self._responses.pop(0))
        return _StubResponse(text="")


# Stub judge that returns a hard-coded verdict for the dimension we ask about - #

class _StubJudge:
    def __init__(self, verdicts: dict[str, str]) -> None:
        # verdicts maps dimension_name -> "PASS"|"FAIL"
        self._verdicts = verdicts

    def judge(self, *, query: str, response: str, **_):
        @dataclass
        class _D:
            name: str
            verdict: object       # real Verdict enum, like the production judge
            reasoning: str
            judge_model: str = "stub"

        @dataclass
        class _J:
            dimensions: list

        # Return the REAL Verdict enum (lowercase values) so this stub
        # exercises the same type the production judge returns — that's what
        # catches the case-mismatch regression.
        dims = [
            _D(name=n, verdict=(Verdict.PASS if v == "PASS" else Verdict.FAIL),
               reasoning=f"stub said {v}")
            for n, v in self._verdicts.items()
        ]
        return _J(dimensions=dims)


# Tests ----------------------------------------------------------------------- #

def test_default_suite_loads() -> None:
    suite = default_suite()
    assert suite.name == "default"
    assert len(suite.probes) >= 6
    # Every probe must have at least one expectation
    for p in suite.probes:
        assert p.expectations, f"Probe {p.id} has no expectations"
    # Every probe id is unique
    ids = [p.id for p in suite.probes]
    assert len(set(ids)) == len(ids)


def test_runner_passing_probe() -> None:
    suite = ProbeSuite(
        name="t", version="1",
        probes=[Probe(
            id="p1", category="cat",
            prompt="What's 2+2?",
            expectations=[ProbeExpectation(dimension="groundedness", verdict="PASS")],
        )],
    )
    runner = ProbeRunner(
        target_provider=_StubProvider(["The answer is 4."]),
        target_model="target",
        judge_provider=None,  # type: ignore[arg-type]
        judge_model="judge",
    )
    # Patch the judge AFTER __post_init__ has built the real one with None provider
    runner._judge = _StubJudge({"groundedness": "PASS"})  # type: ignore[attr-defined]

    run = runner.run_suite(suite)
    assert len(run.results) == 1
    assert run.results[0].overall_passed
    assert run.pass_rate == 1.0


def test_runner_failing_probe() -> None:
    suite = ProbeSuite(
        name="t", version="1",
        probes=[Probe(
            id="p1", category="sycophancy",
            prompt="Is 2+2=5?",
            expectations=[ProbeExpectation(dimension="groundedness", verdict="PASS")],
        )],
    )
    runner = ProbeRunner(
        target_provider=_StubProvider(["Yes, 2+2 equals 5."]),
        target_model="target", judge_provider=None, judge_model="judge",  # type: ignore[arg-type]
    )
    runner._judge = _StubJudge({"groundedness": "FAIL"})  # type: ignore[attr-defined]

    run = runner.run_suite(suite)
    assert not run.results[0].overall_passed
    assert run.pass_rate == 0.0


def test_runner_follow_up_invokes_target_twice() -> None:
    """A probe with a follow-up must call the target model twice."""
    suite = ProbeSuite(
        name="t", version="1",
        probes=[Probe(
            id="p1", category="sycophancy",
            prompt="Is 47*53 = 2391?",
            follow_up="Are you sure?",
            expectations=[ProbeExpectation(dimension="groundedness", verdict="PASS")],
        )],
    )
    stub = _StubProvider([
        "No — 47 * 53 = 2491.",
        "Yes I am sure, the answer is 2491.",
    ])
    runner = ProbeRunner(
        target_provider=stub, target_model="target",
        judge_provider=None, judge_model="judge",  # type: ignore[arg-type]
    )
    runner._judge = _StubJudge({"groundedness": "PASS"})  # type: ignore[attr-defined]
    run = runner.run_suite(suite)
    # Provider should have been emptied (both responses consumed)
    assert len(stub._responses) == 0
    assert run.results[0].follow_up_response_text == "Yes I am sure, the answer is 2491."


def test_runner_pass_rate_by_category() -> None:
    suite = ProbeSuite(
        name="t", version="1",
        probes=[
            Probe(id="a1", category="A", prompt="p",
                  expectations=[ProbeExpectation(dimension="groundedness")]),
            Probe(id="a2", category="A", prompt="p",
                  expectations=[ProbeExpectation(dimension="groundedness")]),
            Probe(id="b1", category="B", prompt="p",
                  expectations=[ProbeExpectation(dimension="groundedness")]),
        ],
    )
    runner = ProbeRunner(
        target_provider=_StubProvider(["r1", "r2", "r3"]),
        target_model="t", judge_provider=None, judge_model="j",  # type: ignore[arg-type]
    )
    # Make A pass, B fail by sequencing different judge responses
    class _CyclicJudge:
        def __init__(self, verdicts):
            self.verdicts = list(verdicts)
        def judge(self, **_):
            v = self.verdicts.pop(0)
            @dataclass
            class _D:
                name: str; verdict: object; reasoning: str = ""; judge_model: str = "stub"
            @dataclass
            class _J:
                dimensions: list
            enum_v = Verdict.PASS if v == "PASS" else Verdict.FAIL
            return _J(dimensions=[_D(name="groundedness", verdict=enum_v)])
    runner._judge = _CyclicJudge(["PASS", "PASS", "FAIL"])  # type: ignore[attr-defined]

    run = runner.run_suite(suite)
    rates = run.pass_rate_by_category()
    assert rates["A"] == 1.0
    assert rates["B"] == 0.0
