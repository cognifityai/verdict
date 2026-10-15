# Verdict

[![Tests](https://github.com/cognifityai/verdict/actions/workflows/test.yml/badge.svg)](https://github.com/cognifityai/verdict/actions/workflows/test.yml) [![PyPI](https://img.shields.io/pypi/v/cognifity-verdict.svg)](https://pypi.org/project/cognifity-verdict/) [![License](https://img.shields.io/github/license/cognifityai/verdict.svg)](LICENSE)

**Monitoring and investigation for AI agents in real applications.**

Verdict connects an agent's runs, turns, tools, tests, and application-reported outcomes to the model calls made along the way. It helps answer what happened, where time and tokens went, which failures need attention, and whether measured behavior changed. The dashboard brings capture, investigation, evaluation, comparison, and monitoring into one workflow.

Verdict is local-first. A Python application can write to SQLite or PostgreSQL, or spool bounded records for a separate local service to import. Claude Code and Codex history imports are additional ways to explore agent activity; they are not the scope of the product.

The published `0.1.0b1` beta supports provider-call capture and monitoring. The agent execution and investigation workflow described here is in this source checkout and is planned for a later release.

## What you can do

| Workflow | Evidence and result |
| --- | --- |
| **Observe an application agent** | Record runs, turns, tool calls and results, tests, feedback, and explicit business outcomes. Supported Anthropic, OpenAI, and Google SDK calls add genuine model-call traces with provider-reported usage, latency, finish reason, and errors where available. |
| **Investigate an issue** | Start from the overview's recorded issues and token chart, open the affected session or run, and inspect the linked event, timeline, and suggested next check. The report summarizes application and model-call activity with coverage shown. |
| **Evaluate and test a change** | Run deterministic checks on recorded evidence. Preview a bounded session, run, turn, tool action, or model call before an optional judge evaluation. Save reviewed cases and compare a configured application baseline and candidate against the same checks. |
| **Monitor ongoing activity** | Schedule deterministic analysis of captured agent activity. Separately, review and activate a prospective statistical monitor for genuine provider model calls, with sample and evaluator gates; configure notifications if needed. |

The [Agent Profiler guide](docs/AGENT_PROFILER.md) explains the investigation workflow and its evidence limits. The [onboarding guide](docs/ONBOARDING.md) covers capture, imports, evaluation, and monitor setup.

## Instrument an application agent

With Python 3.10-3.12, install this checkout in an isolated environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e 'packages/verdict[dashboard]' -e packages/verdict_eval
```

Call `verdict.init()` once at application startup, then record the work your application can actually observe:

```python
import verdict

verdict.init(storage="sqlite:///./verdict.db", service_name="support-agent")

with verdict.agent_run(name="support-agent", session_id="session-123") as run:
    with run.turn(user_input="Look up the order") as turn:
        with turn.tool("lookup_order", arguments={"order_id": "sample"}) as tool:
            tool.set_output({"found": True})
        turn.set_output("The sample order was found.")
    run.record_business_outcome("resolved", True)
```

Run your supported provider SDK calls inside the turn to link their captured model traces to the agent work. Install the corresponding provider extra when the SDK is not already available, and check the [supported entry points](docs/RELEASE_PROFILE.md#2-use-a-released-provider-entry-point). Verdict records the outcome you supply; it does not infer task success from the final answer. See [agent instrumentation](docs/ONBOARDING.md#3a-instrument-the-surrounding-agent-execution) for test, command, retry, and feedback events, and for storage and file transport options.

For an existing Python application, the [integration assistant guide](docs/AGENT_SKILL.md) helps identify supported calls and choose a capture boundary.

Start the local dashboard against the same store:

```bash
.venv/bin/verdict-dashboard --storage sqlite:///./verdict.db --host 127.0.0.1
```

Open **Overview** for activity and issues, **Investigate** for linked run and model-call evidence, **Evaluate** for a selected scope or reviewed case set, **Monitor** for comparisons and schedules, and **Report** for a period summary. A [fully synthetic demo](docs/AGENT_PROFILER.md#synthetic-local-demo) exercises the workflow without a provider key.

## Monitor behavior over time

Verdict has two complementary monitoring paths:

1. **Agent activity:** Opt in to a local schedule to analyze newly captured or imported runs and persist deterministic findings, such as recorded failures or repeated tool problems. A cycle can run once with `verdict-service --storage sqlite:///./verdict.db --once`, after the schedule is configured in **Monitor > Schedule**. Alerts can be delivered to a configured local log or webhook.
2. **Model-call cohorts:** Preview historical calls, then activate a prospective monitor for genuine provider `Trace` records. It can compare provider errors, empty responses, refusal-language signals, and selected judge dimensions when a fixed evaluator has produced usable judgments. The monitor keeps reference and current cohorts separate, enforces sample and evidence gates, and links results back to calls. [How statistical monitoring works](docs/STATS_PRIMER.md).

The statistical monitor currently supports **model-call traces only**. Agent-session comparisons are descriptive; they are not prospective statistical alerts. Judge scores are optional and should be calibrated on held-out, human-labeled examples for the workload before being used as quality evidence. See [monitor setup](docs/ONBOARDING.md#7-see-findings-evaluate-deliberately-and-configure-monitoring) and the [release profile](docs/RELEASE_PROFILE.md) for the exact supported surface.

## Bring in existing evidence

- **Claude Code and Codex:** Preview and approve local history paths in **Settings > Data Sources**. The import is read-only against source files and does not replay commands. These histories can show sessions, turns, tools, and source-reported usage, but coverage differs from directly captured application agents and genuine provider calls.
- **Other telemetry:** `verdict-import --help` lists supported local and hosted sources. Imported model-call traces remain distinct from native agent turns. See [telemetry import](docs/ONBOARDING.md#4-import-telemetry-you-already-collect).

## Evidence and privacy boundaries

- Token counts use the source's reported semantics; missing usage stays unknown. Cache and reasoning components are not added twice. Cost is a static API list-price estimate only when an attributable model and valid usage support it, not a subscription bill. Session elapsed time includes tools and idle time; captured model calls have separate latency.
- Suggestions point to recorded evidence and are prompts to investigate, not proof of waste or guaranteed savings. Fixed-case comparisons test the checks you define; neither a fluent answer nor a judge opinion establishes an application outcome on its own.
- Content is bounded and recursively redacted by default, but pattern-based redaction is not a compliance guarantee. Set `capture_content=False` for metadata-only SDK capture; content-dependent evaluation is then unavailable. Dashboard scoped judge calls require an explicit evidence preview and approval. Provider keys stay in environment variables.
- The dashboard binds to loopback by default. Non-loopback use requires credentials and an allowed-host configuration; see [deployment guidance](docs/RELEASE_PROFILE.md#6-pilot-acceptance-check).

## Published beta and development

For the published beta's pinned installation and supported SDK methods, use the [release profile](docs/RELEASE_PROFILE.md).

Contributors can run the Python and UI checks and the synthetic smoke path from this checkout:

```bash
python -m pytest -q
pnpm --dir ui test
pnpm --dir ui build
python scripts/smoke_test.py
```

See [contributing](CONTRIBUTING.md), [architecture decisions](docs/adrs/), and [evaluation documentation](packages/verdict_eval/README.md). Apache 2.0.
