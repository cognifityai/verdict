import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import * as esbuild from "esbuild";

async function renderRegistry(data) {
  const source = `
    import React from "react";
    import { renderToStaticMarkup } from "react-dom/server";
    import { RegistryView } from "./Registry.jsx";
    export default renderToStaticMarkup(React.createElement(RegistryView, {
      data: ${JSON.stringify(data)},
      operations: { available: true, running: null, jobs: [] },
      onRun() {}, onVersion() {}, onPage() {}, onRefresh() {},
    }));
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

test("an empty registry starts with the historical semantic analysis workflow", async () => {
  const html = await renderRegistry({
    schema: "cluster-registry-dashboard-v1",
    status: "empty",
    tenant: "__verdict_local__",
  });

  assert.match(html, /Create clusters from historical traces/);
  assert.match(html, /Analyze historical traces/);
  assert.match(html, /Semantic clusters/);
});
