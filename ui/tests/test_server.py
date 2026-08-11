from verdict.schema import DriftSignal, Trace
from verdict.storage import SQLiteStorage

from ui.server import _CSP, _cluster_health, _signal_provider, build_bundle


def test_signal_provider_resolves_demo_alias():
    assert _signal_provider("haiku", {"anthropic"}, {}) == "anthropic"


def test_signal_provider_accepts_direct_provider_key():
    assert _signal_provider("anthropic", {"anthropic", "openai"}, {}) == "anthropic"


def test_signal_provider_accepts_single_provider_cluster():
    clusters = {"billing": {"openai"}}
    assert _signal_provider("billing", {"anthropic", "openai"}, clusters) == "openai"


def test_signal_provider_rejects_mixed_provider_cluster():
    clusters = {"billing": {"anthropic", "openai"}}
    assert _signal_provider("billing", {"anthropic", "openai"}, clusters) is None


def test_cluster_health_exposes_fragmentation_to_dashboard():
    health = _cluster_health([f"c{i}" for i in range(8)])

    assert health["status"] == "fragmented"
    assert health["medianClusterSize"] == 1
    assert health["messages"]


def test_cluster_health_distinguishes_low_volume_from_fragmentation():
    health = _cluster_health(["a"] * 20 + ["b"] * 20)

    assert health["status"] == "underpowered"
    assert health["clustersMeetingSampleFloor"] == 0


def test_content_security_policy_allows_only_local_scripts():
    assert "script-src 'self'" in _CSP
    assert "'unsafe-eval'" not in _CSP
    assert "cdn.tailwindcss.com" not in _CSP
    assert "cdnjs.cloudflare.com" not in _CSP
    assert "unpkg.com" not in _CSP


def test_bundle_marks_unknown_cost_and_exposes_signal_examples(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="anthropic",
        request_model="unknown-model",
        cluster_id="support",
        prompt_redacted="help",
        response_redacted="response",
        cost_usd=None,
    )
    storage.insert_trace(trace)
    storage.insert_drift_signal(DriftSignal(
        cluster_id="support",
        dimension="relevance",
        example_trace_ids=[trace.trace_id],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalCost"] is None
    assert bundle["meta"]["totalCostStatus"] == "unavailable"
    assert bundle["driftSignals"][0]["exampleTraceIds"] == [trace.trace_id]
