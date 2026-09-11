# ADR-012: Agent segment shipper

**Status:** Current

## Context

The Agent SDK can write bounded process-owned files, and the collector can
ingest retry-safe Agent batches. Producer hosts still need a delivery component
that does not put database credentials or network retry work in the application
request path.

## Decision

Verdict provides a separately runnable host shipper. The capture segment itself
is the local backlog; the shipper does not copy records into another queue.
Complete records are uploaded in bounded deterministic batches. The collector's
durable receipt is the remote idempotency authority, while one atomic local
checkpoint records acknowledged byte offsets and delivery health. Losing a
checkpoint causes safe replay rather than lost evidence.

Active segments use an `.open` suffix. The shipper may upload their complete
prefix but never deletes or renames them. Normal producer rotation or shutdown
seals a segment as JSONL. A sealed segment is deleted only after every complete
record receives a validated accepted acknowledgement. If any record is
permanently rejected, the sealed segment is renamed `.rejected` and retained.

One process lock owns a spool directory. Retries and work per cycle are bounded.
The checkpoint contains no evidence, credential, tenant, endpoint, or absolute
path. It is bound to a hash of the collector URL so a partially shipped stream
cannot silently switch destinations. The API key is read from an environment
variable and never persisted.

Local status is derived from the bounded segment inventory plus the checkpoint
and does not contact the collector. It reports pending bytes, segment states,
last append, last receipt, and the last bounded error. Missing remote health is
not interpreted as zero backlog or successful delivery.

The first collector accepts only full Agent records, including linked LLM
Traces. A mixed segment can therefore deliver its Agent records, but the source
segment is retained when standalone Trace or Span records are rejected. Those
records continue to use direct or manual file import.

## Compatibility

Existing sealed JSONL segments remain importable and can be shipped after the
producer is restarted on the new file lifecycle. Local import ignores retired
signal records written by earlier alpha releases and continues with later
supported records. Existing direct storage, local import, Claude/Codex,
telemetry, analysis, evaluator, cluster, and monitor paths are unchanged.

## Consequences

Remote Agent capture becomes continuous without a database driver or remote
network dependency in the application process. Host backlog and last receipt
are available from the shipper's JSON status. Centralized host-backlog UI and a
durable receipt-to-analysis cursor require additional server-side state and are
not inferred from missing data or performed inside the ingestion request.
