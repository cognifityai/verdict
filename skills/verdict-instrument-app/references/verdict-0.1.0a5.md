# Verdict 0.1.0a5 target

This historical release preserves the capture, privacy, storage-schema, evaluator,
and statistical contracts in the public
[`0.1.0a4` POC release profile](https://github.com/cognifityai/verdict/blob/v0.1.0a4/docs/POC_RELEASE_PROFILE.md).
It does not expand the supported provider-method matrix or turn the public alpha
into a production-readiness claim.

The operational delta is intentionally small:

- `verdict-dashboard` and `verdict.dashboard.create_app()` ship in
  `cognifity-verdict[dashboard]`;
- the dashboard reads SQLite or PostgreSQL in a read-only transaction and never
  creates or migrates the selected store;
- `verdict-pipeline` and `verdict-probes` ship in `cognifity-verdict-eval`;
- the three historical source entry points remain wrappers when the workspace
  packages are installed;
- live dashboard mode starts empty and never substitutes synthetic values after a
  pending or failed store request; and
- `VERDICT_STORAGE` can supply the pipeline/dashboard storage URL while logs expose
  only the backend name.

Install one synchronized set:

```bash
pip install "cognifity-verdict[dashboard]==0.1.0a5" \
  "cognifity-verdict-eval[semantic]==0.1.0a5" \
  "cognifity-verdict-inspect==0.1.0a5"
```

Add the provider and `postgres` extras actually used by the application. Do not
install the unrelated PyPI distribution named `verdict` in the same environment.
Existing SQLite files and PostgreSQL tables are reused in place; an upgrade does not
delete traces, judgments, calibration records, drift runs, or dashboard history.
