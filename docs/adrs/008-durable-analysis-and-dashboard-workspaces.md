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

## Data ownership

- `deterministic_analysis_runs` stores immutable terminal analysis snapshots.
- `notification_delivery_attempts` stores append-only terminal delivery
  attempts without customer content or secret values.
- Existing typed monitor, cluster, evaluator, and control records remain the
  operational authorities for their respective workflows.
- Buffered trace persistence does not own analysis publication or notification
  delivery.

## Dashboard workspaces

- Overview reports evidence and evaluator coverage without manufacturing a
  combined run verdict.
- Findings link to their affected Agent Runs, traces, and bounded evidence.
- Trace Explorer presents provider execution separately from evaluation states:
  not evaluated, judge error, pass, fail, and unclear.
- Agent Runs show source outcome, evidence coverage, deterministic findings,
  and selected-evaluator coverage separately.
- One Drift workspace contains Overview, Explore, Monitor, Signals, and
  Clusters.
- A prospective cohort reports collection progress until a persisted comparison
  completes.
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
  installations.
- Existing Trace, SpanRecord, Judgment, DriftSignal, and AgentRunBundle public
  constructors remain unchanged.
- Existing dashboard response fields remain available while explicit status and
  coverage fields are added.
- `/dashboard` remains the entry point; URL parameters provide deep links
  without adding a frontend router dependency.

## Consequences

Analysis and notification history are reproducible from immutable records.
Dashboard state is explicit and navigable. Webhook delivery is auditable and
retryable, but distributed exactly-once delivery is outside the current
contract.
