# Existing telemetry import samples

These synthetic, non-customer files mirror the documented export shape of each
supported source. Import any file into the same Verdict database:

```bash
verdict-import file examples/telemetry/otlp-genai.json \
  --format otlp --storage sqlite:///./verdict.db --tenant-id demo
```

The JSONL examples use one source record per line. OTLP uses its normal export
envelope. MLflow and Phoenix keep their trace/span nesting. Voice transcripts
contain text only; Verdict never imports audio bytes or audio URLs.

Generate balanced baseline/current JSONL for every adapter and run the existing
pipeline against the resulting database:

```bash
python scripts/generate_telemetry_samples.py --output /tmp/verdict-telemetry \
  --as-of 2026-08-26T12:00:00Z --per-source-window 5

for source in otlp langfuse langsmith datadog phoenix opik mlflow voice; do
  verdict-import file "/tmp/verdict-telemetry/$source.jsonl" --format "$source" \
    --source-scope "demo-$source" --tenant-id demo \
    --storage sqlite:///./verdict.db
done
```

API examples use the same storage flags plus `--from` and `--to`. Run
`verdict-import <source> --help` for source-specific project/base-URL flags and
credentials. Import stores every eligible LLM call; downstream judgment
sampling remains the responsibility of `verdict-pipeline`.

The default identity scope for a file is its absolute path. The examples pass
`--source-scope` deliberately so regenerating or moving the files still UPSERTs
the same IDs. Scope values must be stable and non-secret.

JSON files are limited to 64 MiB; use JSONL/NDJSON for larger exports. Each
JSONL/NDJSON row is limited to 16 MiB. Mapped content is bounded to 1,000
messages and 100,000 UTF-8 characters per input/output direction.

The samples are contract fixtures, not evidence of a live hosted-API check.
