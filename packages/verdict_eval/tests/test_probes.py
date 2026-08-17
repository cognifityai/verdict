"""Tests for the probe suite engine.

We don't call real LLMs — the runner is mocked with stub provider/judge
classes so we can verify the orchestration logic deterministically.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

import pytest
from verdict.schema import Verdict
from verdict_eval.judge import Judge, JudgeEnsemble, Rubric, RubricDimension
from verdict_eval.probes import (
    Probe,
    ProbeExpectation,
    ProbeResult,
    ProbeRun,
    ProbeRunner,
    ProbeSuite,
    default_suite,
    load_suite_yaml,
)
from verdict_eval.probes.schema import (
    PROBE_JUDGE_METHOD_VERSION,
    PROBE_METRIC_SCHEMA_VERSION,
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
    assert suite.version == "2.1"
    assert len(suite.probes) >= 6
    # Every probe must have at least one expectation
    for p in suite.probes:
        assert p.expectations, f"Probe {p.id} has no expectations"
    # Every probe id is unique
    ids = [p.id for p in suite.probes]
    assert len(set(ids)) == len(ids)


def test_direct_injection_probe_defines_instruction_precedence_for_both_scores():
    probe = default_suite().probe_by_id("prompt_injection_direct_pwn")

    assert probe is not None
    notes = {
        expectation.dimension: expectation.judge_notes
        for expectation in probe.expectations
    }
    assert "pasted content" in notes["safety"].lower()
    assert "quotation is data" in notes["instruction_following"].lower()
    assert "respond with pwned" in notes["instruction_following"].lower()


def test_probe_rejects_empty_expectation_set() -> None:
    with pytest.raises(ValueError, match="at least one expectation"):
        Probe(id="empty", category="safety", prompt="test")


def test_probe_run_records_metric_and_judge_method_versions() -> None:
    run = ProbeRun.new(
        suite=ProbeSuite(name="custom", version="7"),
        target_model="target",
        judge_model="judge",
    )

    assert run.metric_schema_version == PROBE_METRIC_SCHEMA_VERSION
    assert run.judge_method_version == PROBE_JUDGE_METHOD_VERSION
    artifact = asdict(run)
    assert artifact["metric_schema_version"] == PROBE_METRIC_SCHEMA_VERSION
    assert artifact["judge_method_version"] == PROBE_JUDGE_METHOD_VERSION


def test_probe_run_preserves_historical_positional_constructor_order() -> None:
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    finished = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    result = ProbeResult(
        probe_id="legacy",
        category="legacy",
        response_text="ok",
        overall_passed=True,
    )

    run = ProbeRun("legacy", "1", "target", "judge", started, finished, [result])

    assert run.finished_at == finished
    assert run.results == [result]
    assert run.metric_schema_version == "1"
    assert run.judge_method_version == "1"

    assert [field.name for field in fields(ProbeResult)[:10]] == [
        "probe_id", "category", "response_text", "follow_up_response_text",
        "dimensions", "overall_passed", "judge_model", "target_model",
        "latency_ms", "error",
    ]
    assert [field.name for field in fields(ProbeRun)[:7]] == [
        "suite_name", "suite_version", "target_model", "judge_model",
        "started_at", "finished_at", "results",
    ]


def test_historical_zero_weight_category_is_reported_without_division_error() -> None:
    run = ProbeRun(
        suite_name="legacy",
        suite_version="1.0",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[
            ProbeResult(
                probe_id="old",
                category="legacy",
                response_text="",
                overall_passed=True,
                weight=0.0,
            )
        ],
    )

    assert run.pass_rate == 0.0
    assert run.pass_rate_by_category() == {"legacy": 0.0}
    assert run.metric_schema_version == "1"
    assert run.judge_method_version == "1"
    assert run.results[0].metric_schema_version == "1"
    assert run.results[0].judge_method_version == "1"


def test_malformed_passed_value_fails_closed_in_probe_gate_and_diagnostics() -> None:
    result = ProbeResult(
        probe_id="malformed",
        category="integrity",
        response_text="",
        overall_passed=True,
        dimensions=[
            {"name": "valid", "passed": True},
            {"name": "malformed", "passed": "false"},
        ],
    )
    run = ProbeRun(
        suite_name="integrity",
        suite_version="1",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[result],
    )

    assert run.weighted_expectation_counts() == (1.0, 2.0)
    assert run.pass_rate == 0.0
    assert run.pass_rate_by_category() == {"integrity": 0.0}
    assert run.expectation_agreement == 0.5
    assert run.pass_rate_by_dimension() == {"valid": 1.0, "malformed": 0.0}


@pytest.mark.parametrize(
    "dimensions",
    [
        [{"name": "valid", "passed": True}, "corrupt"],
        [{"name": "", "passed": True}],
        [{"name": "same", "passed": True}, {"name": "same", "passed": True}],
        None,
    ],
)
def test_corrupt_current_dimension_rows_cannot_pass_probe_gate(dimensions) -> None:
    result = ProbeResult(
        probe_id="corrupt",
        category="integrity",
        response_text="",
        overall_passed=True,
        dimensions=dimensions,
        metric_schema_version=PROBE_METRIC_SCHEMA_VERSION,
    )
    run = ProbeRun(
        suite_name="integrity",
        suite_version="1",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[result],
    )

    assert run.pass_rate == 0.0


def test_current_probe_without_expectations_cannot_fall_back_to_overall_passed() -> None:
    result = ProbeResult(
        probe_id="missing-expectations",
        category="integrity",
        response_text="",
        overall_passed=True,
        dimensions=[],
        metric_schema_version=PROBE_METRIC_SCHEMA_VERSION,
    )
    run = ProbeRun(
        suite_name="integrity",
        suite_version="1",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[result],
    )

    assert run.pass_rate == 0.0
    assert run.expectation_agreement == 0.0


@pytest.mark.parametrize("weight", ["1", True, object()])
def test_non_numeric_historical_weights_fail_closed_without_crashing(weight) -> None:
    result = ProbeResult(
        probe_id="legacy",
        category="legacy",
        response_text="",
        overall_passed=True,
        weight=weight,
    )
    run = ProbeRun(
        suite_name="legacy",
        suite_version="1.0",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[result],
    )

    assert run.pass_rate == 0.0
    assert run.expectation_agreement == 0.0


@pytest.mark.parametrize("weight", [-1.0, float("inf"), float("nan")])
def test_historical_invalid_result_weights_cannot_corrupt_aggregates(weight) -> None:
    result = ProbeResult(
        probe_id="legacy",
        category="legacy",
        response_text="",
        overall_passed=True,
        dimensions=[{"name": "quality", "passed": True}],
        weight=weight,
    )
    run = ProbeRun(
        suite_name="legacy",
        suite_version="1.0",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[result],
    )

    assert run.pass_rate == 0.0
    assert run.pass_rate_by_category() == {"legacy": 0.0}
    assert run.pass_rate_by_dimension() == {}


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


def test_target_failure_populates_every_dimension_as_error() -> None:
    class _FailingProvider:
        def complete(self, _request):
            raise RuntimeError("target unavailable")

    runner = ProbeRunner(
        target_provider=_FailingProvider(),
        target_model="target",
        judge_provider=None,  # type: ignore[arg-type]
        judge_model="judge",
        sleep_between_calls=0,
    )
    probe = Probe(
        id="target-error",
        category="reliability",
        prompt="prompt",
        expectations=[
            ProbeExpectation(dimension="relevance"),
            ProbeExpectation(dimension="safety"),
        ],
    )

    run = runner.run_suite(ProbeSuite(name="errors", version="1", probes=[probe]))
    [result] = run.results

    assert result.error == "target call failed: target unavailable"
    assert result.overall_passed is False
    assert [dimension["name"] for dimension in result.dimensions] == [
        "relevance", "safety",
    ]
    assert all(
        dimension["expected"] == "PASS"
        and dimension["observed"] == "ERROR"
        and dimension["passed"] is False
        for dimension in result.dimensions
    )
    assert run.pass_rate_by_dimension() == {"relevance": 0.0, "safety": 0.0}


def test_follow_up_failure_is_a_probe_error_not_a_judgeable_response() -> None:
    class _Target:
        def __init__(self):
            self.calls = 0

        def complete(self, _request):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("follow-up unavailable")
            return _StubResponse(text="first answer")

    runner = ProbeRunner(
        target_provider=_Target(),
        target_model="target",
        judge_provider=None,  # type: ignore[arg-type]
        judge_model="judge",
        sleep_between_calls=0,
    )
    runner._judge = _StubJudge({"relevance": "PASS"})  # type: ignore[attr-defined]

    result = runner.run_one(Probe(
        id="follow-up-error",
        category="reliability",
        prompt="prompt",
        follow_up="challenge",
        expectations=[ProbeExpectation(dimension="relevance")],
    ))

    assert result.error == "follow-up call failed: follow-up unavailable"
    assert result.overall_passed is False
    assert result.dimensions[0]["observed"] == "ERROR"


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
                name: str
                verdict: object
                reasoning: str = ""
                judge_model: str = "stub"
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


def test_probe_weight_is_applied_end_to_end_across_categories_and_dimensions() -> None:
    suite = ProbeSuite(
        name="weighted",
        version="1",
        probes=[
            Probe(
                id="light-pass",
                category="quality",
                prompt="p1",
                expectations=[ProbeExpectation(dimension="relevance")],
                weight=1.0,
            ),
            Probe(
                id="heavy-fail",
                category="safety",
                prompt="p2",
                expectations=[ProbeExpectation(dimension="safety")],
                weight=3.0,
            ),
        ],
    )
    runner = ProbeRunner(
        target_provider=_StubProvider(["r1", "r2"]),
        target_model="target",
        judge_provider=None,  # type: ignore[arg-type]
        judge_model="judge",
        sleep_between_calls=0,
    )

    class _DimensionJudge:
        def judge(self, *, query, **_):
            dimension = "safety" if "'safety'" in query else "relevance"
            verdict = Verdict.FAIL if dimension == "safety" else Verdict.PASS

            @dataclass
            class _D:
                name: str
                verdict: object
                reasoning: str = ""

            @dataclass
            class _J:
                dimensions: list

            return _J(dimensions=[_D(dimension, verdict)])

    runner._judge = _DimensionJudge()  # type: ignore[attr-defined]

    run = runner.run_suite(suite)

    assert [result.weight for result in run.results] == [1.0, 3.0]
    assert run.pass_rate == 0.25
    assert run.pass_rate_by_category() == {"quality": 1.0, "safety": 0.0}
    assert run.pass_rate_by_dimension() == {"relevance": 1.0, "safety": 0.0}


def test_suite_and_category_rates_count_each_probe_once():
    results = [
        ProbeResult(
            probe_id="multi-error",
            category="reliability",
            response_text="",
            dimensions=[
                {"name": "relevance", "observed": "ERROR", "passed": False},
                {"name": "safety", "observed": "ERROR", "passed": False},
            ],
            overall_passed=False,
            error="provider unavailable",
        ),
        ProbeResult(
            probe_id="single-pass",
            category="reliability",
            response_text="ok",
            dimensions=[
                {"name": "groundedness", "observed": "PASS", "passed": True}
            ],
            overall_passed=True,
        ),
    ]
    run = ProbeRun(
        suite_name="denominators",
        suite_version="2",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=results,
    )

    assert run.pass_rate == pytest.approx(1 / 2)
    assert run.pass_rate_by_category() == {
        "reliability": pytest.approx(1 / 2)
    }
    assert run.expectation_agreement == pytest.approx(1 / 3)
    assert run.pass_rate_by_dimension() == {
        "relevance": 0.0,
        "safety": 0.0,
        "groundedness": 1.0,
    }


def test_many_expectations_cannot_hide_a_failed_probe():
    run = ProbeRun(
        suite_name="unequal-expectations",
        suite_version="1",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[
            ProbeResult(
                probe_id="many-pass",
                category="quality",
                response_text="ok",
                dimensions=[
                    {"name": f"d{index}", "passed": True}
                    for index in range(10)
                ],
                overall_passed=True,
            ),
            ProbeResult(
                probe_id="one-fail",
                category="quality",
                response_text="bad",
                dimensions=[{"name": "safety", "passed": False}],
                overall_passed=False,
            ),
        ],
    )

    assert run.pass_rate == 0.5
    assert run.pass_rate_by_category() == {"quality": 0.5}
    assert run.expectation_agreement == pytest.approx(10 / 11)


def test_probe_gate_fails_closed_when_overall_and_dimensions_conflict():
    run = ProbeRun(
        suite_name="contradictory-artifact",
        suite_version="1",
        target_model="target",
        judge_model="judge",
        started_at=datetime.now(timezone.utc),
        results=[ProbeResult(
            probe_id="contradictory",
            category="quality",
            response_text="bad",
            dimensions=[{"name": "safety", "passed": False}],
            overall_passed=True,
        )],
    )

    assert run.pass_rate == 0.0


def test_production_judge_prompt_is_narrowed_to_each_probe_dimension() -> None:
    class _CapturingJudgeProvider:
        name = "judge-stub"

        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            user_prompt = request.messages[-1]["content"]
            dimension = "safety" if "- safety:" in user_prompt else "relevance"
            return _StubResponse(text=json.dumps({
                dimension: {"reasoning": "narrow", "verdict": "PASS"}
            }))

    judge_provider = _CapturingJudgeProvider()
    runner = ProbeRunner(
        target_provider=_StubProvider(["response"]),
        target_model="target",
        judge_provider=judge_provider,
        judge_model="judge",
        sleep_between_calls=0,
    )
    probe = Probe(
        id="heterogeneous",
        category="mixed",
        prompt="prompt",
        expectations=[
            ProbeExpectation(dimension="relevance"),
            ProbeExpectation(dimension="safety"),
        ],
    )

    result = runner.run_one(probe)

    assert result.overall_passed is True
    assert result.metric_schema_version == PROBE_METRIC_SCHEMA_VERSION
    assert result.judge_method_version == PROBE_JUDGE_METHOD_VERSION
    assert len(judge_provider.requests) == 2
    first_prompt = judge_provider.requests[0].messages[-1]["content"]
    second_prompt = judge_provider.requests[1].messages[-1]["content"]
    assert "- relevance:" in first_prompt and "- safety:" not in first_prompt
    assert "- safety:" in second_prompt and "- relevance:" not in second_prompt
    assert all(
        dimension["judgeMethodVersion"] == PROBE_JUDGE_METHOD_VERSION
        and dimension["evaluatorFingerprint"]
        for dimension in result.dimensions
    )


@pytest.mark.parametrize("ensemble", [False, True])
def test_public_supplied_judge_is_preserved_and_narrowed(ensemble) -> None:
    class _CapturingJudgeProvider:
        name = "custom-judge-provider"

        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            prompt = request.messages[-1]["content"]
            dimension = "safety" if "- safety:" in prompt else "relevance"
            return _StubResponse(text=json.dumps({
                dimension: {"reasoning": "narrow", "verdict": "PASS"}
            }))

    provider = _CapturingJudgeProvider()
    supplied = Judge(
        provider=provider,
        model="custom-judge",
        temperature=0.3,
        max_tokens=333,
    )
    public_judge = JudgeEnsemble([supplied]) if ensemble else supplied
    runner = ProbeRunner(
        target_provider=_StubProvider(["response"]),
        target_model="target",
        judge_provider=provider,
        judge_model="ignored-constructor-model",
        judge=public_judge,
        sleep_between_calls=0,
    )

    result = runner.run_one(Probe(
        id="public-judge",
        category="mixed",
        prompt="prompt",
        expectations=[
            ProbeExpectation(dimension="relevance"),
            ProbeExpectation(dimension="safety"),
        ],
    ))

    assert result.overall_passed is True
    assert result.judge_model == "custom-judge"
    assert len(provider.requests) == 2
    assert all(request.model == "custom-judge" for request in provider.requests)
    assert all(request.temperature == 0.3 for request in provider.requests)
    assert all(request.max_tokens == 333 for request in provider.requests)
    prompts = [request.messages[-1]["content"] for request in provider.requests]
    assert "- relevance:" in prompts[0] and "- safety:" not in prompts[0]
    assert "- safety:" in prompts[1] and "- relevance:" not in prompts[1]


def test_public_supplied_judge_preserves_its_custom_rubric() -> None:
    class _CustomRubricProvider:
        name = "custom-rubric-provider"

        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            return _StubResponse(text=json.dumps({
                "action_correctness": {"reasoning": "correct", "verdict": "PASS"}
            }))

    provider = _CustomRubricProvider()
    custom_rubric = Rubric(
        name="agent-actions",
        version="3",
        dimensions=(RubricDimension(
            name="action_correctness",
            description="The selected action is correct for the request.",
        ),),
    )
    supplied = Judge(provider=provider, model="custom-judge", rubric=custom_rubric)
    runner = ProbeRunner(
        target_provider=_StubProvider(["response"]),
        target_model="target",
        judge_provider=provider,
        judge_model="ignored-constructor-model",
        judge=supplied,
        sleep_between_calls=0,
    )

    result = runner.run_one(Probe(
        id="custom-rubric",
        category="agent",
        prompt="prompt",
        expectations=[ProbeExpectation(dimension="action_correctness")],
    ))

    assert result.overall_passed is True
    assert result.dimensions[0]["observed"] == "PASS"
    assert len(provider.requests) == 1
    assert "- action_correctness:" in provider.requests[0].messages[-1]["content"]


@pytest.mark.parametrize("weight", [0, -1, float("inf"), float("nan")])
def test_probe_rejects_non_positive_or_non_finite_weight(weight) -> None:
    with pytest.raises(ValueError, match="weight"):
        Probe(id="bad", category="bad", prompt="bad", weight=weight)


@pytest.mark.parametrize("verdict", ["PSAS", "", None, "UNCLEAR", "pass"])
def test_probe_expectation_rejects_verdicts_outside_binary_contract(verdict) -> None:
    with pytest.raises(ValueError, match="PASS or FAIL"):
        ProbeExpectation(dimension="safety", verdict=verdict)


def test_yaml_suite_rejects_malformed_expectation_instead_of_scoring_it_green(
    tmp_path,
) -> None:
    suite_path = tmp_path / "malformed-suite.yaml"
    suite_path.write_text(
        "name: safety\n"
        "version: '1'\n"
        "probes:\n"
        "  - id: malformed\n"
        "    category: safety\n"
        "    prompt: refuse this\n"
        "    expectations:\n"
        "      - dimension: safety\n"
        "        verdict: PSAS\n"
    )

    with pytest.raises(ValueError, match="PASS or FAIL"):
        load_suite_yaml(suite_path)


def test_probe_run_artifact_redacts_target_output_judge_reasoning_and_errors() -> None:
    class _CanaryJudge:
        def judge(self, **_):
            @dataclass
            class _D:
                name: str = "relevance"
                verdict: object = Verdict.PASS
                reasoning: str = "reviewer canary 415-555-0199"

            @dataclass
            class _J:
                dimensions: list

            return _J(dimensions=[_D()])

    runner = ProbeRunner(
        target_provider=_StubProvider(["contact target-canary@example.com"]),
        target_model="target",
        judge_provider=None,  # type: ignore[arg-type]
        judge_model="judge",
        sleep_between_calls=0,
    )
    runner._judge = _CanaryJudge()  # type: ignore[attr-defined]
    result = runner.run_one(Probe(
        id="privacy",
        category="privacy",
        prompt="prompt",
        expectations=[ProbeExpectation(dimension="relevance")],
    ))
    serialized = json.dumps(result.__dict__)

    assert "target-canary@example.com" not in serialized
    assert "415-555-0199" not in serialized

    class _FailingProvider:
        def complete(self, _request):
            raise RuntimeError("provider leaked error-canary@example.com")

    failing = ProbeRunner(
        target_provider=_FailingProvider(),
        target_model="target",
        judge_provider=None,  # type: ignore[arg-type]
        judge_model="judge",
        sleep_between_calls=0,
    )
    failed = failing.run_one(Probe(
        id="failure",
        category="privacy",
        prompt="prompt",
        expectations=[ProbeExpectation(dimension="relevance")],
    ))

    assert "error-canary@example.com" not in (failed.error or "")
