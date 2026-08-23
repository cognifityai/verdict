from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest


def _load_gate():
    path = Path(__file__).with_name("task6_statistical_gate.py")
    spec = importlib.util.spec_from_file_location("task6_statistical_gate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Task 6 statistical gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

STATIONARY_STREAM_DIGESTS = (
    "f72e99780b949206dd6f9493374d425675ce1c15a11ee9890a81e66faa93e97b",
    "cf58a0582f6812f09ce0d1767735a04c843a7329e9eba8496c5d8641a6a0c3ba",
    "2d2b4a08bc29d44004f12d405092ba27c9bef1a1481604f84b139ed7f011ce7e",
    "7c3e6d645044165969ac3d3e57f2e952c1e1abf9dd1b5fd636fe5127ebaf74d4",
    "89b93ce78ca3f3e1c53d732af01045956f16eba6389be21ef431fdd0a724420b",
    "1192eba8e6ac683c2f8a7cad3a8b987871671daf66b09f56bab871f8d6ace6e0",
    "df72243d83ab27e199e6b2ed434d9f328ee2c789956119d74e5728f2ff084f73",
)
POWER_STREAM_DIGESTS = (
    "1857fefa74df0a0cc1feb2aa2d0928a7a51af56b1934c755f721b3c53870f051",
    "7275f3dd695d81395299eb0d855ad53df5d4474a7f8333adcc5906997d6fae84",
    "5b916b926eb13fd9310cb5503512b159c7b3d793179d10174ebe631c957a5af5",
    "65e67506b5e38a8b0b566bf7dc0755752b2986d430ebd79bb135711a7dd25a42",
    "c922689804fe4a9c21c5c95428c08c20f5bdd2c89db6eb0cbcaf45e7e2027e65",
)


def test_frozen_identifier_and_timestamp_formula() -> None:
    session_id, trace_id, started_at = gate.stationary_identifiers(7, 2, "current", 3, 4, 8)

    assert session_id == "c-r0007-c02-s003"
    assert trace_id == "c-r0007-c02-s003-t04"
    assert started_at.isoformat() == "2026-02-01T00:00:00.000508+00:00"


def test_small_production_paths_are_repeatable() -> None:
    first = gate.stationary_point(4, 0.05, seed=61602, runs=2)
    second = gate.stationary_point(4, 0.05, seed=61602, runs=2)
    power_first = gate.power_point(30, 0.30, seed=61650, point_index=0, runs=2)
    power_second = gate.power_point(30, 0.30, seed=61650, point_index=0, runs=2)

    assert first == second
    assert power_first == power_second


def test_frozen_rng_seed_loop_and_draw_protocol_has_literal_stream_vectors() -> None:
    stationary = tuple(
        gate.stationary_stream_digest(*point, seed=seed)
        for point, seed in zip(gate.STATIONARY_POINTS, gate.STATIONARY_SEEDS, strict=True)
    )
    power = tuple(
        gate.power_stream_digest(
            conversations,
            shift,
            seed=gate.POWER_SEEDS[index],
            point_index=index,
        )
        for index, (conversations, shift, _minimum, _low) in enumerate(gate.POWER_POINTS)
    )

    assert stationary == STATIONARY_STREAM_DIGESTS
    assert power == POWER_STREAM_DIGESTS

    turns, sd = gate.STATIONARY_POINTS[0]
    mean = 0.80
    scale = mean * (1 - mean) / sd**2 - 1
    alpha, beta = mean * scale, (1 - mean) * scale
    wrong_family = gate.np.random.Generator(gate.np.random.MT19937(gate.STATIONARY_SEEDS[0]))
    wrong_family_rows = [
        gate._stationary_replicate(wrong_family, replicate, turns, alpha, beta)
        for replicate in range(2)
    ]
    assert gate._generated_stream_digest(wrong_family_rows) != STATIONARY_STREAM_DIGESTS[0]
    assert (
        gate.stationary_stream_digest(turns, sd, seed=gate.STATIONARY_SEEDS[0] + 1)
        != STATIONARY_STREAM_DIGESTS[0]
    )

    rng = gate.np.random.Generator(gate.np.random.PCG64(gate.STATIONARY_SEEDS[0]))
    rows = [gate._stationary_replicate(rng, replicate, turns, alpha, beta) for replicate in range(2)]
    reversed_rows = [(list(reversed(candidates)), outcomes) for candidates, outcomes in rows]
    assert gate._generated_stream_digest(reversed_rows) != STATIONARY_STREAM_DIGESTS[0]
    shifted_rng = gate.np.random.Generator(gate.np.random.PCG64(gate.STATIONARY_SEEDS[0]))
    shifted_rng.random()
    shifted_rows = [
        gate._stationary_replicate(shifted_rng, replicate, turns, alpha, beta)
        for replicate in range(2)
    ]
    assert gate._generated_stream_digest(shifted_rows) != STATIONARY_STREAM_DIGESTS[0]


def test_capture_matrix_rejects_call_local_turn_count_bias() -> None:
    assert gate.capture_matrix(session_count=64)["passed"] is True
    assert gate.capture_matrix(session_count=64, call_local_mutation=True)["passed"] is False


def _passing_report() -> dict:
    stationary = [gate.asdict(gate._point(0, 1000)) for _point in gate.STATIONARY_POINTS]
    mutated = deepcopy(stationary)
    mutated[0] = gate.asdict(gate._point(11, 1000))
    power = [
        gate.asdict(gate._point(minimum, 1000))
        for _n, _shift, minimum, minimum_low in gate.POWER_POINTS
    ]
    digests = {}
    counts = {}
    for rate in gate.CAPTURE_RATES:
        digest, count = gate._expected_sticky_capture(rate, 4096)
        digests[rate] = [digest] * gate.CAPTURE_ENTRIES_PER_RATE
        counts[rate] = [count] * gate.CAPTURE_ENTRIES_PER_RATE
    call_digests = deepcopy(digests)
    call_counts = deepcopy(counts)
    call_digests["0.25"][0] = gate._set_digest({"call-local-mutation"})
    call_counts["0.25"][0] = 1
    return {
        "schema": "task6-statistical-gate-v1",
        "runs_per_point": 1000,
        "capture_sessions": 4096,
        "stationary": stationary,
        "trace_level_mutation": mutated,
        "power": power,
        "capture": {"passed": True, "digests": digests, "counts": counts},
        "call_local_mutation": {
            "passed": False,
            "digests": call_digests,
            "counts": call_counts,
        },
    }


def test_acceptance_is_closed_and_each_gate_fails_independently() -> None:
    report = _passing_report()
    gate.validate_report(report)

    mutations = []
    missing = deepcopy(report)
    missing["power"].pop()
    mutations.append(missing)
    stationary = deepcopy(report)
    stationary["stationary"][0] = gate.asdict(gate._point(11, 1000))
    mutations.append(stationary)
    power = deepcopy(report)
    power["power"][0] = gate.asdict(gate._point(gate.POWER_POINTS[0][2] - 1, 1000))
    mutations.append(power)
    capture = deepcopy(report)
    capture["capture"]["passed"] = False
    mutations.append(capture)
    trace_mutation = deepcopy(report)
    trace_mutation["trace_level_mutation"] = deepcopy(report["stationary"])
    mutations.append(trace_mutation)
    call_mutation = deepcopy(report)
    call_mutation["call_local_mutation"] = deepcopy(report["capture"])
    mutations.append(call_mutation)
    incoherent = deepcopy(report)
    incoherent["stationary"][0]["estimate"] = 1.0
    mutations.append(incoherent)
    reversed_interval = deepcopy(report)
    reversed_interval["stationary"][0]["confidence_low"] = 1.0
    mutations.append(reversed_interval)
    missing_capture_field = deepcopy(report)
    del missing_capture_field["capture"]["counts"]
    mutations.append(missing_capture_field)
    short_capture = deepcopy(report)
    short_capture["capture"]["digests"]["0.25"].pop()
    mutations.append(short_capture)
    invalid_digest = deepcopy(report)
    invalid_digest["capture"]["digests"]["0.25"][0] = "z" * 64
    mutations.append(invalid_digest)
    invalid_count = deepcopy(report)
    invalid_count["capture"]["counts"]["0.25"][0] = 4097
    mutations.append(invalid_count)
    missing_rate = deepcopy(report)
    del missing_rate["capture"]["digests"]["0.5"]
    mutations.append(missing_rate)
    extra_top_level = deepcopy(report)
    extra_top_level["unexpected"] = True
    mutations.append(extra_top_level)

    for mutation in mutations:
        with pytest.raises(ValueError):
            gate.validate_report(mutation)
