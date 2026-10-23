from verdict.trace_facts import (
    deterministic_trace_facts,
    trace_evidence_state,
    trace_judge_evidence_digest,
)


def test_trace_evidence_state_never_infers_provider_success():
    assert trace_evidence_state(error="timeout", ended_at=None) == "error_recorded"
    assert trace_evidence_state(error="", ended_at=None) == "error_recorded"
    assert trace_evidence_state(error=None, ended_at="2026-10-19T00:00:00Z") == "ended_no_error_recorded"
    assert trace_evidence_state(error=None, ended_at=None) == "unfinished"


def test_unfinished_trace_with_partial_response_is_not_judge_eligible():
    facts = deterministic_trace_facts(
        error=None, ended_at=None, prompt="question", response="partial answer",
    )
    assert facts["response_present"] is True
    assert facts["judge_eligible"] is False
    assert facts["not_evaluable_reason"] == "trace_unfinished"
    completed = deterministic_trace_facts(
        error=None, ended_at="2026-10-19T00:00:00Z",
        prompt="question", response="partial answer",
    )
    assert completed["judge_eligible"] is True
    failed = deterministic_trace_facts(
        error="", ended_at=None, prompt="question", response="partial answer",
    )
    assert failed["not_evaluable_reason"] == "provider_call_failed"


def test_deterministic_trace_facts_reports_captured_response_structure():
    facts = deterministic_trace_facts(
        error=None,
        prompt="Answer as JSON",
        response='{"answer": "I am sorry, perhaps not"}',
    )

    assert facts == {
        "provider_outcome": "no_error_recorded",
        "prompt_present": True,
        "response_present": True,
        "judge_eligible": True,
        "not_evaluable_reason": None,
        "response_characters": 37,
        "valid_json": True,
        "refusal_signature": False,
        "apology_start": False,
        "hedge_phrases": 1,
    }


def test_deterministic_trace_facts_preserves_missing_evidence_as_unavailable():
    facts = deterministic_trace_facts(error=None, prompt="prompt", response="   ")

    assert facts["provider_outcome"] == "no_error_recorded"
    assert facts["response_present"] is False
    assert facts["judge_eligible"] is False
    assert facts["not_evaluable_reason"] == "response_not_captured"
    assert facts["response_characters"] == 3
    assert facts["valid_json"] is None
    assert facts["refusal_signature"] is None
    assert facts["apology_start"] is None
    assert facts["hedge_phrases"] is None


def test_deterministic_trace_facts_prioritizes_provider_failure():
    facts = deterministic_trace_facts(
        error="rate limit",
        prompt="prompt",
        response="I can't help with that",
    )

    assert facts["provider_outcome"] == "failed"
    assert facts["not_evaluable_reason"] == "provider_call_failed"
    assert facts["judge_eligible"] is False
    assert facts["refusal_signature"] is True


def test_judge_evidence_digest_is_deterministic_and_handles_unpaired_surrogates():
    evidence = {"error": None, "prompt": "prompt\ud800", "response": "response"}

    first = trace_judge_evidence_digest(**evidence)

    assert first == trace_judge_evidence_digest(**evidence)
    assert first != trace_judge_evidence_digest(**(evidence | {"response": "changed"}))
