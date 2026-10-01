"""Executable element scoring contracts; all examples are fictional."""

from __future__ import annotations

from copy import deepcopy

import pytest
from verdict.conversation_assessments import validate_assessment, validate_rubric
from verdict.conversations import validate_conversation
from verdict.storage.sqlite import SQLiteStorage


def rubric():
    return {
        "name": "fictional_review", "version": "1", "target": "conversation",
        "kind": "element_scoring_v1", "instructions": "Assess fictional messages.",
        "catalog": {
            "coverage": [{"phase": "general", "element": "question"},
                         {"phase": "general", "element": "followup"}],
            "errors": [{"phase": "general", "element": "accuracy"}],
            "timeline": [{"phase": "general", "element": "sequence"}],
        },
        "scoring": {
            "modes": {"coverage": "coverage", "errors": "violation", "timeline": "coverage"},
            "weights": {"coverage": 0.4, "errors": 0.4, "timeline": 0.2},
            "deduplicate": ["errors", "coverage"],
            "optional_elements": [{"category": "coverage", "phase": "general", "element": "followup"}],
            "bonus": {"category": "timeline", "requires_category": "errors",
                      "min_confidence": 0.6, "points": 5},
            "indices": {"risk": ["coverage", "errors"]},
            "gate": {"minimums": {"coverage": 50, "errors": 60},
                     "index": "risk", "index_minimum": 65, "label": "Unsafe"},
            "labels": [{"min": 90, "label": "Excellent"}, {"min": 75, "label": "Strong"},
                       {"min": 60, "label": "Adequate"}, {"min": 40, "label": "Weak"},
                       {"min": 0, "label": "Unsafe"}],
            "alternate": {
                "weights": {"recognition": 0.5, "response": 0.5},
                "labels": [{"min": 75, "label": "Strong"},
                           {"min": 0, "label": "Weak"}],
            },
            "review_below": 0.7,
        },
    }


def conversation():
    return validate_conversation({
        "id": "a" * 32, "tenant_id": "tenant", "source_scope": "b" * 16,
        "messages": [{"role": "user", "content": "Question and sequence."},
                     {"role": "assistant", "content": "Answer and followup."}],
        "event_at": "2026-09-01T12:00:00Z", "end_status": "complete",
    })


def element(name, adequacy="adequate", *, quote=None, position=None, applicable=True):
    return {"phase": "general", "element": name, "applicable": applicable,
            "adequacy": adequacy if applicable else None,
            "description": "Fictional assessment.", "quote": quote,
            "message_position": position}


def standard_output():
    return {"route": "standard", "enabled_phases": [], "categories": {
        "coverage": {"confidence": 0.9, "elements": [element("question"), element("followup")]},
        "errors": {"confidence": 0.9, "elements": [element("accuracy")]},
        "timeline": {"confidence": 0.9, "elements": [element("sequence")]},
    }}


def assessment(rule, output):
    row = conversation()
    result = validate_assessment({
        "tenant_id": row["tenant_id"], "conversation_id": row["id"],
        "revision": row["revision"], "target_position": None,
        "rubric": rule, "evaluator": {"provider": "local", "model": "synthetic",
            "rubric_fingerprint": rule["fingerprint"], "prompt_version": "structured_v1",
            "max_output_tokens": 4096},
        "status": "completed", "dimensions": {}, "findings": [],
        "structured": output, "evaluated_at": "2026-09-01T12:01:00Z",
    }, row)
    assert validate_assessment(result, row) == result
    return result


def test_structured_rubric_is_executable_and_simple_identity_is_unchanged():
    simple = validate_rubric({"name": "quality", "version": "1", "target": "conversation",
                              "dimensions": [{"name": "helpful", "description": "Addresses request."}]})
    assert simple["fingerprint"] == "632855989b2ddd84c8b7abb6328a4d301f843a67296263a6a7013509568470f9"
    rule = validate_rubric(rubric())
    assert rule["kind"] == "element_scoring_v1"
    assert [d["name"] for d in rule["dimensions"]] == [
        "coverage", "errors", "timeline", "standard_overall", "alternate_overall", "safety_gate"
    ]
    assert validate_rubric(rule) == rule


