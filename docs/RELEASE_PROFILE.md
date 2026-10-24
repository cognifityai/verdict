# Run a pilot with Verdict 0.1.0b3

Use this profile to demonstrate Verdict on captured application agents and
verified provider calls without presenting the beta as production-ready. The
historical `0.1.0a4` profile remains available from its release tag.

## 1. Install the current synchronized beta

Use Python 3.10, 3.11, or 3.12 in a clean environment. Install only the
provider, dashboard, semantic, and storage extras the pilot needs:

```bash
python -m pip install \
  "cognifity-verdict[anthropic,openai,google,dashboard]" \
  "cognifity-verdict-eval[semantic]" \
  "cognifity-verdict-inspect"
python -m pip check
python -c "import verdict, verdict_eval, verdict_inspect; print(verdict.__version__, verdict_eval.__version__, verdict_inspect.__version__)"
```

For this release, the final command prints `0.1.0b3 0.1.0b3 0.1.0b3`. Add the `postgres`
extra only when the existing deployment uses PostgreSQL. Back up an existing
store and dependency lockfile before upgrading; the additive migrations
preserve existing trace and evaluation tables.

For a container deployment, use the versioned image
`ghcr.io/cognifityai/verdict:0.1.0b3` after the release workflow succeeds.
It provides the dashboard and the matching eval package. Run PostgreSQL
separately, keep its port private, and supply the storage URL and dashboard
credentials through the deployment's secret mechanism. Builds from the current
Dockerfile initialize only an empty SQLite schema; captured databases are never
copied into the image. The image's default SQLite path is ephemeral and is for
local demonstration, not a customer data store.

## 2. Use a released provider entry point

| Provider | Supported in this pilot | Outside this pilot |
|---|---|---|
| Anthropic | `messages.create(...)`; `messages.create(stream=True)`; `messages.stream(...)` sync/async helpers | Entry points not listed here |
| OpenAI | `chat.completions.create(...)`; Chat stream helper; Responses `create(...)`, `parse(...)`, and new/existing-response stream helpers | `responses.with_streaming_response` raw-response manager; experimental `client.beta.responses` multi-agent resource |
| Google | `models.generate_content(...)`; `models.generate_content_stream(...)` | Entry points not listed here |

Consume supported streams completely or close them explicitly. Dropping an
unconsumed, unclosed stream does not guarantee trace persistence. Before a
live demonstration, run the repository's live capture check with only the
providers actually configured and retain its named entry-point results.

## 3. Configure the pilot safely

Keep writes synchronous. Content capture is on by default for a useful local
pilot; disable it explicitly when approval is metadata-only:

```python
import verdict

verdict.init(
    service_name="pilot",
    storage="sqlite:///./verdict-poc.db",
    buffered_writes=False,
    capture_content=True,
)
```

`buffered_writes=True` requires an explicit `shutdown()` imported from
`verdict.client` before process exit. Do not enable it for this pilot.

Content capture uses best-effort pattern redaction, not a compliance boundary.
Use only specifically approved data, or set `capture_content=False`. For
content-dependent evaluation, approved non-sensitive fixtures in an isolated
store remain the safest pilot path.

Provider credentials remain in the customer's environment. Never put API keys
in the skill file, repository, SQLite database, screenshots, or support bundle.

### Observe the surrounding agent

Use `verdict.agent_run(...)` to record an application run, its turns, tool
actions, tests, feedback, and explicit outcomes. Supported provider SDK calls
inside a turn add linked model-call traces. Inspect the persisted run and its
linked calls in the dashboard before relying on the report. Verdict does not
infer an application outcome from the final answer. See the
[Agent Profiler guide](AGENT_PROFILER.md) for the runnable, synthetic workflow
and bounded evaluation and comparison rules.

The opt-in `verdict-service` can analyze already captured agent activity and
persist deterministic findings. Prospective statistical monitoring still
operates on genuine provider model-call traces only. Notifications and external
judge calls need separate configuration and approval.

## 4. Use supported intent grouping deliberately

The supported versioned-registry path is exact-key `explicit` grouping. Stamp a
validated key around the provider call, then normalize upgraded stores and run
the bounded lifecycle:

```python
with verdict.intent_context("billing.v1"):
    response = client.messages.create(...)
```

```bash
verdict-cluster --storage sqlite:///./verdict-poc.db --tenant __verdict_local__ --actor poc normalize
verdict-cluster --storage sqlite:///./verdict-poc.db --tenant __verdict_local__ --actor poc fit --strategy explicit --cutoff 2026-08-23T00:00:00Z
verdict-cluster --storage sqlite:///./verdict-poc.db --tenant __verdict_local__ --actor poc assign --version <preview-version> --through-cutoff 2026-08-23T00:00:00Z
verdict-cluster --storage sqlite:///./verdict-poc.db --tenant __verdict_local__ --actor poc validate --version <preview-version>
verdict-cluster --storage sqlite:///./verdict-poc.db --tenant __verdict_local__ --actor poc activate --version <preview-version> --expected-generation 0
```

Use an authorized tenant ID instead of `__verdict_local__` for a tenant-scoped
store, and replace the example timestamp with the intended UTC analysis cutoff.
Automatic `semantic` clustering and `hybrid` semantic fallback remain
experimental and opt-in: the frozen evaluation missed its preregistered
fragmentation gate (`30.1047%` largest nonoutlier cluster versus a `30%`
maximum). Do not silently enable those strategies or claim generally validated
semantic quality.

## 5. Interpret the pilot correctly

- Verify that the stored trace count equals the number of sampled supported
  calls before showing downstream results.
- Treat estimated cost as an estimate, not billing truth.
- Calibrate any LLM judge against customer-labeled examples before presenting
  its PASS/FAIL output as trustworthy for that workload.
- Use independently sampled calls for a drift demonstration. Repeated turns
  from one conversation are correlated and outside this profile's inferential
  claim.
- Treat the loopback dashboard as a bounded evidence and explicit-control view,
  not as a hosted multi-tenant service. Outbound webhooks run only through the
  separately configured scheduler process and an HTTPS URL held in an
  environment variable.
- Registry readiness estimates and fragmentation warnings are diagnostics, not
  activation or drift decisions.

## 6. Pilot acceptance check

The pilot is ready to show only when all of these are true:

1. All three installed packages report `0.1.0b3` and `python -m pip check` passes.
2. The application uses only a provider entry point in the supported column.
3. `buffered_writes` is `False`; content capture is approved for the named pilot
   data or explicitly disabled before collection.
4. Exactly one trace is stored for each sampled supported call.
5. Tokens, latency, model, provider, finish reason, and errors are populated
   where the provider supplies them.
6. If showing agent profiling, the selected run, turns, events, explicit
   outcomes, and any genuine linked calls appear in the selected store.
7. Any registry demonstration uses the supported `explicit` lifecycle and the
   active tenant pointer; experimental strategies retain their disclosure.
8. No credential or customer content appears in the skill, logs, screenshots,
   release material, or repository.

The standalone dashboard binds to loopback by default. Non-loopback binding is
refused unless `VERDICT_USER`, `VERDICT_PASS`, and an explicit
`VERDICT_ALLOWED_HOSTS` allowlist are configured.

If any check fails, stop the demonstration and retain the previous application
configuration. Uninstall with:

```bash
python -m pip uninstall cognifity-verdict cognifity-verdict-eval cognifity-verdict-inspect
```
