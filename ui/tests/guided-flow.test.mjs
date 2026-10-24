import assert from "node:assert/strict";
import test from "node:test";

import { analysisNeedsRefresh, cycleVerifiedForPlan, isEvidenceGap, nextAction, monitoringStatus, expectedNextCheck } from "../guided-flow.mjs";

test("unmatched tool outcomes are evidence gaps rather than recorded failures", () => {
  assert.equal(isEvidenceGap({ code: "tool_outcomes_incomplete", severity: "warning" }), true);
});

test("next check estimate requires a live worker and current durable due revision", () => {
  const plan = { revision: 2, payload: { intervalHours: 24 } };
  const control = { worker: { status: "recent" }, nextDue: {
    scheduleRevision: 2, nextDueAt: "2026-10-19T10:00:00+00:00",
  }, lastCycle: { state: "completed", trigger: "manual" } };
  assert.equal(expectedNextCheck(plan, control), "2026-10-19T10:00:00.000Z");
  assert.equal(expectedNextCheck(plan, { ...control, worker: { status: "stale" } }), null);
  assert.equal(expectedNextCheck({ ...plan, revision: 3 }, control), null);
  assert.equal(expectedNextCheck(plan, { ...control, nextDue: { ...control.nextDue, nextDueAt: "bad time" } }), null);
  assert.equal(expectedNextCheck(plan, { ...control, nextDue: null }), null);
  assert.equal(expectedNextCheck(null, { worker: { status: "recent" } }), null);
});

test("a cycle verifies a plan only after all selected checks complete", () => {
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "error" } }, { runMonitor: false }), false);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" }, monitor: null }, { runMonitor: true }), false);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" }, monitor: { state: "active" } }, { runMonitor: true }), false);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" } }, { runMonitor: false }), true);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" }, monitor: { state: "active", snapshot: {} } }, { runMonitor: true }), true);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" }, scheduledEvaluation: { status: "waiting_provider" } }, { includeApprovedEvaluations: true }), false);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" }, scheduledEvaluation: { status: "completed" } }, { includeApprovedEvaluations: true }), true);
  assert.equal(cycleVerifiedForPlan({ analysis: { status: "completed" }, scheduledEvaluation: { status: "idle" } }, { includeApprovedEvaluations: true }), true);
});

const meta = (runs, traces, eligible = traces) => ({ totalAgentRuns: runs, totalTraces: traces, monitorEligibleCalls: eligible });
const analyzed = (findings = []) => ({ analysisState: { status: "completed" }, findings });

test("empty and local-history stores are directed to eligible first actions", () => {
  assert.equal(nextAction({ meta: meta(0, 0), insights: analyzed() }).id, "capture");
  assert.equal(nextAction({ meta: meta(8, 0), insights: { analysisState: { status: "never_run" } } }).id, "analyze");
  const local = nextAction({ meta: meta(8, 0), insights: analyzed() });
  assert.equal(local.id, "plan");
  assert.doesNotMatch(local.description, /model.call drift|semantic cluster/i);
});

test("historical findings remain inspectable without displacing monitoring setup", () => {
  const action = nextAction({ meta: meta(8, 0), insights: analyzed([
    { code: "tool_error", severity: "error", runs: 3, runIds: ["run-1"] },
  ]) });
  assert.equal(action.id, "plan");
  assert.doesNotMatch(`${action.title} ${action.description}`, /new issue|regression|improved/i);
  const configured = nextAction({ meta: meta(8, 0), insights: analyzed([
    { code: "tool_error", severity: "error", runs: 3, runIds: ["run-1"] },
  ]), control: { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
    worker: { status: "recent" } } });
  assert.equal(configured.id, "review_findings");
  assert.match(configured.title, /1 recorded finding type/);
  assert.match(configured.description, /current analyzed scope/i);
  assert.doesNotMatch(`${configured.title} ${configured.description}`, /new alert|recurring issue/i);
});

test("a current historical finding stays visible after the worker runs without a new notice", () => {
  const configured = { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
    worker: { status: "recent" }, openIssues: 0,
    lastCycle: { state: "completed", trigger: "worker" } };
  const findings = { ...analyzed([
    { code: "test_failed", severity: "error", runs: 1, runIds: ["run-1"] },
    { code: "tool_error", severity: "error", runs: 1, runIds: ["run-2"] },
    { code: "run_status_unknown", severity: "warning", runs: 1, runIds: ["run-3"] },
  ]), scope: { availableRuns: 3, traces: { available: 0 } } };
  const action = nextAction({ meta: meta(3, 0), insights: findings, control: configured });
  assert.equal(action.id, "review_findings");
  assert.match(action.title, /2 recorded finding types/);
  assert.match(action.description, /before this plan was saved/);
  assert.equal(nextAction({ meta: meta(3, 0), insights: findings,
    control: { ...configured, openIssues: 1 } }).id, "review_issues");
  assert.equal(nextAction({ meta: meta(3, 0), insights: findings,
    control: { ...configured, worker: { status: "stale" } } }).id, "run_cycle");
  assert.equal(nextAction({ meta: meta(4, 0), insights: findings,
    control: configured }).id, "analyze");
});

