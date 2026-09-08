# ADR-010: Agent SDK and local file transport

**Status:** Current

## Context

Provider auto-instrumentation establishes individual LLM calls, but it cannot
infer an application's session, run, turn, tool, test, retry, or business
outcome boundaries. Requiring every application process to connect directly to
the Verdict database is also unsuitable for some deployments.

## Decision

Verdict provides framework-neutral sync and async context managers for Agent
Runs, turns, and tools. Typed helpers record instruction availability, context,
commands, tests, artifacts, retries, handoffs, user feedback, and business
outcomes. Callers supply only facts their application observes; Verdict does
not infer missing execution success or semantic correctness.

One deterministic decision samples the whole run. Child runs inherit the
parent decision. Context is task-local and is restored after nested or failed
execution. Application exceptions remain authoritative; capture failure is
reported without replacing an application result or exception.

A supported provider call snapshots the active turn when the call begins. Its
genuine `Trace` and linked model-call event are written atomically. The Trace is
the sole owner of prompt, response, and raw-message content. The event contains
only bounded operational fields and the Trace identifier.

The default transport writes directly through the normalized storage port. An
optional local file transport writes redacted, versioned JSONL records with
fixed record, segment, and directory byte limits. Files are process-owned and a
producer uses its own spool directory. Import replays complete records
idempotently through the same normalized storage boundary. An incomplete final
record is observable and ignored; a malformed complete record fails import.

Local files are not a delivery protocol. They are retained until an operator
removes them and do not provide acknowledgements, remote authentication,
network retry, or backpressure. Those guarantees belong to a future collector
boundary.

## Compatibility

The SDK appends to the normalized `import_sources`, `agent_runs`,
`agent_turns`, and `agent_events` relations introduced by ADR-009. It adds no
database table or column and does not change existing Trace, judgment, cluster,
monitor, or drift identity. Existing provider-only initialization continues to
work without an Agent Run context.

## Consequences

Instrumented applications can expose their actual execution structure while
Verdict keeps LLM content canonical and storage writes constant-sized. Direct
SQLite/PostgreSQL capture remains the simplest embedded option; the file
transport removes database credentials from producer hosts but requires an
external import process until a remote collector is implemented.
