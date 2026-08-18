# Run a customer POC with Verdict 0.1.0a4

Use this profile to demonstrate Verdict on supported provider calls without
presenting the public alpha as production-ready.

## 1. Install the exact release

Use Python 3.10, 3.11, or 3.12 in a clean environment. Install only the
provider extras the POC needs:

```bash
python -m pip install "cognifity-verdict[anthropic,openai,google]==0.1.0a4"
python -m pip install "cognifity-verdict-eval[semantic]==0.1.0a4" \
  "cognifity-verdict-inspect==0.1.0a4"
python -c "import verdict, verdict_eval, verdict_inspect; print(verdict.__version__, verdict_eval.__version__, verdict_inspect.__version__)"
```

The final command must print `0.1.0a4 0.1.0a4 0.1.0a4`.

## 2. Use a supported provider entry point

| Provider | Supported in this POC | Do not use in this POC |
|---|---|---|
| Anthropic | `messages.create(...)`; `messages.create(stream=True)` | `messages.stream(...)` |
| OpenAI | `chat.completions.create(...)`; `chat.completions.create(stream=True)`; `chat.completions.stream(...)` | `responses.create(...)`; `responses.stream(...)` |
| Google | `models.generate_content(...)`; `models.generate_content_stream(...)` | Entry points not listed here |

Consume supported streams completely or close them explicitly. Dropping an
unconsumed, unclosed stream does not guarantee trace persistence.

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

## 4. Interpret the POC correctly

- Verify that the stored trace count equals the number of sampled supported
  calls before showing downstream results.
- Treat estimated cost as an estimate, not billing truth.
- Calibrate any LLM judge against customer-labeled examples before presenting
  its PASS/FAIL output as trustworthy for that workload.
- Use independently sampled calls for a drift demonstration. Repeated turns
  from one conversation are correlated and are outside this release profile's
  inferential claim.
- Present the dashboard as a read-only SQLite demonstration. It is not a hosted
  multi-tenant monitoring service.

## 5. POC acceptance check

The POC is ready to show only when all of these are true:

1. All three installed packages report `0.1.0a4`.
2. The application uses only a provider entry point in the supported column.
3. `buffered_writes` and `capture_content` are both `False`.
4. Exactly one trace is stored for each sampled supported call.
5. Tokens, latency, model, provider, and finish reason are populated where the
   provider supplies them.
6. No credential or customer content appears in the skill, logs, screenshots,
   release material, or repository.

If any check fails, stop the demonstration and retain the previous application
configuration. Uninstall with:

```bash
python -m pip uninstall cognifity-verdict cognifity-verdict-eval cognifity-verdict-inspect
```
