from verdict.dashboard.trace_facts import deterministic_trace_facts


def test_deterministic_trace_facts_reports_captured_response_structure():
    facts = deterministic_trace_facts(
        error=None,
        prompt="Answer as JSON",
        response='{"answer": "I am sorry, perhaps not"}',
    )

    assert facts == {
        "provider_outcome": "succeeded",
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

    assert facts["provider_outcome"] == "succeeded"
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
