export function initialDashboardTab(meta = {}) {
  if (Number(meta.totalAgentRuns) > 0) return "overview";
  if (Number(meta.totalTraces) > 0) return "overview";
  return "settings";
}

export function setupFailureMessage(failure, origin) {
  if (failure instanceof TypeError) {
    return `Cannot reach the Verdict server at ${origin}. Restart that server and reload this page.`;
  }
  return String(failure);
}
