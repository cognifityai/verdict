import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from verdict.schema import (
    DimensionScore,
    DriftSignal,
    EvaluatorHealthRecord,
    Judgment,
    Trace,
    Verdict,
)
from verdict.storage import SQLiteStorage

from ui.server import _CSP, _cluster_health, _signal_provider, build_bundle, create_app


def test_signal_provider_resolves_demo_alias():
    assert _signal_provider("haiku", {"anthropic"}, {}) == "anthropic"


def test_signal_provider_rejects_demo_alias_when_provider_is_absent():
    assert _signal_provider("haiku", {"openai"}, {}) is None


def test_signal_provider_prefers_factual_cluster_provider_over_demo_alias():
    clusters = {"haiku": {"openai"}}
    assert _signal_provider("haiku", {"anthropic", "openai"}, clusters) == "openai"


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
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="cost-test-evaluator",
        expected_dimensions=["relevance"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(name="relevance", verdict=Verdict.FAIL)],
    ))
    storage.insert_drift_signal(DriftSignal(
        cluster_id="support",
        dimension="relevance",
        evaluator_fingerprint="cost-test-evaluator",
        example_trace_ids=[trace.trace_id],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalCost"] is None
    assert bundle["meta"]["totalCostStatus"] == "unavailable"
    assert bundle["driftSignals"][0]["exampleTraceIds"] == [trace.trace_id]


def test_bundle_filters_drift_by_selected_evaluator_and_excludes_historical_rows(tmp_path):
    path = tmp_path / "mixed-drift-evaluators.db"
    storage = SQLiteStorage(str(path))
    for suffix, fingerprint, verdict in (
        ("a", "fingerprint-a", Verdict.PASS),
        ("b", "fingerprint-b", Verdict.FAIL),
    ):
        trace = Trace(
            trace_id=f"trace-{suffix}",
            provider="openai",
            cluster_id="support",
            prompt_redacted="Prompt",
            response_redacted="Response",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            evaluator_provider="fake",
            evaluator_config={"temperature": 0},
            evaluator_fingerprint=fingerprint,
            expected_dimensions=["quality"],
            judge_models=[f"judge-{suffix}"],
            dimensions=[DimensionScore(name="quality", verdict=verdict)],
        ))
        storage.insert_drift_signal(DriftSignal(
            signal_id=f"signal-{suffix}",
            cluster_id="support",
            dimension=f"quality-{suffix}",
            evaluator_fingerprint=fingerprint,
        ))
    storage.insert_drift_signal(DriftSignal(
        signal_id="historical-signal",
        cluster_id="support",
        dimension="historical",
    ))
    storage.close()

    ambiguous = build_bundle(path)
    assert ambiguous["evaluation"]["status"] == "selection_required"
    assert ambiguous["evaluation"]["driftStatus"] == "selection_required"
    assert ambiguous["driftSignals"] == []

    evaluator_a = next(
        identity for identity in ambiguous["evaluation"]["availableIdentities"]
        if identity["fingerprint"] == "fingerprint-a"
    )
    selected = build_bundle(path, evaluator_id=evaluator_a["id"])
    assert [signal["id"] for signal in selected["driftSignals"]] == ["signal-a"]
    assert selected["evaluation"]["driftStatus"] == "selected"
    assert selected["evaluation"]["unattributedDriftSignals"] == 1


def test_bundle_builds_independent_cluster_pass_rate_series(tmp_path):
    path = tmp_path / "verdict.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for hour, cluster, verdict in [
        (0, "refund", Verdict.PASS),
        (0, "shipping", Verdict.PASS),
        (1, "refund", Verdict.FAIL),
        (1, "shipping", Verdict.PASS),
    ]:
        trace = Trace(
            started_at=started + timedelta(hours=hour),
            provider="openai",
            request_model="gpt-4o-mini",
            cluster_id=cluster,
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            dimensions=[DimensionScore(name="quality", verdict=verdict)],
        ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["clusterPassrate"] == [
        {"hour": 0, "refund": 100.0, "shipping": 100.0},
        {"hour": 1, "refund": 0.0, "shipping": 100.0},
    ]


def test_bundle_requires_one_evaluator_identity_instead_of_mixing_rows(tmp_path):
    """P0-2: incompatible judge/rubric rows must not be pooled or last-row-wins."""
    path = tmp_path / "mixed-evaluators.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        request_model="gpt-4o-mini",
        cluster_id="refund",
        prompt_redacted="Can I get a refund?",
        response_redacted="Yes.",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="old-evaluator",
        trace_id=trace.trace_id,
        rubric_name="support-quality",
        rubric_version="1",
        judge_models=["judge-model-a"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.insert_judgment(Judgment(
        judgment_id="new-evaluator",
        trace_id=trace.trace_id,
        rubric_name="support-quality",
        rubric_version="2",
        judge_models=["judge-model-b"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.UNCLEAR)],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["evaluation"]["status"] == "selection_required"
    assert len(bundle["evaluation"]["availableIdentities"]) == 2
    assert bundle["dimensionOverall"] == []
    assert "judgment" not in bundle["samples"][0]

    old_identity = next(
        identity for identity in bundle["evaluation"]["availableIdentities"]
        if identity["models"] == ["judge-model-a"]
    )
    selected = build_bundle(path, evaluator_id=old_identity["id"])
    assert selected["evaluation"]["status"] == "selected"
    assert selected["dimensionOverall"][0]["passRate"] == 100.0
    assert selected["samples"][0]["judgment"]["judges"] == ["judge-model-a"]


def test_bundle_exposes_only_selected_evaluator_sentinel_health(tmp_path):
    path = tmp_path / "judge-health.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        cluster_id="refund",
        prompt_redacted="Prompt",
        response_redacted="Response",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_config={"temperature": 0},
        evaluator_fingerprint="selected-fingerprint",
        expected_dimensions=["quality"],
        judge_models=["judge-model"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.insert_evaluator_health(EvaluatorHealthRecord(
        evaluator_fingerprint="selected-fingerprint",
        sentinel_set_name="support-v1",
        sentinel_set_fingerprint="set-one",
        correct_labels=27,
        total_labels=30,
        agreement=0.9,
        confidence_low=0.74,
        confidence_high=0.97,
        status="healthy",
    ))
    storage.insert_evaluator_health(EvaluatorHealthRecord(
        evaluator_fingerprint="other-fingerprint",
        sentinel_set_name="other-v1",
        sentinel_set_fingerprint="set-two",
        correct_labels=0,
        total_labels=30,
        agreement=0,
        confidence_low=0,
        confidence_high=0.11,
        status="degraded",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["evaluation"]["selectedIdentity"]["fingerprint"] == (
        "selected-fingerprint"
    )
    assert bundle["evaluatorHealth"] == [{
        "id": bundle["evaluatorHealth"][0]["id"],
        "evaluatedAt": bundle["evaluatorHealth"][0]["evaluatedAt"],
        "sentinelSetName": "support-v1",
        "sentinelSetFingerprint": "set-one",
        "correctLabels": 27,
        "totalLabels": 30,
        "agreement": 90.0,
        "confidenceLow": 74.0,
        "confidenceHigh": 97.0,
        "status": "healthy",
        "errorCount": 0,
    }]


def test_bundle_excludes_unclear_from_every_pass_rate_denominator(tmp_path):
    """P0-3: PASS / (PASS + FAIL); UNCLEAR is coverage, not failure."""
    path = tmp_path / "unclear-denominators.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, verdict in enumerate((Verdict.PASS, Verdict.FAIL, Verdict.UNCLEAR)):
        trace = Trace(
            trace_id=f"trace-{index}",
            started_at=started,
            provider="openai",
            request_model="gpt-4o-mini",
            cluster_id="refund",
            prompt_redacted=f"Prompt {index}",
            response_redacted=f"Response {index}",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            rubric_name="quality",
            rubric_version="1",
            judge_models=["judge-model"],
            dimensions=[
                DimensionScore(name="quality", verdict=verdict),
                DimensionScore(name="unclear_only", verdict=Verdict.UNCLEAR),
            ],
        ))
    storage.close()

    bundle = build_bundle(path)
    by_dimension = {row["dim"]: row for row in bundle["dimensionOverall"]}

    assert by_dimension["quality"] == {
        "dim": "quality",
        "passRate": 50.0,
        "pass": 1,
        "fail": 1,
        "unclear": 1,
        "tot": 3,
    }
    assert by_dimension["unclear_only"]["passRate"] is None
    assert bundle["providers"][0]["passRate"] == 50.0
    assert bundle["passrate"] == [{"hour": 0, "openai": 50.0}]
    assert bundle["clusterPassrate"] == [{"hour": 0, "refund": 50.0}]
    assert bundle["scoreCoverage"] == {
        "pass": 1, "fail": 1, "unclear": 4,
        "missing": 0, "error": 0, "evaluable": 2,
    }
    sample_by_id = {sample["trace_id"]: sample for sample in bundle["samples"]}
    assert sample_by_id["trace-1"]["judgment"]["summary"]["status"] == "fail"
    assert sample_by_id["trace-2"]["judgment"]["summary"]["status"] == "unclear"


def test_bundle_uses_latest_duplicate_independent_of_insertion_order(tmp_path):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    outputs = []
    for filename, order in (("forward.db", ("old", "new")), ("reverse.db", ("new", "old"))):
        path = tmp_path / filename
        storage = SQLiteStorage(str(path))
        trace = Trace(
            trace_id="trace-1",
            started_at=created,
            provider="openai",
            cluster_id="refund",
            prompt_redacted="Prompt",
            response_redacted="Response",
        )
        storage.insert_trace(trace)
        judgments = {
            "old": Judgment(
                judgment_id="old",
                trace_id=trace.trace_id,
                created_at=created,
                judge_models=["judge-model"],
                dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
            ),
            "new": Judgment(
                judgment_id="new",
                trace_id=trace.trace_id,
                created_at=created + timedelta(seconds=1),
                judge_models=["judge-model"],
                dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
            ),
        }
        for name in order:
            storage.insert_judgment(judgments[name])
        storage.close()
        outputs.append(build_bundle(path))

    for bundle in outputs:
        assert bundle["meta"]["totalJudged"] == 1
        assert bundle["dimensionOverall"][0] == {
            "dim": "quality", "passRate": 0.0,
            "pass": 0, "fail": 1, "unclear": 0, "tot": 1,
        }
        assert bundle["samples"][0]["judgment"]["dims"][0]["verdict"] == "fail"
    assert outputs[0]["dimensionOverall"] == outputs[1]["dimensionOverall"]


def test_bundle_preserves_unknown_provider_and_model_values_without_crashing(tmp_path):
    path = tmp_path / "extension-values.db"
    storage = SQLiteStorage(str(path))
    values = [
        ("custom-gateway", "vendor/model:beta"),
        ("custom.with.dot", "model.one"),
        ("__provider_null__", "literal-marker"),
        ("", ""),
        (None, None),
        ("vendor/模型", "model with spaces"),
        ("x" * 300, "y" * 300),
    ]
    for index, (provider, model) in enumerate(values):
        storage.insert_trace(Trace(
            trace_id=f"trace-{index}",
            provider=provider,
            request_model=model,
            cluster_id="extensions",
            prompt_redacted=f"Prompt {index}",
            response_redacted=f"Response {index}",
        ))
    storage.close()

    bundle = build_bundle(path)

    raw_providers = [provider["rawProvider"] for provider in bundle["providers"]]
    assert {
        "custom-gateway", "custom.with.dot", "__provider_null__", "", None,
        "vendor/模型", "x" * 300,
    } == set(raw_providers)
    assert all(provider["key"] for provider in bundle["providers"])
    assert all(
        provider["key"] in {"anthropic", "openai", "google"}
        or (
            provider["key"].startswith("provider_")
            and len(provider["key"]) == len("provider_") + 16
        )
        for provider in bundle["providers"]
    )
    assert len({provider["key"] for provider in bundle["providers"]}) == len(values)
    assert all(sample["providerKey"] for sample in bundle["samples"])


def test_bundle_labels_provider_aggregate_with_multiple_models_explicitly(tmp_path):
    path = tmp_path / "multiple-models.db"
    storage = SQLiteStorage(str(path))
    for model in ("model-a", "model-b"):
        storage.insert_trace(Trace(
            provider="custom-gateway",
            request_model=model,
            prompt_redacted="Prompt",
        ))
    storage.close()

    provider = build_bundle(path)["providers"][0]

    assert provider["model"] == "multiple models (2)"
    assert provider["models"] == ["model-a", "model-b"]


def test_bundle_never_reemits_content_canaries_from_storage(tmp_path):
    import json

    path = tmp_path / "redaction-boundary.db"
    storage = SQLiteStorage(str(path))
    canary = "api-secret@example.com"
    trace = Trace(
        provider="custom-gateway",
        request_model="model",
        prompt_redacted=f"Prompt {canary}",
        response_redacted=f"Response {canary}",
        error=f"Error {canary}",
        raw_messages=[{
            "role": "assistant",
            "content": [{"type": "tool_result", "content": {"email": canary}}],
        }],
    )
    storage.insert_trace(trace)
    stored = storage.get_trace(trace.trace_id)
    storage.close()

    bundle = build_bundle(path)

    assert canary not in json.dumps(bundle, sort_keys=True)
    assert canary not in repr(stored)


def test_bundle_redacts_legacy_rows_that_bypassed_current_storage_boundary(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "legacy-privacy.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="legacy-trace",
        provider="openai",
        cluster_id="legacy",
        prompt_redacted="safe",
        response_redacted="safe",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="legacy-judgment",
        trace_id=trace.trace_id,
        judge_models=["legacy-judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
    ))
    storage.close()

    canary = "legacy-leak@example.com"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE traces SET prompt_redacted=?, response_redacted=?, error=?",
            (canary, canary, canary),
        )
        connection.execute(
            "UPDATE judgments SET dimensions_json=?",
            (json.dumps([{
                "name": "quality",
                "verdict": "fail",
                "reasoning": canary,
                "judge_model": "legacy-judge",
            }]),),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert canary not in json.dumps(bundle, sort_keys=True)


def test_bundle_supports_database_without_historical_drift_table(tmp_path):
    path = tmp_path / "pre-drift-schema.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(provider="openai", prompt_redacted="Prompt"))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE drift_signals")
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"] == []
    assert bundle["evaluation"]["driftStatus"] == "empty"
    assert bundle["evaluation"]["unattributedDriftSignals"] == 0


def test_bundle_preserves_nullable_historical_drift_statistics(tmp_path):
    path = tmp_path / "nullable-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="nullable-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.insert_drift_signal(DriftSignal(
        signal_id="legacy-null",
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="nullable-evaluator",
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE drift_signals SET statistic_value=NULL, "
            "effect_size_cliffs_delta=NULL, effect_size_cohens_d=NULL "
            "WHERE signal_id='legacy-null'"
        )
        connection.commit()
    finally:
        connection.close()

    signal = build_bundle(path)["driftSignals"][0]

    assert signal["stat"] is None
    assert signal["cliffsDelta"] is None
    assert signal["cohensD"] is None


def test_bundle_preserves_unclear_coverage_statistic_precision(tmp_path):
    path = tmp_path / "coverage-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="openai", cluster_id="support", prompt_redacted="Prompt")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="coverage-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    for signal_id, statistic in (("coverage-42", 0.42), ("coverage-37", 0.37)):
        storage.insert_drift_signal(DriftSignal(
            signal_id=signal_id,
            cluster_id="support",
            dimension="quality",
            evaluator_fingerprint="coverage-evaluator",
            statistic_name="unclear_rate_increase",
            statistic_value=statistic,
        ))
    storage.close()

    statistics = {
        signal["id"]: signal["stat"] for signal in build_bundle(path)["driftSignals"]
    }

    assert statistics == {"coverage-42": 0.42, "coverage-37": 0.37}


def test_nameless_historical_dimension_remains_unclear_in_trace_summary(tmp_path):
    import json

    path = tmp_path / "nameless-dimension.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="trace-nameless",
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        judgment_id="judgment-nameless",
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="nameless-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE judgments SET dimensions_json=? WHERE judgment_id=?",
            (json.dumps([
                {"name": "quality", "verdict": "pass"},
                {"name": "", "verdict": "pass"},
            ]), "judgment-nameless"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)
    [sample] = bundle["samples"]

    assert bundle["scoreCoverage"]["unclear"] == 1
    assert sample["judgment"]["summary"] == {
        "status": "unclear",
        "pass": 1,
        "fail": 0,
        "unclear": 1,
        "missing": 0,
        "passRate": 100.0,
    }
    assert sample["judgment"]["dims"] == [{
        "name": "quality", "verdict": "pass", "reasoning": "",
    }]


def test_bundle_treats_malformed_historical_drift_numbers_as_unavailable(tmp_path):
    path = tmp_path / "malformed-drift.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="openai", cluster_id="support", prompt_redacted="Prompt")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="malformed-number-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.insert_drift_signal(DriftSignal(
        signal_id="legacy-malformed-number",
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="malformed-number-evaluator",
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE drift_signals SET statistic_value='not-a-number', "
            "effect_size_cliffs_delta='not-a-number', "
            "effect_size_cohens_d='not-a-number' "
            "WHERE signal_id='legacy-malformed-number'"
        )
        connection.commit()
    finally:
        connection.close()

    signal = build_bundle(path)["driftSignals"][0]

    assert signal["stat"] is None
    assert signal["cliffsDelta"] is None
    assert signal["cohensD"] is None


def test_bundle_handles_drift_table_missing_additive_effect_columns(tmp_path):
    path = tmp_path / "pre-effect-columns.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        provider="openai",
        cluster_id="support",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="legacy-effects-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE drift_signals")
        connection.executescript(
            """
            CREATE TABLE drift_signals (
                signal_id TEXT PRIMARY KEY,
                detected_at TEXT NOT NULL,
                cluster_id TEXT,
                dimension TEXT,
                direction TEXT,
                evaluator_fingerprint TEXT,
                statistic_name TEXT,
                statistic_value REAL,
                p_value REAL,
                p_value_adjusted REAL,
                effect_size_cohens_d REAL,
                sample_size_current INTEGER,
                sample_size_baseline INTEGER,
                contributing_layers_json TEXT,
                example_trace_ids_json TEXT,
                recommended_action TEXT
            );
            """
        )
        connection.execute(
            """INSERT INTO drift_signals (
                signal_id, detected_at, cluster_id, dimension, direction,
                evaluator_fingerprint, statistic_name, statistic_value,
                p_value, p_value_adjusted, effect_size_cohens_d,
                sample_size_current, sample_size_baseline
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-effects",
                "2026-08-01T00:00:00+00:00",
                "support",
                "quality",
                "regression",
                "legacy-effects-evaluator",
                "mann_whitney_u",
                8.0,
                0.01,
                0.02,
                -0.4,
                30,
                30,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    signal = build_bundle(path)["driftSignals"][0]

    assert signal["id"] == "legacy-effects"
    assert signal["cliffsDelta"] is None
    assert signal["cohensD"] == -0.4


def test_bundle_normalizes_mixed_naive_and_aware_historical_timestamps(tmp_path):
    path = tmp_path / "mixed-timestamps.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="legacy-naive",
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        provider="openai",
        prompt_redacted="Legacy",
    ))
    storage.insert_trace(Trace(
        trace_id="current-aware",
        started_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        provider="openai",
        prompt_redacted="Current",
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE traces SET started_at = ? WHERE trace_id = ?",
            ("2026-01-01T00:00:00", "legacy-naive"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["durationHours"] == 1
    assert {sample["trace_id"] for sample in bundle["samples"]} == {
        "legacy-naive", "current-aware",
    }


def test_bundle_treats_malformed_historical_latency_as_unavailable(tmp_path):
    path = tmp_path / "malformed-latency.db"
    storage = SQLiteStorage(str(path))
    storage.insert_trace(Trace(
        trace_id="bad-latency",
        provider="openai",
        prompt_redacted="Prompt",
        latency_ms=12.5,
    ))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE traces SET latency_ms = ? WHERE trace_id = ?",
            ("not-a-number", "bad-latency"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["providers"][0]["avgLatency"] == 0.0
    assert bundle["samples"][0]["latency_ms"] is None


def test_bundle_handles_sqlite_file_without_verdict_tables(tmp_path):
    path = tmp_path / "empty-schema.db"
    sqlite3.connect(path).close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalTraces"] == 0
    assert bundle["evaluation"]["status"] == "empty"


def test_bundle_handles_historical_database_without_judgments_table(tmp_path):
    path = tmp_path / "traces-only.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE traces (
                trace_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                provider TEXT,
                request_model TEXT,
                cluster_id TEXT,
                latency_ms REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                error TEXT,
                prompt_redacted TEXT,
                response_redacted TEXT,
                finish_reason TEXT
            )"""
        )
        connection.execute(
            """INSERT INTO traces (
                trace_id, started_at, provider, request_model, prompt_redacted
            ) VALUES (?, ?, ?, ?, ?)""",
            ("legacy", "2026-01-01T00:00:00", "openai", "legacy-model", "Prompt"),
        )
        connection.commit()
    finally:
        connection.close()

    bundle = build_bundle(path)

    assert bundle["meta"]["totalTraces"] == 1
    assert bundle["evaluation"]["status"] == "empty"


def test_mixed_provider_lead_signal_falls_back_to_real_chart_series(tmp_path):
    path = tmp_path / "mixed-provider-focus.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index, provider in enumerate(("anthropic", "openai")):
        trace = Trace(
            trace_id=f"trace-{provider}",
            started_at=started + timedelta(hours=index),
            provider=provider,
            cluster_id="mixed-support",
            prompt_redacted="Prompt",
        )
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            evaluator_provider="fake",
            evaluator_fingerprint="mixed-evaluator",
            evaluator_config={"temperature": 0},
            expected_dimensions=["relevance"],
            judge_models=["judge"],
            dimensions=[DimensionScore(name="relevance", verdict=Verdict.PASS)],
        ))
    storage.insert_drift_signal(DriftSignal(
        cluster_id="mixed-support",
        dimension="relevance",
        evaluator_fingerprint="mixed-evaluator",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"][0]["provider"] == ""
    assert bundle["focusProvider"] == "anthropic"
    assert bundle["focusProviderLabel"] == "anthropic"
    assert any(row["relevance"] == 100.0 for row in bundle["haikuDim"])


def test_custom_rubric_dimension_populates_focused_dimension_series(tmp_path):
    path = tmp_path / "custom-dimension-focus.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(
        trace_id="custom-dimension",
        provider="openai",
        cluster_id="agent",
        prompt_redacted="Prompt",
    )
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="custom-dimension-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["action_correctness"],
        judge_models=["judge"],
        dimensions=[DimensionScore(
            name="action_correctness",
            verdict=Verdict.PASS,
        )],
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["dimensionOverall"][0]["dim"] == "action_correctness"
    assert bundle["haikuDim"] == [{"hour": 0, "action_correctness": 100.0}]


def test_multi_provider_bundle_omits_unused_cluster_time_series(tmp_path):
    path = tmp_path / "multi-provider-clusters.db"
    storage = SQLiteStorage(str(path))
    for provider, cluster in (("openai", "refund"), ("anthropic", "shipping")):
        trace = Trace(provider=provider, cluster_id=cluster)
        storage.insert_trace(trace)
        storage.insert_judgment(Judgment(
            trace_id=trace.trace_id,
            dimensions=[DimensionScore(name="quality", verdict=Verdict.PASS)],
        ))
    storage.close()

    bundle = build_bundle(path)

    assert len(bundle["providers"]) == 2
    assert bundle["clusterPassrate"] == []


def test_time_series_size_is_bounded_by_observed_bins_not_elapsed_wall_time(tmp_path):
    path = tmp_path / "sparse-years.db"
    storage = SQLiteStorage(str(path))
    for trace_id, started_at, verdict in (
        ("old", datetime(2000, 1, 1, tzinfo=timezone.utc), Verdict.PASS),
        ("new", datetime(2026, 1, 1, tzinfo=timezone.utc), Verdict.FAIL),
    ):
        storage.insert_trace(Trace(
            trace_id=trace_id,
            started_at=started_at,
            provider="openai",
            cluster_id="support",
        ))
        storage.insert_judgment(Judgment(
            trace_id=trace_id,
            dimensions=[DimensionScore(name="quality", verdict=verdict)],
        ))
    storage.close()

    bundle = build_bundle(path)

    assert len(bundle["tsRows"]) == 2
    assert len(bundle["passrate"]) == 2
    assert len(bundle["clusterPassrate"]) == 2


def test_dashboard_reads_pre_cluster_schema_without_503_or_mutating_it(tmp_path):
    path = tmp_path / "legacy-dashboard.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE traces (
            trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
            provider TEXT, operation TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, temperature REAL,
            max_tokens INTEGER, finish_reason TEXT, error TEXT, latency_ms REAL,
            prompt_redacted TEXT, response_redacted TEXT, raw_messages_json TEXT,
            tenant_id TEXT, session_id TEXT, user_id_hash TEXT,
            tags_json TEXT, cost_usd REAL
        );
        INSERT INTO traces (
            trace_id, started_at, provider, request_model, prompt_redacted
        ) VALUES (
            'legacy', '2026-01-01T00:00:00+00:00', 'openai', 'legacy-model', 'Prompt'
        );
        """
    )
    connection.close()

    before = path.read_bytes()
    bundle = build_bundle(path)

    assert bundle["meta"]["totalTraces"] == 1
    assert bundle["clusters"] == []
    assert bundle["samples"][0]["cluster_id"] is None
    assert path.read_bytes() == before


def test_fisher_odds_ratio_keeps_small_nonzero_value(tmp_path):
    path = tmp_path / "odds-ratio.db"
    storage = SQLiteStorage(str(path))
    trace = Trace(provider="openai", cluster_id="support")
    storage.insert_trace(trace)
    storage.insert_judgment(Judgment(
        trace_id=trace.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["quality"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="quality", verdict=Verdict.FAIL)],
    ))
    storage.insert_drift_signal(DriftSignal(
        cluster_id="support",
        dimension="quality",
        evaluator_fingerprint="evaluator",
        statistic_name="fisher_exact",
        statistic_value=0.04,
    ))
    storage.close()

    bundle = build_bundle(path)
    assert bundle["driftSignals"][0]["stat"] == 0.04


def test_custom_provider_late_trace_is_selected_using_chart_safe_key(tmp_path):
    path = tmp_path / "custom-provider-late.db"
    storage = SQLiteStorage(str(path))
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(45):
        storage.insert_trace(Trace(
            trace_id=f"early-{index:02d}",
            started_at=started + timedelta(minutes=index),
            provider="openai",
            cluster_id="other",
            prompt_redacted="Early",
        ))
    late = Trace(
        trace_id="late-custom",
        started_at=started + timedelta(hours=5),
        provider="custom-gateway",
        cluster_id="custom-cluster",
        prompt_redacted="Late custom",
    )
    storage.insert_trace(late)
    storage.insert_judgment(Judgment(
        trace_id=late.trace_id,
        evaluator_provider="fake",
        evaluator_fingerprint="custom-evaluator",
        evaluator_config={"temperature": 0},
        expected_dimensions=["relevance"],
        judge_models=["judge"],
        dimensions=[DimensionScore(name="relevance", verdict=Verdict.FAIL)],
    ))
    storage.insert_drift_signal(DriftSignal(
        cluster_id="custom-cluster",
        dimension="relevance",
        evaluator_fingerprint="custom-evaluator",
    ))
    storage.close()

    bundle = build_bundle(path)

    assert bundle["driftSignals"][0]["provider"].startswith("provider_")
    assert "late-custom" in {sample["trace_id"] for sample in bundle["samples"]}


def test_authenticated_cors_preflight_reaches_cors_middleware(monkeypatch):
    import httpx

    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    monkeypatch.setenv("VERDICT_CORS_ORIGINS", "https://review.example")
    async def request_preflight():
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.options(
                "/api/data",
                headers={
                    "Origin": "https://review.example",
                    "Access-Control-Request-Method": "GET",
                },
            )

    response = asyncio.run(request_preflight())

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://review.example"


def test_basic_auth_gates_dashboard_and_data_but_not_public_routes(monkeypatch):
    import base64

    import httpx

    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    app = create_app()

    async def requests():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = base64.b64encode(b"reviewer:secret").decode()
            return (
                await client.get("/"),
                await client.get("/api/health"),
                await client.get("/dashboard"),
                await client.get("/api/data"),
                await client.get("/dashboard", headers={"Authorization": f"Basic {token}"}),
            )

    landing, health, dashboard, data, authenticated = asyncio.run(requests())

    assert landing.status_code == 200
    assert health.status_code == 200
    assert dashboard.status_code == 401
    assert data.status_code == 401
    assert dashboard.headers["www-authenticate"] == 'Basic realm="Verdict"'
    assert authenticated.status_code == 200


def test_basic_auth_compares_both_credentials_without_username_short_circuit(monkeypatch):
    import base64
    import secrets

    import httpx

    calls = []
    real_compare = secrets.compare_digest

    def counting_compare(left, right):
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setenv("VERDICT_USER", "reviewer")
    monkeypatch.setenv("VERDICT_PASS", "secret")
    monkeypatch.setattr(secrets, "compare_digest", counting_compare)
    app = create_app()

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            token = base64.b64encode(b"wrong:also-wrong").decode()
            return await client.get(
                "/dashboard", headers={"Authorization": f"Basic {token}"}
            )

    response = asyncio.run(request())

    assert response.status_code == 401
    assert calls == [("wrong", "reviewer"), ("also-wrong", "secret")]
