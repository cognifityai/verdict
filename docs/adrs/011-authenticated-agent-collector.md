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

The collector accepts full `agent` records, standalone `trace` records, and
manual `span` records. Agent records contain the run, turn, typed events, and
any genuinely linked model-call Trace. Every accepted record is assigned the
collector's configured tenant. Trace IDs already belong to a tenant in
storage; Span IDs reject a write from another tenant. A Span's explicit Trace
link is removed when that Trace is missing or belongs to another tenant.
Retired `signal` records remain rejected. Existing direct and local-file import
paths are unchanged. This expansion is in the current source;
the published `0.1.0b2` collector accepts only Agent records.
Exact batch retries return the same stored receipt. Different batches that
reuse one same-tenant Trace or Span ID retain the existing direct-storage
update semantics; producers must use unique IDs, and arbitrary out-of-order
updates are not guaranteed to converge.

The collector is not mounted into the dashboard. Its request size, record
count, identifier size, acknowledgement size, connection pools, database waits,
and in-flight work are bounded. Deployments terminate TLS before the collector;
credentials and PostgreSQL connection strings are supplied through protected
environment configuration.

## Compatibility

The collector continues using the PostgreSQL `collector_receipts` table and
canonical storage boundaries. Completed rejection receipts from older
collectors remain immutable. SQLite, direct SDK capture, local file import,
Claude/Codex capture, telemetry import, evaluation, clustering, monitoring, and
dashboard reads retain their existing interfaces.

## Consequences

SDK producers can send Agent and standalone records to central PostgreSQL
without database credentials. ADR-012 provides file checkpointing and
acknowledged segment deletion. Ingestion-triggered analysis remains separate.
