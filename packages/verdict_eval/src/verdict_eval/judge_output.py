"""One decoder for every judge reply.

A judge asks a model for JSON and receives it wrapped the way models wrap
things: in a Markdown fence, after a sentence of prose, inside an outer
``{"dimensions": ...}`` object, or with the per-dimension results as a list of
objects that each carry ``name`` instead of an object keyed by name. Both
judge paths decode through this module so that a new wrapper is fixed once,
and both reject the same things: duplicate keys, duplicate dimension names,
non-finite numbers, and oversized replies.

``OUTPUT_CONTRACT_VERSION`` is part of every evaluator identity. Bump it when
the decode rules or the field contract change: a different decoder turns the
same provider reply into a different judgment, so it is a different evaluator.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

OUTPUT_CONTRACT_VERSION = "judge_output_v2"
MAX_OUTPUT_BYTES = 64_000
_MAX_CANDIDATE_STARTS = 64
_TRUNCATED_FINISH_REASONS = frozenset({"LENGTH", "MAX_TOKENS"})
_REASON_KEYS = ("reason", "reasoning")
_SCALARS = (str, int, float, bool)


class JudgeOutputError(ValueError):
    """The judge reply cannot be read as a result for the declared dimensions."""


class JudgeOutputUnusable(JudgeOutputError):
    """The reply is not a judgment at all: cut off at the output ceiling or oversized.

    Callers record this as a judge error (retryable) rather than as UNCLEAR
    dimensions, because the provider never delivered a complete answer.
    """


@dataclass(frozen=True)
class DimensionFields:
    """The fields a judge may report for one dimension, in canonical form."""

    verdict: str | None
    score: object
    reason: str
    findings: list


def is_truncated(finish_reason: object) -> bool:
    """True when the provider stopped at its output ceiling, in any provider's spelling.

    OpenAI and LiteLLM report ``length``, Anthropic ``max_tokens``, and Gemini
    ``MAX_TOKENS`` (sometimes as ``FinishReason.MAX_TOKENS``).
    """
    if finish_reason is None:
        return False
    name = str(finish_reason).rsplit(".", 1)[-1].strip().upper()
    return name in _TRUNCATED_FINISH_REASONS


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise JudgeOutputError("duplicate judge output key")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise JudgeOutputError(f"non-finite judge output number: {name}")


_DECODER = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant,
)


def _candidates(text: str) -> Iterator[object]:
    """Yield every JSON object or array that starts in the text, in order.

    ``raw_decode`` finds the structural end of each value, so fences, prose,
    and braces inside JSON strings are never mistaken for structure. The scan
    is bounded so malformed output cannot force unbounded decode attempts. A
    duplicate key or non-finite number anywhere in a decodable value rejects
    the whole reply: continuing the scan could otherwise select an inner
    object of a value that contradicted itself.
    """
    starts = 0
    for index, character in enumerate(text):
        if character not in "{[":
            continue
        starts += 1
        if starts > _MAX_CANDIDATE_STARTS:
            raise JudgeOutputError("judge output has too many candidate values")
        try:
            candidate, _ = _DECODER.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        yield candidate


def _named_items(items: list) -> dict[str, object] | None:
    """Key a list of dimension results by their ``name``; None when it is not one."""
    if not all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in items):
        return None
    result: dict[str, object] = {}
    for item in items:
        if item["name"] in result:
            raise JudgeOutputError("duplicate judge dimension")
        result[item["name"]] = {key: item[key] for key in item if key != "name"}
    return result


def _by_name(value: object) -> dict[str, object]:
    """Normalize the dimension results to one mapping keyed by dimension name."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        named = _named_items(value)
        if named is None:
            raise JudgeOutputError("judge dimension list item requires a name")
        return named
    raise JudgeOutputError("judge dimensions must be an object or a list")


def decode_judge_dimensions(
    text: object, expected: Iterable[str], *, max_bytes: int = MAX_OUTPUT_BYTES,
) -> dict[str, object]:
    """Return ``{dimension_name: raw result}`` from a judge reply.

    Accepted shapes, anywhere in the text: an object keyed by dimension name;
    ``{"dimensions": <that object>}``; ``{"dimensions": [<objects with "name">]}``;
    or a bare list of objects with ``name``. An object with a ``dimensions``
    key is the judge's answer even when it names no expected dimension; a flat
    object or bare list is selected only when it names at least one expected
    dimension, so unrelated JSON in surrounding prose is skipped. Values are
    returned as the judge wrote them; use :func:`dimension_fields` to read one.

    Raises :class:`JudgeOutputError` when nothing usable is found or the reply
    is oversized, self-contradictory (duplicate keys or names), or non-finite.
    """
    if not isinstance(text, str):
        raise JudgeOutputError("judge output is not text")
    if len(text.encode("utf-8")) > max_bytes:
        raise JudgeOutputUnusable("judge output exceeds size limit")
    expected_names = set(expected)
    for candidate in _candidates(text):
        if isinstance(candidate, dict):
            if "dimensions" in candidate:
                return _by_name(candidate["dimensions"])
            named: dict[str, object] | None = candidate
        elif isinstance(candidate, list):
            named = _named_items(candidate)
        else:
            continue
        if named is not None and expected_names.intersection(named):
            return named
    raise JudgeOutputError("judge output contains no result for the rubric dimensions")


def dimension_fields(item: object) -> DimensionFields:
    """Read one dimension result, tolerating the field spellings judges use.

    ``verdict`` is upper-cased text (``None`` when absent); ``reason`` accepts
    ``reasoning`` as an alias and is empty when absent; ``findings`` is a list,
    empty when absent. Keys the judge echoes from the rubric, such as ``type``,
    are ignored. Structural mistakes raise :class:`JudgeOutputError`.
    """
    if not isinstance(item, dict):
        raise JudgeOutputError("judge dimension result must be an object")
    verdict = item.get("verdict")
    if verdict is not None:
        if not isinstance(verdict, _SCALARS):
            raise JudgeOutputError("judge verdict must be text")
        verdict = str(verdict).strip().upper()
    reason = ""
    for key in _REASON_KEYS:
        if item.get(key) is not None:
            if not isinstance(item[key], _SCALARS):
                raise JudgeOutputError("judge reason must be text")
            reason = str(item[key]).strip()
            break
    findings = item.get("findings")
    if findings is None:
        findings = []
    if not isinstance(findings, list):
        raise JudgeOutputError("judge findings must be a list")
    return DimensionFields(verdict=verdict, score=item.get("score"), reason=reason, findings=findings)
