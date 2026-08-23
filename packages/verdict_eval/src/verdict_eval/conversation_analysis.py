"""Conversation-independent drift planning and immutable run snapshots."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from verdict.redaction import redact
from verdict.schema import (
    ConversationDriftRun,
    ConversationDriftSample,
    ConversationDriftSignal,
    ConversationTraceCandidate,
    ConversationTraceContent,
    Judgment,
    conversation_json_fingerprint,
    datetime_to_utc_us,
)
from verdict.storage.base import ConversationDriftStorage

from verdict_eval.drift import DriftDetector, build_windows_from_judgments
from verdict_eval.sampling import (
    ConversationCandidate,
    ConversationRepresentative,
    ConversationRepresentativePlan,
    select_conversation_representatives,
)

_ROUTING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_CAPTURE_POLICIES = {"full-v1", "session-v1"}
_WINDOW_CODES = {"baseline": 0, "current": 1}
_EXCLUSION_REASONS = (
    "unsupported_capture_sampling",
    "missing_session",
    "invalid_session",
    "session_too_large",
    "provider_error",
    "incomplete_response",
    "missing_content",
    "invalid_content_encoding",
    "content_too_large",
)
_MEMBERSHIP_REASONS = tuple(
    f"outlier:{reason}"
    for reason in ("distance", "explicit_key_not_in_version", "semantic_fit_too_small")
) + tuple(
    f"ineligible:{reason}"
    for reason in (
        "invalid_workload",
        "unsafe_workload",
        "missing_intent_key",
        "invalid_intent_key",
        "unsafe_intent_key",
        "content_not_captured",
        "raw_messages_oversize",
        "malformed_messages",
        "no_supported_user_text",
        "text_too_short",
        "text_too_long",
        "redaction_error",
    )
)
_STATE_CODES = {
    "empty": 0,
    "cross_window_reuse": 1,
    "one_sided": 2,
    "tested": 3,
    "unclear_tested": 4,
    "error": 5,
    "insufficient": 6,
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ConversationAnalysisConfig:
    tenant_id: str
    run_id: str
    target_workload: str
    baseline_start: datetime
    baseline_end: datetime
    current_start: datetime
    current_end: datetime
    analysis_cutoff: datetime
    actor: str
    seed: int = 0
    target_per_cell: int = 40
    min_sample_size: int = 30
    p_threshold: float = 0.01
    effect_size_threshold: float = 0.147
    max_candidate_rows: int = 50_000
    max_candidate_metadata_bytes: int = 33_554_432
    max_judge_calls: int = 20_000
    max_selected_content_bytes: int = 67_108_864
    max_estimated_input_tokens: int = 16_000_000
    judge_concurrency: int = 1
    judge_attempt_timeout: int = 30

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("tenant_id", self.tenant_id, 128),
            ("run_id", self.run_id, 256),
            ("actor", self.actor, 256),
            ("target_workload", self.target_workload, 64),
        ):
            try:
                size = len(value.encode("utf-8"))
            except (AttributeError, UnicodeError) as exc:
                raise ValueError(f"{name} is invalid") from exc
            if not value or size > maximum or "\x00" in value:
                raise ValueError(f"{name} is invalid")
        if (
            _ROUTING.fullmatch(self.target_workload) is None
            or redact(self.target_workload, mode="redact") != self.target_workload
        ):
            raise ValueError("target_workload is invalid")
        bounds = tuple(value.astimezone(timezone.utc) for value in self._times())
        baseline_start, baseline_end, current_start, current_end, cutoff = bounds
        if not baseline_start < baseline_end <= current_start < current_end <= cutoff:
            raise ValueError("window_config")
        if (
            baseline_end - baseline_start > timedelta(days=90)
            or current_end - current_start > timedelta(days=90)
            or current_end - baseline_start > timedelta(days=180)
        ):
            raise ValueError("window_config")
        if any(type(value) is not int for value in (self.seed, self.target_per_cell)):
            raise ValueError("sampling configuration is invalid")
        if not 1 <= self.target_per_cell <= 50_000 or not 1 <= self.min_sample_size <= 50_000:
            raise ValueError("sampling configuration is invalid")
        if not 0 < self.p_threshold <= 1 or not 0 <= self.effect_size_threshold <= 1:
            raise ValueError("detector configuration is invalid")
        hard_limits = (
            (self.max_candidate_rows, 50_000),
            (self.max_candidate_metadata_bytes, 33_554_432),
            (self.max_judge_calls, 20_000),
            (self.max_selected_content_bytes, 67_108_864),
            (self.max_estimated_input_tokens, 16_000_000),
            (self.judge_concurrency, 64),
            (self.judge_attempt_timeout, 300),
        )
        if any(
            type(value) is not int or not 1 <= value <= maximum for value, maximum in hard_limits
        ):
            raise ValueError("analysis resource configuration is invalid")
        for name, value in zip(
            ("baseline_start", "baseline_end", "current_start", "current_end", "analysis_cutoff"),
            bounds,
            strict=True,
        ):
            object.__setattr__(self, name, value)

    def _times(self) -> tuple[datetime, ...]:
        values = (
            self.baseline_start,
            self.baseline_end,
            self.current_start,
            self.current_end,
            self.analysis_cutoff,
        )
        if any(value.tzinfo is None for value in values):
            raise ValueError("window_config")
        return values


@dataclass
class ConversationPlan:
    source_rows: list[ConversationTraceCandidate]
    representatives: ConversationRepresentativePlan
    contents: dict[str, ConversationTraceContent]
    exclusions: dict[str, int] = field(default_factory=dict)
    cell_exclusions: dict[tuple[str, str, str], int] = field(default_factory=dict)
    membership: dict[str, int] = field(default_factory=dict)
    metadata_bytes: int = 0


@dataclass(frozen=True)
class ConversationAnalysisResult:
    run_id: str
    status: str
    reason: str | None
    provider_calls: int
    signal_count: int = 0


class _Unavailable(ValueError):
    def __init__(self, reason: str, *, plan: ConversationPlan | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.plan = plan


def _framed_size(value: str | None) -> int:
    return 4 if value is None else 4 + len(value.encode("utf-8", "surrogatepass"))


def _metadata_size(
    rows: list[ConversationTraceCandidate],
    registry_version: str,
    maximum: int,
) -> int:
    total = 0
    for row in rows:
        started_at = datetime.fromtimestamp(row.started_at_us / 1_000_000, timezone.utc)
        started_rfc3339 = started_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        total += 8 * 11 + 5
        total += sum(
            _framed_size(value)
            for value in (
                row.trace_id,
                row.tenant_id,
                row.workload_json_type,
                row.workload,
                started_rfc3339,
                row.session_id,
                row.success_sampling,
                registry_version,
                row.assignment_status,
                row.assignment_reason,
                row.cluster_id,
                row.stream_completion_state,
                row.stream_completion,
            )
        )
        if total > maximum:
            return maximum + 1
    return total


def _candidate_rows(
    source: object,
    registry_version: str,
    config: ConversationAnalysisConfig,
) -> tuple[list[ConversationTraceCandidate], int]:
    if source.count_pending_analysis_rows(config.tenant_id):
        raise _Unavailable("analysis_index_pending")
    rows = source.list_conversation_trace_candidates(
        config.tenant_id,
        registry_version,
        datetime_to_utc_us(config.baseline_start),
        datetime_to_utc_us(config.current_end),
        target_workload=config.target_workload,
        limit=config.max_candidate_rows + 1,
    )
    if len(rows) > config.max_candidate_rows or any(row.trace_id is None for row in rows):
        raise _Unavailable("candidate_limit")
    metadata_bytes = _metadata_size(
        rows,
        registry_version,
        config.max_candidate_metadata_bytes,
    )
    if metadata_bytes > config.max_candidate_metadata_bytes:
        raise _Unavailable("candidate_limit")
    return rows, metadata_bytes


def _window(row: ConversationTraceCandidate, config: ConversationAnalysisConfig) -> str | None:
    if (
        datetime_to_utc_us(config.baseline_start)
        <= row.started_at_us
        < datetime_to_utc_us(config.baseline_end)
    ):
        return "baseline"
    if (
        datetime_to_utc_us(config.current_start)
        <= row.started_at_us
        < datetime_to_utc_us(config.current_end)
    ):
        return "current"
    return None


def _exclusion(row: ConversationTraceCandidate) -> str | None:
    if row.success_sampling_state != "string" or row.success_sampling not in _CAPTURE_POLICIES:
        return "unsupported_capture_sampling"
    if row.session_state == "missing" or row.session_id == "":
        return "missing_session"
    if row.session_utf8_bytes is not None and row.session_utf8_bytes > 256:
        return "session_too_large"
    if row.session_state != "string" or row.session_id is None:
        return "invalid_session"
    if "\x00" in row.session_id:
        return "invalid_session"
    if not row.provider_success:
        return "provider_error"
    if row.stream_completion_state != "missing" and not (
        row.stream_completion_state == "string" and row.stream_completion == "complete"
    ):
        return "incomplete_response"
    if not row.prompt_present or not row.response_present:
        return "missing_content"
    if not row.prompt_utf8_valid or not row.response_utf8_valid:
        return "invalid_content_encoding"
    prompt = row.prompt_utf8_bytes or 0
    response = row.response_utf8_bytes or 0
    if prompt == 0 or response == 0:
        return "missing_content"
    if prompt + response > 16 * 1024:
        return "content_too_large"
    return None


def plan_conversation_analysis(
    storage: object,
    registry_version: str,
    config: ConversationAnalysisConfig,
) -> ConversationPlan:
    if not isinstance(storage, ConversationDriftStorage):
        raise TypeError("storage lacks conversation drift capability")
    with storage.cluster_analysis_snapshot() as source:
        rows, metadata_bytes = _candidate_rows(source, registry_version, config)

        candidates: list[ConversationCandidate] = []
        exclusions: dict[str, int] = {}
        cell_exclusions: dict[tuple[str, str, str], int] = {}
        membership: dict[str, int] = {}
        for row in rows:
            if row.assignment_status is None:
                raise _Unavailable("assignment_coverage")
            if row.assignment_status != "assigned" or row.cluster_id is None:
                key = f"{row.assignment_status}:{row.assignment_reason}"
                if key not in _MEMBERSHIP_REASONS:
                    raise _Unavailable("assignment_coverage")
                membership[key] = membership.get(key, 0) + 1
                continue
            if row.assignment_reason is not None:
                raise _Unavailable("assignment_coverage")
            try:
                cluster_size = len(row.cluster_id.encode("utf-8"))
            except UnicodeError as exc:
                raise _Unavailable("assignment_coverage") from exc
            if not 1 <= cluster_size <= 64 or "\x00" in row.cluster_id:
                raise _Unavailable("assignment_coverage")
            window = _window(row, config)
            if window is None:
                continue
            reason = _exclusion(row)
            if reason is not None:
                exclusions[reason] = exclusions.get(reason, 0) + 1
                key = (row.cluster_id, window, reason)
                cell_exclusions[key] = cell_exclusions.get(key, 0) + 1
                continue
            candidates.append(
                ConversationCandidate(
                    config.tenant_id,
                    registry_version,
                    row.cluster_id,
                    window,
                    row.session_id or "",
                    row.trace_id or "",
                    datetime.fromtimestamp(row.started_at_us / 1_000_000, timezone.utc),
                )
            )
        representatives = select_conversation_representatives(
            candidates,
            target_per_cell=config.target_per_cell,
            seed=config.seed,
        )
        contents = source.get_conversation_trace_contents(
            config.tenant_id, [item.trace_id for item in representatives.selected]
        )
        plan = ConversationPlan(
            rows,
            representatives,
            contents,
            exclusions,
            cell_exclusions,
            membership,
            metadata_bytes,
        )
        if len(contents) != len(representatives.selected):
            raise _Unavailable("selected_content", plan=plan)
        return plan


def _definitions(
    config: ConversationAnalysisConfig,
    judge: object,
    *,
    require_provider_deadline: bool = True,
) -> tuple[str, str]:
    from verdict_eval.judge import SYSTEM_PROMPT, _rubric_payload, _user_prompt
    from verdict_eval.providers import FakeProvider

    provider = getattr(judge, "provider", None)
    if require_provider_deadline and provider.__class__ is not FakeProvider:
        request_timeout = getattr(provider, "request_timeout_seconds", None)
        request_attempts = getattr(provider, "request_max_attempts", None)
        if (
            type(request_timeout) not in {int, float}
            or not math.isfinite(request_timeout)
            or not 0 < request_timeout <= config.judge_attempt_timeout
            or type(request_attempts) is not int
            or request_attempts != 1
        ):
            raise _Unavailable("evaluator_definition")

    identity = judge.evaluator_identity(context=None)
    rubric = judge._effective_rubric(None)
    dimensions = identity.get("expected_dimensions")
    if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 64:
        raise _Unavailable("evaluator_definition")
    normalized: list[str] = []
    for value in dimensions:
        if (
            not isinstance(value, str)
            or unicodedata.normalize("NFC", value) != value
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise _Unavailable("evaluator_definition")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise _Unavailable("evaluator_definition") from exc
        if not 1 <= size <= 1024 or "\x00" in value:
            raise _Unavailable("evaluator_definition")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise _Unavailable("evaluator_definition")
    evaluator = _json(
        {
            "config": identity.get("evaluator_config", {}),
            "dimensions": normalized,
            "models": identity.get("judge_models", []),
            "prompt_version": "judge-v1",
            "provider": identity.get("evaluator_provider", ""),
            "rubric": _rubric_payload(rubric),
            "rubric_name": identity.get("rubric_name", ""),
            "rubric_version": identity.get("rubric_version", ""),
            "schema": "evaluator-definition-v1",
            "system_prompt": SYSTEM_PROMPT,
            "user_prompt_template": _user_prompt(
                "__QUERY__", "__RESPONSE__", "__CONTEXT__", rubric
            ),
            "input_policy": ["prompt_redacted", "response_redacted", None],
        }
    )
    if (
        len(evaluator.encode("utf-8")) > 64 * 1024
        or len(_json({dimension: "UNCLEAR" for dimension in normalized}).encode("utf-8"))
        > 32 * 1024
    ):
        raise _Unavailable("evaluator_definition")
    policy = _json(
        {
            "effect_size_threshold": config.effect_size_threshold,
            "max_candidate_metadata_bytes": config.max_candidate_metadata_bytes,
            "max_candidate_rows": config.max_candidate_rows,
            "max_estimated_input_tokens": config.max_estimated_input_tokens,
            "max_judge_calls": config.max_judge_calls,
            "max_selected_content_bytes": config.max_selected_content_bytes,
            "judge_attempt_timeout": config.judge_attempt_timeout,
            "judge_concurrency": config.judge_concurrency,
            "method": "conversation-v1",
            "multiple_test_correction": "benjamini-hochberg-v1",
            "min_sample_size": config.min_sample_size,
            "p_threshold": config.p_threshold,
            "representative_policy": "conversation-representative-v1",
            "sampling": "stratified",
            "schema": "analysis-policy-v1",
            "seed": config.seed,
            "success_sampling": ["full-v1", "session-v1"],
            "target_per_cell": config.target_per_cell,
            "target_workload": config.target_workload,
            "unclear_policy": "exclude-from-binary-test-v1",
            "window_policy": "utc-half-open-nonoverlap-v1",
        }
    )
    return policy, evaluator


def _coverage(
    clusters: list[str],
    dimensions: list[str],
    plan: ConversationPlan | None,
    samples: list[ConversationDriftSample],
    min_sample_size: int,
) -> tuple[str, str]:
    cells: list[list[int]] = []
    populated_states: list[str] = []
    for cluster_index, cluster_id in enumerate(clusters):
        for dimension_index, dimension in enumerate(dimensions):
            pre = [
                plan.representatives.pre_counts.get((cluster_id, window), 0) if plan else 0
                for window in ("baseline", "current")
            ]
            post = [
                plan.representatives.post_counts.get((cluster_id, window), 0) if plan else 0
                for window in ("baseline", "current")
            ]
            selected = [
                sum(item.cluster_id == cluster_id and item.window == window for item in samples)
                for window in ("baseline", "current")
            ]
            completed: list[int] = []
            scored: list[int] = []
            unclear: list[int] = []
            errors: list[int] = []
            for window in ("baseline", "current"):
                window_samples = [
                    item
                    for item in samples
                    if item.cluster_id == cluster_id and item.window == window
                ]
                outcomes = [
                    json.loads(item.outcomes_json).get(dimension)
                    for item in window_samples
                    if item.attempt_status == "completed"
                ]
                completed.append(len(outcomes))
                scored.append(sum(value in {"PASS", "FAIL"} for value in outcomes))
                unclear.append(sum(value == "UNCLEAR" for value in outcomes))
                errors.append(sum(item.attempt_status == "error" for item in window_samples))
            cross = (
                sum(key[0] == cluster_id for key in plan.representatives.cross_removed)
                if plan
                else 0
            )
            if pre == [0, 0]:
                state = "empty"
            elif post == [0, 0] and cross:
                state = "cross_window_reuse"
            elif (post[0] == 0) != (post[1] == 0):
                state = "one_sided"
            elif min(scored) >= min_sample_size:
                state = "tested"
            elif min(completed) >= min_sample_size:
                state = "unclear_tested"
            elif errors[0] or errors[1]:
                state = "error"
            else:
                state = "insufficient"
            cells.append(
                [
                    cluster_index,
                    dimension_index,
                    pre[0],
                    pre[1],
                    cross,
                    post[0],
                    post[1],
                    selected[0],
                    selected[1],
                    completed[0],
                    completed[1],
                    scored[0],
                    scored[1],
                    unclear[0],
                    unclear[1],
                    errors[0],
                    errors[1],
                    _STATE_CODES[state],
                ]
            )
            if pre != [0, 0]:
                populated_states.append(state)
    if not populated_states or not any(
        state in {"tested", "unclear_tested"} for state in populated_states
    ):
        status = "insufficient"
    elif all(state in {"tested", "unclear_tested"} for state in populated_states):
        status = "ready"
    else:
        status = "partial"
    payload = {
        "cells": cells,
        "cluster_ids": clusters,
        "dimensions": dimensions,
        "exclusions": (
            sorted(
                [
                    clusters.index(cluster_id),
                    _WINDOW_CODES[window],
                    _EXCLUSION_REASONS.index(reason),
                    count,
                ]
                for (cluster_id, window, reason), count in plan.cell_exclusions.items()
            )
            if plan
            else []
        ),
        "global": {
            "exclusion_counts": [
                (plan.exclusions if plan else {}).get(reason, 0)
                for reason in _EXCLUSION_REASONS
            ],
            "membership_counts": [
                (plan.membership if plan else {}).get(reason, 0)
                for reason in _MEMBERSHIP_REASONS
            ],
            "metadata_bytes": plan.metadata_bytes if plan else 0,
        },
        "exclusion_reasons": list(_EXCLUSION_REASONS),
        "membership_reasons": list(_MEMBERSHIP_REASONS),
        "schema": "coverage-v1",
    }
    return _json(payload), status


def _coverage_worst_case_size(clusters: list[str], dimensions: list[str]) -> int:
    cells = [
        [cluster, dimension, *([50_000] * 15), 6]
        for cluster in range(len(clusters))
        for dimension in range(len(dimensions))
    ]
    exclusions = [
        [cluster, window, reason, 50_000]
        for cluster in range(len(clusters))
        for window in _WINDOW_CODES.values()
        for reason in range(len(_EXCLUSION_REASONS))
    ]
    return len(
        _json(
            {
                "cells": cells,
                "cluster_ids": clusters,
                "dimensions": dimensions,
                "exclusion_reasons": list(_EXCLUSION_REASONS),
                "exclusions": exclusions,
                "global": {
                    "exclusion_counts": [50_000] * len(_EXCLUSION_REASONS),
                    "membership_counts": [50_000] * len(_MEMBERSHIP_REASONS),
                    "metadata_bytes": 33_554_432,
                },
                "membership_reasons": list(_MEMBERSHIP_REASONS),
                "schema": "coverage-v1",
            }
        ).encode("utf-8")
    )


def _persist_unavailable(
    storage: ConversationDriftStorage,
    config: ConversationAnalysisConfig,
    version_id: str,
    policy: str,
    evaluator: str,
    reason: str,
    started_at: datetime,
    plan: ConversationPlan | None = None,
) -> ConversationAnalysisResult:
    dimensions = json.loads(evaluator)["dimensions"]
    clusters = sorted(
        cluster.cluster_id
        for cluster in storage.list_cluster_registry_clusters(config.tenant_id, version_id)
    )
    coverage, _ = _coverage(clusters, dimensions, plan, [], config.min_sample_size)
    completed_at = datetime.now(timezone.utc)
    run = ConversationDriftRun(
        config.tenant_id,
        config.run_id,
        version_id,
        policy,
        conversation_json_fingerprint(policy),
        evaluator,
        conversation_json_fingerprint(evaluator),
        config.target_workload,
        config.baseline_start,
        config.baseline_end,
        config.current_start,
        config.current_end,
        config.analysis_cutoff,
        "unavailable",
        coverage,
        0,
        0,
        started_at,
        completed_at,
        config.actor,
        reason,
    )
    storage.insert_conversation_drift_snapshot(run, [], [])
    return ConversationAnalysisResult(config.run_id, "unavailable", reason, 0)


def _judge_attempt(judge: object, query: str, response: str, trace_id: str) -> tuple[datetime, object]:
    try:
        result = judge.judge(query=query, response=response, context=None, trace_id=trace_id)
    except Exception as exc:
        result = exc
    return datetime.now(timezone.utc), result


def _judge_attempts(
    judge: object,
    selected: list[ConversationRepresentative],
    contents: dict[str, ConversationTraceContent],
    *,
    concurrency: int,
    timeout: int,
) -> tuple[list[tuple[datetime, object]], int]:
    """Run fixed waves and join every launched provider attempt before finalization."""
    results: list[tuple[datetime, object]] = []
    submitted = 0
    for start in range(0, len(selected), concurrency):
        batch = selected[start : start + concurrency]
        executor = ThreadPoolExecutor(max_workers=concurrency)
        futures = [
            executor.submit(
                _judge_attempt,
                judge,
                contents[item.trace_id].prompt_redacted,
                contents[item.trace_id].response_redacted,
                item.trace_id,
            )
            for item in batch
        ]
        submitted += len(futures)
        done, unfinished = wait(futures, timeout=timeout)
        timed_out_at = datetime.now(timezone.utc)
        # Built-in live adapters are constructed with this same native request
        # timeout and one attempt. Join even after the logical deadline so no
        # provider work survives the immutable terminal snapshot.
        executor.shutdown(wait=True, cancel_futures=True)
        try:
            for future in futures:
                if future in done:
                    results.append(future.result())
                else:
                    results.append((timed_out_at, TimeoutError()))
        finally:
            for future in unfinished:
                future.cancel()
        if unfinished:
            results.extend((timed_out_at, TimeoutError()) for _item in selected[start + len(batch) :])
            break
    return results, submitted


def run_conversation_analysis(
    storage: object,
    judge: object,
    config: ConversationAnalysisConfig,
    *,
    assigner: Callable[[str, str, datetime], object] | None = None,
    judge_factory: Callable[[], object] | None = None,
) -> ConversationAnalysisResult:
    if not isinstance(storage, ConversationDriftStorage):
        raise TypeError("storage lacks conversation drift capability")
    started_at = datetime.now(timezone.utc)
    policy, evaluator = _definitions(
        config,
        judge,
        require_provider_deadline=judge_factory is None,
    )
    existing = storage.get_conversation_drift_snapshot(config.tenant_id, config.run_id)
    if existing is not None:
        run, _samples, signals = existing
        expected = (
            policy,
            evaluator,
            config.target_workload,
            config.baseline_start,
            config.baseline_end,
            config.current_start,
            config.current_end,
            config.analysis_cutoff,
            config.actor,
        )
        actual = (
            run.analysis_policy_json,
            run.evaluator_definition_json,
            run.target_workload,
            run.baseline_start,
            run.baseline_end,
            run.current_start,
            run.current_end,
            run.analysis_cutoff,
            run.actor,
        )
        if actual != expected:
            raise ValueError("immutable conversation run conflict")
        return ConversationAnalysisResult(
            config.run_id,
            run.status,
            run.unavailable_reason,
            0,
            len(signals),
        )

    pointer = storage.get_active_cluster_registry(config.tenant_id)
    if pointer.version_id is None:
        return ConversationAnalysisResult(config.run_id, "unavailable", "registry", 0)
    version_id = pointer.version_id
    if assigner is None:
        from verdict_eval.cluster_registry import ClusterRegistryService

        service = ClusterRegistryService(storage)
        assigner = lambda tenant, pinned, cutoff: service.assign(  # noqa: E731
            tenant, pinned, through_cutoff=cutoff
        )
    try:
        with storage.cluster_analysis_snapshot() as source:
            _candidate_rows(source, version_id, config)
        assigner(config.tenant_id, version_id, config.analysis_cutoff)
        plan = plan_conversation_analysis(storage, version_id, config)
    except _Unavailable as exc:
        return _persist_unavailable(
            storage, config, version_id, policy, evaluator, exc.reason, started_at, exc.plan
        )
    except Exception:
        return _persist_unavailable(
            storage, config, version_id, policy, evaluator, "assignment_coverage", started_at
        )

    total_content = sum(
        len(item.prompt_redacted.encode("utf-8")) + len(item.response_redacted.encode("utf-8"))
        for item in plan.contents.values()
    )
    estimated_tokens = math.ceil(total_content / 4)
    clusters = sorted(
        cluster.cluster_id
        for cluster in storage.list_cluster_registry_clusters(config.tenant_id, version_id)
    )
    dimensions = json.loads(evaluator)["dimensions"]
    if _coverage_worst_case_size(clusters, dimensions) > 16 * 1024 * 1024:
        return _persist_unavailable(
            storage, config, version_id, policy, evaluator, "coverage_limit", started_at, plan
        )
    if (
        len(plan.representatives.selected) > config.max_judge_calls
        or total_content > config.max_selected_content_bytes
        or estimated_tokens > config.max_estimated_input_tokens
    ):
        return _persist_unavailable(
            storage, config, version_id, policy, evaluator, "judge_budget", started_at, plan
        )

    if judge_factory is not None and plan.representatives.selected:
        judge = judge_factory()
        actual_policy, actual_evaluator = _definitions(config, judge)
        if actual_policy != policy or actual_evaluator != evaluator:
            raise ValueError("constructed judge does not match evaluator definition")

    samples: list[ConversationDriftSample] = []
    judgments_by_window: dict[str, list[Judgment]] = {"baseline": [], "current": []}
    cluster_for_trace: dict[str, str] = {}
    selected = plan.representatives.selected
    attempts, provider_calls = _judge_attempts(
        judge,
        selected,
        plan.contents,
        concurrency=config.judge_concurrency,
        timeout=config.judge_attempt_timeout,
    )
    for representative, attempt in zip(selected, attempts, strict=True):
        try:
            terminal_at, judgment = attempt
            if isinstance(judgment, Exception):
                raise judgment
            if getattr(judgment.status, "value", judgment.status) != "completed":
                raise RuntimeError("judge returned a terminal error")
            outcomes = {item.name: item.verdict.value.upper() for item in judgment.dimensions}
            if set(outcomes) != set(dimensions):
                raise ValueError("invalid judge dimension set")
            legacy_status = "not_attempted"
            judgment_id: str | None = None
            try:
                storage.insert_judgment(judgment)
                legacy_status = "written"
                judgment_id = judgment.judgment_id
            except Exception:
                legacy_status = (
                    "source_deleted"
                    if not storage.trace_exists(representative.trace_id)
                    else "storage_error"
                )
            samples.append(
                ConversationDriftSample(
                    config.tenant_id,
                    config.run_id,
                    version_id,
                    representative.cluster_id,
                    representative.session_ordinal,
                    representative.window,
                    representative.trace_id,
                    representative.started_at,
                    terminal_at,
                    "completed",
                    legacy_status,
                    _json(outcomes),
                    judgment_id=judgment_id,
                )
            )
            judgments_by_window[representative.window].append(judgment)
            cluster_for_trace[representative.trace_id] = representative.cluster_id
        except Exception as exc:
            category = (
                "timeout"
                if isinstance(exc, TimeoutError)
                else "connection"
                if isinstance(exc, ConnectionError)
                else "invalid_response"
                if isinstance(exc, ValueError)
                else "provider"
            )
            samples.append(
                ConversationDriftSample(
                    config.tenant_id,
                    config.run_id,
                    version_id,
                    representative.cluster_id,
                    representative.session_ordinal,
                    representative.window,
                    representative.trace_id,
                    representative.started_at,
                    terminal_at,
                    "error",
                    "not_attempted",
                    "{}",
                    error_category=category,
                )
            )

    detector = DriftDetector(
        min_sample_size=config.min_sample_size,
        p_threshold=config.p_threshold,
        effect_size_threshold=config.effect_size_threshold,
    )
    detected = detector.detect(
        current=build_windows_from_judgments(judgments_by_window["current"], cluster_for_trace),
        baseline=build_windows_from_judgments(judgments_by_window["baseline"], cluster_for_trace),
    )
    signals = [
        ConversationDriftSignal(
            config.tenant_id,
            config.run_id,
            uuid5(
                NAMESPACE_URL,
                "|".join(
                    (
                        "conversation-signal-v1",
                        config.run_id,
                        signal.cluster_id,
                        signal.dimension,
                        signal.statistic_name,
                        signal.direction.value,
                    )
                ),
            ).hex,
            version_id,
            signal.cluster_id,
            signal.dimension,
            signal.direction.value,
            signal.statistic_name,
            signal.statistic_value,
            signal.p_value,
            signal.p_value_adjusted,
            signal.effect_size_cliffs_delta,
            signal.sample_size_current,
            signal.sample_size_baseline,
            _json(signal.example_trace_ids),
            signal.recommended_action,
        )
        for signal in detected
    ]
    clusters = sorted(
        cluster.cluster_id
        for cluster in storage.list_cluster_registry_clusters(config.tenant_id, version_id)
    )
    coverage, status = _coverage(clusters, dimensions, plan, samples, config.min_sample_size)
    decoded = json.loads(coverage)
    if not plan.source_rows:
        status = "insufficient"
    elif not plan.representatives.pre_counts:
        status = "ineligible"
    elif not plan.representatives.selected:
        status = "insufficient"
        decoded["global"]["reason"] = "cross_window_reuse"
    coverage = _json(decoded)
    completed_at = datetime.now(timezone.utc)
    run = ConversationDriftRun(
        config.tenant_id,
        config.run_id,
        version_id,
        policy,
        conversation_json_fingerprint(policy),
        evaluator,
        conversation_json_fingerprint(evaluator),
        config.target_workload,
        config.baseline_start,
        config.baseline_end,
        config.current_start,
        config.current_end,
        config.analysis_cutoff,
        status,
        coverage,
        len(signals),
        len(samples),
        started_at,
        completed_at,
        config.actor,
    )
    storage.insert_conversation_drift_snapshot(run, samples, signals)
    return ConversationAnalysisResult(
        config.run_id, status, None, provider_calls, len(signals)
    )
