# Verdict 0.1.0a6 target

This release keeps the bounded capture-method evidence in the public
[`0.1.0a4` POC release profile](https://github.com/cognifityai/verdict/blob/v0.1.0a4/docs/POC_RELEASE_PROFILE.md).
It remains a Python public alpha, not a production-readiness claim.

## Install one synchronized set

Use the customer application's existing Python 3.10+ environment. Add only the
provider and storage extras it needs:

```bash
python -m pip install \
  "cognifity-verdict[anthropic,dashboard]==0.1.0a6" \
  "cognifity-verdict-eval[semantic]==0.1.0a6" \
  "cognifity-verdict-inspect==0.1.0a6"
```

Replace `anthropic` with `openai` or `google` when appropriate. Add `postgres`
for PostgreSQL. Do not install the unrelated distribution named `verdict`.
Normal capture, pipeline, probe, Inspect, and dashboard operation does not require
a Verdict source checkout.

## Upgrade from 0.1.0a5

Back up the selected store and dependency lockfile, stop Verdict writers, and run
the same synchronized command with `--upgrade`. The command also replaces editable
`0.1.0a5` installs with published wheels; it does not require deleting or recloning
the old checkout.

The package upgrade reuses existing SQLite files and PostgreSQL tables. It does not
run a storage migration or delete traces, judgments, calibration records, drift
runs, or dashboard history. Historical traces without a workload tag remain visible
as unclassified. Verify package versions, `python -m pip check`, installed commands,
record counts, and the dashboard against a non-production copy before restarting.

## Storage and dashboard behavior

- Preserve the application's current backend by default.
- Use an absolute SQLite path for a local single-host trial.
- Use the same protected PostgreSQL DSN for shared or multi-instance capture and
  the dashboard; install the `postgres` extra.
- `verdict-dashboard` reads SQLite or PostgreSQL directly and never creates or
  migrates the selected store.
- Moving SQLite data to PostgreSQL is a separate migration, not a package upgrade.
- The optional Operations tab appears only when an authenticated host passes a
  same-origin `operations_url`; standalone dashboards remain read-only.

`VerdictClient.runtime_metrics` exposes aggregate process-local counters and latency
summaries without prompts, responses, storage URLs, or exception text. Workload
labels distinguish `agent`, `judge`, and unclassified costs without rewriting
historical records.