def test_standard_scores_are_calculated_and_gate_overrides_label():
    rule = validate_rubric(rubric())
    output = standard_output()
    output["categories"]["coverage"]["elements"] = [
        element("question", "critical"), element("followup", "critical")]
    result = assessment(rule, output)
    scored = result["structured"]["computed"]
    assert scored["categories"]["coverage"]["score"] == 0
    assert scored["categories"]["timeline"]["score"] == 100
    assert scored["raw_weighted"] == 60
    assert scored["indices"]["risk"] == 50
    assert scored["overall"] == 50
    assert scored["label"] == "Unsafe"
    assert scored["gate"] is True
    assert result["dimensions"]["safety_gate"]["state"] == "fail"
    assert result["dimensions"]["alternate_overall"]["score"] is None


def test_alternate_route_never_scores_standard_categories():
    rule = validate_rubric(rubric())
    result = assessment(rule, {"route": "alternate", "alternate": {
        "scores": {"recognition": 5, "response": 1}, "adequacy": "inadequate",
        "score_reasons": {"recognition": "Detected.", "response": "Delayed."},
        "rationale": "Fictional alternate path.", "context": "synthetic urgent case",
        "critical_flags": []}})
    assert result["structured"]["computed"]["overall"] == 60
    assert result["dimensions"]["alternate_overall"]["score"] == 60
    assert result["dimensions"]["coverage"]["score"] is None
    assert result["dimensions"]["safety_gate"]["state"] == "unclear"


def test_inapplicable_element_is_visible_and_excluded_from_score():
    rule = validate_rubric(rubric())
    output = standard_output()
    output["categories"]["coverage"]["elements"] = [
        element("question", "inadequate"),
        element("followup", applicable=False),
    ]
    result = assessment(rule, output)
    assert result["structured"]["computed"]["categories"]["coverage"]["score"] == 25
    assert result["structured"]["output"]["categories"]["coverage"]["elements"][1]["applicable"] is False


def test_all_inapplicable_category_cannot_publish_overall():
    output = standard_output()
    profile = rubric()
    profile["scoring"]["optional_elements"].append(
        {"category": "coverage", "phase": "general", "element": "question"}
    )
    output["categories"]["coverage"]["elements"] = [
        element("question", applicable=False), element("followup", applicable=False),
    ]
    with pytest.raises(ValueError, match="no applicable"):
        assessment(validate_rubric(profile), output)


def test_judge_cannot_skip_a_required_element():
    output = standard_output()
    output["categories"]["coverage"]["elements"][0] = element("question", applicable=False)
    with pytest.raises(ValueError, match="cannot be skipped"):
        assessment(validate_rubric(rubric()), output)


def test_deduplication_cannot_hide_more_severe_finding():
    output = standard_output()
    output["categories"]["errors"]["elements"] = [
        element("accuracy", "borderline", quote="Question and sequence.", position=0)
    ]
    output["categories"]["coverage"]["elements"][0] = element(
        "question", "critical", quote="Question and sequence.", position=0
    )
    with pytest.raises(ValueError, match="more severe"):
        assessment(validate_rubric(rubric()), output)


def test_deduplication_cannot_publish_empty_category_as_perfect():
    output = standard_output()
    output["categories"]["errors"]["elements"] = [
        element("accuracy", "critical", quote="Question and sequence.", position=0)
    ]
    output["categories"]["coverage"]["elements"] = [
        element("question", "critical", quote="Question and sequence.", position=0),
        element("followup", "critical", quote="Question and sequence.", position=0),
    ]
    with pytest.raises(ValueError, match="removed an assessed category"):
        assessment(validate_rubric(rubric()), output)


def test_equal_severity_duplicate_is_removed_but_distinct_evidence_is_kept():
    rule = validate_rubric(rubric())
    output = standard_output()
    output["categories"]["errors"]["elements"] = [
        element("accuracy", "critical", quote="Question and sequence.", position=0)
    ]
    output["categories"]["coverage"]["elements"][0] = element(
        "question", "critical", quote="Question and sequence.", position=0
    )
    duplicate = assessment(rule, output)["structured"]["computed"]
    assert duplicate["deduplicated"] == [["coverage", "general", "question"]]
    assert duplicate["gate"] is True
    output["categories"]["coverage"]["elements"][0] = element(
        "question", "critical", quote="Answer and followup.", position=1
    )
    distinct = assessment(rule, output)["structured"]["computed"]
    assert distinct["deduplicated"] == []
    assert distinct["categories"]["coverage"]["score"] == 50
    output["categories"]["coverage"]["elements"][0] = element("question", "critical")
    without_quote = assessment(rule, output)["structured"]["computed"]
    assert without_quote["deduplicated"] == []


