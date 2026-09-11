# Changelog

All notable changes to Verdict are documented here. This project follows
[Semantic Versioning](https://semver.org/); alpha releases can still change as
the product is refined.

## [Unreleased]

### Added

- `verdict.model_call_context()` yields a generated pre-call identifier and
  assigns it to at most one supported instrumented provider Trace. This lets a
  separately owned gateway integration propagate the same identity without
  adding gateway headers, dependencies, or storage fields to Verdict.
- `verdict.read_port` provides trusted same-process dependent packages with one
  versioned, tenant-scoped Agent Run lookup. Its bounded immutable DTOs expose
  exact model-call event/Trace identity and deterministic finding facts without
  exposing Verdict tables, prompts, responses, model names, or write methods.
- A framework-neutral Agent Run SDK provides sync and async run, turn, and tool
  contexts; typed instruction, context, command, test, artifact, retry,
  handoff, feedback, and outcome events; and automatic links from supported
  provider calls to genuine LLM Traces.
- A bounded local file transport writes redacted, process-owned versioned JSONL
  segments for provider Traces, Agent evidence, manual spans, and user signals.
  `verdict-import agent-file` replays them idempotently through their canonical
  storage boundaries. Completed records bypass Python userspace buffering;
  rejected records are counted and warnings are deduplicated without affecting
  application execution.
- An independently deployable authenticated collector accepts bounded full
  Agent-record batches into PostgreSQL with tenant authority, partial rejection,
  cross-process idempotency, and durable byte-identical acknowledgements.
- `verdict-shipper` uploads complete prefixes of process-owned capture segments
  with bounded retry, validates every acknowledged record, checkpoints accepted
  offsets, deletes only sealed fully accepted segments, and preserves rejected
  segments for recovery.

### Changed

- Retained Anthropic and Google bound methods and lazy stream managers now
  remain pass-through after Verdict shutdown; they cannot resume Trace capture
  or consume a pending one-call correlation reservation.
- Shared Fisher exact, Benjamini-Hochberg, Wilson-interval, and Gwet agreement
  calculations now use one dependency-free implementation. Agreement reports
  correctly identify the existing nominal statistic as Gwet's AC1; versioned
  alignment readers still accept earlier AC2-labeled reports.
- Evaluator Lab now displays long-running judge activity and elapsed time,
  prevents repeated submission while a request is active, and explains that
  coverage belongs to the exact evaluator configuration. New Monitor previews
  default to judge-free deterministic trace checks. The OpenAI-compatible
  label now selects the canonical configured OpenAI provider.
- Monitor is now the only current drift workflow. Historical and prospective
  comparisons share one result contract, all traffic is the default, and
  provider/model or reviewed clusters are optional facets. The evaluation CLI
  continues to prepare judgments but no longer writes fixed-window drift runs;
  existing rows remain available as read-only legacy history.
- Activating a Monitor policy records an immutable event-time boundary and
  starts an empty prospective cohort, preventing older imported history from
  being presented as new post-activation traffic. Existing active monitors
  without that boundary require a new reviewed preview.
- Local Claude Code and Codex capture now retains source-identified child
  histories as separate linked runs, keeps final-response previews up to 64 KiB
  with explicit truncation state, and stores source-reported turn usage. Codex
  usage uses within-turn cumulative-counter deltas; Claude usage deduplicates
  split rows by provider response and retains cache-read/cache-creation counts.
  Content is redacted before the preview cutoff. Missing or incomplete usage
  remains partial or unavailable rather than becoming a complete total or zero;
  Claude totals appear only after terminal, complete response usage.
- Agent insights now presents local histories as source evidence coverage and
  activity, not as a head-to-head agent comparison. Provider-call tokens,
  latency, cost, judge results, and model comparisons use genuine `Trace`
  records only; Codex history still does not create synthetic model calls.
- Claude Code history capture now coalesces split records for the same provider
  response, so later text completes the existing model-call Trace without
  changing its identity. Genuine tool-only calls remain explicitly textless.
- Agent evidence now uses normalized source, run, turn, and event storage.
  Model-call events link to genuine Trace records without duplicating LLM
  content, run detail is paginated, and supported legacy bundle rows migrate
  transactionally on first SQLite or PostgreSQL open. Migrated stores reject
  writes from older agent-bundle writers instead of silently hiding them. Trace
  retention preserves the surrounding execution event while clearing its
  removed Trace link.
- Agent runs can retain optional session, parent-run, service, environment,
  instance, producer, and producer-sequence correlation when a source exposes
  it. Source-local turn and event identifiers remain isolated by run.
- Deterministic Agent Run analysis reports typed test failures and retries when
  the instrumented application supplies that evidence.
- A failed Agent evidence stream no longer discards later provider Traces from
  the sampled run; those calls remain available as standalone Traces. Missing
  turn output is now distinct from deliberately disabled content capture.
- Agent Run exploration now pages through the complete newest-first run list;
  finding links continue to load their exact bounded set of affected runs.
- Live file segments use an `.open` suffix and become sealed JSONL segments on
  normal rotation or shutdown. Local import continues to read complete records
  from open, sealed, and rejected segments.

### Fixed

- Evaluator Lab now shows the exact effective rubric before approval, including
  context-required dimensions skipped because traces have no retrieved-context
  field. A rubric with no evaluable dimension fails before provider egress
  instead of silently widening to unsupported dimensions.
- Provider calls started by inherited background work after an Agent Turn closes
  remain standalone, and the data-source summary identifies observed SDK and
  local-agent evidence without treating all Agent Runs as local history.
- Collector routes support the AnyIO 3 and early AnyIO 4 versions permitted by
  Verdict's existing dependency range.

## [0.1.0a17] - 2026-09-07

### Fixed

- The dashboard again exposes persisted fixed-window evaluation drift under
  **Monitor → Signals**, including effect sizes, adjusted p-values, sample
  counts, recommended actions, and example-trace links. These signals are
  labeled separately from prospective cohort-monitor alerts. Signal totals
  remain accurate when result cards are bounded, and unavailable evaluator or
  inconsistent-run states no longer appear as zero drift.

## [0.1.0a16] - 2026-09-06

### Fixed

- Standalone dashboards now include historical traces stored without a tenant
  in local trace, cluster, analysis, and monitor queries while continuing to
  exclude explicitly different tenants.
- Semantic clustering resolves the pinned MiniLM snapshot from
  `HF_HUB_CACHE`, `HF_HOME`, or the default Hugging Face cache and reports a
  bounded error when first-use download fails.
- Tool-call-only OTLP assistant messages no longer turn the `"(no content)"`
  placeholder into response evidence; text-bearing assistant messages are
  unchanged.
- OpenAI chat instrumentation no longer consumes arbitrary message iterables
  before the provider SDK sends them.
- A broken optional provider SDK availability check no longer prevents
  `verdict.init()` from initializing the remaining instrumentation.
- The dashboard no longer displays an uncomputed change-point marker.
- Bundled model rates and their review date were reverified against current
  provider pricing pages.
- Live PostgreSQL tests require an explicitly disposable database and isolate
  registry fixtures from other test data.
- Monitor facets now compare metrics within each provider/model or frozen
  reviewed cluster, apply one correction across the complete tested family,
  and report unassigned or new-group coverage instead of pooling it.
- Dashboard, command-line, manual, and scheduled monitor runs now resolve the
  same stored evaluator results, dimensions, trace scope, and frozen cluster
  version.
- Evaluator-backed monitors now keep a prospective cohort open until its fixed
  members receive stored evaluator results, then compare those same members.
- Cluster-grouped monitors finish bounded projection through the pinned
  registry before saving cohort membership instead of treating queued traces
  as unassigned.
- Monitor writes reject stale policy or snapshot authority across concurrent
  runners, and PostgreSQL resolves snapshot timestamp ties by write order.

## [0.1.0a15] - 2026-09-02

### Changed

- Monitor can bind one existing complete evaluator identity and compare stored
  per-dimension PASS/FAIL rates alongside deterministic trace metrics without
  making judge calls. Its fingerprint and dimensions are frozen in the policy;
  UNCLEAR, missing, and judge-error coverage remains visible and outside the
  statistical denominator.
- The dashboard cluster workflow anchors its default 90-day range to the latest
  eligible trace. The primary workflow is Analyze, Review, and Use; validation,
  replay, model overrides, and rollback remain under Advanced.
- A semantic analysis uses the pinned local MiniLM snapshot when available and
  downloads that exact revision on first use when the semantic extra is
  installed. Later imports are assigned incrementally without changing the
  reviewed fit membership during activation.

### Fixed

- The Clusters workspace now exposes the first-fit controls before any registry
  version exists. Fit-time exclusions are persisted and reported as ineligible
  with their reason instead of disappearing from the candidate totals.
- Monitor activation now distinguishes the approved historical preview from
  the active prospective bucket that starts collecting at zero.
- PostgreSQL control routes now recognize both URL and libpq connection-string
  forms, and prior alert lookup no longer misparses SQL wildcard characters as
  driver placeholders.
- Daily Operations hides Claude Code and Codex paths for telemetry-only stores.
- Evaluator Lab identifies a configured OpenAI-compatible endpoint without
  returning its URL and keeps unknown-model cost estimates unavailable.

## [0.1.0a14] - 2026-09-01

### Added

- `verdict` now launches a loopback setup UI after the base package install;
  the base distribution includes its dashboard runtime without depending on
  the separately versioned evaluation package.
- Typed, bounded `AgentRun`, `AgentTurn`, and `AgentEvent` evidence with atomic
  SQLite/PostgreSQL/in-memory storage, read APIs, and an Agent runs timeline.
- Read-only, idempotent Claude Code/Codex history capture with deterministic
  identities, typed observable events, recursive redaction, bounded content-on
  local setup, explicit omissions/truncation, and no turn-as-Trace projection.
- Key-free deterministic agent findings for execution status, tool/command
  errors, possible loops, evidence coverage, and configurable required,
  prohibited, and output-schema checks.
- A reviewed Monitor lifecycle with immutable policy fingerprints and exact
  cohort manifests, count or explicit event-time windows, per-metric eligible
  denominators, Fisher's exact tests, Benjamini-Hochberg correction, effect
  thresholds, prospective non-overlapping cohorts, late-arrival accounting,
  `insufficient`, and `reference_stale` states.
- `verdict-monitor run`, an idempotent one-shot command intended for cron or an
  existing scheduler. Verdict does not install a scheduler daemon.
- Immutable deterministic-analysis snapshots and append-only notification
  delivery attempts on SQLite, PostgreSQL, memory, and buffered storage.
- Evaluator-aware Trace status filters, dataset analysis/judge coverage, and
  finding links that preserve affected run and evidence-event identity.
- Judge-free LLM Trace outcomes, prompt/response coverage, operation and
  finish-reason summaries, including explicit unavailability when no Agent Run
  hierarchy was captured.

### Changed

- Completed local capture now routes to Agent runs, changes Setup into a
  restart-stable Data sources view derived from stored evidence, and reports
  Agent Run and LLM Trace totals separately. Filesystem paths remain
  approval-time inputs rather than persisted display configuration.
- Semantic clustering is no longer required for the new Monitor path. Existing
  registry and legacy judge/drift workflows remain available and retain their
  original semantics.
- Setup and Monitor actions are explicit dashboard write operations; ordinary
  overview/explorer reads continue not to rewrite trace or judgment history.
- The dashboard now presents one Drift workspace with Overview, Explore,
  Monitor, Signals, and Clusters subsections. Capture, deterministic findings,
  judge results, source outcomes, and completed drift comparisons remain
  separate states throughout the API and UI.
- Manual local capture approvals remain process-local. An explicitly saved
  daily schedule intentionally retains approved source paths as local control
  configuration for `verdict-service`; no OS-level scheduler is installed.
- Dashboard setup, monitor, control, evaluator, query, and analysis lifecycles
  are separated into capability modules; the application factory now wires
  those contracts instead of implementing each workflow inline.
- Local setup imports now enter the dashboard's local analysis workspace, OTLP
  typed text parts are retained as bounded response evidence, and Evaluator Lab
  distinguishes all-trace selection from an explicit numeric call limit.

### Fixed

- Dashboard section help now opens as a visible mouse, keyboard, and click
  tooltip instead of relying on a browser title attribute.
- Selecting a Trace Explorer row keeps the bounded trace page visible while
  opening its detail. Trace detail now reports the same judge-free evidence and
  structural facts used by dataset-wide Reliability, Performance, and Behavior
  analysis.

### Security

- Setup writes require a process-local same-origin token, local source paths
  must be explicitly approved, symlinked sources are skipped, and preview and
  persisted evidence are bounded. No provider key is accepted or persisted by
  the setup UI.
- Local home-directory prefixes are normalized inside every nested captured
  tool, command, result, stdout, and stderr string before evidence persistence.

## [0.1.0a13] - 2026-08-27

### Added

- Trace Explorer can page through every stored non-judge application trace in
  30-row pages without changing dashboard aggregates or trace persistence.
- `verdict-import` now normalizes existing OTLP/HTTP JSON or protobuf,
  Langfuse v2, LangSmith, Datadog LLM Observability, Phoenix, Opik, MLflow
  2.x/3.x, and bounded text-only voice exports into the existing Verdict
  `Trace` storage contract. File imports accept JSON/JSONL/NDJSON, hosted API
  reads require bounded time windows, and the OTLP listener is loopback-only.
- Deterministic adapter/tenant/source-scoped IDs make retries idempotent without
  storing raw vendor envelopes. Imported content uses an allowlist and the
  existing storage redaction boundary; missing optional metrics remain absent.
- Synthetic source-shaped fixtures and an 80-trace end-to-end generator exercise
  import, SQLite persistence, clustering, judging, drift, and dashboard reads.

## [0.1.0a12] - 2026-08-24

### Fixed

- Trace Explorer now reserves its bounded newest-first sample for application
  traces, so newer judge telemetry cannot hide captured prompts and responses.
  Judge-call telemetry remains included in aggregate cost and store totals.

## [0.1.0a11] - 2026-08-24

### Fixed

- `verdict-pipeline --capture-judge-telemetry` now shuts down through the core
  client API, so successful jobs return their real exit status instead of
  failing after results have been persisted.

## [0.1.0a10] - 2026-08-24

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
- Bounded, redacted content capture is on by default; deployments that cannot
  retain content must explicitly select metadata-only capture. Redaction remains
  best-effort rather than a compliance control.

[Unreleased]: https://github.com/cognifityai/verdict/compare/v0.1.0a17...HEAD
[0.1.0a17]: https://github.com/cognifityai/verdict/compare/v0.1.0a16...v0.1.0a17
[0.1.0a16]: https://github.com/cognifityai/verdict/compare/v0.1.0a15...v0.1.0a16
[0.1.0a15]: https://github.com/cognifityai/verdict/compare/v0.1.0a14...v0.1.0a15
[0.1.0a14]: https://github.com/cognifityai/verdict/compare/v0.1.0a13...v0.1.0a14
[0.1.0a13]: https://github.com/cognifityai/verdict/compare/v0.1.0a12...v0.1.0a13
[0.1.0a12]: https://github.com/cognifityai/verdict/compare/v0.1.0a11...v0.1.0a12
[0.1.0a11]: https://github.com/cognifityai/verdict/compare/v0.1.0a10...v0.1.0a11
[0.1.0a10]: https://github.com/cognifityai/verdict/compare/v0.1.0a9...v0.1.0a10
[0.1.0a9]: https://github.com/cognifityai/verdict/compare/v0.1.0a8...v0.1.0a9
[0.1.0a8]: https://github.com/cognifityai/verdict/compare/v0.1.0a7...v0.1.0a8
[0.1.0a7]: https://github.com/cognifityai/verdict/compare/v0.1.0a6...v0.1.0a7
[0.1.0a6]: https://github.com/cognifityai/verdict/compare/v0.1.0a5...v0.1.0a6
[0.1.0a5]: https://github.com/cognifityai/verdict/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/cognifityai/verdict/compare/v0.1.0a3...v0.1.0a4
