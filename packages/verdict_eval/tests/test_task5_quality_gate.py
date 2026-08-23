from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import verdict_eval.cluster_registry as registry_module
from verdict_eval.cluster_registry import ClusterRegistryService
from verdict_eval.clustering_strategies import InputSelection


def _load_gate():
    path = Path(__file__).with_name("task5_quality_gate.py")
    spec = importlib.util.spec_from_file_location("task5_quality_gate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Task 5 quality gate")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _passing_values():
    return (
        {"ari": 0.61, "nmi": 0.80, "purity": 0.80, "outlier_rate": 0.10},
        {"ari": 0.60, "nmi": 0.70, "purity": 0.70, "outlier_rate": 0.10},
        {
            "ari": [0.50, 0.90],
            "nmi": [0.70, 0.90],
            "purity": [0.75, 0.90],
            "outlier_rate": [0.05, 0.20],
            "ari_improvement": [0.01, 0.30],
        },
        {"clusters": 5, "terminal_minimum": 5, "dominant_share": 0.30},
        {
            "exact_partition": True,
            "deletion_ari": [0.65, *([0.80] * 9)],
            "deletion_median": 0.80,
            "deletion_minimum": 0.65,
        },
    )


def test_quality_gate_synthetic_product_preflight_uses_scored_fit_path() -> None:
    gate._synthetic_product_preflight()


def test_quality_gate_preflight_rejects_rank_bypass(monkeypatch) -> None:
    def inverted_rank(tenant: str, trace_id: str) -> bytes:
        return bytes(255 - value for value in gate._rank(tenant, trace_id))

    monkeypatch.setattr(
        ClusterRegistryService,
        "_rank",
        staticmethod(inverted_rank),
    )
    with pytest.raises(gate.GateFailure, match="candidate rank bypassed"):
        gate._synthetic_product_preflight()


def test_quality_gate_preflight_rejects_first_user_selector(monkeypatch) -> None:
    def first_user(trace):
        for message in trace.raw_messages or []:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                return InputSelection(message["content"])
        return InputSelection(None, "no_supported_user_text")

    monkeypatch.setattr(registry_module, "select_cluster_input", first_user)
    with pytest.raises(gate.GateFailure, match="latest-user selector bypassed"):
        gate._synthetic_product_preflight()


@pytest.mark.parametrize(("clusters", "truth_intents"), [(5, 10), (15, 10)])
def test_quality_gate_acceptance_inclusive_boundaries_pass(
    clusters: int,
    truth_intents: int,
) -> None:
    candidate, baseline, intervals, summary, stability = _passing_values()
    summary["clusters"] = clusters
    improvement, fragmentation, checks, accepted = gate._acceptance(
        candidate,
        baseline,
        intervals,
        candidate_summary=summary,
        stability=stability,
        truth_intents=truth_intents,
    )
    assert improvement == pytest.approx(0.01)
    assert fragmentation == pytest.approx(clusters / truth_intents)
    assert all(checks.values())
    assert accepted is True


@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    [
        (("candidate", "ari", 0.60), "ari_improvement_point"),
        (("interval", "ari_improvement", [0.0, 0.30]), "ari_improvement_interval"),
        (("interval", "ari", [0.499, 0.90]), "ari_lower"),
        (("interval", "nmi", [0.699, 0.90]), "nmi_lower"),
        (("interval", "purity", [0.749, 0.90]), "purity_lower"),
        (("interval", "outlier_rate", [0.05, 0.201]), "outlier_upper"),
        (("summary", "clusters", 4), "useful_clusters"),
        (("truth_intents", None, 20), "fragmentation"),
        (("summary", "terminal_minimum", 4), "terminal_minimum"),
        (("summary", "dominant_share", 0.301), "dominant_share"),
        (("stability", "exact_partition", False), "order_stability"),
        (("stability", "deletion_ari", [0.79] * 10), "deletion_median"),
        (("stability", "deletion_ari", [0.64, *([0.90] * 9)]), "deletion_minimum"),
    ],
)
def test_quality_gate_acceptance_rejects_each_failed_gate(
    mutation,
    failed_check: str,
) -> None:
    candidate, baseline, intervals, summary, stability = deepcopy(_passing_values())
    truth_intents = 8 if mutation == ("summary", "clusters", 4) else 10
    target, name, value = mutation
    if target == "candidate":
        candidate[name] = value
    elif target == "interval":
        intervals[name] = value
    elif target == "summary":
        summary[name] = value
    elif target == "stability":
        stability[name] = value
        if name == "deletion_ari":
            stability["deletion_median"] = float(gate.np.median(value))
            stability["deletion_minimum"] = min(value)
    else:
        truth_intents = value
    _, _, checks, accepted = gate._acceptance(
        candidate,
        baseline,
        intervals,
        candidate_summary=summary,
        stability=stability,
        truth_intents=truth_intents,
    )
    assert checks[failed_check] is False
    assert accepted is False


def test_quality_gate_acceptance_rejects_missing_nonfinite_and_deleted_checks() -> None:
    candidate, baseline, intervals, summary, stability = _passing_values()
    del candidate["purity"]
    with pytest.raises(gate.GateFailure, match="point-estimate schema"):
        gate._acceptance(
            candidate,
            baseline,
            intervals,
            candidate_summary=summary,
            stability=stability,
            truth_intents=10,
        )

    candidate, baseline, intervals, summary, stability = _passing_values()
    intervals["ari"][0] = float("nan")
    with pytest.raises(gate.GateFailure, match="finite number"):
        gate._acceptance(
            candidate,
            baseline,
            intervals,
            candidate_summary=summary,
            stability=stability,
            truth_intents=10,
        )

    checks = {name: True for name in gate.EXPECTED_CHECKS}
    del checks["dominant_share"]
    with pytest.raises(gate.GateFailure, match="check schema"):
        gate._finalize_checks(checks)
