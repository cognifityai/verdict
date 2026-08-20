# Changelog

All notable changes to Verdict are documented here. This project follows
[Semantic Versioning](https://semver.org/); alpha releases can still change as
the customer POC profile is refined.

## [Unreleased]

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
- The historical `ui/server.py`, `scripts/run_drift_pipeline.py`, and
  `scripts/run_probes.py` entry points remain as compatibility wrappers.

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

[Unreleased]: https://github.com/cognifityai/verdict/compare/v0.1.0a5...HEAD
[0.1.0a5]: https://github.com/cognifityai/verdict/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/cognifityai/verdict/compare/v0.1.0a3...v0.1.0a4
