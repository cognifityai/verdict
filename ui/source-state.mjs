export function initialDashboardTab(meta = {}) {
  if (Number(meta.totalAgentRuns) > 0) return "insights";
  if (Number(meta.totalTraces) > 0) return "overview";
  return "setup";
}

export function sourceNavigationLabel(meta = {}) {
  return Number(meta.totalAgentRuns) > 0 || Number(meta.totalTraces) > 0
    ? "Data sources"
    : "Setup";
}

export function setupFailureMessage(failure, origin) {
  if (failure instanceof TypeError) {
    return `Cannot reach the Verdict server at ${origin}. Restart that server and reload this page.`;
  }
  return String(failure);
}
