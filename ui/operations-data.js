const MAX_METRICS = 100;
const MAX_POINTS = 500;
const MAX_JOBS = 50;

function list(value, field) {
  if (value == null) return [];
  if (!Array.isArray(value)) throw new Error(`invalid operations ${field}`);
  return value;
}

function text(value, fallback = "", max = 240) {
  return typeof value === "string" ? value.slice(0, max) : fallback;
}

function status(value) {
  return ["available", "partial", "unavailable"].includes(value)
    ? value
    : "unavailable";
}

export function normalizeOperationsPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("invalid operations response");
  }

  const metrics = list(payload.metrics, "metrics").slice(0, MAX_METRICS).map((metric, index) => ({
    id: text(metric?.id, `metric-${index}`, 80),
    label: text(metric?.label, "Unnamed metric", 120),
    group: text(metric?.group, "custom", 80),
    unit: text(metric?.unit, "count", 32),
    status: status(metric?.status),
    value: Number.isFinite(metric?.value) ? metric.value : null,
    message: text(metric?.message, "", 300),
    series: list(metric?.series, "metric series").slice(0, MAX_POINTS)
      .filter((point) => Number.isFinite(point?.value))
      .map((point) => ({
        timestamp: text(point.timestamp, "", 64),
        value: point.value,
      })),
  }));

  const actions = list(payload.actions, "actions").slice(0, 20).map((action, index) => ({
    id: text(action?.id, `action-${index}`, 80),
    label: text(action?.label, "Unnamed action", 120),
    description: text(action?.description, "", 300),
    enabled: action?.enabled === true,
    disabledReason: text(action?.disabledReason, "", 300),
  }));

  const jobs = list(payload.jobs, "jobs").slice(0, MAX_JOBS).map((job, index) => ({
    id: text(job?.id, `job-${index}`, 100),
    kind: text(job?.kind, "unknown", 80),
    status: text(job?.status, "unknown", 32),
    startedAt: text(job?.startedAt, "", 64),
    finishedAt: text(job?.finishedAt, "", 64),
    summary: text(job?.summary, "", 300),
    logs: list(job?.logs, "job logs").slice(0, 50).map((line) => text(line, "", 500)),
  }));

  const links = list(payload.links, "links").slice(0, 20)
    .filter((link) => typeof link?.url === "string" && link.url.startsWith("https://"))
    .map((link) => ({ label: text(link.label, "Open console", 100), url: link.url }));

  return {
    status: status(payload.status),
    generatedAt: text(payload.generatedAt, "", 64),
    windowMinutes: Number.isFinite(payload.windowMinutes) ? payload.windowMinutes : null,
    metrics,
    actions,
    jobs,
    links,
    csrfToken: text(payload.csrfToken, "", 500),
  };
}
