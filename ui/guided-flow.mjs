const schedule = (control) => (control?.documents || []).find(
  (document) => document.kind === "schedule" && document.documentId === "daily"
    && document.state === "active",
);
const pausedSchedule = (control) => (control?.documents || []).find(
  (document) => document.kind === "schedule" && document.documentId === "daily"
    && document.state === "disabled",
);
const EVIDENCE_GAP_CODES = new Set(["run_status_unknown", "response_not_evaluable", "event_capture_partial", "tool_outcomes_incomplete"]);

export function isEvidenceGap(finding) {
  return EVIDENCE_GAP_CODES.has(finding?.code);
}

export function cycleVerifiedForPlan(result, plan) {
  if (result?.analysis?.status !== "completed") return false;
  if (plan?.runMonitor === true && (result.monitor?.state !== "active" || !result.monitor.snapshot)) return false;
  if (plan?.includeApprovedEvaluations === true && !["idle", "completed"].includes(result.scheduledEvaluation?.status)) return false;
  return true;
}

export function expectedNextCheck(plan, control) {
  const due = control?.nextDue;
  if (!plan || !due || control?.worker?.status !== "recent" || due.scheduleRevision !== plan.revision
      || (control?.lastCycle?.state === "started" && control.lastCycle.trigger === "worker")) return null;
  const timestamp = Date.parse(due.nextDueAt);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : null;
}

export function analysisNeedsRefresh(meta, insights) {
  if (insights?.analysisState?.status !== "completed") return false;
  const scope = insights.scope;
  if (!scope) return false;
  const revision = meta?.evidenceRevision;
  const verified = insights.analysisState?.verifiedRevision;
  const countsChanged = (Number.isInteger(scope.availableRuns) && Number.isInteger(meta?.totalAgentRuns)
      && scope.availableRuns !== meta.totalAgentRuns)
    || (Number.isInteger(scope.traces?.available) && Number.isInteger(meta?.totalTraces)
      && scope.traces.available !== meta.totalTraces);
  if (countsChanged) return true;
  if (meta?.evidenceRevision === null
      && (Number(meta?.totalAgentRuns || 0) > 0 || Number(meta?.totalTraces || 0) > 0)) return true;
  if (Number.isSafeInteger(revision) && revision >= 0) {
    return !Number.isSafeInteger(verified) || verified !== revision;
  }
  const stored = preciseEpochMicros(meta?.lastAgentStoredAt);
  const cutoff = preciseEpochMicros(insights.analysisState?.cutoff);
  return stored !== null && cutoff !== null && stored > cutoff;
}

function preciseEpochMicros(value) {
  if (typeof value !== "string") return null;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return null;
  const fraction = value.match(/\.(\d{1,6})(?:Z|[+-]\d\d:\d\d)$/);
  return milliseconds * 1000 + (fraction ? Number(fraction[1].padEnd(6, "0")) % 1000 : 0);
}

export function primaryFinding(insights) {
  return (insights?.findings || []).filter((finding) =>
    ["error", "warning"].includes(finding.severity)
      && !isEvidenceGap(finding)
      && Array.isArray(finding.runIds) && finding.runIds.length > 0)
    .sort((left, right) => (left.severity === "error" ? 0 : 1) - (right.severity === "error" ? 0 : 1)
      || (right.runs || 0) - (left.runs || 0) || left.code.localeCompare(right.code))[0] || null;
}

