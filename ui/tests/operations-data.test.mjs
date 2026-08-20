import assert from "node:assert/strict";
import test from "node:test";

import { normalizeOperationsPayload } from "../operations-data.js";

test("operations payload preserves partial and unavailable evidence", () => {
  const normalized = normalizeOperationsPayload({
    status: "partial",
    generatedAt: "2026-08-20T12:00:00Z",
    windowMinutes: 60,
    metrics: [
      { id: "rpm", label: "Requests/minute", group: "application", unit: "rpm", status: "available", value: 12.5, series: [{ timestamp: "2026-08-20T12:00:00Z", value: 12.5 }] },
      { id: "db", label: "PostgreSQL connections", group: "database", unit: "count", status: "unavailable", message: "Monitoring metric has no samples" },
    ],
    actions: [{ id: "drift_analysis", label: "Run drift analysis", enabled: false, disabledReason: "Content capture is disabled" }],
    jobs: [{ id: "job-1", kind: "inspect_report", status: "succeeded", startedAt: "2026-08-20T11:00:00Z", logs: ["Started", "Completed"] }],
    links: [{ label: "Cloud Monitoring", url: "https://console.cloud.google.com/monitoring" }],
    csrfToken: "token",
  });

  assert.equal(normalized.status, "partial");
  assert.equal(normalized.metrics[0].value, 12.5);
  assert.equal(normalized.metrics[1].status, "unavailable");
  assert.equal(normalized.actions[0].disabledReason, "Content capture is disabled");
  assert.deepEqual(normalized.jobs[0].logs, ["Started", "Completed"]);
  assert.equal(normalized.csrfToken, "token");
});

test("operations payload bounds untrusted adapter arrays and rejects bad shapes", () => {
  assert.throws(() => normalizeOperationsPayload(null), /operations response/);
  assert.throws(() => normalizeOperationsPayload({ metrics: "bad" }), /metrics/);

  const normalized = normalizeOperationsPayload({
    metrics: Array.from({ length: 150 }, (_, index) => ({
      id: `m-${index}`,
      label: `Metric ${index}`,
      group: "custom",
      unit: "count",
      status: "available",
      value: index,
      series: Array.from({ length: 600 }, (_unused, point) => ({ timestamp: String(point), value: point })),
    })),
  });

  assert.equal(normalized.metrics.length, 100);
  assert.equal(normalized.metrics[0].series.length, 500);
});
