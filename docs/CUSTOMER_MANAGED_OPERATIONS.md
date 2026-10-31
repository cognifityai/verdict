# Customer-managed single-tenant operations

This guide describes the customer-managed deployment of the current beta. This guide does not establish an availability target. Use the [release profile](RELEASE_PROFILE.md) for its supported package surface.

## One store, one routing ID, one recurring worker

Use a dedicated PostgreSQL database for Verdict evidence and controls. Give
direct-store SDK producers, or the collector for shipped local-file records,
the same protected storage URL and tenant ID as the dashboard and
`verdict-service`. Start one supervised continuous worker for the saved
schedule. The dashboard does not start or supervise that process when a plan
is saved. The SDK does **not** read `VERDICT_STORAGE` or `VERDICT_TENANT_ID`
automatically; a direct-store application must pass them at startup:

Initialize a new or older Verdict schema before starting evidence writers.
That first startup applies schema changes in one transaction. Once the current
revision is recorded, later process starts skip those DDL statements and can
open alongside active writers. A failed initialization does not record the
revision; fix the database error before starting producers.

For an initial installation, start with one instrumented agent and a
dedicated PostgreSQL store. The direct SDK route captures Agent Runs and
supported provider calls when their instrumentors are enabled. The
file-spool/shipper/collector route centrally ingests Agent records with their
linked calls, standalone provider Traces, and manual Spans. The collector
replaces untrusted record tenant IDs with its configured routing ID; a Span
cannot replace an ID already owned by another tenant. Inspect shipper backlog and
`capture.dropped_records` before treating counts as complete. A saved schedule
requires the separately supervised worker below. Verify a recorded run, exact
event navigation, one deterministic issue, and a completed worker cycle on the
chosen topology before interpreting absence of alerts.
If the collector cannot check an explicit Span-to-Trace link because storage is
temporarily unavailable, it responds with a retryable error; the shipper retains
that source segment until the link can be checked and acknowledged.

The repository's `test_customer_topology.py` starts separate dashboard,
collector, and worker CLI processes against an isolated disposable PostgreSQL
schema. It sends a wholly synthetic Agent Run through the SDK file spool and
HTTP collector, then checks the saved-plan worker cycle and exact run notice.
It is a local integration check; the customer's TLS ingress, supervisor,
network, secrets, and load still require acceptance in their environment.

```python
import os
import verdict

verdict.init(
    service_name="your-agent-service",
    storage=os.environ["VERDICT_STORAGE"],
    tenant_id=os.environ["VERDICT_TENANT_ID"],
    capture_content=False,  # Choose content capture only after data approval.
)
```

```bash
# Inject these from a secret manager or service supervisor, not a checked-in file.
export VERDICT_STORAGE='postgresql://…'
export VERDICT_TENANT_ID='your-routing-id'

verdict-dashboard --tenant-id "$VERDICT_TENANT_ID" \
  --host 127.0.0.1 --port 8000

# In a separately supervised process, with the same environment:
verdict-service --tenant-id "$VERDICT_TENANT_ID"

# If producers use the file-spool/shipper route, inject a collector API key
# from the secret manager and start the separate collector process:
verdict-collector --tenant-id "$VERDICT_TENANT_ID" \
  --host 127.0.0.1 --port 8765
```

All three CLIs read `VERDICT_STORAGE` from their environment. A PostgreSQL URL
or valid libpq keyword connection string is accepted by the SDK, dashboard,
collector, and worker; use the same value in each process. Malformed keyword
connection strings fail before any SQLite file is created. Keep credentials
out of command arguments, which may be visible in process listings.

### Local container profile

The source checkout includes a [three-process Compose profile](../deploy/customer-managed/compose.yaml)
and a [wheel-consuming Dockerfile](../deploy/customer-managed/Dockerfile). Build
the wheel from the reviewed checkout, then build one image from that exact
wheel. The existing root Dockerfile installs the published package rather than
the wheel built from this checkout.

