# Verdict Python SDK

PyPI distribution: `cognifity-verdict` · Python import: `verdict` · Apache 2.0

Verdict records the work a Python agent performs and connects supported model
calls to that work. The dashboard helps a team investigate recorded failures
and token usage, evaluate a selected scope, compare a change on reviewed
cases, and monitor incoming activity.

The product is for agents in real applications. Claude Code and Codex history
imports are additional local starting points.

## Start locally

The published release is currently a beta pre-release, so use `--pre` to
install the newest beta without hardcoding a version:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade --pre 'cognifity-verdict[dashboard]'
.venv/bin/verdict
```

The first-run screen can preview and import your local Claude Code or Codex
history, connect an existing Verdict store, or import supported telemetry.
Preview and approve local history capture; Verdict does not replay agent
commands. Home then shows reported usage and recorded issues. Investigation
opens the affected sessions and runs. No provider key is needed for capture,
local checks, or the synthetic demo.

## Instrument an application agent

Install the extras for the provider SDKs your application actually uses. The
example below includes all three supported provider families:

```bash
python -m pip install --upgrade --pre \
  'cognifity-verdict[anthropic,openai,google,dashboard]'
```

Initialize once at startup and record application work at the point where it
happens:

```python
import verdict

verdict.init(storage="sqlite:///./verdict.db", service_name="support-agent")

with verdict.agent_run(name="support-agent", session_id="session-123") as run:
    with run.turn(user_input="Look up the sample order") as turn:
        with turn.tool("lookup_order", arguments={"order_id": "sample"}) as tool:
            tool.set_output({"found": True})
        turn.set_output("The sample order was found.")
    run.record_business_outcome("resolved", True)
```

Supported Anthropic, OpenAI, and Google SDK calls inside the turn create
genuine linked model-call traces. Verdict captures provider-reported usage,
latency, finish reason, and errors where the provider supplies them. The
application supplies its own run, tool, test, feedback, and outcome records;
Verdict does not infer task success from a model answer. SDK content capture is
bounded and on by default; use `verdict.init(capture_content=False)` when the
application should retain metadata without prompts or responses.

For exact SDK entry points and coverage, read the
[release profile](https://github.com/cognifityai/verdict/blob/main/docs/RELEASE_PROFILE.md).
For more event helpers and imports, read the
[onboarding guide](https://github.com/cognifityai/verdict/blob/main/docs/ONBOARDING.md).

## Investigate, evaluate, monitor

- **Home and Investigation** show source-reported tokens, captured runs, and
  recorded issues. A finding links to the affected run or event. Missing
  usage or source coverage remains visible.
- **Suggested setup** proposes checks supported by the connected evidence.
  Run a local check once and review it before saving a recurring plan.
- **Evaluation** combines key-free deterministic checks with optional,
  explicitly approved provider-backed judging of bounded selected evidence.
  Reviewed cases can compare a configured baseline and candidate application.
- **Monitoring** can persist deterministic agent findings on a schedule. A
  separate prospective statistical monitor compares eligible genuine
  provider-call traces. A saved schedule or policy is not evidence that its
  worker has run; check the worker's cycle status.

Start the dashboard against the same store:

```bash
.venv/bin/verdict-dashboard --storage sqlite:///./verdict.db --host 127.0.0.1
```

The [Agent Profiler guide](https://github.com/cognifityai/verdict/blob/main/docs/AGENT_PROFILER.md)
contains a key-free synthetic demo and the evidence boundaries for local
histories. The [statistics primer](https://github.com/cognifityai/verdict/blob/main/docs/STATS_PRIMER.md)
explains model-call comparison eligibility and alert gates.

## Customer-managed deployment

A single-tenant team can use a dedicated PostgreSQL store with a separately
run dashboard and supervised `verdict-service` worker. Instrumented Python
processes can write directly to PostgreSQL, or spool bounded local files for
the host shipper and authenticated collector. The released collector accepts
Agent records, standalone provider Traces, and manual Spans. The host team
configures TLS, authentication, retention, access, provider keys, and worker
supervision.

See [customer-managed operations](https://github.com/cognifityai/verdict/blob/main/docs/CUSTOMER_MANAGED_OPERATIONS.md)
for the topology, commands, health checks, and upgrade boundary. Verdict is a
customer-managed toolkit, not a hosted service.

## Evidence boundaries

Local agent histories report tokens according to their source. Those counts
are not provider bills. Estimated cost uses a dated list-price table and may
omit caching, subscription, or negotiated pricing. Agent-session comparisons
are descriptive. Statistical drift alerts apply only to eligible genuine
model-call traces; they require a reviewed reference, new traffic, and sample
and measurement gates. Optional judge calls send the approved bounded evidence
to the selected provider. Redaction is best-effort, so choose capture fields
and provider egress policies for your own environment.

For the supported behaviors and limitations, start with the
[main README](https://github.com/cognifityai/verdict/blob/main/README.md) and
[Verdict explainer](https://github.com/cognifityai/verdict/blob/main/docs/EXPLAINER.md).
