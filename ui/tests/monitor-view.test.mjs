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