```bash
uv build packages/verdict --wheel --out-dir build/customer-managed
# Use the exact wheel path printed by the build; keep just one wheel in this ignored directory.
docker build -f deploy/customer-managed/Dockerfile \
  --build-arg VERDICT_WHEEL=build/customer-managed/cognifity_verdict-0.1.0b3-py3-none-any.whl \
  -t verdict-local:review .
```

Set `VERDICT_IMAGE=verdict-local:review`, `VERDICT_STORAGE`,
`VERDICT_TENANT_ID`, `VERDICT_USER`, `VERDICT_PASS`,
`VERDICT_ALLOWED_HOSTS`, and `VERDICT_COLLECTOR_API_KEY` through the local
operator environment or a protected secret-injection mechanism. The Compose
profile's dashboard health probe uses `Host: 127.0.0.1`, so include
`127.0.0.1` in `VERDICT_ALLOWED_HOSTS` alongside the intended TLS hostname
(for example, `dashboard.internal.example,127.0.0.1`). Keep the hostname
allowlist explicit; the profile does not bypass host validation. The Compose
profile requires every value and runs dashboard, collector, and worker from
the same locally built image and tenant. Compose never pulls that image from a
registry; an absent or mistyped local tag fails startup. It connects to an external private PostgreSQL
database; it does not start one. It binds dashboard and collector only to
loopback (`VERDICT_DASHBOARD_PORT`, default 8765;
`VERDICT_COLLECTOR_PORT`, default 8766) for a local TLS proxy. Container
environment variables are visible to operators with Docker access, so
protect that access and do not paste the resolved Compose configuration into
reports.

```bash
docker-compose -f deploy/customer-managed/compose.yaml config --quiet
docker-compose -f deploy/customer-managed/compose.yaml up -d --wait
```

The wait covers dashboard and collector HTTP startup, preventing a first
request from racing their listeners. Collector `/healthz` checks its
PostgreSQL receipt connection at that moment; dashboard `/api/health` checks
only its web process. Neither proves a completed monitoring cycle. Confirm
the saved plan and worker state through authenticated `/api/readiness`.

Use the authenticated dashboard's `/api/readiness` response to confirm a
completed worker cycle for the saved plan revision. A running worker
container or healthy web process alone does not prove monitoring is active.
The repository's synthetic integration checks exercise separate processes,
file-spool and direct-PostgreSQL capture, linked Trace and Span records, and
a completed saved worker cycle. Validate TLS, external networking, supervisor
recovery, and target-rate load in the deployment environment. For separate hosts,
run the three CLIs under that environment's own supervisor with the same
storage and routing ID instead of assuming this single-host profile spans
machines.

