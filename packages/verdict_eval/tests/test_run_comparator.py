from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from verdict_eval.pairwise import PairwiseJudgment, PairwiseStatus, PairwiseVerdict

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_comparator.py"
SPEC = importlib.util.spec_from_file_location("run_comparator", SCRIPT)
assert SPEC is not None
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def test_comparator_runner_rejects_unusable_judgment_instead_of_making_a_tie() -> None:
    judgment = PairwiseJudgment(
        verdict=None,
        raw_verdict_ab=None,
        raw_verdict_ba=None,
        status=PairwiseStatus.INVALID,
        status_ab=PairwiseStatus.INVALID,
        status_ba=PairwiseStatus.INVALID,
    )

    with pytest.raises(RuntimeError, match="unusable"):
        runner._pairwise_result_fields(judgment, "model-a", "model-b")


@pytest.mark.parametrize(
    ("verdict", "winner", "consistent"),
    [
        (PairwiseVerdict.A_BETTER, "model-a", True),
        (PairwiseVerdict.B_BETTER, "model-b", True),
        (PairwiseVerdict.TIE, "tie", True),
        (PairwiseVerdict.INCONSISTENT, "tie", False),
    ],
)
def test_comparator_runner_maps_only_usable_verdicts(
    verdict: PairwiseVerdict,
    winner: str,
    consistent: bool,
) -> None:
    judgment = PairwiseJudgment(
        verdict=verdict,
        raw_verdict_ab=verdict,
        raw_verdict_ba=verdict,
    )

    assert runner._pairwise_result_fields(judgment, "model-a", "model-b") == (
        winner,
        consistent,
    )
