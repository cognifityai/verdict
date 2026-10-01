import assert from "node:assert/strict";
import test from "node:test";

import {
  agentEvidenceValue,
  agentTurnListPriceValue,
  datasetActivitySummary,
  datasetEvidenceRows,
  failureCountValue,
  sourceListPriceValue,
  sourceTokenValue,
} from "../insights-data.mjs";

test("trace-only evidence health uses trace prompts and responses", () => {
  const rows = Object.fromEntries(datasetEvidenceRows({
    counts: { runs: 0, turns: 0, events: 0 },
    promptStates: {},
    responseStates: {},
    traceEvidence: {
      promptPresent: 358,
      responsePresent: 337,
      judgeEligible: 337,
      notEvaluable: 21,
      notEvaluableReasons: { response_not_captured: 21 },
    },
  }, { traces: { analyzed: 358 } }));

  assert.equal(rows["Trace prompt evidence"], "358 present");
  assert.equal(rows["Trace response evidence"], "337 present · 21 absent");
  assert.equal(rows["Trace judge eligibility"], "337 eligible · 21 unavailable");
  assert.equal(rows["Agent-turn prompt evidence"], "Not available — no Agent Runs captured");
  assert.equal(rows["Agent-turn response evidence"], "Not available — no Agent Runs captured");
  assert.equal(
    agentEvidenceValue({ counts: { runs: 0 } }, 0),
    "Not available — no Agent Runs captured",
  );
});

test("agent evidence reports observed zero only when Agent Runs exist", () => {
  assert.equal(agentEvidenceValue({ counts: { runs: 2 } }, 0), 0);
});

test("trace-only activity does not present unavailable Agent Event evidence as zero", () => {
  assert.deepEqual(datasetActivitySummary({
    counts: { runs: 0, events: 0 },
    traceLinks: { modelCalls: 0, linked: 0, unlinked: 0 },
  }, { modelCalls: 20, toolCalls: 0 }), {
    activityLabel: "LLM calls",
    activityValue: 20,
    activityDetail: "Trace-derived · Agent Events unavailable",
    linkLabel: "Agent-to-trace links",
    linkValue: "Not available",
    linkDetail: "No Agent Runs captured",
  });
});

test("Agent Run activity reports normalized events and observed trace links", () => {
  assert.deepEqual(datasetActivitySummary({
    counts: { runs: 3, events: 12 },
    traceLinks: { modelCalls: 5, linked: 4, unlinked: 1 },
  }, { modelCalls: 8, toolCalls: 2 }), {
    activityLabel: "Normalized events",
    activityValue: 12,
    activityDetail: "5 model · 2 tool calls",
    linkLabel: "Model-call trace links",
    linkValue: "4/5",
    linkDetail: "1 unlinked",
  });
});

test("source token activity distinguishes unavailable, reported zero, and partial", () => {
  assert.equal(sourceTokenValue({ tokenUsageState: "not_captured", totalTokens: null }), "Not captured");
  assert.equal(sourceTokenValue({ tokenUsageState: "complete", totalTokens: 0 }), "0");
  assert.equal(sourceTokenValue({ tokenUsageState: "complete", totalTokens: 1200 }), "1,200");
  assert.equal(sourceTokenValue({ tokenUsageState: "partial", totalTokens: 1200 }), "1,200 (partial)");
  assert.equal(
    sourceTokenValue({ tokenUsageState: "partial", totalTokens: null }),
    "Components captured (partial)",
  );
});

test("a zero failure count needs at least one reported outcome", () => {
  assert.equal(failureCountValue(0, 0), "Not reported by source");
  assert.equal(failureCountValue(0, 0, "—"), "—");
  assert.equal(failureCountValue(0, 12), 0);
  assert.equal(failureCountValue(3, 12), 3);
  // Snapshots written before outcome counts existed keep their stored value.
  assert.equal(failureCountValue(0, undefined), 0);
  assert.equal(failureCountValue(4, null), 4);
});

test("the agent-turn list price says what it is and how much it covers", () => {
  assert.equal(agentTurnListPriceValue({}), "Not available");
  assert.equal(agentTurnListPriceValue({ agentTurnListPriceUsd: null, agentTurnsPriced: 0, agentTurnsUnpriced: 0 }), "Not available");
  assert.equal(agentTurnListPriceValue({ agentTurnListPriceUsd: null, agentTurnsPriced: 0, agentTurnsUnpriced: 7 }),
    "Not priced — model or token basis unknown");
  assert.equal(agentTurnListPriceValue({ agentTurnListPriceUsd: 12.3456, agentTurnsPriced: 40, agentTurnsUnpriced: 2 }),
    "$12.35 list price · 40 of 42 turns priced");
  assert.equal(sourceListPriceValue({ listPriceUsd: null, pricedTurns: 0, turns: 3 }), "—");
  assert.equal(sourceListPriceValue({ listPriceUsd: 0.5, pricedTurns: 2, turns: 3 }), "$0.50 (2/3 turns)");
});
