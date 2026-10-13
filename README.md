# Verdict

[![Tests](https://github.com/cognifityai/verdict/actions/workflows/test.yml/badge.svg)](https://github.com/cognifityai/verdict/actions/workflows/test.yml) [![PyPI](https://img.shields.io/pypi/v/cognifity-verdict.svg)](https://pypi.org/project/cognifity-verdict/) [![License](https://img.shields.io/github/license/cognifityai/verdict.svg)](LICENSE)

**Find where agent time and tokens went, inspect what happened, and test a change before you trust it.**

Verdict is a local-first agent profiler and reliability toolkit. It imports Claude Code and Codex histories, captures explicitly instrumented application agents, and keeps genuine provider model calls separate from agent turns. The first report is deterministic and needs no judge key. Optional evaluation uses a provider you configure and requires an evidence preview and approval.

## Try Verdict

Python 3.10+ is required. The latest public beta has its own [versioned install and provider support profile](docs/RELEASE_PROFILE.md). To try this development checkout's Agent Profiler workflow, install this checkout in an isolated environment and launch the loopback dashboard:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e 'packages/verdict[dashboard]' -e packages/verdict_eval
.venv/bin/verdict-dashboard --storage sqlite:///./verdict.db --host 127.0.0.1
```

In **Settings → Data Sources**, preview and approve the Claude Code / Codex history directories before importing. The import reads local files and stores normalized, redacted evidence in the selected database. It does not replay agent commands. A rescan is idempotent.

For a fully synthetic, key-free demo, follow [Agent Profiler demo](docs/AGENT_PROFILER.md#synthetic-local-demo). The demo database is disposable and never reads your history or calls a provider.

## The investigation flow

1. **Usage:** See reported token activity by UTC day, source, model, and session. Open a contributor to reach its runs and turns.
2. **Session / run:** The selected session report combines its runs, requests, recorded actions, outcomes, known usage, findings, historical saved evaluations, and suggested actions. Open a run for deeper evidence.
3. **Turn / action:** Click **Analyze** for a local inspection of recorded failures and tool results. A selected turn or genuine model call can be evaluated in Evaluator Lab. A bounded session, run, or tool action can be previewed for optional external evaluation.
4. **Test a change:** Save a reviewed, runnable case set. A captured run can start an unsaved draft from its complete, redacted request; you supply the expected behavior and check. Reviewed case fields are stored and passed to the configured local runner exactly as entered, so remove private data before saving. The runner executes the same cases against baseline and candidate versions and reports fixes, collateral regressions, unchanged cases, and unverified runs.
5. **Monitor:** An opt-in service can rescan approved local histories or analyze already captured agent activity, persist deterministic findings, and run an existing genuine model-call monitor. Its notifications require separate configuration. A reviewed bounded scope can also authorize one later external evaluation, with a one-attempt limit and a user-set static list-price estimate limit.

See [Agent Profiler workflows and limits](docs/AGENT_PROFILER.md) for exact steps and evidence requirements.

## Instrument an application agent

```python
import verdict

verdict.init(storage="sqlite:///./verdict.db", service_name="my-agent")

with verdict.agent_run(name="support-agent", session_id="session-123") as run:
    with run.turn(user_input="Look up the order") as turn:
        with turn.tool("lookup_order", arguments={"order_id": "sample"}) as tool:
            tool.set_output({"found": True})
        turn.set_output("The sample order was found.")
    run.record_business_outcome("resolved", True)
```

The surrounding run, turn, tool, test, feedback, and outcome facts come from your application. Supported Anthropic, OpenAI, and Google SDK calls can additionally create genuine model-call traces. Verdict does not infer task success from a fluent final answer. The [SDK guide](docs/ONBOARDING.md#3a-instrument-the-surrounding-agent-execution) covers event helpers and capture options.

For an existing Python app, the [integration assistant guide](docs/AGENT_SKILL.md) helps identify supported provider calls and choose a safe capture boundary.

## What the numbers mean

- Token counts follow the source's reported semantics. Cache and reasoning components may already be included in input or output totals; Verdict does not add them again. Missing usage is **unknown**, not zero.
- Price is a static API list-price estimate only where one supported model and valid input/output usage are attributable; a missing reported total stays unknown. It is not a Claude/Codex subscription bill. Unknown or mixed models remain unpriced.
- A session's elapsed span includes tools and idle time. It is not model latency. Genuine provider calls have their own latency when captured.
- Suggestions link to recorded evidence and are hypotheses until tested. A failed command can be followed by a successful recovery; an expensive turn is not automatically waste.
- An optional LLM judgment is an evaluator's opinion about the bounded evidence shown before consent. Missing, truncated, and uncertain evidence remain visible; a judge cannot reconstruct hidden reasoning or absent files.

## Advanced capabilities

Verdict also imports supported [existing telemetry](docs/ONBOARDING.md#4-import-telemetry-you-already-collect), provides [rubric judging](docs/ONBOARDING.md#6-judge-calibration-with-verdict_eval-only-if-you-want-quality-drift), and supports [prospective statistical monitoring](docs/STATS_PRIMER.md) of genuine provider model-call traces. Those monitors use reviewed cohorts and fixed evaluator identities; descriptive agent-session comparisons are not drift alerts. Explore, Evaluate, Monitor, Report, and Settings remain available after the first-use profiler path.

Content is recursively redacted on capture by default, but pattern-based redaction is not a compliance guarantee. Set `capture_content=False` for metadata-only SDK capture; content-dependent evaluation then becomes unavailable. Provider keys stay in environment variables. External judge calls and notification webhooks are opt-in.

## Import telemetry you already have

Use `verdict-import --help` for supported local and hosted sources. Imported model-call traces and native agent evidence have different coverage; an imported trace is not silently converted into an agent turn. The [onboarding guide](docs/ONBOARDING.md#4-import-telemetry-you-already-collect) documents source-specific requirements and limits.

## Development and verification

The source checkout includes Python and UI tests, a synthetic demo agent, and a dashboard build. Run the checks using the repository's contributor environment:

```bash
python -m pytest -q
pnpm --dir ui test
pnpm --dir ui build
python scripts/smoke_test.py
```

The synthetic demo proves the local wiring and fixed-case comparison, not live judge accuracy or real savings. For pilot claims, use the exact [release profile](docs/RELEASE_PROFILE.md) and validate the provider calls and evaluator on your own approved data.

Apache 2.0. See [onboarding](docs/ONBOARDING.md), [architecture decisions](docs/adrs/), and [contributing](CONTRIBUTING.md).
