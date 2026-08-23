import assert from "node:assert/strict";
import test from "node:test";

import { assignmentExplanation, normalizeRegistryPayload } from "../registry-data.js";

const payload = {
  schema: "cluster-registry-dashboard-v1",
  tenant: "tenant-a",
  status: "ready",
  active: { versionId: "crv-active", generation: 2, activatedAt: "2026-08-23T12:00:00Z", activatedBy: "admin" },
  versions: [{ versionId: "crv-active", strategy: "hybrid", active: true, createdAt: "2026-08-23T11:00:00Z", strategyStatus: { strategy: "hybrid", experimental: true, semantic_component: "fallback" } }],
  selectedVersion: { versionId: "crv-active", strategy: "hybrid", active: true, algorithm: "ward-best-k-v2", selector: "latest-user-v1", model: { name: "MiniLM" }, configuration: { target_workload: "agent" }, strategyStatus: { strategy: "hybrid", experimental: true, semantic_component: "fallback" }, preview: { warnings: ["fit warning"] } },
  readiness: { status: "validated", passed: true, coverage: true, structural: true, definition: true, model: true },
  counts: { assigned: 1, outlier: 1, ineligible: 1, total: 3 },
  modelDistribution: [{ provider: "anthropic", model: "claude", count: 1 }],
  trafficWindow: { cutoff: "2026-08-23T12:00:00Z", baselineDays: 7, gapDays: 1, currentDays: 1, conversationFloor: 30, diagnosticOnly: true },
  healthWarnings: ["fragmented_semantic_space"],
  clusterDetailsTruncated: false,
  clusters: [{ clusterId: "clu-a", displayName: "Billing <script>", kind: "semantic", lifecycle: "active", radius: 0.4, memberCount: 8, outlierCount: 1, assignedCount: 1, detailsAvailable: true, representatives: [{ traceId: "trace-a", prompt: "redacted billing prompt", provider: "anthropic", model: "claude" }], modelDistribution: [{ provider: "anthropic", model: "claude", count: 1 }], conversationReadiness: { status: "collecting", floor: 30, baseline: 12, current: 8, remainingBaseline: 18, remainingCurrent: 22, estimatedDaysToReady: 3 }, warnings: ["oversized_semantic_cluster"] }],
  assignments: [
    { traceId: "trace-a", origin: "fit", status: "assigned", clusterId: "clu-a", clusterKind: "semantic", reason: null, distance: 0.125, assignedAt: "2026-08-23T11:00:00Z" },
    { traceId: "trace-b", origin: "incremental", status: "outlier", clusterId: null, clusterKind: null, reason: "distance", distance: 0.7, assignedAt: "2026-08-23T12:00:00Z" },
    { traceId: "trace-c", origin: "incremental", status: "ineligible", clusterId: null, clusterKind: null, reason: "content_not_captured", distance: null, assignedAt: "2026-08-23T12:00:00Z" },
  ],
  reasons: [{ status: "outlier", reason: "distance", count: 1 }],
  events: [],
  page: { limit: 100, offset: 0, shown: 3, available: 3, truncated: false },
};

test("registry payload preserves experimental strategy and bounded explanations", () => {
  const normalized = normalizeRegistryPayload(payload);

  assert.equal(normalized.selectedVersion.strategyStatus.experimental, true);
  assert.equal(normalized.selectedVersion.algorithm, "ward-best-k-v2");
  assert.equal(normalized.clusters[0].displayName, "Billing <script>");
  assert.equal(normalized.clusters[0].representatives[0].prompt, "redacted billing prompt");
  assert.equal(normalized.clusters[0].conversationReadiness.current, 8);
  assert.equal(normalized.clusters[0].detailsAvailable, true);
  assert.deepEqual(normalized.healthWarnings, ["fragmented_semantic_space"]);
  assert.match(assignmentExplanation(normalized.assignments[0], normalized.clusters), /0.125/);
  assert.match(assignmentExplanation(normalized.assignments[1], normalized.clusters), /Outside/);
  assert.match(assignmentExplanation(normalized.assignments[2], normalized.clusters), /not captured/);
});

test("registry payload rejects cross-schema shapes and bounds untrusted arrays", () => {
  assert.throws(() => normalizeRegistryPayload(null), /registry response/);
  assert.throws(
    () => normalizeRegistryPayload({ ...payload, schema: "other" }),
    /registry schema/,
  );
  assert.throws(
    () => normalizeRegistryPayload({ ...payload, assignments: "bad" }),
    /assignments/,
  );

  const normalized = normalizeRegistryPayload({
    ...payload,
    assignments: Array.from({ length: 300 }, (_, index) => ({
      traceId: `trace-${index}`,
      origin: "incremental",
      status: "ineligible",
      reason: "content_not_captured",
    })),
  });
  assert.equal(normalized.assignments.length, 50);
});
