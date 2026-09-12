# ADR-008: Durable analysis and dashboard workspaces

**Status:** Current

## Context

Findings used for history, navigation, or notification need a durable input
cutoff and stable identity. Provider outcome, deterministic finding severity,
evaluator judgment, and drift status are separate states. The dashboard must
present those states without treating missing evidence as success or failure.

## Invariants

1. A persisted analysis record is terminal and immutable. A crash before
   publication leaves no authoritative result.
2. One record contains the complete bounded analysis snapshot, including an
   explicit empty findings list.
3. The input fingerprint covers every field that can change the deterministic
   result without retaining additional raw content.
4. One selected evaluator identity determines judgment coverage and trace
   verdicts; results from different identities are not combined.
5. A notification references one persisted analysis or monitor result and one
   destination-configuration fingerprint.
6. A recorded successful delivery is not sent again. Webhook requests carry a
   stable idempotency key.
7. Verdict supports one scheduled worker per store. Distributed exactly-once
   webhook delivery is not claimed.
8. URL state owns workspace, subsection, evaluator, finding, run, and trace
   selection so navigation survives refresh and direct links.
9. Activating a monitor candidate retires sibling candidates for that scope;
   only previews created after activation may appear beside the active result.
10. A monitor candidate policy and its initial comparison snapshot are persisted
    in one storage transaction. Candidate reads require that initial snapshot,
    so incomplete legacy records remain inert after a restart.
11. Monitor is the only current drift workflow. Activation stores an immutable
    event-time boundary and creates an empty prospective cohort; evidence whose
    event time predates that boundary never enters a later current cohort.

## Data ownership

- `deterministic_analysis_runs` stores immutable terminal analysis snapshots.
- `notification_delivery_attempts` stores append-only terminal delivery
  attempts without customer content or secret values.
- Existing typed monitor, cluster, evaluator, and control records remain the
  operational authorities for their respective workflows.
- Buffered trace persistence does not own analysis publication or notification
  delivery.

## Dashboard workspaces

- Overview presents the persisted monitor result, findings, evidence coverage,
  and supporting Reliability, Performance, and Behavior views without
  manufacturing a combined run verdict.
- Findings link to their affected Agent Runs, traces, and bounded evidence.
- Trace Explorer presents provider execution separately from evaluation states:
  not evaluated, judge error, pass, fail, and unclear.
- Agent Runs show source outcome, evidence coverage, deterministic findings,
  and selected-evaluator coverage separately.
- Six top-level workspaces separate concerns: Overview, Explore, Evaluate,
  Monitor, Report, and Settings. Report summarizes application calls separately
  from evaluator traffic and emits aggregate-only management exports. Monitor
  contains current cohort status, historical comparisons, optional segments,
  schedules, and read-only legacy history.
- Fixed-window evaluation signals created by earlier releases remain readable
  for audit and trace navigation. They are not current status, do not contribute
  to Overview or navigation alert counts, and are not produced by the current
  evaluation pipeline.
- A prospective cohort distinguishes traffic collection from waiting for
  selected-evaluator results until a persisted comparison completes.
- Generic change records are a decision log; typed workflows perform actual
  evaluator, cluster, or monitor activation.

## Notification delivery

Notification selection uses persisted findings or completed monitor results and
the configured finding/drift filters. Each delivery attempt records success or
failure. Retries stop after a recorded success. A process crash after remote
acceptance but before local persistence can result in another delivery, so the
receiver must honor the idempotency key for end-to-end deduplication.

## Compatibility

- Schema initialization is additive for supported SQLite and PostgreSQL
  installations. Legacy serialized agent bundles migrate transactionally into
  normalized source/run/turn/event rows and remain available only for rollback.
- Existing public constructors remain backward compatible. Trace appends
  optional `service_name` and `environment` fields after its published
  positional fields; the other constructors are unchanged.
- Existing dashboard response fields remain available while explicit status and
  coverage fields are added.
- Monitor snapshot JSON adds evaluator-finalization state. PostgreSQL schema
  initialization also adds a generated snapshot write sequence to resolve
  timestamp ties deterministically; existing rows are backfilled automatically.
  Older evaluator-backed monitors remain readable but require a new reviewed
  preview before execution.
- Prospective monitor snapshots add an activation event-time boundary. Existing
  active prospective monitors without it remain readable but require a new
  reviewed preview before they can advance. No database schema migration is
  required because the field is inside the existing snapshot document.
- `/dashboard` remains the entry point; URL parameters provide deep links
  without adding a frontend router dependency.

## Consequences

Analysis and notification history are reproducible from immutable records.
Dashboard state is explicit and navigable. Webhook delivery is auditable and
retryable, but distributed exactly-once delivery is outside the current
contract.
