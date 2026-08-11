# Verdict Contributor Guide

This file gives humans and coding agents enough context to work in the repo
without re-learning the architecture each session.

## What Verdict Is

Verdict is an open-source LLM observability and drift-detection toolkit. It
captures supported provider calls, stores traces, evaluates responses with a
rubric, and helps inspect quality, cost, latency, and drift across similar
workloads.

Current scope: v0 operates at the individual LLM-call layer. Agent-run graphs,
tool-call sequence analysis, plan adherence, and task-success metrics are planned
work and should not be described as current functionality.

## Vocabulary

- **Trace**: one captured LLM request/response pair plus metadata.
- **Span**: a manually recorded non-LLM unit of work.
- **Judge**: a model that scores responses using a rubric.
- **Rubric**: PASS/FAIL dimensions such as groundedness, relevance,
  completeness, safety, and instruction following.
- **Cluster**: a group of similar prompts used for like-with-like comparison.
- **Reference baseline**: the historical distribution used for drift comparison.
- **Drift signal**: a statistically meaningful change for a cluster/dimension.
- **Probe**: a configured external evaluation run.
- **Shadow traffic**: sampled traffic mirrored to another model for comparison.

## Architecture Principles

1. Keep core logic separate from provider SDKs, storage engines, and UI code.
2. Put external systems behind interfaces and keep in-memory adapters for tests.
3. Capture through supported SDK instrumentation; do not require users to rewrite
   their application around Verdict-specific clients.
4. Keep content capture opt-in and redact before persistence when it is enabled.
5. Keep provider names and model IDs data-driven where possible.
6. Make validation workflows reproducible with scripts and tests.
7. Prefer maintained OSS libraries when they reduce provider, parser, or
   statistical edge-case maintenance.

## Current Boundaries

- The SDK stores Verdict's own `Trace`, `SpanRecord`, `Judgment`, and
  `DriftSignal` schemas. It does not currently emit OpenTelemetry or
  OpenInference spans.
- SQLite is the default local store. Postgres is available for shared
  environments.
- Redaction is best-effort pattern matching plus Luhn validation for payment-card
  candidates. It is not a compliance guarantee.
- `encrypt` redaction mode is rejected at init time; reversible encryption is not
  implemented.
- The local dashboard is intended for localhost or trusted-network use.

## Working Conventions

- Maintain ADRs in `docs/adrs/` for major architecture decisions.
- Do not import provider SDKs in core evaluation or storage modules.
- Keep tests next to the package they cover.
- Use the in-memory storage/provider adapters in unit tests where practical.
- Run the test suite before claiming behavior is fixed.
- Keep docs explicit about what ships today versus what is planned.

## Documentation Definition Of Done

Documentation updates are required in the same change whenever code modifies
public behavior, defaults, configuration, schemas, installation, CLI or UI
workflows, architecture, security/privacy boundaries, limitations, or validation
claims. Do not defer them to a later cleanup pass.

Before declaring a coding task complete:

1. Identify the affected documentation before or while implementing the change.
2. Search the entire repository for stale descriptions of the old behavior,
   including package READMEs, onboarding and architecture docs, ADRs, examples,
   command help/docstrings, screenshots, and diagrams.
3. Update every affected document and validate links, commands, generated assets,
   and diagrams where applicable.
4. In the final report, list the documentation files changed. If none changed,
   state explicitly why the code change has no documentation impact.

The task is not complete while any known code/documentation mismatch remains.

## Useful Commands

```bash
python scripts/smoke_test.py
python -m pytest
python scripts/live_capture_check.py
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
python -m pytest -q ui/tests
```
