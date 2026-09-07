# ADR-009: Normalized agent-evidence storage

**Status:** Current

## Context

An agent run can contain many turns and thousands of observable events. Storing
that hierarchy as one serialized row makes append-only rescans, event paging,
and multi-process ingestion scale with the size of the entire run. Model-call
content also belongs to the existing `Trace` record and must not be copied into
an event envelope.

## Decision

Verdict stores captured agent evidence in four normalized relations:

- `import_sources` owns source identity and observation time;
- `agent_runs` owns run, session, parent-run, agent, service, environment, and
  instance correlation;
- `agent_turns` owns ordered request/response evidence for one run; and
- `agent_events` owns ordered model, tool, command, test, context, and omission
  observations.

Tenant and run scope are part of turn and event identity because source-local
identifiers can repeat across runs. An event may link to one genuine `Trace`.
The `Trace` remains the sole owner of LLM request/response content and the
complete provider-call record used by LLM evaluation and drift. The linked
event stores the Trace identifier and may retain bounded scalar metadata needed
to summarize the surrounding agent execution; it never copies prompt, response,
or raw-message content.

Capture validates and sanitizes the complete incoming projection, then writes
linked Traces and normalized hierarchy rows in one storage transaction. A
rescan may add new evidence or advance an open item to a terminal state; it
cannot replace terminal facts. Missing rows in a later source projection do not
delete previously captured evidence. Conflicts fail without partial mutation.
Explicit Trace deletion or retention keeps the surrounding model-call event
and clears its Trace link, so execution history never becomes a dangling
reference.

The dashboard pages run events directly from `agent_events`. Summary scans are
bounded and explicitly report partial coverage. PostgreSQL schema creation is
serialized so concurrent process startup cannot race the initial DDL.

## Compatibility

The public `SourceSession`, `AgentRun`, `AgentTurn`, `AgentEvent`, and
`AgentRunBundle` types remain available. `AgentRunBundle` is a transfer object,
not the current physical storage format. Existing Trace, Judgment, cluster,
monitor, and drift identities are unchanged.

On first open, a supported SQLite or PostgreSQL store transactionally migrates
legacy `agent_run_bundles` rows into the normalized relations. The legacy table
is retained as rollback evidence but current capture and dashboard paths do not
read or write it. Operators must stop older Verdict processes and back up the
store before upgrading; mixed old and new writers are not supported.

## Consequences

Event append cost and run-detail paging no longer require rewriting or reading
one growing run blob. Foreign keys preserve hierarchy and Trace linkage, while
source/run/agent/service fields allow multiple instrumented agents and machines
to share one PostgreSQL store without merging their identities in the UI.
