from __future__ import annotations

import importlib.util
from pathlib import Path

from verdict.statistics import gwet_ac1

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_rubric_alignment.py"
SPEC = importlib.util.spec_from_file_location("verify_rubric_alignment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


def test_bootstrap_ci_is_deterministic_and_bounded() -> None:
    pairs = [(0, 0), (0, 0), (1, 1), (1, 0), (1, 1)]

    first = verifier.bootstrap_ci(pairs, gwet_ac1, 2, iters=100, seed=7)
    second = verifier.bootstrap_ci(pairs, gwet_ac1, 2, iters=100, seed=7)

    assert first == second
    assert -1.0 <= first[0] <= first[1] <= 1.0


def test_offline_check_exercises_the_binary_judge_path() -> None:
    assert verifier.run_offline() == 0
