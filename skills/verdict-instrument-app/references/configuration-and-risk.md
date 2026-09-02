# Configuration questions, plan, and risk register

Inspect first, then ask one batched question set containing only unresolved material
choices. Give a recommended answer and consequence for each question.

## Batched decision categories

### Scope and authorization

- Which environment, processes, providers, and live call paths are in scope?
- May the agent edit the listed files and install the exact Cognifity packages after
  presenting the plan?
- May it start a temporary local dashboard and create a synthetic local database?
- Who approves persistent storage, schedules, and live evaluator calls?

### Data and privacy

- Metadata-only, or prompt/response content? Recommend metadata-only first.
- Which fields are prohibited, and which synthetic canaries represent them?
- What stable context dimensions are needed, and can they be pseudonymized?
- What explicit retention period and deletion responsibility apply?
- Is one tenant stored per analysis store? If not, redesign isolation before drift
  analysis.

An approval to add Verdict is not approval to store prompts and responses.

### Storage and dashboard

- Local single-host trial or shared capture?
- Exact storage owner and absolute SQLite path, or approved Postgres endpoint/secret?
- Does the bundled dashboard satisfy the need? It reads SQLite or PostgreSQL.
- If non-local, what authentication and TLS/reverse-proxy boundary protects it?

| Need | Recommended starting point | Constraint to disclose |
|---|---|---|
| Isolated developer trial | absolute-path SQLite | local file durability and concurrency |
| Shared/multi-instance capture | Postgres | keep the DSN in `VERDICT_STORAGE` and mount the UI behind application auth |
| Dashboard demonstration | SQLite plus localhost bind | not an outbound alerting service |
| Production analytics | design separately | public-alpha release is not a production claim |

### Jobs, latency, and cost

- Which are needed: drift analysis, retention cleanup, or health checks?
- What is the slowest acceptable detection delay?
- What eligible trace volume exists per cluster/window?
- Which evaluator and rubric are approved, and what is the spend/rate limit?
- Which existing scheduler should own each job?

Do not hide low volume by scheduling more frequently. If a statistically useful
window takes days to fill, report that detection limit.

## Safe trial profile

Unless the customer approves otherwise, propose:

- one non-production environment and one supported provider path;
- one service/environment scope per store because those init labels are not persisted
  on trace rows in `0.1.0a14`;
- `capture_content=False`;
- `buffered_writes=False`;
- a customer-owned absolute SQLite path;
- no unrestricted metadata;
- an explicit short retention period;
- no live evaluator calls during setup;
- a temporary dashboard bound to loopback; and
- no schedule until the command succeeds manually through the final sink.

Quality drift requires stored content. Do not silently switch content on to make the
dashboard interesting; obtain separate approval after explaining the data flow and
redaction limitations.

## Required plan contents

Before edits, show:

1. exact files, dependency names/versions, and config keys;
2. initialization placement and every covered process;
3. trace data flow from provider call through the final store;
4. collected and excluded fields, redaction tests, retention, and deletion command;
5. job commands, scheduler, frequency basis, lock, timeout, logs, credentials, and
   cost estimate;
6. dashboard binding/authentication and selected storage backend;
7. tests for success, provider error, storage error, sampling, lifecycle, privacy,
   drift/no-drift, and rollback; and
8. rollback steps that remove instrumentation without changing provider behavior.

Ask for explicit approval of this exact plan.

## Minimum risk register

Include likelihood, impact, mitigation, owner, verification, and residual risk for:

- silent zero capture from unsupported calls or late initialization;
- duplicate/lost traces from retries, sampling, buffering, or process exit;
- sensitive content surviving best-effort redaction;
- relative/ephemeral SQLite paths and shared-writer contention;
- a credential-bearing storage URL leaking through a checked-in command, process
  argument, or generated document;
- weak statistical evidence from low volume or correlated conversation turns;
- judge/provider spend, latency, rate limits, and credential exposure;
- overlapping or partially failed scheduled jobs;
- remotely exposed dashboard data; and
- Python package namespace collision with the unrelated `verdict` distribution.

## Documentation to generate

Place customer-facing setup material where that repository normally keeps runbooks or
integration docs. Cover setup, architecture/data flow, privacy and retention, schedule
and operations, rollback, verified evidence, untested paths, and residual risks. Do
not include secrets, production trace samples, or claims broader than executed tests.
