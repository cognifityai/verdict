"""Frozen Task 6 conversation-observation statistical release gate.

Do not change seeds, construction order, or acceptance floors after viewing a
result. The harness calls the production capture decision, representative
selector, and detector; it does not reimplement those product behaviors.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from verdict import request_context
from verdict.client import init, shutdown
from verdict.instrumentors import base as instrumentor_base
from verdict.instrumentors.base import apply_routing_context, should_sample_success
from verdict.schema import Trace
from verdict_eval.drift import DriftDetector, DriftWindow
from verdict_eval.sampling import ConversationCandidate, select_conversation_representatives

TENANT = "__task6_stat__"
REGISTRY_VERSION = "v_stat"
WORKLOAD = "agent"
BASELINE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
CURRENT_START = datetime(2026, 2, 1, tzinfo=timezone.utc)
STATIONARY_POINTS = ((1, 0.05), (4, 0.05), (8, 0.05), (16, 0.05), (30, 0.05), (8, 0.12), (30, 0.12))
STATIONARY_SEEDS = tuple(61601 + index for index in range(len(STATIONARY_POINTS)))
POWER_POINTS = (
    (30, 0.30, 285, 0.25),
    (40, 0.30, 450, 0.41),
    (60, 0.30, 710, 0.67),
    (100, 0.30, 940, 0.92),
    (100, 0.20, 590, 0.55),
)
POWER_SEEDS = tuple(61650 + index for index in range(len(POWER_POINTS)))
Z_95 = 1.959963984540054
REPORT_KEYS = {
    "schema",
    "runs_per_point",
    "capture_sessions",
    "stationary",
    "trace_level_mutation",
    "power",
    "capture",
    "call_local_mutation",
}
POINT_KEYS = {
    "detections",
    "runs",
    "estimate",
    "confidence_low",
    "confidence_high",
}
CAPTURE_KEYS = {"passed", "digests", "counts"}
CAPTURE_RATES = tuple(str(rate) for rate in (0.0, 0.25, 0.5, 1.0))
CAPTURE_ENTRIES_PER_RATE = 2 * 2 * 4


@dataclass(frozen=True)
class GatePoint:
    detections: int
    runs: int
    estimate: float
    confidence_low: float
    confidence_high: float


def wilson(successes: int, total: int) -> tuple[float, float]:
    proportion = successes / total
    denominator = 1 + Z_95**2 / total
    center = (proportion + Z_95**2 / (2 * total)) / denominator
    radius = (
        Z_95
        * math.sqrt(proportion * (1 - proportion) / total + Z_95**2 / (4 * total**2))
        / denominator
    )
    low = 0.0 if successes == 0 else max(0.0, center - radius)
    high = 1.0 if successes == total else min(1.0, center + radius)
    return low, high


def _point(detections: int, runs: int) -> GatePoint:
    low, high = wilson(detections, runs)
    return GatePoint(detections, runs, detections / runs, low, high)


def stationary_identifiers(
    replicate: int,
    cluster: int,
    window: str,
    session: int,
    turn: int,
    turns_per_conversation: int,
) -> tuple[str, str, datetime]:
    prefix = "b" if window == "baseline" else "c"
    session_id = f"{prefix}-r{replicate:04d}-c{cluster:02d}-s{session:03d}"
    trace_id = f"{session_id}-t{turn:02d}"
    start = BASELINE_START if window == "baseline" else CURRENT_START
    offset = (cluster * 30 + session) * turns_per_conversation + turn
    return session_id, trace_id, start + timedelta(microseconds=offset)


def _detect(
    selected: list[ConversationCandidate],
    outcomes: dict[str, tuple[float, ...]],
    *,
    dimensions: int,
) -> bool:
    buckets: dict[tuple[str, str, int], list[tuple[str, float]]] = {}
    for item in selected:
        for dimension, value in enumerate(outcomes[item.trace_id]):
            buckets.setdefault((item.window, item.cluster_id, dimension), []).append(
                (item.trace_id, value)
            )

    def windows(label: str) -> list[DriftWindow]:
        result: list[DriftWindow] = []
        for cluster in sorted({item.cluster_id for item in selected}):
            for dimension in range(dimensions):
                rows = buckets.get((label, cluster, dimension), [])
                result.append(
                    DriftWindow(
                        cluster,
                        f"dim-{dimension:02d}",
                        [value for _trace_id, value in rows],
                        trace_ids=[trace_id for trace_id, _value in rows],
                    )
                )
        return result

    detector = DriftDetector(min_sample_size=30, p_threshold=0.01, effect_size_threshold=0.147)
    return bool(detector.detect(current=windows("current"), baseline=windows("baseline")))


def _stationary_replicate(
    rng: np.random.Generator,
    replicate: int,
    turns_per_conversation: int,
    alpha: float,
    beta: float,
) -> tuple[list[ConversationCandidate], dict[str, tuple[float, ...]]]:
    candidates: list[ConversationCandidate] = []
    outcomes: dict[str, tuple[float, ...]] = {}
    for cluster in range(5):
        cluster_id = f"clu-{cluster:02d}"
        for window in ("baseline", "current"):
            for session in range(30):
                turn_outcomes = [[0.0] * 5 for _turn in range(turns_per_conversation)]
                for dimension in range(5):
                    latent = rng.beta(alpha, beta)
                    for turn in range(turns_per_conversation):
                        turn_outcomes[turn][dimension] = float(rng.binomial(1, latent))
                for turn in range(turns_per_conversation):
                    session_id, trace_id, started_at = stationary_identifiers(
                        replicate,
                        cluster,
                        window,
                        session,
                        turn,
                        turns_per_conversation,
                    )
                    if not instrumentor_base._session_success_sampled(
                        TENANT, WORKLOAD, session_id, 1.0
                    ):
                        raise AssertionError("rate-one session capture dropped a call")
                    candidates.append(
                        ConversationCandidate(
                            TENANT,
                            REGISTRY_VERSION,
                            cluster_id,
                            window,
                            session_id,
                            trace_id,
                            started_at,
                        )
                    )
                    outcomes[trace_id] = tuple(turn_outcomes[turn])
    return candidates, outcomes


def stationary_point(
    turns_per_conversation: int,
    between_conversation_sd: float,
    *,
    seed: int,
    runs: int = 1_000,
    trace_level_mutation: bool = False,
) -> GatePoint:
    rng = np.random.Generator(np.random.PCG64(seed))
    mean = 0.80
    variance = between_conversation_sd**2
    scale = mean * (1 - mean) / variance - 1
    alpha, beta = mean * scale, (1 - mean) * scale
    detections = 0
    for replicate in range(runs):
        candidates, outcomes = _stationary_replicate(
            rng, replicate, turns_per_conversation, alpha, beta
        )
        selected = (
            candidates
            if trace_level_mutation
            else [
                ConversationCandidate(
                    item.tenant_id,
                    item.registry_version,
                    item.cluster_id,
                    item.window,
                    item.session_id,
                    item.trace_id,
                    item.started_at,
                )
                for item in select_conversation_representatives(
                    candidates, target_per_cell=30, seed=0
                ).selected
            ]
        )
        detections += _detect(selected, outcomes, dimensions=5)
    return _point(detections, runs)


def _power_replicate(
    rng: np.random.Generator,
    replicate: int,
    conversations: int,
    shift: float,
    point_index: int,
) -> tuple[list[ConversationCandidate], dict[str, tuple[float, ...]]]:
    candidates: list[ConversationCandidate] = []
    outcomes: dict[str, tuple[float, ...]] = {}
    for window, probability, prefix, start in (
        ("baseline", 0.80, "b", BASELINE_START),
        ("current", 0.80 - shift, "c", CURRENT_START),
    ):
        for session in range(conversations):
            session_id = f"p{point_index:02d}-{prefix}-r{replicate:04d}-s{session:03d}"
            trace_id = f"{session_id}-t00"
            candidates.append(
                ConversationCandidate(
                    TENANT,
                    REGISTRY_VERSION,
                    "clu-00",
                    window,
                    session_id,
                    trace_id,
                    start + timedelta(microseconds=session),
                )
            )
            outcomes[trace_id] = (float(rng.binomial(1, probability)),)
    return candidates, outcomes


def power_point(
    conversations: int,
    shift: float,
    *,
    seed: int,
    point_index: int,
    runs: int = 1_000,
) -> GatePoint:
    rng = np.random.Generator(np.random.PCG64(seed))
    detections = 0
    for replicate in range(runs):
        candidates, outcomes = _power_replicate(
            rng, replicate, conversations, shift, point_index
        )
        selected = [
            ConversationCandidate(
                item.tenant_id,
                item.registry_version,
                item.cluster_id,
                item.window,
                item.session_id,
                item.trace_id,
                item.started_at,
            )
            for item in select_conversation_representatives(
                candidates, target_per_cell=conversations, seed=0
            ).selected
        ]
        detections += _detect(selected, outcomes, dimensions=1)
    return _point(detections, runs)


def _generated_stream_digest(
    rows: list[tuple[list[ConversationCandidate], dict[str, tuple[float, ...]]]],
) -> str:
    digest = hashlib.sha256()
    for candidates, outcomes in rows:
        for candidate in candidates:
            values = (
                candidate.tenant_id,
                candidate.registry_version,
                candidate.cluster_id,
                candidate.window,
                candidate.session_id,
                candidate.trace_id,
                candidate.started_at.isoformat(timespec="microseconds"),
                *tuple(str(int(value)) for value in outcomes[candidate.trace_id]),
            )
            for value in values:
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(4, "big"))
                digest.update(encoded)
    return digest.hexdigest()


def stationary_stream_digest(
    turns_per_conversation: int,
    between_conversation_sd: float,
    *,
    seed: int,
    runs: int = 2,
) -> str:
    rng = np.random.Generator(np.random.PCG64(seed))
    mean = 0.80
    scale = mean * (1 - mean) / between_conversation_sd**2 - 1
    alpha, beta = mean * scale, (1 - mean) * scale
    return _generated_stream_digest(
        [
            _stationary_replicate(rng, replicate, turns_per_conversation, alpha, beta)
            for replicate in range(runs)
        ]
    )


def power_stream_digest(
    conversations: int,
    shift: float,
    *,
    seed: int,
    point_index: int,
    runs: int = 2,
) -> str:
    rng = np.random.Generator(np.random.PCG64(seed))
    return _generated_stream_digest(
        [
            _power_replicate(rng, replicate, conversations, shift, point_index)
            for replicate in range(runs)
        ]
    )


def _set_digest(values: set[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(values):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def capture_matrix(*, session_count: int = 4_096, call_local_mutation: bool = False) -> dict:
    digests: dict[str, list[str]] = {str(rate): [] for rate in (0.0, 0.25, 0.5, 1.0)}
    counts: dict[str, list[int]] = {str(rate): [] for rate in (0.0, 0.25, 0.5, 1.0)}
    for restart in range(2):
        shutdown()
        client = init(storage="memory://", instrumentors=[], tenant_mode="request")
        if call_local_mutation:
            instrumentor_base._SUCCESS_RNG.seed(61700 + restart)
        try:
            for rate in (0.0, 0.25, 0.5, 1.0):
                for provider in ("anthropic", "openai"):
                    for turns in (1, 4, 8, 30):
                        retained: set[str] = set()
                        for session in range(session_count):
                            session_id = f"capture-s{session:05d}"
                            for turn in range(turns):
                                with request_context(
                                    tenant_id=TENANT,
                                    session_id=session_id,
                                    workload=WORKLOAD,
                                    sample_rate=rate,
                                    success_sampling=(
                                        "call" if call_local_mutation else "session"
                                    ),
                                ):
                                    trace = Trace(
                                        trace_id=f"{provider}-{session_id}-t{turn:02d}",
                                        provider=provider,
                                    )
                                    apply_routing_context(client, trace)
                                    expected_policy = (
                                        "call-v1"
                                        if call_local_mutation
                                        else "full-v1"
                                        if rate == 1.0
                                        else "session-v1"
                                    )
                                    if trace.tags.get("verdict.success_sampling") != expected_policy:
                                        raise AssertionError("capture policy tag mismatch")
                                    if should_sample_success(client, trace):
                                        retained.add(session_id)
                        digests[str(rate)].append(_set_digest(retained))
                        counts[str(rate)].append(len(retained))
        finally:
            shutdown()
    passed = all(len(set(values)) == 1 for values in digests.values())
    return {"passed": passed, "digests": digests, "counts": counts}


def run_gate(*, runs: int = 1_000, capture_sessions: int = 4_096) -> dict:
    stationary = []
    mutated = []
    for index, (turns, sd) in enumerate(STATIONARY_POINTS):
        stationary.append(asdict(stationary_point(turns, sd, seed=STATIONARY_SEEDS[index], runs=runs)))
        mutated.append(
            asdict(
                stationary_point(
                    turns,
                    sd,
                    seed=STATIONARY_SEEDS[index],
                    runs=runs,
                    trace_level_mutation=True,
                )
            )
        )
    power = [
        asdict(
            power_point(
                conversations,
                shift,
                seed=POWER_SEEDS[index],
                point_index=index,
                runs=runs,
            )
        )
        for index, (conversations, shift, _detections, _low) in enumerate(POWER_POINTS)
    ]
    report = {
        "schema": "task6-statistical-gate-v1",
        "runs_per_point": runs,
        "capture_sessions": capture_sessions,
        "stationary": stationary,
        "trace_level_mutation": mutated,
        "power": power,
        "capture": capture_matrix(session_count=capture_sessions),
        "call_local_mutation": capture_matrix(
            session_count=capture_sessions, call_local_mutation=True
        ),
    }
    validate_report(report)
    return report


def _validate_point(point: object) -> None:
    if not isinstance(point, dict) or set(point) != POINT_KEYS:
        raise ValueError("invalid Task 6 statistical point")
    if (
        type(point["detections"]) is not int
        or type(point["runs"]) is not int
        or point["runs"] != 1_000
        or not 0 <= point["detections"] <= point["runs"]
    ):
        raise ValueError("invalid Task 6 statistical point")
    if any(
        type(point[name]) is not float
        or not math.isfinite(point[name])
        or not 0 <= point[name] <= 1
        for name in ("estimate", "confidence_low", "confidence_high")
    ):
        raise ValueError("invalid Task 6 statistical point")
    expected = _point(point["detections"], point["runs"])
    if point != asdict(expected):
        raise ValueError("incoherent Task 6 statistical point")
    if not point["confidence_low"] <= point["estimate"] <= point["confidence_high"]:
        raise ValueError("incoherent Task 6 confidence interval")


def _expected_sticky_capture(rate: str, session_count: int) -> tuple[str, int]:
    retained = {
        f"capture-s{session:05d}"
        for session in range(session_count)
        if instrumentor_base._session_success_sampled(
            TENANT, WORKLOAD, f"capture-s{session:05d}", float(rate)
        )
    }
    return _set_digest(retained), len(retained)


def _validate_capture_evidence(
    evidence: object,
    *,
    session_count: int,
    sticky: bool,
) -> None:
    if not isinstance(evidence, dict) or set(evidence) != CAPTURE_KEYS:
        raise ValueError("invalid Task 6 capture evidence")
    if type(evidence["passed"]) is not bool:
        raise ValueError("invalid Task 6 capture result")
    digests = evidence["digests"]
    counts = evidence["counts"]
    if (
        not isinstance(digests, dict)
        or not isinstance(counts, dict)
        or set(digests) != set(CAPTURE_RATES)
        or set(counts) != set(CAPTURE_RATES)
    ):
        raise ValueError("invalid Task 6 capture matrix")
    digest_counts: dict[str, int] = {}
    stable = True
    for rate in CAPTURE_RATES:
        rate_digests = digests[rate]
        rate_counts = counts[rate]
        if (
            not isinstance(rate_digests, list)
            or not isinstance(rate_counts, list)
            or len(rate_digests) != CAPTURE_ENTRIES_PER_RATE
            or len(rate_counts) != CAPTURE_ENTRIES_PER_RATE
        ):
            raise ValueError("incomplete Task 6 capture matrix")
        for digest, count in zip(rate_digests, rate_counts, strict=True):
            if (
                type(digest) is not str
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or type(count) is not int
                or not 0 <= count <= session_count
            ):
                raise ValueError("invalid Task 6 capture entry")
            known_count = digest_counts.setdefault(digest, count)
            if known_count != count:
                raise ValueError("incoherent Task 6 capture entry")
        stable = stable and len(set(rate_digests)) == 1 and len(set(rate_counts)) == 1
        if sticky or rate in {"0.0", "1.0"}:
            expected_digest, expected_count = _expected_sticky_capture(rate, session_count)
            if any(digest != expected_digest for digest in rate_digests) or any(
                count != expected_count for count in rate_counts
            ):
                raise ValueError("Task 6 capture evidence disagrees with frozen policy")
    if evidence["passed"] is not stable or evidence["passed"] is not sticky:
        raise ValueError("Task 6 capture result disagrees with its evidence")


def validate_report(report: dict) -> None:
    if not isinstance(report, dict) or set(report) != REPORT_KEYS:
        raise ValueError("invalid Task 6 statistical report shape")
    if report["schema"] != "task6-statistical-gate-v1":
        raise ValueError("invalid Task 6 statistical report schema")
    if (
        type(report["runs_per_point"]) is not int
        or type(report["capture_sessions"]) is not int
        or report["runs_per_point"] != 1_000
        or report["capture_sessions"] != 4_096
    ):
        raise ValueError("Task 6 statistical report used the wrong sample size")
    stationary = report["stationary"]
    mutated = report["trace_level_mutation"]
    power = report["power"]
    if not isinstance(stationary, list) or len(stationary) != len(STATIONARY_POINTS):
        raise ValueError("incomplete stationary report")
    if not isinstance(mutated, list) or len(mutated) != len(STATIONARY_POINTS):
        raise ValueError("incomplete stationary mutation report")
    if not isinstance(power, list) or len(power) != len(POWER_POINTS):
        raise ValueError("incomplete power report")
    for point in [*stationary, *mutated, *power]:
        _validate_point(point)
    if any(point["detections"] > 10 or point["confidence_high"] >= 0.02 for point in stationary):
        raise ValueError("stationary false-alert gate failed")
    if not any(
        point["detections"] > 10 or point["confidence_high"] >= 0.02 for point in mutated
    ):
        raise ValueError("trace-level mutation survived")
    for point, (_n, _shift, minimum, minimum_low) in zip(power, POWER_POINTS, strict=True):
        if point["detections"] < minimum or point["confidence_low"] < minimum_low:
            raise ValueError("power regression gate failed")
    _validate_capture_evidence(
        report["capture"], session_count=report["capture_sessions"], sticky=True
    )
    _validate_capture_evidence(
        report["call_local_mutation"],
        session_count=report["capture_sessions"],
        sticky=False,
    )
    if (
        report["capture"]["digests"] == report["call_local_mutation"]["digests"]
        and report["capture"]["counts"] == report["call_local_mutation"]["counts"]
    ):
        raise ValueError("call-local sampling mutation survived")


def main(output: Path) -> int:
    report = run_gate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output))