test("a gap-only current analysis directs inspection before recurring setup", () => {
  const gap = { code: "event_capture_partial", severity: "warning", runs: 1,
    runIds: ["run-1"] };
  const action = nextAction({ meta: meta(8, 0), insights: analyzed([gap]) });
  assert.equal(action.id, "inspect_gaps");
  assert.match(`${action.title} ${action.description}`, /evidence gaps/i);
  const missingOutcome = nextAction({ meta: meta(8, 0), insights: analyzed([
    { code: "run_status_unknown", severity: "warning", runs: 1, runIds: ["run-2"] },
  ]) });
  assert.doesNotMatch(missingOutcome.description, /capture limit/i);
  assert.equal(nextAction({ meta: meta(8, 0), insights: analyzed([gap]),
    control: { openIssues: 1 } }).id, "review_issues");
  assert.equal(nextAction({ meta: meta(8, 0), insights: analyzed([gap]),
    control: { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
      worker: { status: "stale" } } }).id, "run_cycle");
});

test("a current monitor alert takes priority over historical findings", () => {
  const action = nextAction({ meta: meta(8, 0), insights: analyzed([
    { code: "possible_tool_loop", severity: "warning", runs: 4, runIds: ["r1"] },
    { code: "tool_error", severity: "error", runs: 1, runIds: ["r2"] },
  ]), monitor: { active: { snapshot: { comparison: { status: "alert" },
    manifest: { prospective_open: false } } } } });
  assert.equal(action.id, "monitor_alert");
});

test("newly captured runs or calls require refreshed findings before setup", () => {
  const snapshot = { analysisState: { status: "completed" },
    scope: { availableRuns: 4, traces: { available: 2 } }, findings: [] };
  assert.equal(analysisNeedsRefresh(meta(5, 2), snapshot), true);
  assert.equal(analysisNeedsRefresh(meta(4, 3), snapshot), true);
  assert.equal(analysisNeedsRefresh(meta(4, 2), snapshot), false);
  assert.equal(nextAction({ meta: meta(5, 2), insights: snapshot }).id, "analyze");
});

test("stale imported activity gets a runnable local action before a stopped-worker reminder", () => {
  const snapshot = { analysisState: { status: "completed" },
    scope: { availableRuns: 4, traces: { available: 0 } }, findings: [] };
  const saved = { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
    worker: { status: "unverified" } };
  assert.equal(nextAction({ meta: meta(4, 0), insights: snapshot, control: saved }).id, "run_cycle");
  assert.equal(nextAction({ meta: meta(5, 0), insights: snapshot, control: saved }).id, "analyze");
  assert.equal(nextAction({ meta: meta(5, 0), insights: snapshot,
    control: { ...saved, lastCycle: { state: "failed", failureCode: "analysis_error" } } }).id, "check_failed");
});

test("same-count Agent Run reimport after analysis cutoff is stale", () => {
  const snapshot = { analysisState: { status: "completed", cutoff: "2026-10-17T10:00:00Z" },
    scope: { availableRuns: 8, traces: { available: 0 } }, findings: [] };
  const current = { ...meta(8, 0), lastAgentStoredAt: "2026-10-17T10:00:01Z" };
  assert.equal(analysisNeedsRefresh(current, snapshot), true);
  assert.equal(nextAction({ meta: current, insights: snapshot }).id, "analyze");
  assert.equal(analysisNeedsRefresh({ ...current, lastAgentStoredAt: "2026-10-17T09:59:59Z" }, snapshot), false);
  assert.equal(analysisNeedsRefresh({ ...current, lastAgentStoredAt: "invalid" }, snapshot), false);
  assert.equal(analysisNeedsRefresh({ ...current, lastAgentStoredAt: "2026-10-17T10:00:00.000002+00:00" },
    { ...snapshot, analysisState: { status: "completed", cutoff: "2026-10-17T10:00:00.000001+00:00" } }), true);
});

