# Verdict 0.1.0a13 target

This Python public alpha uses the bounded
[`0.1.0a13` POC release profile](https://github.com/cognifityai/verdict/blob/v0.1.0a13/docs/POC_RELEASE_PROFILE.md).
It is not a production-readiness claim.

## Install one synchronized set

Use the customer application's existing Python 3.10+ environment. Add only the
provider and storage extras it needs:

```bash
python -m pip install \
  "cognifity-verdict[anthropic,dashboard]==0.1.0a13" \
  "cognifity-verdict-eval[semantic]==0.1.0a13" \
  "cognifity-verdict-inspect==0.1.0a13"
```

Replace `anthropic` with `openai` or `google` when appropriate. Add `postgres`
for PostgreSQL. Do not install the unrelated distribution named `verdict`.
Normal capture, pipeline, probe, Inspect, dashboard, and registry operation does
not require a Verdict source checkout.

## Released capture surface

- Anthropic `messages.create(...)`, including `stream=True`, and the sync/async
  `messages.stream(...)` helper.
- OpenAI `chat.completions.create(...)`, its stream helper, and synchronous or
  asynchronous Responses `create`, `parse`, and new/existing-response stream
  helpers.
- Google GenAI `models.generate_content(...)` and
  `models.generate_content_stream(...)`.

OpenAI's `responses.with_streaming_response` raw-response manager, the
experimental `client.beta.responses` multi-agent resource, and provider entry
points not named above remain unsupported. Consume or explicitly close every
supported stream; garbage collection is not a persistence boundary.

## Upgrade from an earlier synchronized alpha

Back up the selected store and dependency lockfile, stop Verdict writers, and
run the same synchronized command with `--upgrade`. The command also replaces
editable older installs with published wheels; it does not require deleting or
recloning the old checkout.

The package upgrade reuses existing SQLite files and PostgreSQL tables. Task 5
adds registry tables and bounded analysis projections without rewriting existing
traces or evaluation history. Run the tenant-scoped `verdict-cluster normalize`
workflow before fitting a registry against upgraded rows. Verify package
versions, `python -m pip check`, installed commands, record counts, and the
dashboard against a non-production copy before restarting.

## Registry strategy boundary

Versioned-registry `explicit` clustering is supported. Use
`verdict.intent_context(...)`, then the bounded `normalize`, `fit --strategy
explicit`, `assign`, `validate`, and `activate` lifecycle. Automatic `semantic`
and `hybrid` strategies remain experimental and opt-in because the frozen
evaluation missed its preregistered fragmentation gate (`30.1047%` versus the
`30%` maximum). Do not silently enable them or claim generally validated
semantic quality. Registry shadow analysis remains disabled; active analysis is
pinned to the authorized tenant's active version.

## Storage and dashboard behavior

- Preserve the application's current backend by default.
- Use an absolute SQLite path for a local single-host trial.
- Use the same protected PostgreSQL DSN for shared or multi-instance capture and
  the dashboard; install the `postgres` extra.
- Moving SQLite data to PostgreSQL is a separate migration, not a package upgrade.
- The Registry tab is a bounded read-only view. When a mounted host injects its
  authorized registry tenant, that active registry also supplies Overview,
  Trace Explorer, pass-rate, and drift labels. Mounted hosts own tenant
  authorization and mutations through the existing same-origin Operations adapter.

`VerdictClient.runtime_metrics` exposes aggregate process-local counters and
latency summaries without prompts, responses, storage URLs, or exception text.
Workload labels distinguish `agent`, `judge`, and unclassified costs without
rewriting historical records.
