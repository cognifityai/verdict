"""Stateful adapter for completed root Codex turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from verdict.telemetry.files import iter_jsonl_records
from verdict.telemetry.model import ImportContext, MappingResult
from verdict.telemetry.sources.agent_common import (
    MAX_AGENT_CONTENT_CHARS,
    MAX_HISTORY_EVENTS,
    iter_history_paths,
    map_completed_turn,
    nonnegative_token,
    object_mapping,
    parse_time,
    safe_agent_text,
    token_delta,
)


@dataclass(slots=True)
class _ActiveTurn:
    turn_id: str
    started_at: object
    prompts: list[str] = field(default_factory=list)
    prompt_chars: int = 0
    final: str = ""
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    tool_calls: int = 0
    resource_limit_exceeded: bool = False


def iter_codex_history(
    root: Path,
    *,
    context: ImportContext,
    home: Path | None = None,
):
    """Yield canonical mapping outcomes from Codex JSONL histories."""
    if not root.is_dir():
        return
    for path in iter_history_paths(root):
        yield from _iter_codex_file(path, context=context, home=home)


def _iter_codex_file(
    path: Path,
    *,
    context: ImportContext,
    home: Path | None,
):
    active: _ActiveTurn | None = None
    accepted = False
    session_id = ""
    source_version = ""
    project: object = None
    branch: object = None
    emitted = 0
    previous_usage: tuple[int | None, int | None, int | None] = (None, None, None)

    for record in iter_jsonl_records(path, max_records=MAX_HISTORY_EVENTS):
        row = object_mapping(record)
        if row is None:
            yield MappingResult.skipped("malformed_record")
            continue
        payload = object_mapping(row.get("payload"))
        outer = row.get("type")
        if outer == "session_meta" and payload is not None and not accepted:
            source = payload.get("source")
            if payload.get("originator") != "Codex Desktop" or bool(
                payload.get("parent_thread_id")
                or payload.get("forked_from_id")
                or isinstance(source, dict)
            ):
                yield MappingResult.skipped("child_session")
                return
            raw_session = payload.get("session_id") or payload.get("id")
            if not isinstance(raw_session, str) or not raw_session:
                yield MappingResult.skipped("missing_session_id")
                return
            accepted = True
            session_id = raw_session
            source_version = (
                payload["cli_version"] if isinstance(payload.get("cli_version"), str) else ""
            )
            project = payload.get("cwd")
            git = object_mapping(payload.get("git"))
            branch = git.get("branch") if git else None
            continue

        if not accepted or payload is None:
            continue
        inner = payload.get("type")
        if outer == "event_msg" and inner == "task_started":
            if active is not None:
                yield MappingResult.skipped("incomplete_turn")
            raw_turn = payload.get("turn_id")
            if not isinstance(raw_turn, str) or not raw_turn:
                active = None
                yield MappingResult.skipped("missing_turn_id")
                continue
            # Current Codex histories expose a monotonic/relative numeric
            # ``payload.started_at`` alongside the authoritative ISO event
            # timestamp. Event time, not process-relative time, owns ordering.
            active = _ActiveTurn(raw_turn, row.get("timestamp") or payload.get("started_at"))
            continue
        if active is None:
            continue
        if outer == "turn_context":
            if payload.get("turn_id") in (None, active.turn_id) and isinstance(
                payload.get("model"), str
            ):
                active.model = payload["model"]
            continue
        if outer == "response_item" and inner in {"custom_tool_call", "function_call"}:
            if active.tool_calls >= 10_000:
                active.resource_limit_exceeded = True
            else:
                active.tool_calls += 1
            continue
        if outer != "event_msg":
            continue
        if inner == "user_message":
            prompt = safe_agent_text(payload.get("message"), home=home)
            remaining = MAX_AGENT_CONTENT_CHARS - active.prompt_chars
            if prompt and remaining > 0:
                bounded = prompt[:remaining]
                active.prompts.append(bounded)
                active.prompt_chars += len(bounded)
        elif inner == "agent_message" and payload.get("phase") == "final_answer":
            active.final = safe_agent_text(payload.get("message"), home=home)
        elif inner == "token_count":
            info = object_mapping(payload.get("info"))
            usage = object_mapping(info.get("total_token_usage")) if info else None
            if usage:
                active.input_tokens = nonnegative_token(usage.get("input_tokens"))
                active.output_tokens = nonnegative_token(usage.get("output_tokens"))
                active.cached_input_tokens = nonnegative_token(usage.get("cached_input_tokens"))
        elif inner == "turn_aborted":
            if payload.get("turn_id") == active.turn_id:
                yield MappingResult.skipped("aborted_turn")
                active = None
        elif inner == "task_complete":
            if payload.get("turn_id") != active.turn_id:
                yield MappingResult.skipped("conflicting_turn_id")
                continue
            if active.resource_limit_exceeded:
                yield MappingResult.skipped("resource_limit_exceeded")
                active = None
                continue
            response = safe_agent_text(payload.get("last_agent_message"), home=home) or active.final
            prompt = "\n\n".join(active.prompts).strip()
            current = (
                active.input_tokens,
                active.output_tokens,
                active.cached_input_tokens,
            )
            delta = tuple(
                token_delta(value, previous)
                for value, previous in zip(current, previous_usage, strict=True)
            )
            previous_usage = tuple(
                value if value is not None else previous
                for value, previous in zip(current, previous_usage, strict=True)
            )
            result = map_completed_turn(
                context=context,
                external_id=f"{session_id}:{active.turn_id}",
                session_id=session_id,
                started_at=parse_time(active.started_at),
                ended_at=parse_time(payload.get("completed_at") or row.get("timestamp")),
                provider="openai",
                model=active.model or "unknown",
                prompt=prompt,
                response=response,
                input_tokens=delta[0],
                output_tokens=delta[1],
                cached_input_tokens=delta[2],
                finish_reason="completed",
                tool_calls=active.tool_calls,
                assistant_calls=1,
                project=project,
                branch=branch,
                source_version=source_version,
            )
            if not prompt or not response:
                result = MappingResult.skipped("incomplete_turn")
            yield result
            emitted += result.trace is not None
            active = None

    if active is not None:
        yield MappingResult.skipped("incomplete_turn")
    elif not accepted:
        yield MappingResult.skipped("unsupported_history")
    elif emitted == 0:
        # A recognized file with only explicit skipped turns is already accounted for.
        return
