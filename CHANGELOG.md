# Changelog

All notable changes to Verdict are documented here. This project follows
[Semantic Versioning](https://semver.org/); alpha releases can still change as
the customer POC profile is refined.

## [Unreleased]

### Fixed

- Mounted dashboards now project Overview, Trace Explorer, cluster pass-rate
  data, and drift labels from the host-authorized active registry instead of
  showing stale or empty legacy `Trace.cluster_id` values. Standalone and legacy
  stores keep their existing trace-cluster behavior.

## [0.1.0a9] - 2026-08-23

### Fixed

- Trace Explorer now shows the bounded 30 newest traces with deterministic ties,
  recorded UTC timestamps, relative age, provider and content-state filters,
  complete store totals, and distinct metadata-only, empty, partial, failed, and
  judged states.
- Dashboard drift and judge views distinguish no completed run, evaluator
  selection, a completed zero-signal run, and a completed signaling run. The
  displayed default-window counts are labeled global content availability rather
  than statistical readiness; the pipeline still decides judged sufficiency for
  each eligible cluster and rubric dimension.
- Completed drift runs remain selectable by a bounded incomplete evaluator
  fingerprint when normal retention has removed the last defining judgment.
  Missing provider, model, and rubric details are not reconstructed.

## [0.1.0a8] - 2026-08-23

### Added

- A bounded tenant/version cluster registry with supported exact-key `explicit`
  clustering, stable cluster identities and labels, immutable previews,
  assignment/validation/activation/rollback commands, additive SQLite and
  PostgreSQL migrations, and an upgrade normalization workflow. Automatic
  `semantic` and `hybrid` clustering remain explicitly experimental after the
  frozen evaluation missed its preregistered fragmentation gate.
- A bounded Registry dashboard view for active and preview versions, stable
  labels, frozen algorithm/selector/model definitions, representative redacted
  prompts, provider/model mix, membership explanations, terminal reasons,
  coverage, readiness estimates, and fragmentation warnings. Mounted hosts own
  tenant authorization and mutations through the existing Operations adapter.
- The version-matched `verdict-instrument-app` skill now discovers the complete
  released provider surface and guides operators through registry normalization,
  supported explicit clustering, activation, inspection, and rollback.

### Fixed

- Anthropic `messages.stream(...)` is captured for synchronous and asynchronous
  event iteration, `text_stream`, `until_done`, `get_final_message`, and
  `get_final_text`. Split streaming usage updates are merged field by field;
  complete, partial, and error boundaries finalize exactly once. Both Anthropic
  resource layouts in the declared `anthropic>=0.30` range are supported and
  exercised in CI. Lazy helpers bind routing at each manager entry, preserve
  one-shot message iterables, do not buffer content when capture is disabled,
  and preserve captured empty text distinctly from unavailable content.
- The live capture gate now exercises Anthropic's stream helper, requires
  exactly one new trace per entry point, names the providers and entry points it
  verified, and exits nonzero when any requested provider could not run.
- OpenAI Responses calls are captured for synchronous and asynchronous
  `create`, `parse`, and new/existing-response stream helpers.
  Complete, incomplete, failed, cancelled, queued, in-progress, partial-close,
  application-error, provider-error, and cancellation boundaries retain their
  status and persist exactly once; an owned post-request-hook native HTTP
  transport (`httpx` or current `httpx2` SDK layouts)
  marker prevents local validation, nested same-client requests, request-hook
  failures, or cancellation-shaped traversal failures from creating false
  provider traces. Capture reads allowlisted fields from serialized outbound
  JSON, preserving SDK mapping/list semantics, actual `extra_body` precedence,
  aliases, and the wire-time mutable snapshot. With content capture disabled,
  only serialized scalar metadata is retained at that boundary. Nested helper reuse closes both
  traces, stale helpers stay
  inactive after shutdown, captured content is recursively redacted, empty
  captured content remains distinct from unavailable content, and disabled
  capture retains no response text. The Responses resource is
  feature-detected so the declared OpenAI minimum continues to capture Chat
  Completions. The declared minimum is OpenAI 1.56.2, whose ordinary default
  client is exercised separately without constraining the Google extra's HTTPX.
  Partial streams retain done-event-only output text and refusal content; an
  authoritative done value replaces any observed suffix deltas without
  duplicating normal complete delta sequences.
  The `responses.with_streaming_response` raw-response manager and experimental
  `client.beta.responses` multi-agent resource remain outside this bounded
  support surface.
- The live capture gate now names and verifies the OpenAI Responses entry points
  it actually exercises, including the helper error boundary.
- Trace Explorer distinguishes captured empty prompts and responses from traces
  whose content capture was disabled.

### Security

- Repository, container, cloud-upload, and artifact gates now reject plaintext
  `.env*`, `*.env*`, `*.envrc*`, and `.direnv` fallbacks. The checked-in example
  is a variable-name reference only; runtime keys should be injected from a
  managed secret store or OS credential manager. The alignment-sweep wrapper
  no longer sources a repository-root `.env` fallback.
- IPv6 redaction now separates validated addresses from trailing non-address
  text in message and host/port shapes, preventing complete or partial address
  fragments from crossing storage, export, dashboard API, and UI payload
  boundaries.

## [0.1.0a7] - 2026-08-21

### Fixed

- Trace Explorer now includes metadata-only traces and explicitly says when
  prompt and response content was not captured.
- The live provider comparison shows a regression badge only when the selected
  completed drift run attributes a persisted regression to that provider; it no
  longer labels Anthropic sample data as a live Haiku regression.

## [0.1.0a6] - 2026-08-20

### Added

- Optional same-origin Operations dashboard integration for authenticated host
  applications. Standalone dashboards remain unchanged unless the host passes
  `operations_url=` to `verdict.dashboard.create_app()`.
- Bounded process-local capture overhead, adapter-failure, and buffered-writer
  queue telemetry on `VerdictClient.runtime_metrics`.
- Task-local workload provenance via `set_context(workload=...)` and
  `workload_context(...)`, with dashboard cost attribution for `agent`, `judge`,
  and unclassified traces.
- Opt-in `verdict-pipeline --capture-judge-telemetry`; evaluator traces are
  excluded from later target-workload drift analysis.
- A secret-safe agent-skill environment inspector that distinguishes fresh
  installs, synchronized `0.1.0a5` upgrades, current installs, mixed-package
  repairs, editable installs, and the unrelated `verdict` distribution.

### Changed

- `Judge.judge()` temporarily marks its provider call as the `judge` workload
  and restores any prior caller workload on success or failure.
- Buffered storage exposes aggregate queue/write counters without payloads or
  exception text.
- The instrumentation skill preserves an existing SQLite or PostgreSQL backend
  by default, requires approval before package changes, and no longer requires
  the obsolete historical source-checkout verifier.

## [0.1.0a5] - 2026-08-20

### Added

- A portable `verdict-instrument-app` coding-agent skill for discovering
  supported customer call paths, planning a consented POC, verifying capture
  through storage, gating clustering and judge spend, and handing off bounded
  dashboard and scheduling instructions.
- An installable, mountable Verdict dashboard with read-only SQLite and
  PostgreSQL backends.
- Installed `verdict-pipeline` and `verdict-probes` operator commands.

### Changed

- Live dashboards start empty and never substitute synthetic metrics while a
  store request is pending or failed.
- The historical `ui/server.py --db ...` entry point remains a compatibility
  wrapper; `scripts/run_drift_pipeline.py` and `scripts/run_probes.py` remain
  source wrappers after installing the workspace packages.
- Dashboard storage auto-discovery checks the current working directory; set
  `VERDICT_STORAGE` or pass `--storage` when launching elsewhere.

## [0.1.0a4] - 2026-08-18

### Added

- Versioned evaluator identities, atomic drift-run snapshots, judge-health
  gating, probe artifact method versions, and bounded dashboard evidence views.
- Explicit pairwise execution status so invalid output and provider errors are
  not reported as genuine ties.
- A bounded customer POC profile naming supported provider entry points and
  required persistence/privacy settings.

### Changed

- Evaluator-specific judgments and signals remain isolated throughout the
  pipeline and dashboard.
- `UNCLEAR` results stay outside PASS/FAIL denominators while retaining their
  own coverage signal.
- Provider scalar metadata is normalized before storage, and persistence
  failures emit a bounded warning without replacing the application result.
- Eval now requires `cognifity-verdict>=0.1.0a4`; Inspect requires
  `cognifity-verdict-eval>=0.1.0a4` to prevent mixed alpha installations.

### Fixed

- Valid fenced judge JSON no longer becomes `UNCLEAR` when reasoning contains
  Markdown fences, braces, quotes, or escapes.
- Provider SDK unset sentinels no longer make otherwise valid trace writes fail.
- Sentence-final IPv6 addresses are redacted without consuming punctuation,
  and adversarial IPv6 candidates no longer trigger quadratic matching work.
- Invalid pairwise outcomes fail closed instead of silently becoming ties.
- Dashboard response bounds, evaluator focus, and generated assets now remain
  consistent under large or mixed datasets.
- Unknown Opus pricing aliases no longer receive an incorrect catch-all rate.

### Security

- Redaction traversal and candidate matching have explicit work bounds.
- Content capture remains off by default and is documented as best-effort rather
  than a compliance control.

[Unreleased]: https://github.com/cognifityai/verdict/compare/v0.1.0a9...HEAD
[0.1.0a9]: https://github.com/cognifityai/verdict/compare/v0.1.0a8...v0.1.0a9
[0.1.0a8]: https://github.com/cognifityai/verdict/compare/v0.1.0a7...v0.1.0a8
[0.1.0a7]: https://github.com/cognifityai/verdict/compare/v0.1.0a6...v0.1.0a7
[0.1.0a6]: https://github.com/cognifityai/verdict/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/cognifityai/verdict/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/cognifityai/verdict/compare/v0.1.0a3...v0.1.0a4
