# ADR-011: Authenticated Agent collector

**Status:** Current

## Context

Instrumented application hosts can write bounded capture records to local
files, but a shared Verdict deployment needs a network boundary that does not
give every host PostgreSQL credentials. Network retries must not duplicate or
silently replace accepted Agent evidence.

## Decision

Verdict provides a separately runnable HTTP collector. Each collector process
has one configured tenant and bearer credential and accepts bounded NDJSON
batches from many producer hosts. Payload tenant fields are replaced by the
configured tenant before storage.

A caller supplies a producer ID and batch ID. PostgreSQL binds that batch ID to
the producer and exact request-body digest. A completed request stores its
bounded acknowledgement bytes; an exact retry returns those bytes unchanged.
Concurrent processing has one transaction-scoped receipt owner. Malformed or
conflicting records receive terminal per-record results, while infrastructure
failures produce no acknowledgement and remain retryable.

The collector accepts full `agent` records. These records contain the run,
turn, typed events, and any genuinely linked model-call Trace. Standalone
`trace`, `span`, and `signal` records are rejected at this boundary because
their existing storage identities do not yet provide the tenant-scoped,
non-regressing merge contract required for untrusted network retries. Their
existing direct and local-file import paths are unchanged.

The collector is not mounted into the dashboard. Its request size, record
count, identifier size, acknowledgement size, connection pools, database waits,
and in-flight work are bounded. Deployments terminate TLS before the collector;
credentials and PostgreSQL connection strings are supplied through protected
environment configuration.

## Compatibility

The only new persistence is the additive PostgreSQL `collector_receipts`
table. Canonical Agent and Trace evidence continues through the existing
storage transaction boundary. SQLite, direct SDK capture, local file import,
Claude/Codex capture, telemetry import, evaluation, clustering, monitoring, and
dashboard reads are unchanged.

## Consequences

Many SDK Agent producers can send retry-safe evidence to a central PostgreSQL
deployment without database credentials. ADR-012 provides file checkpointing
and acknowledged segment deletion. Remote standalone-record delivery and
ingestion-triggered analysis remain separate capabilities.