def test_alternate_route_can_grade_without_source_phases():
    profile = rubric()
    profile["catalog"]["coverage"][0]["phase"] = "intake"
    rule = validate_rubric(profile)
    result = assessment(rule, {"route": "alternate", "alternate": {
        "scores": {"recognition": 5, "response": 5},
        "score_reasons": {"recognition": "Detected.", "response": "Prompt response."},
        "adequacy": "adequate", "rationale": "Special escalation condition.",
        "context": "synthetic urgent case", "critical_flags": [],
    }})
    assert result["structured"]["computed"]["overall"] == 100
    assert result["dimensions"]["safety_gate"]["state"] == "unclear"


def test_source_phases_bind_standard_judge_findings():
    profile = rubric()
    profile["catalog"]["coverage"][0]["phase"] = "intake"
    rule = validate_rubric(profile)
    row = conversation()
    from verdict.conversation_assessments import evaluation_targets

    assert evaluation_targets(row, rule) == ((None,), None)
    row = validate_conversation({**row, "enabled_phases": ["intake"], "revision": None})
    assert evaluation_targets(row, rule) == ((None,), None)
    output = standard_output()
    output["enabled_phases"] = []
    output["categories"]["coverage"]["elements"][0]["phase"] = "intake"
    from verdict.structured_rubrics import score_output

    with pytest.raises(ValueError, match="enabled phases"):
        score_output(rule, output, row["messages"], row["enabled_phases"])
    output["enabled_phases"] = ["intake"]
    assert score_output(rule, output, row["messages"], row["enabled_phases"])[0]["computed"]["overall"] == 100
    with pytest.raises(ValueError, match="enabled phases"):
        score_output(rule, {**output, "enabled_phases": []}, row["messages"], None)


def test_structured_finding_text_is_redacted_at_storage_sink(tmp_path):
    output = standard_output()
    output["categories"]["errors"]["elements"][0]["description"] = "Email clinician@example.org."
    grade = assessment(validate_rubric(rubric()), output)
    storage = SQLiteStorage(str(tmp_path / "grades.db"))
    storage.save_conversation(conversation())
    storage.save_conversation_assessment(grade)
    storage.close()
    assert b"clinician@example.org" not in (tmp_path / "grades.db").read_bytes()


@pytest.mark.parametrize("mutation", [
    lambda r: r["scoring"]["weights"].update(coverage=0.5),
    lambda r: r["catalog"]["coverage"].append({"phase": "general", "element": "question"}),
    lambda r: r.update(kind="unknown"),
])
def test_invalid_profile_fails_closed(mutation):
    candidate = deepcopy(rubric())
    mutation(candidate)
    with pytest.raises(ValueError):
        validate_rubric(candidate)


@pytest.mark.parametrize("mutation", [
    lambda o: o["categories"]["coverage"]["elements"].pop(),
    lambda o: o["categories"]["coverage"]["elements"][0].update(adequacy="invented"),
    lambda o: o["categories"]["errors"]["elements"][0].update(quote="Invented", message_position=1),
])
def test_incomplete_or_invented_judge_evidence_fails_closed(mutation):
    output = standard_output()
    mutation(output)
    with pytest.raises(ValueError):
        assessment(validate_rubric(rubric()), output)


@pytest.mark.parametrize("mutation", [
    lambda r: r["scoring"]["modes"].update(coverage=[]),
    lambda r: r["scoring"].update(deduplicate=[{}]),
    lambda r: r["scoring"]["bonus"].update(category=[]),
    lambda r: r["scoring"]["indices"].update(risk=[{}]),
    lambda r: r["scoring"]["gate"].update(index=[]),
])
def test_nested_unhashable_rubric_value_is_validation_error(mutation):
    candidate = deepcopy(rubric())
    mutation(candidate)
    with pytest.raises(ValueError):
        validate_rubric(candidate)


@pytest.mark.parametrize("mutation", [
    lambda o: o.update(enabled_phases=[{}]),
    lambda o: o["categories"]["coverage"]["elements"][0].update(adequacy=[]),
])
def test_nested_unhashable_judge_value_is_validation_error(mutation):
    output = standard_output()
    mutation(output)
    with pytest.raises(ValueError):
        assessment(validate_rubric(rubric()), output)
