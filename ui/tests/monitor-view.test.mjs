import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import * as esbuild from "esbuild";

async function render(component) {
  const built = await esbuild.build({
    stdin: {
      contents: `
        import React from "react";
        import { renderToStaticMarkup } from "react-dom/server";
        import { Monitor, MonitorComparisonMetrics } from "./Monitor.jsx";
        export default renderToStaticMarkup(${component});
      `,
      resolveDir: new URL("..", import.meta.url).pathname,
    },
    bundle: true, format: "cjs", platform: "node", write: false, jsx: "automatic",
  });
  const module = { exports: {} };
  Function("require", "module", "exports", built.outputFiles[0].text)(
    createRequire(import.meta.url), module, module.exports,
  );
  return module.exports.default;
}

test("monitor selects one existing evaluator and names the comparison action", async () => {
  const html = await render(`React.createElement(Monitor, {
    configUrl: "/api/config",
    evaluation: {
      selectedIdentity: { complete: true, fingerprint: "${"a".repeat(64)}" },
      availableIdentities: [{
        complete: true, fingerprint: "${"a".repeat(64)}", label: "judge · quality v1",
      }],
    },
  })`);
  assert.match(html, /Measurement/);
  assert.match(html, /judge · quality v1/);
  assert.match(html, /existing stored judgments; this monitor makes no judge calls/);
  assert.match(html, /Preview comparison/);
  assert.doesNotMatch(html, /Preview candidate/);
});

test("judge comparison renders evaluable and unavailable coverage", async () => {
  const html = await render(`React.createElement(MonitorComparisonMetrics, {
    comparison: {
      metrics: [],
      metric_coverage: [{ metric: "judge.answer_quality.pass",
        reference_evaluable: 10, current_evaluable: 10,
        reference_unclear: 1, current_unclear: 2,
        reference_missing: 3, current_missing: 4,
        reference_error: 5, current_error: 6 }],
    },
  })`);
  assert.match(html, /answer quality pass rate/);
  assert.match(html, /No PASS\/FAIL comparison yet/);
  assert.match(html, /10 → 10 evaluable/);
  assert.match(html, /3 → 4 not judged/);
  assert.match(html, /5 → 6 judge errors/);
});

test("grouped comparison renders the same metric once per group", async () => {
  const html = await render(`React.createElement(MonitorComparisonMetrics, {
    comparison: {
      metrics: [
        { group_id: "openai:model-a", metric: "provider_error", alert: false,
          reference_value: 0, current_value: 0.1, effect: 0.1,
          p_adjusted: 0.2, reference_n: 10, current_n: 10 },
        { group_id: "anthropic:model-b", metric: "provider_error", alert: true,
          reference_value: 0.1, current_value: 0.8, effect: 0.7,
          p_adjusted: 0.01, reference_n: 10, current_n: 10 },
      ],
      metric_coverage: [],
    },
  })`);
  assert.match(html, /Group:.*openai:model-a/);
  assert.match(html, /Group:.*anthropic:model-b/);
  assert.equal((html.match(/Provider error rate/g) || []).length, 2);
});

test("grouped comparison renders reviewed labels instead of opaque identities", async () => {
  const html = await render(`React.createElement(MonitorComparisonMetrics, {
    comparison: {
      groups: [{ group_id: "clu_internal", label: "Billing questions",
        reference_units: 10, current_units: 10 }],
      metrics: [{ group_id: "clu_internal", metric: "provider_error", alert: false,
        reference_value: 0, current_value: 0, effect: 0,
        p_adjusted: 1, reference_n: 10, current_n: 10 }],
      metric_coverage: [],
    },
  })`);
  assert.match(html, /Group:.*Billing questions/);
  assert.doesNotMatch(html, />clu_internal</);
});

test("monitor status shows active authority beside a newer candidate", async () => {
  const response = (state, metric) => ({
    state,
    policy: { prospective_target: 10, grouping_mode: "none" },
    snapshot: {
      manifest: {
        reference_unit_ids: Array(8).fill("r"),
        current_unit_ids: Array(2).fill("c"),
        prospective_open: false,
        comparison_index: 0,
      },
      comparison: {
        status: state === "active" ? "alert" : "no_alert",
        alpha_threshold: 0.05,
        metrics: [{ metric, alert: state === "active",
          reference_value: 0.9, current_value: 0.6, effect: -0.3,
          p_adjusted: 0.01, reference_n: 8, current_n: 2 }],
        metric_coverage: [], groups: [], unseen_group_share: 0,
      },
    },
  });
  const html = await render(`React.createElement(Monitor, {
    configUrl: "/api/config", view: "status",
    initialState: {
      active: ${JSON.stringify(response("active", "provider_error"))},
      candidate: ${JSON.stringify(response("candidate", "response_empty"))},
    },
  })`);
  assert.match(html, /ACTIVE PROSPECTIVE MONITOR/);
  assert.match(html, /Provider error rate/);
  assert.match(html, /EXPLORATORY HISTORICAL COMPARISON/);
  assert.match(html, /Empty-response rate/);
});
