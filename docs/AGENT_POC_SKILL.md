# Run a customer POC with the Verdict agent skill

Use `verdict-instrument-app` to have a coding agent inspect an existing Python
LLM application, propose a bounded integration, instrument approved paths, and
verify evidence through storage and the local dashboard.

This is an agent instruction package, not an executable installer. A GitHub URL
does not automatically install a skill in every coding agent. The portable path
is to give the agent the checked-out `SKILL.md` explicitly.

## Before the session

1. Check out this Verdict repository at the commit you intend to use.
2. Create a reversible branch in the customer application.
3. Use a non-production environment first.
4. Decide who can approve code edits, dependencies, content capture, storage,
   paid provider/judge calls, and persistent schedules.
5. Keep credentials in the customer's existing secret manager or environment.
   Do not paste keys into the prompt or generated documentation.

## Point the customer's agent at the skill

Give the agent this prompt, replacing both absolute paths:

```text
Read /absolute/path/to/verdict/skills/verdict-instrument-app/SKILL.md completely
and follow it as the governing workflow. Work in
/absolute/path/to/customer/application.

First inspect the repository and run the skill's read-only scanner. Return the
support matrix, proposed instrumentation points, data flow, risks, rollback,
test plan, and one batched set of unresolved questions. Do not edit files,
install dependencies, enable content capture, make paid calls, create storage,
start services, or schedule jobs until I approve that exact plan. After
approval, implement the smallest supported POC and verify every claim through
its final sink. Label live, synthetic, mocked, and unverified evidence
separately.
```

Agents with a native skill installer may install the
`skills/verdict-instrument-app` directory using that host's documented
mechanism. A native install copies the skill but does not install the
repository-local pipeline or dashboard. Give the agent a separate absolute path
to a pinned Verdict source checkout and require the bundled checkout verifier
to pass before those tools run. The direct-file prompt above is the
cross-host fallback and is the only usage assumed by this repository.

## Expect three separate milestones

| Milestone | What must be proven | What it does not prove |
|---|---|---|
| Capture | A supported SDK call creates exactly one expected stored trace | Intent quality or regression detection |
| Quality | Approved content is safe enough for the trial, clusters match the regression question, and the judge clears a customer-label gate | A baseline/current regression exists |
| Regression | Independent windows meet the sample floor and the latest `DriftRun` is visible in the SQLite dashboard | Production readiness or outbound alerting |

The agent must stop at the last milestone that has evidence. It must not turn on
content merely to make quality charts appear.

## Decisions the customer should expect

After discovery, the agent asks one batch covering:

- environment, process, provider, and call sites in scope;
- metadata-only or separately approved prompt/response content;
- prohibited data classes, synthetic redaction canaries, and retention period;
- absolute SQLite path for a local POC or Postgres owner for shared capture;
- stable route/task taxonomy or independently labelled intent examples;
- evaluator provider/model/rubric, credentials, call ceiling, and spend owner;
- baseline/current window, acceptable detection delay, and available volume;
- existing scheduler and dashboard network boundary; and
- permission to edit, install, launch, test, and schedule.

SQLite is the recommended local POC store. The bundled dashboard reads SQLite
only. Postgres capture is supported, but a Postgres-to-dashboard
materialization is a separate, unshipped integration. The `0.1.0a4` drift
runner also prints its storage argument, so a credential-bearing Postgres URL
must not be passed to that command or its logs.

## What the agent may automate after approval

The skill can direct the agent to:

- add the pinned Cognifity distributions;
- initialize Verdict once per covered Python process;
- verify supported sync, async, or streaming calls against the real customer
  entry point and final database;
- test sampling, errors, re-initialization, privacy canaries, and rollback;
- run clustering preflight before judge spend;
- run an approved drift command manually;
- launch and browser-check the local dashboard; and
- add a job to the customer's existing scheduler after the same command passes
  manually and as a one-shot scheduled invocation.

The skill cannot make unsupported SDK methods capturable, guarantee redaction,
invent an intent field absent from captured data, calibrate a judge without
independent labels, make correlated turns statistically independent, expose
Postgres in the bundled dashboard, configure schedules through the dashboard,
or send outbound alerts that Verdict `0.1.0a4` does not ship.

## Release-specific boundary

The skill targets public Verdict `0.1.0a4` and the source tools in this
checkout. Read the [POC release profile](POC_RELEASE_PROFILE.md) before the
session. Re-inspect all commands and provider entry points before using the
skill with a later release.
