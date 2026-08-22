# Changelog

All notable changes to Verdict are documented here. This project follows
[Semantic Versioning](https://semver.org/); alpha releases can still change as
the customer POC profile is refined.

## [Unreleased]

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

[Unreleased]: https://github.com/cognifityai/verdict/compare/v0.1.0a7...HEAD
[0.1.0a7]: https://github.com/cognifityai/verdict/compare/v0.1.0a6...v0.1.0a7
[0.1.0a6]: https://github.com/cognifityai/verdict/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/cognifityai/verdict/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/cognifityai/verdict/compare/v0.1.0a3...v0.1.0a4
