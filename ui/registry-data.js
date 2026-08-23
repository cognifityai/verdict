const MAX_VERSIONS = 10;
const MAX_CLUSTERS = 250;
const MAX_ASSIGNMENTS = 50;
const MAX_REASONS = 32;
const MAX_EVENTS = 20;
const MAX_MODELS = 20;
const MAX_CLUSTER_MODELS = 5;
const MAX_REPRESENTATIVES = 3;
const MAX_WARNINGS = 20;

function list(value, field) {
  if (!Array.isArray(value)) throw new Error(`invalid registry ${field}`);
  return value;
}

function text(value, fallback = "", max = 256) {
  return typeof value === "string" ? value.slice(0, max) : fallback;
}

function nullableText(value, max = 256) {
  return value == null ? null : text(value, null, max);
}

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function finite(value) {
  return Number.isFinite(value) ? value : null;
}

function modelDistribution(value, limit) {
  return list(value, "model distribution").slice(0, limit).map((item) => ({
    provider: text(item?.provider, "unknown", 80),
    model: text(item?.model, "unknown", 160),
    count: count(item?.count),
  }));
}

function conversationReadiness(value) {
  const status = ["ready", "collecting", "unavailable"].includes(value?.status)
    ? value.status
    : "unavailable";
  return {
    status,
    floor: count(value?.floor),
    baseline: count(value?.baseline),
    current: count(value?.current),
    remainingBaseline: count(value?.remainingBaseline),
    remainingCurrent: count(value?.remainingCurrent),
    estimatedDaysToReady: value?.estimatedDaysToReady == null
      ? null
      : count(value.estimatedDaysToReady),
  };
}

function strategyStatus(value, strategy) {
  const semanticComponent = ["none", "automatic", "fallback"].includes(value?.semantic_component)
    ? value.semantic_component
    : strategy === "explicit" ? "none" : strategy === "semantic" ? "automatic" : "fallback";
  return {
    strategy,
    experimental: value?.experimental === true,
    semanticComponent,
  };
}

function version(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("invalid registry version");
  }
  const strategy = ["explicit", "semantic", "hybrid"].includes(value.strategy)
    ? value.strategy
    : "unknown";
  return {
    versionId: text(value.versionId, "unknown", 64),
    parentVersionId: nullableText(value.parentVersionId, 64),
    strategy,
    strategyStatus: strategyStatus(value.strategyStatus, strategy),
    active: value.active === true,
    cutoff: text(value.cutoff, "", 64),
    lookbackDays: count(value.lookbackDays),
    createdAt: text(value.createdAt, "", 64),
    createdBy: text(value.createdBy, "", 256),
    configuration: value.configuration && typeof value.configuration === "object" && !Array.isArray(value.configuration) ? value.configuration : {},
    algorithm: nullableText(value.algorithm, 120),
    selector: nullableText(value.selector, 120),
    model: value.model && typeof value.model === "object" && !Array.isArray(value.model) ? value.model : {},
    preview: value.preview && typeof value.preview === "object" && !Array.isArray(value.preview) ? value.preview : {},
  };
}