export function monitoringStatus(monitor, control) {
  const active = monitor?.active;
  const comparison = active?.snapshot?.comparison;
  const collecting = active?.snapshot?.manifest?.prospective_open === true;
  if (comparison?.status === "alert" && !collecting) return {
    id: "alert", title: "A monitored change needs review",
    description: "Open the comparison to see the metric, cohort sizes, and supporting calls.",
  };
  if (pausedSchedule(control)) return {
    id: "paused", title: "Recurring checks are paused",
    description: "The saved plan will not run again until you review and resume it. A check already in progress when paused may still finish.",
  };
  if (schedule(control) && control?.lastCycle?.state === "failed") return {
    id: "check_failed", title: "Latest check did not complete",
    description: `The latest ${control.lastCycle.trigger === "manual" ? "manual" : "worker"} cycle recorded ${String(control.lastCycle.failureCode || "a check failure").replaceAll("_", " ")}. Review the plan and retry before relying on monitoring.`,
  };
  if (schedule(control) && control?.lastCycle?.state === "started") return {
    id: "check_unfinished", title: "Check completion not recorded",
    description: "A cycle started, but no completion was recorded. Inspect the worker and rerun a check before relying on monitoring.",
  };
  if (schedule(control) && control?.worker?.status !== "recent") return {
    id: "saved", title: "Schedule saved; worker not verified",
    description: "The plan is saved, but recurring execution is not verified. Start verdict-service and confirm a recent heartbeat before relying on new checks.",
  };
  if (collecting) return {
    id: "collecting", title: "Collecting new calls",
    description: "The active policy is waiting for its prospective cohort; the earlier result does not describe new traffic.",
  };
  if (comparison?.status === "no_alert") return {
    id: "compared", title: "Latest comparison found no alert",
    description: `The selected metrics were compared for the shown cohorts.${schedule(control) ? " A worker was seen recently." : ""} This does not establish task quality.`,
  };
  if (active) return {
    id: "waiting", title: "Monitoring needs more evidence",
    description: "Review comparison coverage before treating this policy as a health signal.",
  };
  if (schedule(control)) return {
    id: "worker_recent", title: "Recurring worker seen recently",
    description: "A recent heartbeat was recorded. Inspect the latest cycle and analysis result before interpreting agent health.",
  };
  return {
    id: "unconfigured", title: "No recurring check configured",
    description: "Review the suggested checks for the evidence Verdict has captured.",
  };
}

export function nextAction({ meta = {}, insights = null, monitor = null, control = null }) {
  const runs = meta.totalAgentRuns || 0;
  const calls = meta.monitorEligibleCalls || 0;
  if (!runs && !calls) return Number(control?.openIssues) > 0 ? {
    id: "review_issues",
    title: `Review ${control.openIssues} recorded issue${control.openIssues === 1 ? "" : "s"}`,
    description: "Open the issue inbox to inspect the recorded issue and any available run evidence, then choose the next check.",
  } : {
    id: "capture", title: "Connect an agent or model source",
    description: "Capture agent runs or genuine model calls to see issues and usage.",
  };
  const alerts = monitoringStatus(monitor, control);
  if (alerts.id === "alert") return { ...alerts, id: "monitor_alert" };
  if (["check_failed", "check_unfinished"].includes(alerts.id)) return alerts;
  if (Number(control?.openIssues) > 0) return {
    id: "review_issues",
    title: `Review ${control.openIssues} recorded issue${control.openIssues === 1 ? "" : "s"}`,
    description: "Open the issue inbox to inspect the recorded issue and any available run evidence, then choose the next check.",
  };
  if (alerts.id === "paused") return { ...alerts, id: "resume" };
  if (!insights || insights.analysisState?.status === "never_run"
      || insights.analysisState?.status === "error") return {
    id: "analyze", title: "Analyze captured activity",
    description: "Run local deterministic checks to find recorded failures and evidence gaps.",
  };
  if (analysisNeedsRefresh(meta, insights)) return {
    id: "analyze", title: "Refresh analysis of changed activity",
    description: "Captured activity changed since these findings were saved. Refresh local analysis before choosing the next check.",
  };
  if (alerts.id === "saved") return { ...alerts, id: "run_cycle" };
  const findings = insights.findings || [];
  if (findings.some(isEvidenceGap) && !findings.some((finding) => !isEvidenceGap(finding))) return {
    id: "inspect_gaps", title: "Inspect evidence gaps",
    description: "The current check found no recorded issue, but missing or partial evidence limits what can be concluded. Inspect those gaps before setting up recurring checks.",
  };
  if (!schedule(control)) return {
    id: "plan", title: "Review suggested monitoring",
    description: "Review the captured evidence, run a local check, and confirm what can keep watching.",
  };
  if (calls && !monitor?.active) return {
    id: "monitor", title: "Preview a model-call comparison",
    description: "Use captured calls to review baseline and current cohorts before activating a monitor.",
  };
  const recordedFindings = findings.filter((finding) => !isEvidenceGap(finding));
  if (recordedFindings.length) return {
    id: "review_findings",
    title: `Inspect ${recordedFindings.length} recorded finding type${recordedFindings.length === 1 ? "" : "s"}`,
    description: "Open the linked evidence in the current analyzed scope. Some findings may be from before this plan was saved; they are not new recurring alerts.",
  };
  return {
    id: "review", title: "Review the latest evidence",
    description: "No active alert or open recurring issue was reported. Inspect recorded findings and coverage before drawing conclusions.",
  };
}
