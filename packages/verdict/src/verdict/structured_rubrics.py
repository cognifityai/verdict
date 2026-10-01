"""Bounded element findings and deterministic conversation scoring."""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from verdict.redaction import redact

KIND = "element_scoring_v1"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,127}\Z")
_ADEQUACY = {"adequate": 0, "borderline": 1, "inadequate": 2, "critical": 3}
_CREDIT = {"adequate": Decimal(1), "borderline": Decimal("0.5"),
           "inadequate": Decimal("0.25"), "critical": Decimal(0)}
_BANDS = {"borderline": (70, 89), "inadequate": (50, 69), "critical": (0, 39)}
_RESERVED = {"standard_overall", "alternate_overall", "safety_gate"}


def _keys(value: object, expected: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid structured rubric fields")
    return value


def _id(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or redact(value) != value:
        raise ValueError("invalid structured rubric identifier")
    return value


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or (not empty and not value.strip()):
        raise ValueError("invalid structured rubric text")
    safe = redact(value)
    if safe is None or len(safe.encode("utf-8")) > maximum:
        raise ValueError("structured rubric text exceeds limit")
    return safe


def _number(value: object, low: float, high: float) -> float:
    if type(value) not in (int, float):
        raise ValueError("invalid structured rubric number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("invalid structured rubric number") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError("structured rubric number outside range")
    return number


def _weights(value: object, keys: set[str]) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("scoring weights must match their declared keys")
    weights = {key: _number(value[key], 0, 1) for key in sorted(keys)}
    if any(weight <= 0 for weight in weights.values()) or abs(sum(weights.values()) - 1) > 1e-9:
        raise ValueError("scoring weights must be positive and sum to one")
    return weights


def _labels(value: object) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ValueError("invalid scoring labels")
    labels = []
    for item in value:
        row = _keys(item, {"min", "label"})
        labels.append({"min": _number(row["min"], 0, 100),
                       "label": _text(row["label"], 128)})
    if labels[-1]["min"] != 0 or any(
        left["min"] <= right["min"] for left, right in pairwise(labels)
    ):
        raise ValueError("scoring labels must descend to zero")
    return labels


def validate_profile(value: dict) -> dict:
    """Canonicalize one explicit scoring format without changing simple rubrics."""
    if not isinstance(value, dict) or set(value) - {
        "name", "version", "target", "kind", "instructions", "catalog", "scoring",
        "dimensions", "fingerprint",
    } or value.get("kind") != KIND or value.get("target") != "conversation":
        raise ValueError("unsupported structured scoring profile")
    catalog = value.get("catalog")
    if not isinstance(catalog, dict) or not 1 <= len(catalog) <= 9:
        raise ValueError("structured rubric requires 1-9 categories")
    categories = {}
    total_elements = 0
    for key, raw in catalog.items():
        name = _id(key)
        if name in _RESERVED or not isinstance(raw, list) or not 1 <= len(raw) <= 100:
            raise ValueError("invalid structured rubric category")
        elements = []
        for item in raw:
            entry = _keys(item, {"phase", "element"})
            elements.append({"phase": _id(entry["phase"]), "element": _id(entry["element"])})
        if len({(item["phase"], item["element"]) for item in elements}) != len(elements):
            raise ValueError("duplicate structured rubric element")
        total_elements += len(elements)
        categories[name] = elements
    if total_elements > 100:
        raise ValueError("structured rubric exceeds element limit")
    names = set(categories)
    scoring = _keys(value.get("scoring"), {
        "modes", "weights", "deduplicate", "optional_elements", "bonus", "indices", "gate",
        "labels", "alternate", "review_below",
    })
    modes = scoring["modes"]
    if (not isinstance(modes, dict) or set(modes) != names
            or any(mode not in ("coverage", "violation") for mode in modes.values())):
        raise ValueError("invalid category scoring modes")
    weights = _weights(scoring["weights"], names)
    order = scoring["deduplicate"]
    if (not isinstance(order, list) or any(type(item) is not str for item in order)
            or len(order) != len(set(order))
            or any(item not in names for item in order)):
        raise ValueError("invalid evidence deduplication order")
    optional = scoring["optional_elements"]
    if not isinstance(optional, list) or len(optional) > total_elements:
        raise ValueError("invalid optional elements")
    optional_keys = set()
    catalog_keys = {(category, item["phase"], item["element"])
                    for category, elements in categories.items() for item in elements}
    for item in optional:
        entry = _keys(item, {"category", "phase", "element"})
        key = tuple(_id(entry[name]) for name in ("category", "phase", "element"))
        if key not in catalog_keys or key in optional_keys:
            raise ValueError("invalid optional element reference")
        optional_keys.add(key)
    bonus = scoring["bonus"]
    if bonus is not None:
        bonus = _keys(bonus, {"category", "requires_category", "min_confidence", "points"})
        if (type(bonus["category"]) is not str or type(bonus["requires_category"]) is not str
                or bonus["category"] not in names or bonus["requires_category"] not in names
                or modes[bonus["category"]] != "coverage"):
            raise ValueError("invalid scoring bonus category")
        bonus = {"category": bonus["category"], "requires_category": bonus["requires_category"],
                 "min_confidence": _number(bonus["min_confidence"], 0, 1),
                 "points": _number(bonus["points"], 0, 100)}
    indices = scoring["indices"]
    if not isinstance(indices, dict) or not 1 <= len(indices) <= 5:
        raise ValueError("invalid score indices")
    normalized_indices = {}
    for key, members in indices.items():
        name = _id(key)
        if (name in names | _RESERVED or not isinstance(members, list)
                or not members or any(type(member) is not str for member in members)
                or len(set(members)) != len(members)
                or any(member not in names for member in members)):
            raise ValueError("invalid score index members")
        normalized_indices[name] = members
    gate = _keys(scoring["gate"], {"minimums", "index", "index_minimum", "label"})
    minimums = gate["minimums"]
    if (not isinstance(minimums, dict) or not minimums
            or any(name not in names for name in minimums)
            or type(gate["index"]) is not str or gate["index"] not in normalized_indices):
        raise ValueError("invalid score gate")
    normalized_gate = {
        "minimums": {name: _number(threshold, 0, 100)
                     for name, threshold in sorted(minimums.items())},
        "index": gate["index"],
        "index_minimum": _number(gate["index_minimum"], 0, 100),
        "label": _text(gate["label"], 128),
    }
    alternate = _keys(scoring["alternate"], {"weights", "labels"})
    raw_alternate_weights = alternate["weights"]
    if not isinstance(raw_alternate_weights, dict) or not 1 <= len(raw_alternate_weights) <= 8:
        raise ValueError("invalid alternate score dimensions")
    alternate_names = {_id(name) for name in raw_alternate_weights}
    if alternate_names & names:
        raise ValueError("alternate score names overlap categories")
    normalized_scoring = {
        "modes": {name: modes[name] for name in sorted(names)},
        "weights": weights, "deduplicate": order,
        "optional_elements": [{"category": category, "phase": phase, "element": element}
                              for category, phase, element in sorted(optional_keys)],
        "bonus": bonus,
        "indices": {name: normalized_indices[name] for name in sorted(normalized_indices)},
        "gate": normalized_gate, "labels": _labels(scoring["labels"]),
        "alternate": {"weights": _weights(raw_alternate_weights, alternate_names),
                      "labels": _labels(alternate["labels"])},
        "review_below": _number(scoring["review_below"], 0, 1),
    }
    dimensions = [
        {"name": name, "description": f"Computed {name} score", "type": "number",
         "min": 0, "max": 100, "direction": "higher_is_better"}
        for name in sorted(names)
    ]
    dimensions += [
        {"name": name, "description": f"Computed {name} score", "type": "number",
         "min": 0, "max": 100, "direction": "higher_is_better"}
        for name in ("standard_overall", "alternate_overall")
    ]
    dimensions.append({"name": "safety_gate", "description": "Declared gate state",
                       "type": "binary"})
    if value.get("dimensions", dimensions) != dimensions:
        raise ValueError("structured dimensions must be derived from scoring")
    return {
        "name": _id(value.get("name")), "version": _id(value.get("version")),
        "target": "conversation", "kind": KIND,
        "instructions": _text(value.get("instructions", ""), 64_000, empty=True),
        "catalog": {name: categories[name] for name in sorted(names)},
        "scoring": normalized_scoring, "dimensions": dimensions,
    }


def _rounded(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _band(score: float, labels: list[dict]) -> str:
    return next(row["label"] for row in labels if score >= row["min"])


def _canonical_element(
    item: object, category: str, expected: set[tuple[str, str]],
    optional: set[tuple[str, str, str]], messages: list[dict],
) -> dict:
    raw = _keys(item, {"phase", "element", "applicable", "adequacy", "description", "quote", "message_position"})
    phase, name = _id(raw["phase"]), _id(raw["element"])
    applicable = raw["applicable"]
    if (phase, name) not in expected or type(applicable) is not bool:
        raise ValueError("unknown structured finding")
    if (applicable and (type(raw["adequacy"]) is not str or raw["adequacy"] not in _ADEQUACY)):
        raise ValueError("unknown structured adequacy")
    if not applicable and (raw["adequacy"] is not None or raw["quote"] is not None
                           or raw["message_position"] is not None):
        raise ValueError("unknown structured finding")
    if not applicable and (category, phase, name) not in optional:
        raise ValueError("element cannot be skipped")
    quote, position = raw["quote"], raw["message_position"]
    if quote is not None:
        if (type(position) is not int or not 0 <= position < len(messages)
                or _text(quote, 1_000) != quote or quote not in messages[position]["content"]):
            raise ValueError("structured finding quote absent from evidence")
    elif position is not None:
        raise ValueError("structured finding position requires quote")
    return {"phase": phase, "element": name, "applicable": applicable,
            "adequacy": raw["adequacy"],
            "description": _text(raw["description"], 1_000),
            "quote": quote, "message_position": position}


def score_output(
    rubric: dict, value: object, messages: list[dict], enabled_phases: list[str] | None
) -> tuple[dict, dict]:
    """Validate judge findings, then calculate every persisted score ourselves."""
    if not isinstance(value, dict):
        raise ValueError("invalid structured judge output")
    supplied = value.get("computed") if set(value) == {"output", "computed"} else None
    output = value["output"] if set(value) == {"output", "computed"} else value
    if not isinstance(output, dict):
        raise ValueError("invalid structured judge output")
    scoring = rubric["scoring"]
    optional = {(item["category"], item["phase"], item["element"])
                for item in scoring["optional_elements"]}
    route = output.get("route")
    dimensions = {d["name"]: {"state": "unclear", "score": None, "reason": "Other route"}
                  for d in rubric["dimensions"]}
    if route == "alternate":
        raw = _keys(output, {"route", "alternate"})
        alternate = _keys(raw["alternate"], {
            "scores", "score_reasons", "adequacy", "rationale", "context", "critical_flags",
        })
        weights = scoring["alternate"]["weights"]
        scores = alternate["scores"]
        if not isinstance(scores, dict) or set(scores) != set(weights):
            raise ValueError("alternate scores differ from rubric")
        normalized_scores = {}
        for name in weights:
            score = scores[name]
            if type(score) is not int or not 1 <= score <= 5:
                raise ValueError("alternate scores must be integers 1-5")
            normalized_scores[name] = score
        reasons = alternate["score_reasons"]
        if not isinstance(reasons, dict) or set(reasons) != set(weights):
            raise ValueError("alternate score reasons differ from rubric")
        if type(alternate["adequacy"]) is not str or alternate["adequacy"] not in _ADEQUACY:
            raise ValueError("invalid alternate adequacy")
        flags = alternate["critical_flags"]
        if not isinstance(flags, list) or len(flags) > 10:
            raise ValueError("invalid alternate flags")
        canonical = {"route": "alternate", "alternate": {
            "scores": normalized_scores,
            "score_reasons": {name: _text(reasons[name], 1_000) for name in weights},
            "adequacy": alternate["adequacy"],
            "rationale": _text(alternate["rationale"], 4_000),
            "context": _text(alternate["context"], 128),
            "critical_flags": [_text(flag, 512) for flag in flags],
        }}
        weighted = sum(Decimal(score) * Decimal(str(weights[name]))
                       for name, score in normalized_scores.items())
        score = _rounded(weighted * 20)
        published = _rounded(Decimal(str(score)), "1")
        computed = {"route": "alternate", "score": score, "overall": published,
                    "label": _band(published, scoring["alternate"]["labels"])}
        dimensions["alternate_overall"] = {"state": "unclear", "score": score,
                                            "reason": "Computed alternate score"}
    elif route == "standard":
        raw = _keys(output, {"route", "enabled_phases", "categories"})
        known_phases = {element["phase"] for items in rubric["catalog"].values()
                        for element in items} - {"general"}
        phases = raw["enabled_phases"]
        if (not isinstance(phases, list) or any(type(phase) is not str for phase in phases)
                or len(phases) != len(set(phases))
                or any(phase not in known_phases for phase in phases)
                or (bool(known_phases) and not enabled_phases)
                or set(phases) != set(enabled_phases or [])):
            raise ValueError("invalid enabled phases")
        if not isinstance(raw["categories"], dict) or set(raw["categories"]) != set(rubric["catalog"]):
            raise ValueError("judge categories differ from rubric")
        categories = {}
        for name, catalog in rubric["catalog"].items():
            category = _keys(raw["categories"][name], {"confidence", "elements"})
            expected = {(e["phase"], e["element"]) for e in catalog
                        if e["phase"] == "general" or e["phase"] in phases}
            elements = category["elements"]
            if not isinstance(elements, list) or len(elements) != len(expected):
                raise ValueError("judge element count differs from rubric")
            normalized = [_canonical_element(item, name, expected, optional, messages)
                          for item in elements]
            if {(e["phase"], e["element"]) for e in normalized} != expected:
                raise ValueError("judge elements differ from rubric")
            if not any(e["applicable"] for e in normalized):
                raise ValueError("standard category has no applicable elements")
            categories[name] = {"confidence": _number(category["confidence"], 0, 1),
                                "elements": normalized}
        canonical = {"route": "standard", "enabled_phases": sorted(phases),
                     "categories": categories}
        excluded: set[tuple[str, str, str]] = set()
        seen: dict[str, tuple[str, int]] = {}
        for name in scoring["deduplicate"]:
            for item in categories[name]["elements"]:
                quote = item["quote"]
                if not item["applicable"] or item["adequacy"] == "adequate" or not quote:
                    continue
                normalized = " ".join(quote.split()).casefold()
                previous = seen.get(normalized)
                if previous is not None and previous[0] != name:
                    if _ADEQUACY[item["adequacy"]] > previous[1]:
                        raise ValueError("deduplication would hide a more severe finding")
                    excluded.add((name, item["phase"], item["element"]))
                else:
                    seen[normalized] = (name, _ADEQUACY[item["adequacy"]])
        scores = {}
        for name, category in categories.items():
            elements = [e for e in category["elements"] if e["applicable"]
                        if (name, e["phase"], e["element"]) not in excluded]
            if not elements:
                raise ValueError("deduplication removed an assessed category")
            if scoring["modes"][name] == "coverage":
                score = _rounded(Decimal(100) * sum(
                    (_CREDIT[e["adequacy"]] for e in elements), Decimal(0)
                ) / Decimal(len(elements))) if elements else 100.0
            else:
                worst = max((_ADEQUACY[e["adequacy"]] for e in elements), default=0)
                if worst == 0:
                    score = 100.0
                else:
                    severity = next(key for key, rank in _ADEQUACY.items() if rank == worst)
                    minimum, maximum = _BANDS[severity]
                    count = sum(e["adequacy"] == severity for e in elements)
                    score = float(max(minimum, maximum - 5 * (count - 1)))
            scores[name] = {"score": score, "assessed": bool(elements),
                            "confidence": category["confidence"]}
        bonus = scoring["bonus"]
        if bonus is not None:
            beneficiary = categories[bonus["category"]]
            required = categories[bonus["requires_category"]]
            beneficiary_elements = [e for e in beneficiary["elements"] if e["applicable"]]
            required_elements = [e for e in required["elements"] if e["applicable"]]
            if (beneficiary_elements and required_elements
                    and all(e["adequacy"] == "adequate" for e in beneficiary_elements)
                    and all(e["adequacy"] == "adequate" for e in required_elements)
                    and beneficiary["confidence"] >= bonus["min_confidence"]):
                scores[bonus["category"]]["score"] = min(
                    100.0, scores[bonus["category"]]["score"] + bonus["points"]
                )
        indices = {name: _rounded(sum(
            (Decimal(str(scores[member]["score"])) for member in members), Decimal(0)
        ) / Decimal(len(members))) for name, members in scoring["indices"].items()}
        raw_weighted = _rounded(sum(
            (Decimal(str(scores[name]["score"])) * Decimal(str(weight))
             for name, weight in scoring["weights"].items()), Decimal(0)
        ))
        gate = scoring["gate"]
        activated = (any(scores[name]["score"] < minimum
                         for name, minimum in gate["minimums"].items())
                     or indices[gate["index"]] < gate["index_minimum"])
        capped = min(raw_weighted, indices[gate["index"]]) if activated else raw_weighted
        overall = _rounded(Decimal(str(capped)), "1")
        label = gate["label"] if activated else _band(overall, scoring["labels"])
        aggregate_confidence = _rounded(sum(
            (Decimal(str(category["confidence"])) for category in categories.values()),
            Decimal(0)
        ) / Decimal(len(categories)) * 100)
        computed = {"route": "standard", "categories": scores, "indices": indices,
                    "raw_weighted": raw_weighted, "overall": overall, "label": label,
                    "gate": activated, "aggregate_confidence": aggregate_confidence,
                    "review_categories": [name for name, category in categories.items()
                                          if category["confidence"] < scoring["review_below"]],
                    "deduplicated": [list(item) for item in sorted(excluded)]}
        for name, category in scores.items():
            dimensions[name] = {"state": "unclear", "score": category["score"]
                                if category["assessed"] else None,
                                "reason": "Computed category score" if category["assessed"]
                                else "Not assessed"}
        dimensions["standard_overall"] = {"state": "unclear", "score": overall,
                                           "reason": "Computed standard score"}
        dimensions["safety_gate"] = {"state": "fail" if activated else "pass",
                                     "score": None, "reason": "Activated" if activated
                                     else "Not activated"}
    else:
        raise ValueError("unknown structured scoring route")
    if supplied is not None and supplied != computed:
        raise ValueError("stored structured scores contradict source findings")
    return {"output": canonical, "computed": computed}, dimensions
