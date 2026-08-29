"""Stateful adapter for completed main-chain Claude Code turns."""

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
    message_text,
    nonnegative_token,
    object_mapping,
    parse_time,
    safe_agent_text,
    sum_tokens,
)


@dataclass(slots=True)
class _Message:
    model: str = ""
    texts: list[str] = field(default_factory=list)
    text_chars: int = 0
    usage: tuple[int | None, int | None, int | None] | None = None
    stop_reason: str = ""
    ended_at: object = None


@dataclass(slots=True)
class _ActiveTurn:
    external_id: str
    session_id: str
    started_at: object
    prompt: str
    project: object
    branch: object
    source_version: object
    messages: dict[str, _Message] = field(default_factory=dict)
    message_order: list[str] = field(default_factory=list)
    tool_ids: set[str] = field(default_factory=set)
    assistant_chars: int = 0
    invalid: bool = False


def iter_claude_history(
    root: Path,
    *,
    context: ImportContext,
    home: Path | None = None,
):
    """Yield canonical mapping outcomes from Claude Code JSONL histories."""
    if not root.is_dir():
        return
    for path in iter_history_paths(root):
        yield from _iter_claude_file(path, context=context, home=home)


def _finish(active: _ActiveTurn, context: ImportContext) -> MappingResult:
    if active.invalid:
        return MappingResult.skipped("conflicting_turn_records")
    groups = [active.messages[item] for item in active.message_order]
    candidates = [group for group in groups if group.texts]
    final = next(
        (group for group in reversed(candidates) if group.stop_reason == "end_turn"),
        candidates[-1] if candidates else None,
    )
    if final is None or final.model == "<synthetic>" or final.stop_reason != "end_turn":
        return MappingResult.skipped("synthetic_or_missing_response")
    models = [group.model for group in groups if group.model]
    usages = [group.usage for group in groups if group.usage is not None]
    return map_completed_turn(
        context=context,
        external_id=f"{active.session_id}:{active.external_id}",
        session_id=active.session_id,
        started_at=parse_time(active.started_at),
        ended_at=parse_time(final.ended_at),
        provider="anthropic",
        model=final.model or (models[-1] if models else "unknown"),
        prompt=active.prompt,
        response="\n".join(final.texts).strip(),
        input_tokens=sum_tokens([usage[0] for usage in usages]),
        output_tokens=sum_tokens([usage[1] for usage in usages]),
        cached_input_tokens=sum_tokens([usage[2] for usage in usages]),
        finish_reason=final.stop_reason or "completed",
        tool_calls=len(active.tool_ids),
        assistant_calls=len(groups),
        project=active.project,
        branch=active.branch,
        source_version=active.source_version,
    )


def _iter_claude_file(
    path: Path,
    *,
    context: ImportContext,
    home: Path | None,
):
    active: _ActiveTurn | None = None
    recognized = False
    emitted = 0
    skipped = 0
    for record in iter_jsonl_records(path, max_records=MAX_HISTORY_EVENTS):
        row = object_mapping(record)
        if row is None:
            yield MappingResult.skipped("malformed_record")
            skipped += 1
            continue
        kind = row.get("type")
        if kind not in {"user", "assistant"}:
            continue
        recognized = True
        if row.get("isSidechain") is True:
            continue
        message = object_mapping(row.get("message"))
        if message is None:
            continue
        is_human_user = (
            kind == "user"
            and row.get("sourceToolAssistantUUID") is None
            and row.get("toolUseResult") is None
        )
        if is_human_user:
            if active is not None:
                result = _finish(active, context)
                yield result
                emitted += result.trace is not None
                skipped += result.trace is None
            prompt = safe_agent_text(message_text(message), home=home)
            raw_source = row.get("uuid")
            raw_session = row.get("sessionId")
            if not prompt or not isinstance(raw_source, str) or not isinstance(raw_session, str):
                active = None
                yield MappingResult.skipped("incomplete_turn")
                skipped += 1
                continue
            active = _ActiveTurn(
                external_id=raw_source,
                session_id=raw_session,
                started_at=row.get("timestamp"),
                prompt=prompt,
                project=row.get("cwd"),
                branch=row.get("gitBranch"),
                source_version=row.get("version"),
            )
            continue
        if active is None or kind != "assistant":
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            active.invalid = True
            continue
        group = active.messages.get(message_id)
        if group is None:
            if len(active.messages) >= 10_000:
                active.invalid = True
                continue
            group = _Message()
            active.messages[message_id] = group
            active.message_order.append(message_id)
        model = message.get("model")
        if isinstance(model, str) and model:
            if group.model and group.model != model:
                active.invalid = True
            group.model = model
        stop_reason = message.get("stop_reason")
        if isinstance(stop_reason, str) and stop_reason:
            if group.stop_reason and group.stop_reason != stop_reason:
                active.invalid = True
            group.stop_reason = stop_reason
            if stop_reason != "end_turn" and group.text_chars:
                active.assistant_chars -= group.text_chars
                group.texts.clear()
                group.text_chars = 0
        group.ended_at = row.get("timestamp") or group.ended_at
        usage = object_mapping(message.get("usage"))
        if usage is not None:
            normalized_usage = (
                nonnegative_token(usage.get("input_tokens")),
                nonnegative_token(usage.get("output_tokens")),
                sum_tokens(
                    [
                        nonnegative_token(usage.get("cache_read_input_tokens")),
                        nonnegative_token(usage.get("cache_creation_input_tokens")),
                    ]
                ),
            )
            if group.usage is not None and group.usage != normalized_usage:
                active.invalid = True
            group.usage = normalized_usage
        content = message.get("content")
        if not isinstance(content, list):
            active.invalid = True
            continue
        for index, block in enumerate(content):
            item = object_mapping(block)
            if item is None:
                active.invalid = True
                continue
            text = item.get("text")
            if item.get("type") == "text" and isinstance(text, str):
                if group.stop_reason and group.stop_reason != "end_turn":
                    continue
                cleaned = safe_agent_text(text, home=home)
                remaining = MAX_AGENT_CONTENT_CHARS - active.assistant_chars
                if cleaned and remaining > 0:
                    bounded = cleaned[:remaining]
                    group.texts.append(bounded)
                    group.text_chars += len(bounded)
                    active.assistant_chars += len(bounded)
            elif item.get("type") == "tool_use":
                if len(active.tool_ids) >= 10_000:
                    active.invalid = True
                    continue
                tool_id = item.get("id")
                active.tool_ids.add(
                    tool_id
                    if isinstance(tool_id, str) and tool_id
                    else f"{message_id}:{row.get('uuid')}:{index}"
                )

    if active is not None:
        result = _finish(active, context)
        yield result
        emitted += result.trace is not None
        skipped += result.trace is None
    if not recognized:
        yield MappingResult.skipped("unsupported_history")
    elif emitted == 0 and skipped == 0:
        yield MappingResult.skipped("no_root_turns")
