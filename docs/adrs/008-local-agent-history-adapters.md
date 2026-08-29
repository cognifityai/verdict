# ADR 008: Local agent histories use the canonical telemetry importer

- Status: Accepted
- Date: 2026-08-29

## Context

Claude Code and Codex retain local JSONL event histories. Developers should be
able to import completed root turns, run Verdict's count-cohort monitor, and
open the local dashboard without instrumenting either agent.

An earlier standalone prototype parsed those files into its own `CapturedTurn`
type, independently generated trace identifiers and tags, constructed Verdict
`Trace` objects, and wrote directly to SQLite. Verdict already owns those
responsibilities through `ImportContext`, `MappingResult`, shared normalization,
`import_into_storage`, and its storage port. Keeping both paths would allow
identity, privacy, error, retry, and backend behavior to diverge.

## Decision

Claude Code and Codex are stateful source adapters inside Verdict's telemetry
import subsystem. Their only source-specific responsibility is interpreting a
sequence of source events as a completed root agent turn and discarding
non-authoritative content such as thinking, tool arguments/results, sidechains,
and ambient local context.

The adapters emit `MappingResult` through the shared `make_trace` normalizer.
The existing import runner owns accounting and persistence. The existing
storage port owns redaction, deterministic upsert behavior, and SQLite/Postgres
parity. No raw source envelope is persisted.

Verdict exposes two installed commands:

- `verdict-agent-capture` imports local history through the canonical importer
  and exits. It requires only the core distribution.
- `verdict-local` composes that importer with the count-cohort monitor and the
  packaged dashboard. It is installed by the eval distribution and available
  through the core package's `local` extra.

One imported `Trace` represents one completed root agent turn. `session_id`
retains a pseudonymous source session identity so statistical analysis can use
sessions as independent units rather than treating correlated turns as
independent samples.

Full rescans are intentional. Deterministic identity plus storage upsert makes
retries idempotent without a second checkpoint database or watcher state.

## Failure contract

- Missing source directories produce an empty successful import.
- Child/subagent histories, sidechains, synthetic responses, incomplete turns,
  and unsupported records are skipped with bounded reasons.
- Malformed or oversized JSONL raises the existing source-stage import error;
  already accepted rows remain safe to replay because identity is deterministic.
- Source histories are opened read-only and are never modified.
- SQLite created by the local default is restricted to the current user.
- Local capture rejects in-memory storage because later processes must reopen
  the same traces.
- Storage failures use the shared partial-progress error contract and never
  expose a credential-bearing storage URL.

## Alternatives rejected

Keeping the standalone Agent Capture importer behind a Verdict-facing wrapper
was rejected. It would preserve two JSON readers, normalization paths, identity
schemes, persistence loops, and test matrices. Moving that duplicate code into
the Verdict repository would change its location, not repair the boundary.

## Consequences

Local source formats remain replaceable adapters. Improvements to shared
normalization, redaction, storage, and retry behavior automatically apply to
local capture. The separate unpublished Agent Capture product path is
superseded; only its source-format knowledge and adversarial fixtures are
retained.