Keep PostgreSQL on a private network. A loopback dashboard needs an
authenticated TLS ingress before other users reach it. If binding Verdict
directly to a non-loopback interface, set `VERDICT_USER`, `VERDICT_PASS`, and
an explicit `VERDICT_ALLOWED_HOSTS` allowlist, and still terminate TLS at a
trusted ingress. Basic Auth protects dashboard APIs except `/api/health`.
Treat dashboard URLs and access logs as sensitive because structural run,
event, and Trace IDs are exact navigation keys and may contain producer-supplied
identifiers. The collector requires its own API key and
TLS termination at a trusted ingress; see
[Onboarding](ONBOARDING.md#3a-instrument-the-surrounding-agent-execution).

## First check and daily operation

1. Capture a small approved synthetic run and check that its turns and events
   appear in **Investigation**. Confirm the configured tenant matches every
   process. A source count alone does not prove complete capture.
2. In **Monitoring**, review the suggested plan, run one local check, inspect
   its result, and save the recurring plan. A saved plan is configuration, not
   proof that a worker ran.
3. Start the supervised worker. When a schedule is saved but no recent worker
   is seen, **Suggested setup** shows the service command and the two
   environment variables that must match the dashboard. Check `/api/readiness`
   through the authenticated dashboard and confirm a recent worker heartbeat
   **and** a completed worker
   cycle for the saved revision. `/api/health` only confirms that the web process
   responds. The UI shows an unfinished cycle as unknown and a failed cycle as
   failed; neither is a successful watch.
   If local history files are skipped, the recent-event index fails, or Codex
   diagnostics cannot be imported, the latest cycle reports
   `capture_incomplete` even when analysis completed. Inspect the cycle's
   capture summary and source logs, then rescan. Previously captured runs are
   the last committed snapshots; this cycle status does not identify which
   individual source revision is current.
   One cycle attempts every discovered `.jsonl` history file and Codex
   `.jsonl.zst` archive once and reports file progress. An active `.jsonl`
   file takes precedence over its same-stem archive. An archive replacing a
   captured active file must preserve the previous source bytes. Corruption
   beyond a partial decoded prefix is unobserved.
   Discovery is limited to 100,000 files per root; a limit error
   fails the cycle before import. Each file retains its own parse and
   projection limits. A rescan reads the source files again to detect changes.
   Setup's **Cancel capture** stops after the current file, preserves earlier
   committed files, and reports a terminal `cancelled` job. Preview and rescan
   to continue. Cancellation is unavailable after analysis starts. The
   `/api/setup/capture/attempts` response counts cancelled file attempts
   separately from source failures.
   A local history file with more than 128 MiB of decoded records is parsed
   only through a bounded prefix. Verdict hashes the whole physical file for
   revision checks and reports `bounded_source_prefix` as partial source
   coverage. Files above 1 GiB of physical bytes, including archives, are
   skipped as `history_file_too_large`; earlier committed evidence is retained.
   A separate redacted preview of up to 50 newest source records may be shown
   for inspection. It may overlap the canonical prefix and adds nothing to
   totals, monitoring, or evaluation. The
   preview is unavailable when a compressed archive expands beyond 512 MiB.
   Uncaptured records cannot support whole-session conclusions. A file with no usable prefix or
   a record over the line bound can still be skipped with a reason.
   A file that expands beyond 100,000 parsed events is rejected with
   `history_event_limit`; this counts multiple events from one source row and
   retains the prior accepted revision.
   Setup capture also reports incomplete when files are skipped;
   previously captured evidence stays available. Per-file attempt records
   retain bounded failure categories, not raw paths or exception text. The
   Data Sources page and `/api/setup/capture/attempts` show tenant-scoped
   latest-attempt counts after a dashboard restart. Counts may include files
   from a previously configured root; rescan current roots before treating
   the count as current-source coverage.
   Send SIGTERM for a supervised stop. It lets the current poll finish before
   removing the worker heartbeat, so allow more than the webhook send timeout
   in the supervisor's stop grace period. Ctrl-C interrupts the current poll
   but also exits without a traceback and removes the heartbeat.
   A hard kill or crash cannot remove the heartbeat; readiness can show it as
   recent for up to 90 seconds before the lease expires. A long local source
   read or provider call may delay a graceful stop further.
4. Follow a recurring notice to its exact run and, for an event notice, its
   event. A terminal failed or timed-out run-status notice describes recorded
   execution status, not task failure or drift. Acknowledging or dismissing
   either notice records review, not recovery. If a later explicit pass is
   recorded, attach its exact run/event IDs in the notice and compare the
   failed event with each attached pass. For SDK command/test events, a matching
   application-supplied `check_id` identifies the reported check on both events.
   The link still does not prove the same inputs, task, or application recovery.
   Verify the repair using the customer's own check identity and acceptance
   procedure.
5. Check Monitoring for failed, blocked, or exhausted alert deliveries, then
   open **Alert destinations → Delivery status** and worker logs. The worker
   records an alert obligation with its source result, then retries failed
   delivery on its polling loop, including when the analysis plan is paused.
   A missing or invalid webhook URL remains visible as waiting for a valid
   final HTTPS URL in the configured worker environment variable. A
   changed or disabled destination blocks the old obligation; review the
   preview and confirm a new destination explicitly. Exhausted delivery can
   be queued for one more attempt. Webhook destinations must be final HTTPS
   URLs; redirects are rejected. Each attempt is recorded, and repeated
   attempts use the same idempotency key. A 2xx means the receiver accepted
   the POST; verify downstream handling with the destination operator. A
   crash after receiver acceptance but before the outcome is stored can send
   the same key again, so the receiver must deduplicate it. Each worker drain
   sends at most two queued alerts (with a 10-second timeout per POST); later
   polls continue the backlog. The dashboard shows delivery problem counts and
   puts problems first among loaded alerts. It pages through older alerts with
   **Show more alerts**, including older problems. The list is
   live; use **Refresh delivery status** to include status changes and newly
   queued alerts while reviewing it.
   A delivered attempt recorded by the published beta for the same finding
   and destination is treated as complete when the new queue sees it.

Without a provider key, capture, deterministic findings, usage, and the
reviewed setup flow remain available. Optional AI explanations require a
bounded evidence preview and explicit approval before egress. Do not use
private traces for a provider test unless their owner separately approves it.

## Backup, restore, and upgrade boundary

Back up the **whole dedicated database**, not selected tables. Keep the
archive and any local producer spool under customer-controlled encryption and
retention. A custom-format PostgreSQL archive is **not encrypted** by
`pg_dump`. A PostgreSQL archive does not include a producer's local spool or
provider secrets. Use a protected PostgreSQL service definition or credential
manager rather than putting passwords in command arguments or scripts.
Provision `VERDICT_BACKUP_DIR` as a protected absolute directory outside the
source checkout and shared workspaces; restrict directory access before running
the command.

```bash
# PGSERVICE points to the dedicated Verdict source database.
umask 077
: "${VERDICT_BACKUP_DIR:?set a protected absolute backup directory}"
case "$VERDICT_BACKUP_DIR" in
  /*) ;;
  *) printf '%s\n' 'VERDICT_BACKUP_DIR must be absolute' >&2; exit 1 ;;
esac
backup="$VERDICT_BACKUP_DIR/verdict-$(date -u +%Y%m%dT%H%M%SZ).dump"
PGSERVICE=verdict_source pg_dump --format=custom --file "$backup"

# Provision a new empty database named verdict_restore first. The service
# supplies host/authentication; --dbname selects that exact database.
# Never overwrite the live database in place.
PGSERVICE=verdict_restore pg_restore --exit-on-error --single-transaction \
  --no-owner --no-privileges --dbname verdict_restore "$backup"
```

Before an upgrade, retain an archive made by the old version and its exact
package/lockfile versions. Rehearse the new candidate against a disposable
restored database. Verify an Agent Run with its turns/events, saved plan
revision, recurring notice state, worker cycle, and tenant-scoped dashboard
reads. Stop old processes before switching a live store to a new version.
Rollback to an older executable requires restoring its **pre-upgrade** archive
into a separate database; do not assume an older executable can read a store
opened by a newer schema. Redirect producers and the worker only after the
restored instance passes its checks.

The repository's disposable PostgreSQL test exercises `pg_dump`/`pg_restore`
and restored application reads for synthetic evidence, schedule, worker and
notice state. It does not establish customer backup frequency, point-in-time
recovery, disaster recovery time, or an upgrade from the published beta.

## Retention and deletion

The dashboard has no enforced Agent Run retention-days control. The storage
`prune_before` method does not remove all normalized agent evidence or its
bundled copies. For this dedicated single-tenant profile, configure database,
archive, and producer-spool retention with the customer's infrastructure.
For whole-store deletion, stop producers, collector, dashboard, and worker;
remove the dedicated database and all retained archives and spools under the
customer's policy. A selective, in-app Agent Run deletion workflow remains
unimplemented. Never represent `prune_before` as complete agent-data erasure.

This procedure needs a customer-specific restore rehearsal and security review
before a production availability or recovery target can be promised.
