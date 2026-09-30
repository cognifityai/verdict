function counts(values) {
  return Object.entries(values || {}).map(([name, count]) => `${name}: ${count}`).join(" · ") || "None";
}

const NO_AGENT_RUNS = "Not available — no Agent Runs captured";

export function agentEvidenceValue(dataHealth = {}, value) {
  return Number(dataHealth.counts?.runs) > 0 ? value : NO_AGENT_RUNS;
}

const OUTCOME_NOT_REPORTED = "Not reported by source";

// A zero failure count only means "no failures" when the source reported at
// least one outcome for that event type. Snapshots written before outcome
// counts existed carry no count and keep their stored value.
export function failureCountValue(failures, reported, notReported = OUTCOME_NOT_REPORTED) {
  if (reported === undefined || reported === null) return failures ?? 0;
  return Number(reported) > 0 ? (failures ?? 0) : notReported;
}

export function sourceTokenValue(activity = {}) {
  if (activity.tokenUsageState === "not_captured") {
    return "Not captured";
  }
  if (activity.totalTokens == null) return "Components captured (partial)";
  const total = Number(activity.totalTokens).toLocaleString();
  return activity.tokenUsageState === "partial" ? `${total} (partial)` : total;
}

export function datasetActivitySummary(dataHealth = {}, performance = {}) {
  const hasAgentRuns = Number(dataHealth.counts?.runs) > 0;
  const traceLinks = dataHealth.traceLinks || {};
  if (!hasAgentRuns) {
    return {
      activityLabel: "LLM calls",
      activityValue: Number(performance.modelCalls) || 0,
      activityDetail: "Trace-derived · Agent Events unavailable",
      linkLabel: "Agent-to-trace links",
      linkValue: "Not available",
      linkDetail: "No Agent Runs captured",
    };
  }
  return {
    activityLabel: "Normalized events",
    activityValue: Number(dataHealth.counts?.events) || 0,
    activityDetail: `${Number(traceLinks.modelCalls) || 0} model · ${Number(performance.toolCalls) || 0} tool calls`,
    linkLabel: "Model-call trace links",
    linkValue: `${Number(traceLinks.linked) || 0}/${Number(traceLinks.modelCalls) || 0}`,
    linkDetail: `${Number(traceLinks.unlinked) || 0} unlinked`,
  };
}

export function datasetEvidenceRows(dataHealth = {}, scope = {}) {
  const trace = dataHealth.traceEvidence || {};
  const total = Number(scope.traces?.analyzed) || 0;
  const prompts = Number(trace.promptPresent) || 0;
  const responses = Number(trace.responsePresent) || 0;
  const hasAgentRuns = Number(dataHealth.counts?.runs) > 0;
  const coverage = (present) => `${present.toLocaleString()} present${total > present ? ` · ${(total - present).toLocaleString()} absent` : ""}`;
  return [
    ["Trace prompt evidence", coverage(prompts)],
    ["Trace response evidence", coverage(responses)],
    ["Trace judge eligibility", `${(Number(trace.judgeEligible) || 0).toLocaleString()} eligible · ${(Number(trace.notEvaluable) || 0).toLocaleString()} unavailable`],
    ["Agent-turn prompt evidence", hasAgentRuns ? counts(dataHealth.promptStates) : NO_AGENT_RUNS],
    ["Agent-turn response evidence", hasAgentRuns ? counts(dataHealth.responseStates) : NO_AGENT_RUNS],
  ];
}
