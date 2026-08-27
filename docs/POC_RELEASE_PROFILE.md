# Run a customer POC with Verdict 0.1.0a13

Use this profile to demonstrate Verdict on verified provider calls without
presenting the public alpha as production-ready. The historical `0.1.0a4`
profile remains available from its release tag.

## 1. Install the exact synchronized release

Use Python 3.10, 3.11, or 3.12 in a clean environment. Install only the
provider, dashboard, semantic, and storage extras the POC needs:

```bash
python -m pip install \
  "cognifity-verdict[anthropic,openai,google,dashboard]==0.1.0a13" \
  "cognifity-verdict-eval[semantic]==0.1.0a13" \
  "cognifity-verdict-inspect==0.1.0a13"
python -m pip check
python -c "import verdict, verdict_eval, verdict_inspect; print(verdict.__version__, verdict_eval.__version__, verdict_inspect.__version__)"
```

The final command must print `0.1.0a13 0.1.0a13 0.1.0a13`. Add the `postgres`
extra only when the existing deployment uses PostgreSQL. Back up an existing
store and dependency lockfile before upgrading; the additive registry migration
preserves existing trace and evaluation tables.

## 2. Use a released provider entry point

| Provider | Supported in this POC | Outside this POC |
|---|---|---|
| Anthropic | `messages.create(...)`; `messages.create(stream=True)`; `messages.stream(...)` sync/async helpers | Entry points not listed here |
| OpenAI | `chat.completions.create(...)`; Chat stream helper; Responses `create(...)`, `parse(...)`, and new/existing-response stream helpers | `responses.with_streaming_response` raw-response manager; experimental `client.beta.responses` multi-agent resource |
| Google | `models.generate_content(...)`; `models.generate_content_stream(...)` | Entry points not listed here |

Consume supported streams completely or close them explicitly. Dropping an
unconsumed, unclosed stream does not guarantee trace persistence. Before a
customer demonstration, run the repository's live capture check with only the
providers actually configured and retain its named entry-point results.

## 3. Configure the POC safely

Keep writes synchronous and content capture off:

```python
import verdict

verdict.init(
    service_name="customer-poc",
    storage="sqlite:///./verdict-poc.db",
    buffered_writes=False,
    capture_content=False,
)
```

`buffered_writes=True` requires an explicit `shutdown()` imported from
`verdict.client` before process exit. Do not enable it for this POC.

Content capture is opt-in and uses best-effort pattern redaction, not a
compliance boundary. Keep it off for customer data. If the POC must demonstrate
content-dependent evaluation, use synthetic or specifically approved
non-sensitive fixtures in an isolated store.

Provider credentials remain in the customer's environment. Never put API keys
in the skill file, repository, SQLite database, screenshots, or support bundle.

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

## 5. Interpret the POC correctly

- Verify that the stored trace count equals the number of sampled supported
  calls before showing downstream results.
- Treat estimated cost as an estimate, not billing truth.
- Calibrate any LLM judge against customer-labeled examples before presenting
  its PASS/FAIL output as trustworthy for that workload.
- Use independently sampled calls for a drift demonstration. Repeated turns
  from one conversation are correlated and outside this profile's inferential
  claim.
- Treat the dashboard as a bounded read-only evidence view. It is not a hosted
  multi-tenant monitoring or outbound-alerting service.
- Registry readiness estimates and fragmentation warnings are diagnostics, not
  activation or drift decisions.

## 6. POC acceptance check

The POC is ready to show only when all of these are true:

1. All three installed packages report `0.1.0a13` and `python -m pip check` passes.
2. The application uses only a provider entry point in the supported column.
3. `buffered_writes` and `capture_content` are both `False`, unless content was
   separately approved with synthetic privacy tests.
4. Exactly one trace is stored for each sampled supported call.
5. Tokens, latency, model, provider, finish reason, and errors are populated
   where the provider supplies them.
6. Any registry demonstration uses the supported `explicit` lifecycle and the
   active tenant pointer; experimental strategies retain their disclosure.
7. No credential or customer content appears in the skill, logs, screenshots,
   release material, or repository.

If any check fails, stop the demonstration and retain the previous application
configuration. Uninstall with:

```bash
python -m pip uninstall cognifity-verdict cognifity-verdict-eval cognifity-verdict-inspect
```
