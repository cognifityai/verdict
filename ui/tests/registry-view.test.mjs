import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import * as esbuild from "esbuild";

async function renderRegistry(data, { sourceGroups = null, sourceGroupsTruncated = false } = {}) {
  const component = sourceGroups === null
    ? `React.createElement(RegistryView, {
      data: ${JSON.stringify(data)},
      operations: { available: true, running: null, jobs: [] },
      onRun() {}, onVersion() {}, onPage() {}, onRefresh() {},
    })`
    : `React.createElement(Registry, {
      url: "/api/registry", sourceGroups: ${JSON.stringify(sourceGroups)},
      sourceGroupsTruncated: ${JSON.stringify(sourceGroupsTruncated)},
      onRuns() {},
    })`;
  const source = `
    import React from "react";
    import { renderToStaticMarkup } from "react-dom/server";
    import { Registry, RegistryView } from "./Registry.jsx";
    export default renderToStaticMarkup(${component});
  `;
  const built = await esbuild.build({
    stdin: { contents: source, resolveDir: new URL("..", import.meta.url).pathname },
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
  return module.exports.default;
}

test("an empty registry with no calls does not offer a semantic fit", async () => {
  const html = await renderRegistry({
    schema: "cluster-registry-dashboard-v1",
    status: "empty",
    tenant: "__verdict_local__",
  });

  assert.match(html, /No genuine model calls are captured/);
  assert.doesNotMatch(html, /Analyze historical traces|Semantic clusters/);
});

test("truncated source groups invite browsing captured runs without promising complete history", async () => {
  const html = await renderRegistry(null, {
    sourceGroups: [{ sourceKind: "synthetic-codex", runs: 4 }],
    sourceGroupsTruncated: true,
  });
  assert.match(html, /Only the first source groups are shown/);
  assert.match(html, /Browse captured runs for more/);
  assert.doesNotMatch(html, /full history/);
});
