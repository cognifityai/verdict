from verdict_eval.injector import (
    CorruptionInjector,
    CorruptionKind,
    build_corruption_battery,
)

SAMPLE_Q = "What is the capital of France?"
SAMPLE_R = "The capital of France is Paris, located in the north of the country."


def test_none_corruption_is_noop():
    inj = CorruptionInjector()
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.NONE)
    assert out.corrupted_response == SAMPLE_R
    assert out.kind == CorruptionKind.NONE


def test_refusal_returns_boilerplate():
    inj = CorruptionInjector()
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.REFUSAL)
    assert "can't help" in out.corrupted_response.lower()
    assert "Paris" not in out.corrupted_response


def test_truncation_shortens_response():
    inj = CorruptionInjector()
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.TRUNCATION, strength=1.0)
    assert len(out.corrupted_response) < len(SAMPLE_R)


def test_off_topic_drops_original_content():
    inj = CorruptionInjector()
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.OFF_TOPIC)
    assert "Paris" not in out.corrupted_response


def test_pii_leak_injects_fake_pii():
    inj = CorruptionInjector()
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.PII_LEAK)
    # At least one PII pattern should appear
    cr = out.corrupted_response
    assert any(s in cr for s in ["SSN", "Credit card", "Driver's license"])


def test_prompt_injection_echo_contains_instruction():
    inj = CorruptionInjector()
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.PROMPT_INJECTION_ECHO)
    assert "Ignore previous instructions" in out.corrupted_response


def test_hallucination_extends_response():
    inj = CorruptionInjector(seed=1)
    out = inj.corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.HALLUCINATION, strength=0.3)
    assert out.corrupted_response != SAMPLE_R
    assert out.kind == CorruptionKind.HALLUCINATION


def test_deterministic_with_seed():
    a = CorruptionInjector(seed=99).corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.HALLUCINATION, 1.0)
    b = CorruptionInjector(seed=99).corrupt(SAMPLE_Q, SAMPLE_R, CorruptionKind.HALLUCINATION, 1.0)
    assert a.corrupted_response == b.corrupted_response


def test_build_corruption_battery_includes_baseline_and_all_kinds():
    samples = [(SAMPLE_Q, SAMPLE_R), ("What's 2+2?", "Two plus two equals four.")]
    battery = build_corruption_battery(samples, strengths=[1.0])
    # 2 inputs x (1 baseline + 7 kinds x 1 strength) = 16
    assert len(battery) == 2 * (1 + 7)
    kinds = {s.kind for s in battery}
    # All categories should appear
    expected_kinds = set(CorruptionInjector().all_categories()) | {CorruptionKind.NONE}
    assert kinds == expected_kinds