test("a verified identical rescan clears freshness without replacing its snapshot", () => {
  const snapshot = { analysisState: { status: "completed", cutoff: "2026-10-17T10:00:00Z",
    verifiedRevision: 12 },
    scope: { availableRuns: 8, traces: { available: 2 } }, findings: [] };
  const current = { ...meta(8, 2), evidenceRevision: 12 };
  assert.equal(analysisNeedsRefresh(current, snapshot), false);
  assert.equal(analysisNeedsRefresh({ ...current, evidenceRevision: 13 }, snapshot), true);
  assert.equal(analysisNeedsRefresh({ ...current, evidenceRevision: 13 },
    { ...snapshot, analysisState: { ...snapshot.analysisState, verifiedRevision: null } }), true);
  assert.equal(analysisNeedsRefresh({ ...current, evidenceRevision: null }, snapshot), true);
});

test("genuine calls lead to a reviewed monitor only after analysis", () => {
  const action = nextAction({ meta: meta(0, 90), insights: analyzed(),
    control: { documents: [{ kind: "schedule", documentId: "daily", state: "active" }], worker: { status: "recent" } } });
  assert.equal(action.id, "monitor");
});

test("internal or unfinished traces do not trigger capture-complete or monitor advice", () => {
  const action = nextAction({ meta: meta(0, 5, 0), insights: analyzed() });
  assert.equal(action.id, "capture");
  assert.equal(nextAction({ meta: meta(0, 5, 1), insights: analyzed(),
    control: { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
      worker: { status: "recent" } } }).id, "monitor");
});

test("retention changes request refresh without claiming new capture", () => {
  const snapshot = { analysisState: { status: "completed" },
    scope: { availableRuns: 8, traces: { available: 5 } }, findings: [] };
  const action = nextAction({ meta: meta(7, 4), insights: snapshot });
  assert.equal(action.id, "analyze");
  assert.match(action.description, /changed/);
  assert.doesNotMatch(`${action.title} ${action.description}`, /newly captured|new activity/i);
});

test("saved schedules and collecting monitors never masquerade as healthy monitoring", () => {
  assert.equal(monitoringStatus({ active: { snapshot: { comparison: { status: "no_alert" }, manifest: { prospective_open: true } } } }, null).id, "collecting");
  assert.equal(monitoringStatus(null, { documents: [{ kind: "schedule", documentId: "daily", state: "active" }] }).id, "saved");
  assert.equal(monitoringStatus({ active: { snapshot: { comparison: { status: "alert" }, manifest: { prospective_open: false } } } }, null).id, "alert");
  assert.equal(monitoringStatus({ active: { snapshot: { comparison: { status: "no_alert" }, manifest: { prospective_open: false } } } }, null).id, "compared");
  const configured = { documents: [{ kind: "schedule", documentId: "daily", state: "active" }], worker: { status: "stale" } };
  assert.equal(monitoringStatus({ active: { snapshot: { comparison: { status: "no_alert" }, manifest: { prospective_open: false } } } }, configured).id, "saved");
  assert.equal(monitoringStatus(null, { ...configured, worker: { status: "recent" } }).id, "worker_recent");
  assert.equal(monitoringStatus(null, { ...configured, worker: { status: "recent" },
    lastCycle: { state: "failed", failureCode: "analysis_error" } }).id, "check_failed");
  assert.equal(monitoringStatus(null, { ...configured, worker: { status: "recent" },
    lastCycle: { state: "started" } }).id, "check_unfinished");
  assert.equal(nextAction({ meta: meta(2, 0), insights: analyzed(),
    control: { ...configured, worker: { status: "recent" },
      lastCycle: { state: "failed", failureCode: "analysis_error" } } }).id, "check_failed");
  const paused = { documents: [{ kind: "schedule", documentId: "daily", state: "disabled" }],
    worker: { status: "recent" } };
  assert.equal(monitoringStatus(null, paused).id, "paused");
  assert.equal(nextAction({ meta: meta(2, 0), insights: analyzed(), control: paused }).id, "resume");
});

test("open durable issues lead from Home to the monitoring inbox", () => {
  const action = nextAction({ meta: meta(4, 0), insights: analyzed(),
    control: { documents: [{ kind: "schedule", documentId: "daily", state: "active" }],
      worker: { status: "recent" }, openIssues: 2 } });
  assert.equal(action.id, "review_issues");
  assert.match(action.title, /2 recorded issues/);
  assert.match(action.description, /recorded issue and any available run evidence/);
});

test("retained open issues remain the next action after source data expires", () => {
  const action = nextAction({ meta: meta(0, 0), control: { openIssues: 1 } });
  assert.equal(action.id, "review_issues");
  assert.match(action.title, /1 recorded issue/);
  assert.match(action.description, /recorded issue and any available run evidence/);
});