export function normalizeRegistryPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("invalid registry response");
  }
  if (payload.schema !== "cluster-registry-dashboard-v1") {
    throw new Error("invalid registry schema");
  }
  const status = ["ready", "empty", "unavailable"].includes(payload.status)
    ? payload.status
    : "unavailable";
  const result = {
    schema: payload.schema,
    tenant: text(payload.tenant, "", 128),
    status,
    reason: nullableText(payload.reason, 100),
    active: {
      versionId: nullableText(payload.active?.versionId, 64),
      generation: count(payload.active?.generation),
      activatedAt: nullableText(payload.active?.activatedAt, 64),
      activatedBy: nullableText(payload.active?.activatedBy, 256),
    },
    versions: [],
    versionsTruncated: payload.versionsTruncated === true,
    selectedVersion: null,
    readiness: {
      status: text(payload.readiness?.status, "unvalidated", 40),
      passed: payload.readiness?.passed === true,
      coverage: typeof payload.readiness?.coverage === "boolean" ? payload.readiness.coverage : null,
      structural: typeof payload.readiness?.structural === "boolean" ? payload.readiness.structural : null,
      definition: typeof payload.readiness?.definition === "boolean" ? payload.readiness.definition : null,
      model: typeof payload.readiness?.model === "boolean" ? payload.readiness.model : null,
    },
    activationHistory: payload.activationHistory === true,
    counts: {
      assigned: count(payload.counts?.assigned),
      outlier: count(payload.counts?.outlier),
      ineligible: count(payload.counts?.ineligible),
      total: count(payload.counts?.total),
    },
    modelDistribution: [],
    modelDistributionTruncated: payload.modelDistributionTruncated === true,
    trafficWindow: {
      cutoff: text(payload.trafficWindow?.cutoff, "", 64),
      baselineDays: count(payload.trafficWindow?.baselineDays),
      gapDays: count(payload.trafficWindow?.gapDays),
      currentDays: count(payload.trafficWindow?.currentDays),
      conversationFloor: count(payload.trafficWindow?.conversationFloor),
      diagnosticOnly: payload.trafficWindow?.diagnosticOnly === true,
    },
    healthWarnings: [],
    clusterDetailsTruncated: payload.clusterDetailsTruncated === true,
    clusters: [],
    assignments: [],
    reasons: [],
    events: [],
    page: {
      limit: count(payload.page?.limit),
      offset: count(payload.page?.offset),
      shown: count(payload.page?.shown),
      available: count(payload.page?.available),
      truncated: payload.page?.truncated === true,
    },
  };
  if (status !== "ready") {
    if (payload.versions != null && !Array.isArray(payload.versions)) {
      throw new Error("invalid registry versions");
    }
    return result;
  }
  result.versions = list(payload.versions, "versions").slice(0, MAX_VERSIONS).map(version);
  result.selectedVersion = version(payload.selectedVersion);
  result.modelDistribution = modelDistribution(payload.modelDistribution, MAX_MODELS);
  result.healthWarnings = list(payload.healthWarnings, "health warnings")
    .slice(0, MAX_WARNINGS)
    .map((item) => text(item, "unknown", 80));
  result.clusters = list(payload.clusters, "clusters").slice(0, MAX_CLUSTERS).map((item) => {
    const detailsAvailable = item?.detailsAvailable === true;
    return {
      clusterId: text(item?.clusterId, "unknown", 64),
      displayName: text(item?.displayName, "Unnamed cluster", 256),
      kind: ["explicit", "semantic"].includes(item?.kind) ? item.kind : "unknown",
      lifecycle: ["provisional", "active"].includes(item?.lifecycle) ? item.lifecycle : "unknown",
      explicitKey: nullableText(item?.explicitKey, 64),
      radius: finite(item?.radius),
      memberCount: count(item?.memberCount),
      outlierCount: count(item?.outlierCount),
      assignedCount: count(item?.assignedCount),
      detailsAvailable,
      representatives: detailsAvailable
        ? list(item?.representatives, "cluster representatives")
          .slice(0, MAX_REPRESENTATIVES)
          .map((representative) => ({
            traceId: text(representative?.traceId, "unknown", 256),
            prompt: text(representative?.prompt, "", 240),
            provider: text(representative?.provider, "unknown", 80),
            model: text(representative?.model, "unknown", 160),
          }))
        : [],
      representativesTruncated: detailsAvailable && item?.representativesTruncated === true,
      modelDistribution: detailsAvailable
        ? modelDistribution(item?.modelDistribution, MAX_CLUSTER_MODELS)
        : [],
      modelDistributionTruncated: detailsAvailable && item?.modelDistributionTruncated === true,
      conversationReadiness: conversationReadiness(item?.conversationReadiness),
      warnings: detailsAvailable
        ? list(item?.warnings, "cluster warnings")
          .slice(0, MAX_WARNINGS)
          .map((warning) => text(warning, "unknown", 80))
        : [],
    };
  });
  result.assignments = list(payload.assignments, "assignments").slice(0, MAX_ASSIGNMENTS).map((item) => ({
    traceId: text(item?.traceId, "unknown", 256),
    origin: ["fit", "incremental"].includes(item?.origin) ? item.origin : "unknown",
    status: ["assigned", "outlier", "ineligible"].includes(item?.status) ? item.status : "unknown",
    clusterId: nullableText(item?.clusterId, 64),
    clusterKind: nullableText(item?.clusterKind, 16),
    reason: nullableText(item?.reason, 80),
    distance: finite(item?.distance),
    assignedAt: text(item?.assignedAt, "", 64),
  }));
  result.reasons = list(payload.reasons, "reasons").slice(0, MAX_REASONS).map((item) => ({
    status: text(item?.status, "unknown", 20),
    reason: text(item?.reason, "unknown", 80),
    count: count(item?.count),
  }));
  result.events = list(payload.events, "events").slice(0, MAX_EVENTS).map((item) => ({
    eventId: text(item?.eventId, "unknown", 64),
    action: text(item?.action, "unknown", 40),
    createdAt: text(item?.createdAt, "", 64),
    actor: text(item?.actor, "", 256),
    pointerGeneration: item?.pointerGeneration == null ? null : count(item.pointerGeneration),
  }));
  return result;
}

const REASONS = {
  distance: "Outside every semantic cluster assignment radius.",
  explicit_key_not_in_version: "The explicit intent key is not present in this immutable version.",
  semantic_fit_too_small: "The semantic fallback did not have enough fit evidence.",
  invalid_workload: "The stored workload identifier is invalid.",
  unsafe_workload: "The workload identifier is not safe at the redaction boundary.",
  missing_intent_key: "No explicit intent key was captured.",
  invalid_intent_key: "The captured explicit intent key is invalid.",
  unsafe_intent_key: "The explicit intent key is not safe at the redaction boundary.",
  content_not_captured: "Semantic content was not captured for this trace.",
  raw_messages_oversize: "Captured messages exceeded the semantic analysis bound.",
  malformed_messages: "Captured messages did not match the supported role-aware shape.",
  no_supported_user_text: "No supported user text was available for semantic assignment.",
  text_too_short: "The selected user text was too short.",
  text_too_long: "The selected user text was too long.",
  redaction_error: "The last semantic redaction boundary failed closed.",
};

export function assignmentExplanation(assignment, clusters) {
  if (assignment.status === "assigned") {
    const cluster = clusters.find((item) => item.clusterId === assignment.clusterId);
    if (assignment.clusterKind === "explicit") {
      return cluster?.explicitKey
        ? `Exact explicit key match: ${cluster.explicitKey}.`
        : "Exact explicit key match.";
    }
    const distance = assignment.distance == null ? "unknown" : assignment.distance.toFixed(3);
    const radius = cluster?.radius == null ? "unknown" : cluster.radius.toFixed(3);
    return `Nearest semantic centroid distance ${distance}; version radius ${radius}.`;
  }
  return REASONS[assignment.reason] || "The immutable version recorded this trace as unavailable for clustering.";
}
