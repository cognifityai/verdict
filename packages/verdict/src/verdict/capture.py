"""Canonical transaction boundary for normalized agent evidence."""

from __future__ import annotations

import copy
from collections.abc import Iterable

from verdict.evidence import AgentEventType, AgentRunBundle
from verdict.redaction import sanitize_agent_run_bundle, sanitize_trace
from verdict.schema import Trace
from verdict.storage.base import Storage


class AgentCaptureService:
    """Validate one capture completely before asking storage to commit it."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def capture(
        self,
        bundle: AgentRunBundle,
        *,
        traces: Iterable[Trace] = (),
    ) -> None:
        sanitized_bundle = sanitize_agent_run_bundle(bundle)
        sanitized_traces = tuple(sanitize_trace(copy.deepcopy(trace)) for trace in traces)
        trace_by_id: dict[str, Trace] = {}
        for trace in sanitized_traces:
            if trace.trace_id in trace_by_id:
                raise ValueError("agent capture contains duplicate trace_id")
            if trace.tenant_id != sanitized_bundle.run.tenant_id:
                raise ValueError("agent capture Trace tenant must match the Agent Run tenant")
            trace_by_id[trace.trace_id] = trace

        linked_ids = {
            event.trace_id
            for event in sanitized_bundle.events
            if event.event_type is AgentEventType.MODEL_CALL and event.trace_id is not None
        }
        if set(trace_by_id) - linked_ids:
            raise ValueError("agent capture contains an unlinked Trace")
        for trace_id in linked_ids:
            trace = trace_by_id.get(trace_id) or self._storage.get_trace(trace_id)
            if trace is None:
                raise ValueError("model-call event references a missing Trace")
            if trace.tenant_id != sanitized_bundle.run.tenant_id:
                raise ValueError("model-call event references a Trace from another tenant")

        self._storage.replace_agent_capture(sanitized_bundle, sanitized_traces)
