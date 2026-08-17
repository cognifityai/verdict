from dataclasses import fields
from datetime import datetime, timezone

import verdict
from verdict.metrics import count_scores, verdict_label
from verdict.schema import (
    DimensionScore,
    DriftRun,
    DriftSignal,
    Judgment,
    Trace,
    Verdict,
)


def test_shared_verdict_label_uses_one_uppercase_contract():
    assert verdict_label(Verdict.PASS) == "PASS"
    assert verdict_label(Verdict.FAIL) == "FAIL"
    assert verdict_label(Verdict.UNCLEAR) == "UNCLEAR"
    assert verdict_label("pass") == "PASS"
    assert verdict_label("UnClEaR") == "UNCLEAR"


def test_shared_score_counts_exclude_unclear_from_pass_rate():
    counts = count_scores(
        [Verdict.PASS, "FAIL", "unclear", "malformed"],
        missing=2,
        errors=1,
    )

    assert counts.passed == 1
    assert counts.failed == 1
    assert counts.unclear == 2
    assert counts.missing == 2
    assert counts.errors == 1
    assert counts.evaluable == 2
    assert counts.pass_rate == 0.5
    assert counts.evaluability_rate == 2 / 7


def test_eval_and_core_imports_share_the_same_normalizer():
    from verdict_eval.judge import verdict_label as eval_verdict_label

    assert eval_verdict_label is verdict_label
    assert Judgment().pass_rate is None


def test_drift_run_is_available_from_the_published_package_namespace():
    assert verdict.DriftRun is DriftRun


def test_judgment_preserves_the_published_positional_constructor_order():
    """The first eight fields are the positional API published in 0.1.0a3."""
    published_prefix = [
        "judgment_id",
        "trace_id",
        "rubric_name",
        "rubric_version",
        "created_at",
        "judge_models",
        "dimensions",
        "position_swap_consistent",
    ]
    assert [field.name for field in fields(Judgment)[:8]] == published_prefix

    created_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
    dimensions = [DimensionScore(name="quality", verdict=Verdict.PASS)]
    judgment = Judgment(
        "legacy-id",
        "legacy-trace",
        "legacy-rubric",
        "7",
        created_at,
        ["legacy-judge"],
        dimensions,
        False,
    )

    assert judgment.position_swap_consistent is False
    assert judgment.evaluator_provider == ""
    assert judgment.status.value == "completed"


def test_extended_core_dataclasses_append_fields_after_published_constructors():
    published_trace_fields = [
        "trace_id", "started_at", "ended_at", "provider", "operation",
        "request_model", "response_model", "input_tokens", "output_tokens",
        "temperature", "max_tokens", "finish_reason", "error", "latency_ms",
        "prompt_redacted", "response_redacted", "raw_messages", "tenant_id",
        "session_id", "user_id_hash", "cluster_id", "tags", "cost_usd",
    ]
    published_drift_fields = [
        "signal_id", "detected_at", "cluster_id", "dimension", "direction",
        "statistic_name", "statistic_value", "p_value", "p_value_adjusted",
        "effect_size_cliffs_delta", "effect_size_cohens_d",
        "wasserstein_distance", "psi", "sample_size_current",
        "sample_size_baseline", "contributing_layers", "example_trace_ids",
        "recommended_action",
    ]

    assert [field.name for field in fields(Trace)[:23]] == published_trace_fields
    assert [field.name for field in fields(DriftSignal)[:18]] == published_drift_fields
