import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import * as esbuild from "esbuild";

async function renderSetupWizard(agentSummary) {
  const built = await esbuild.build({
    stdin: {
      contents: `
        import React from "react";
        import { renderToStaticMarkup } from "react-dom/server";
        import { SetupWizard } from "./SetupWizard.jsx";
        export default (summary) => renderToStaticMarkup(React.createElement(SetupWizard, {
          configUrl: "/api/config", agentSummary: summary,
        }));
      `,
      resolveDir: new URL("..", import.meta.url).pathname,
    },
    bundle: true,
    format: "cjs",
    platform: "node",
    write: false,
    jsx: "automatic",
  });
  const module = { exports: {} };
  Function("require", "module", "exports", built.outputFiles[0].text)(
    createRequire(import.meta.url), module, module.exports,
  );
  return module.exports.default(agentSummary);
}

test("zero tenant-visible records do not claim that the shared store is empty", async () => {
  const html = await renderSetupWizard({
    storageBackend: "sqlite",
    totalAgentRuns: 0,
    totalTraces: 0,
  });

  assert.match(html, /Connected SQLite store/);
  assert.match(html, /No Agent Runs or LLM Traces are visible for this tenant/);
  assert.doesNotMatch(html, /Empty SQLite store|are stored yet/);
});

test("nonempty and malformed metadata keep their existing source workflows", async () => {
  const observed = await renderSetupWizard({
    storageBackend: "sqlite",
    totalAgentRuns: 0,
    totalTraces: 1,
  });
  assert.match(observed, /OBSERVED DATA SOURCES/);
  assert.doesNotMatch(observed, /No Agent Runs or LLM Traces are visible/);

  const malformed = await renderSetupWizard({
    storageBackend: "sqlite",
    totalAgentRuns: "0",
    totalTraces: 0,
  });
  assert.match(malformed, /What do you want to analyze/);
  assert.doesNotMatch(malformed, /CONNECTED VERDICT STORE/);
});
