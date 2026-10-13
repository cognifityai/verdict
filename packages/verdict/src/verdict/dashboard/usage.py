"""Bounded, source-reported usage rollups for local agent sessions."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from verdict.dashboard.query import QuerySession
from verdict.pricing import estimate_turn_cost_usd

MAX_USAGE_TURNS = 20_000
MAX_MODEL_EVENTS = 100_000
MAX_DAY_SESSIONS = 100
MAX_SESSION_TURNS = 50
MAX_DAY_MODELS = 20
MAX_DAY_SOURCES = 20

# Codex input includes cached input; Claude's provider response usage does not.
_TURN_INPUT_INCLUDES_CACHED = {
    "codex_turn_delta": True,
    "claude_provider_response_sum": False,
}
_TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)


def model_from_event(kind: str, attributes: dict[str, Any]) -> str | None:
    if kind == "model_call":
        model = attributes.get("request_model")
    elif kind == "context" and attributes.get("name") == "model_configuration":
        model = attributes.get("source")
    else:
        return None
    if isinstance(model, str) and model and model != "unknown" and not model.startswith("<"):
        try:
            if len(model.encode("utf-8")) <= 256:
                return model
        except UnicodeError:
            pass
    return None


def _record_model_evidence(
    models: dict[Any, set[str | None]], key: Any, kind: str, attributes: dict[str, Any],
) -> None:
    if kind == "model_call" or (kind == "context" and attributes.get("name") == "model_configuration"):
        model = model_from_event(kind, attributes)
        # Local clients may emit synthetic zero-usage notices as model_call
        # events. They cannot account for any of the turn's reported tokens.
        if (kind == "model_call" and model is None
                and type(attributes.get("input_tokens")) is int
                and attributes["input_tokens"] == 0
                and type(attributes.get("output_tokens")) is int
                and attributes["output_tokens"] == 0
                and all(attributes.get(field) in (None, 0) for field in (
                    "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"
                ))):
            return
        models[key].add(model)


def _complete_models(names: set[str | None]) -> frozenset[str]:
    return frozenset() if None in names else frozenset(names)


def models_by_turn(events) -> dict[str, frozenset[str]]:
    models: dict[str, set[str | None]] = defaultdict(set)
    for event in events:
        _record_model_evidence(models, event.turn_id, event.event_type.value, event.attributes)
    return {turn_id: _complete_models(names) for turn_id, names in models.items()}


def turn_list_price_usd(turn, models: frozenset[str]) -> float | None:
    """Return a list-price estimate only for one known model and complete basis."""
    includes_cached = _TURN_INPUT_INCLUDES_CACHED.get(turn.token_usage_basis)
    if includes_cached is None or len(models) != 1:
        return None
    if turn.input_tokens is None or turn.output_tokens is None:
        return None
    return estimate_turn_cost_usd(
        next(iter(models)),
        input_tokens=turn.input_tokens,
        cached_input_tokens=turn.cached_input_tokens,
        cache_write_input_tokens=turn.cache_write_input_tokens,
        output_tokens=turn.output_tokens,
        input_includes_cached=includes_cached,
    )


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _summary() -> dict[str, Any]:
    return {
        "turns": 0, "tokenKnownTurns": 0, "totalTokens": 0,
        "pricedTurns": 0, "listPriceUsd": 0.0,
        "models": defaultdict(lambda: {
            "turns": 0, "tokenKnownTurns": 0, "totalTokens": 0,
            "pricedTurns": 0, "listPriceUsd": 0.0,
        }),
        "unattributedTokens": 0,
        **{field: 0 for field in _TOKEN_FIELDS},
        **{f"{field}_known": 0 for field in _TOKEN_FIELDS},
    }


def _add(summary: dict[str, Any], turn: dict[str, Any]) -> None:
    summary["turns"] += 1
    tokens = turn["total_tokens"]
    if tokens is not None:
        summary["tokenKnownTurns"] += 1
        summary["totalTokens"] += int(tokens)
    price = turn["price"]
    if price is not None:
        summary["pricedTurns"] += 1
        summary["listPriceUsd"] += price
    for field in _TOKEN_FIELDS:
        value = turn[field]
        if value is not None:
            summary[field] += int(value)
            summary[f"{field}_known"] += 1
    models = turn["models"]
    if len(models) == 1:
        model = next(iter(models))
        entry = summary["models"][model]
        entry["turns"] += 1
        if tokens is not None:
            entry["tokenKnownTurns"] += 1
            entry["totalTokens"] += int(tokens)
        if price is not None:
            entry["pricedTurns"] += 1
            entry["listPriceUsd"] += price
    elif tokens is not None:
        summary["unattributedTokens"] += int(tokens)


def _present(summary: dict[str, Any], *, include_models: bool = False) -> dict[str, Any]:
    turns = summary["turns"]
    result = {
        "turns": turns,
        "tokenKnownTurns": summary["tokenKnownTurns"],
        "totalTokens": summary["totalTokens"] if summary["tokenKnownTurns"] or not turns else None,
        "pricedTurns": summary["pricedTurns"],
        "listPriceUsd": (
            round(summary["listPriceUsd"], 8) if summary["pricedTurns"] or not turns else None
        ),
        "unattributedTokens": summary["unattributedTokens"] if summary["tokenKnownTurns"] else None,
    }
    # Explicit names keep the response stable for multiword token components.
    for field, name in (
        ("input_tokens", "inputTokens"),
        ("cached_input_tokens", "cachedInputTokens"),
        ("cache_write_input_tokens", "cacheWriteInputTokens"),
        ("output_tokens", "outputTokens"),
        ("reasoning_output_tokens", "reasoningOutputTokens"),
    ):
        result[name] = summary[field] if summary[f"{field}_known"] else None
    result["componentCoverage"] = {
        name: summary[f"{field}_known"]
        for field, name in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("cache_write_input_tokens", "cacheWriteInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
        )
    }
    if include_models:
        ranked = sorted(summary["models"].items(), key=lambda item: (-item[1]["totalTokens"], item[0]))
        result["models"] = [
            {"model": name, **{key: value for key, value in values.items()
                                if key != "listPriceUsd"},
             "listPriceUsd": round(values["listPriceUsd"], 8)
             if values["pricedTurns"] else None}
            for name, values in ranked[:MAX_DAY_MODELS]
        ]
        result["modelsTruncated"] = len(ranked) > MAX_DAY_MODELS
    return result


def build_usage_view(
    session: QuerySession, *, tenant: str, now: datetime, day: str | None,
    session_id: str | None, source_kind: str | None = None,
) -> dict[str, Any]:
    """Aggregate a UTC 30-day turn window without exposing captured content."""
    end = now.astimezone(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=30)
    first = datetime.combine(start, datetime.min.time(), timezone.utc).isoformat()
    last = datetime.combine(end, datetime.min.time(), timezone.utc).isoformat()
    dates = [(start + timedelta(days=index)).isoformat() for index in range(30)]
    if day is not None and day not in dates:
        raise ValueError("day outside usage window")
    if not all(session.table_exists(table) for table in ("agent_turns", "agent_runs", "import_sources", "agent_events")):
        return {
            "window": {"startDay": dates[0], "endDay": dates[-1], "timezone": "UTC",
                       "complete": True, "scannedTurns": 0},
            "days": [{"day": value, **_present(_summary())} for value in dates],
            "selectedDay": day or dates[-1], "dayModels": [], "daySources": [], "sessions": [],
            "sessionsTruncated": False, "selectedSession": None,
            "selectionReason": None,
        }
    rows = list(session.execute(
        """SELECT t.run_id,t.turn_id,t.sequence,t.started_at,t.ended_at,
                  t.input_tokens,t.cached_input_tokens,t.cache_write_input_tokens,
                  t.output_tokens,t.reasoning_output_tokens,t.total_tokens,t.token_usage_basis,
                  r.session_id,r.source_session_id,r.agent_name,s.source_kind
           FROM agent_turns t JOIN agent_runs r
             ON r.tenant_id=t.tenant_id AND r.run_id=t.run_id
           JOIN import_sources s
             ON s.tenant_id=r.tenant_id AND s.source_session_id=r.source_session_id
           WHERE t.tenant_id=? AND t.started_at>=? AND t.started_at<?
           ORDER BY t.started_at DESC,t.run_id DESC,t.sequence DESC
           LIMIT ?""",
        (tenant, first, last, MAX_USAGE_TURNS + 1),
    ))
    complete = len(rows) <= MAX_USAGE_TURNS
    rows = rows[:MAX_USAGE_TURNS]
    models: dict[tuple[str, str], set[str | None]] = defaultdict(set)
    event_count = 0
    for event in session.execute(
        """SELECT e.run_id,e.turn_id,e.event_type,e.attributes_json
           FROM agent_events e JOIN agent_turns t
             ON t.tenant_id=e.tenant_id AND t.run_id=e.run_id AND t.turn_id=e.turn_id
           WHERE t.tenant_id=? AND t.started_at>=? AND t.started_at<?
             AND e.event_type IN ('model_call','context')
           ORDER BY t.started_at DESC,e.run_id,e.turn_id,e.sequence
           LIMIT ?""",
        (tenant, first, last, MAX_MODEL_EVENTS + 1),
    ):
        event_count += 1
        if event_count > MAX_MODEL_EVENTS:
            break
        raw_attributes = event["attributes_json"]
        if isinstance(raw_attributes, dict):
            attributes = raw_attributes
        else:
            try:
                attributes = json.loads(raw_attributes)
            except (TypeError, ValueError):
                attributes = {}
        if not isinstance(attributes, dict):
            attributes = {}
        _record_model_evidence(
            models, (event["run_id"], event["turn_id"]), event["event_type"], attributes,
        )
    model_complete = event_count <= MAX_MODEL_EVENTS

    daily = {value: _summary() for value in dates}
    turns = []
    for row in rows:
        started = _utc(row["started_at"])
        turn_day = started.date().isoformat()
        turn_models = _complete_models(models[(row["run_id"], row["turn_id"])]) if model_complete else frozenset()
        price = turn_list_price_usd(SimpleNamespace(**row), turn_models) if model_complete else None
        item = {**row, "day": turn_day, "sessionKey": row["session_id"] or row["source_session_id"],
                "models": turn_models, "price": price}
        turns.append(item)
        _add(daily[turn_day], item)

    selected_day = day or next((value for value in reversed(dates) if daily[value]["turns"]), dates[-1])
    source_summaries: dict[str, dict[str, Any]] = defaultdict(_summary)
    for turn in turns:
        if turn["day"] == selected_day:
            _add(source_summaries[turn["source_kind"]], turn)
    ranked_sources = sorted(source_summaries.items(), key=lambda item: (
        -item[1]["totalTokens"], item[0],
    ))
    day_sessions: dict[tuple[str, str], dict[str, Any]] = {}
    for turn in turns:
        if turn["day"] != selected_day:
            continue
        key = (turn["source_kind"], turn["sessionKey"])
        entry = day_sessions.setdefault(key, {
            "sessionId": turn["sessionKey"], "sourceKind": turn["source_kind"],
            "agentName": turn["agent_name"] or turn["source_kind"],
            "runIds": set(), "summary": _summary(),
        })
        entry["runIds"].add(turn["run_id"])
        _add(entry["summary"], turn)
    ranked_sessions = sorted(day_sessions.values(), key=lambda item: (
        -(item["summary"]["totalTokens"] if item["summary"]["tokenKnownTurns"] else -1),
        item["sessionId"],
    ))
    presented_sessions = [
        {"sessionId": item["sessionId"], "sourceKind": item["sourceKind"],
         "agentName": item["agentName"], "runIds": sorted(item["runIds"]),
         **_present(item["summary"])}
        for item in ranked_sessions[:MAX_DAY_SESSIONS]
    ]
    selected = None
    matching_keys = [key for key in day_sessions if key[1] == session_id and
                     (source_kind is None or key[0] == source_kind)]
    selection_reason = "ambiguous_source" if session_id is not None and len(matching_keys) > 1 else None
    if session_id is not None and len(matching_keys) == 1:
        selected_key = matching_keys[0]
        matching = [turn for turn in turns if
                    (turn["source_kind"], turn["sessionKey"]) == selected_key]
        summary = _summary()
        for turn in matching:
            _add(summary, turn)
        ranked_turns = sorted(matching, key=lambda item: (
            -(item["total_tokens"] if item["total_tokens"] is not None else -1),
            item["started_at"], item["turn_id"],
        ))
        def turn_view(turn: dict[str, Any]) -> dict[str, Any]:
            return {
                "turnId": turn["turn_id"], "runId": turn["run_id"],
                "sequence": turn["sequence"], "startedAt": turn["started_at"],
                "totalTokens": turn["total_tokens"],
                "inputTokens": turn["input_tokens"],
                "cachedInputTokens": turn["cached_input_tokens"],
                "cacheWriteInputTokens": turn["cache_write_input_tokens"],
                "outputTokens": turn["output_tokens"],
                "reasoningOutputTokens": turn["reasoning_output_tokens"],
                "models": sorted(turn["models"]),
                "listPriceUsd": round(turn["price"], 8) if turn["price"] is not None else None,
            }
        ends = [_utc(turn["ended_at"]) for turn in matching if turn["ended_at"] is not None]
        elapsed = None
        if len(ends) == len(matching):
            seconds = (max(ends) - min(_utc(turn["started_at"]) for turn in matching)).total_seconds()
            if seconds >= 0:
                elapsed = round(seconds)
        selected = {
            "sessionId": session_id, "sourceKind": selected_key[0],
            "runIds": sorted({turn["run_id"] for turn in matching}),
            **_present(summary, include_models=True),
            "turnCount": len(matching),
            "elapsedSpanSeconds": elapsed,
            "turns": [turn_view(turn) for turn in ranked_turns[:MAX_SESSION_TURNS]],
            "turnsTruncated": len(ranked_turns) > MAX_SESSION_TURNS,
            "topTurns": [turn_view(turn) for turn in ranked_turns[:3]],
            "selectedDayTokens": day_sessions[selected_key]["summary"]["totalTokens"]
            if day_sessions[selected_key]["summary"]["tokenKnownTurns"] else None,
        }
    return {
        "window": {"startDay": dates[0], "endDay": dates[-1], "timezone": "UTC",
                   "complete": complete and model_complete, "turnsComplete": complete,
                   "modelEvidenceComplete": model_complete, "scannedTurns": len(turns)},
        "days": [{"day": value, **_present(daily[value])} for value in dates],
        "selectedDay": selected_day,
        "dayModels": _present(daily[selected_day], include_models=True)["models"],
        "dayModelsTruncated": _present(daily[selected_day], include_models=True)["modelsTruncated"],
        "daySources": [{"sourceKind": source, **_present(summary)}
                       for source, summary in ranked_sources[:MAX_DAY_SOURCES]],
        "daySourcesTruncated": len(ranked_sources) > MAX_DAY_SOURCES,
        "sessions": presented_sessions,
        "sessionsTruncated": len(ranked_sessions) > MAX_DAY_SESSIONS,
        "selectedSession": selected,
        "selectionReason": selection_reason,
    }
