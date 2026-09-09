export function initialDashboardTab(meta = {}) {
  if (Number(meta.totalAgentRuns) > 0) return "overview";
  if (Number(meta.totalTraces) > 0) return "overview";
  return "settings";
}

export function observedSourcePresentation(meta = {}) {
  const rows = (Array.isArray(meta.agentRunSources) ? meta.agentRunSources : [])
    .filter((item) => typeof item?.sourceKind === "string"
      && item.sourceKind
      && Number.isInteger(Number(item.runs))
      && Number(item.runs) > 0);
  const kinds = new Set(rows.map((item) => item.sourceKind));
  const localKinds = new Set(["claude-code", "codex"]);
  const totalAgentRuns = Number(meta.totalAgentRuns);
  const hasAgentRuns = Number.isInteger(totalAgentRuns) && totalAgentRuns > 0;
  const hasTraces = Number(meta.totalTraces) > 0;
  const hasLocalAgents = [...kinds].some((kind) => localKinds.has(kind));
  const hasSdkAgents = kinds.has("verdict_sdk");
  const completeInventory = !meta.agentRunSourcesTruncated
    && hasAgentRuns
    && kinds.size === rows.length
    && rows.reduce((total, item) => total + Number(item.runs), 0) === totalAgentRuns;
  const onlyLocalAgents = completeInventory
    && hasLocalAgents
    && [...kinds].every((kind) => localKinds.has(kind));
  const onlySdkAgents = completeInventory && kinds.size === 1 && hasSdkAgents;

  let heading = "LLM telemetry";
  if (hasAgentRuns && hasTraces) heading = "Agent and LLM telemetry";
  else if (hasAgentRuns && onlyLocalAgents) heading = "Claude Code / Codex";
  else if (hasAgentRuns && onlySdkAgents) heading = "Instrumented agent telemetry";
  else if (hasAgentRuns) heading = "Agent telemetry";

  return {
    heading,
    sourceLabel: hasAgentRuns ? (onlyLocalAgents ? "Local sources" : "Agent sources") : "Source type",
    hasLocalAgents,
    hasSdkAgents,
  };
}

export function setupFailureMessage(failure, origin) {
  if (failure instanceof TypeError) {
    return `Cannot reach the Verdict server at ${origin}. Restart that server and reload this page.`;
  }
  return String(failure);
}
