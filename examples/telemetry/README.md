# Existing telemetry import samples

These synthetic, non-customer files mirror the documented export shape of each
supported source. Import any file into the same Verdict database:

```bash
verdict-import file examples/telemetry/otlp-genai.json \
  --format otlp --storage sqlite:///./verdict.db --tenant-id demo
```

The JSONL examples use one source record per line. OTLP uses its normal export
envelope. MLflow and Phoenix keep their trace/span nesting. Voice transcripts
contain text only; Verdict never imports audio bytes or audio URLs. Voice import
also keeps one current, bounded conversation snapshot in storage for later
review. Evaluator Lab can grade clean, closed snapshots with an uploaded JSON
rubric in synchronized `0.1.0b1` source builds. Monitor can compare stored
whole-conversation binary grades in two historical windows. An optional
top-level `labels` object, for example `{"persona":"example_avatar","group":"example_workflow"}`,
lets that comparison show source-group mix and within-group rates; keys must
match `[a-z][a-z_]{0,31}`, with at most eight nonsensitive keys and 128 UTF-8 bytes per
redacted string value. Changing a label invalidates the old grade. The view
does not create a prospective alert or semantic cluster. The published
`0.1.0a22` packages do not store these snapshots.
For a dated conversation, set top-level `ended_at` (or `end_time`) on each Voice
record; turn-level timestamps do not set the conversation end time. Verdict
stores the normalized UTC value as `event_at`. If neither source field is
present, the snapshot is untimed and retention starts at first import.

In **Evaluate → Evaluator Lab → Conversation or reply**, upload
[`conversation-rubric.example.json`](conversation-rubric.example.json) for a
simple whole-conversation dimension rubric, or
[`element-rubric.example.json`](element-rubric.example.json) for declared
element findings and deterministic scoring. The upload detects `kind` in the
file; `element_scoring_v1` always grades a whole conversation. Its judge must
return one finding per declared element in each enabled phase, or the separate alternate
route scores. Verdict computes the final scores and gate and stores them with
the exact transcript revision. Upload validation and preview do not call the
judge. This example is fictional and does not validate any domain-specific
rubric. The published `0.1.0b1` packages do not include element scoring.
If the catalog contains named phases, each Voice record must supply a
top-level `enabled_phases` array of phase keys from that catalog, for example
`"enabled_phases":["intake"]`. Missing or empty phases restrict the record
to the alternate route. Preview counts these alternate-only targets; a
standard judge output for one is rejected. Unknown phase keys are excluded.
Changing the list changes the snapshot revision and invalidates old grades.
The judge must repeat the supplied phases exactly. For a conditional element
that does not apply, the rubric must declare its category/phase/element in
`scoring.optional_elements`. The judge can then return `applicable:false`, an
explanation, and null adequacy/evidence. Verdict excludes it from scoring but keeps the
decision visible. If any standard category has no applicable elements, the
result is rejected rather than publishing a potentially misleading score.
Deduplication also rejects conflicting severity for identical evidence when
the lower-priority finding is more severe, or when it would erase an entire
assessed category. Such results appear as judge errors for review and retry.

For an exploratory paired comparison in **Explore → Compare**, put an opaque
`pair_id` and `variant` in each source conversation's top-level `labels`; use
the same pair ID only for two variants of the same evaluation input. Both
conversations need current whole-conversation grades from one evaluator and
end times inside the chosen UTC window. Verdict shows paired binary outcomes
or numeric score differences with coverage, but does not verify the source's
pairing claim or generate an alert.

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
