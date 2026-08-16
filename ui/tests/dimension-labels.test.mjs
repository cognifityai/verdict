import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { dimensionAxisLabel, dimensionLabel } from "../dimension-labels.js";
import { providerPresentation } from "../provider-presentation.js";

test("formats built-in rubric dimensions", () => {
  assert.equal(dimensionLabel("groundedness"), "Groundedness");
  assert.equal(dimensionAxisLabel("instruction_following"), "Instruction");
});

test("formats custom rubric dimensions instead of crashing", () => {
  assert.equal(dimensionLabel("action_correctness"), "Action correctness");
  assert.equal(dimensionAxisLabel("tool-selection"), "Tool selection");
});

test("focused chart derives series from the active rubric instead of fixed dimensions", () => {
  const source = readFileSync(new URL("../VerdictUI.jsx", import.meta.url), "utf8");
  const driftSource = source.slice(
    source.indexOf("function Drift()"),
    source.indexOf("function Stat("),
  );
  assert.match(
    driftSource,
    /const dimensionSeries = DATA\.dimensionOverall\.map/,
  );
  assert.doesNotMatch(driftSource, /\[\["completeness"[\s\S]*\]\]\.map/);
  assert.match(driftSource, /ReferenceArea x1=\{DATA\.meta\.regressionHour\}/);
  assert.match(driftSource, /ReferenceLine x=\{DATA\.meta\.regressionHour\}/);
  assert.doesNotMatch(driftSource, /Reference(?:Area|Line) x1?=\{4\}/);
});

test("judge page renders the server's evaluation coverage counts", () => {
  const source = readFileSync(new URL("../VerdictUI.jsx", import.meta.url), "utf8");
  const judgeSource = source.slice(
    source.indexOf("function Judge()"),
    source.indexOf("function Compare()"),
  );
  assert.match(judgeSource, /DATA\.scoreCoverage/);
  for (const label of ["PASS", "FAIL", "UNCLEAR", "Missing", "Errors", "Evaluable"]) {
    assert.match(judgeSource, new RegExp(label));
  }
});

test("handles malformed dimension names defensively", () => {
  assert.equal(dimensionLabel(undefined), "Unknown dimension");
  assert.equal(dimensionLabel("  "), "Unknown dimension");
});

test("all dashboard views use guarded provider presentation", () => {
  const source = readFileSync(new URL("../VerdictUI.jsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /PROV\[[^\]\n]+\]\.(?:color|label|short)/);
});

test("provider presentation supports built-in and custom providers", () => {
  assert.equal(providerPresentation("openai").short, "OpenAI");

  const custom = providerPresentation("custom-gateway", "vendor/model:beta");
  assert.equal(custom.short, "custom-gateway");
  assert.equal(custom.label, "custom-gateway · vendor/model:beta");
  assert.equal(custom.raw, "custom-gateway");
  assert.equal(custom.color, providerPresentation("custom-gateway").color);
});

test("provider and model remain distinct in presentation labels", () => {
  assert.equal(
    providerPresentation("openai", "gpt-4o-mini", "GPT-4o-mini").label,
    "OpenAI · GPT-4o-mini",
  );
});

test("trace filtering recomputes when the evaluator replaces live DATA", () => {
  const source = readFileSync(new URL("../VerdictUI.jsx", import.meta.url), "utf8");
  const tracesSource = source.slice(
    source.indexOf("function Traces()"),
    source.indexOf("function TraceDetail("),
  );
  assert.doesNotMatch(tracesSource, /useMemo\(\(\) => DATA\.samples\.filter/);
});

test("provider presentation handles malformed and unusual extension values", () => {
  for (const value of [undefined, null, "", "   ", "vendor/模型:beta", "x".repeat(300)]) {
    const presentation = providerPresentation(value, null);
    assert.equal(typeof presentation.color, "string");
    assert.ok(presentation.short.length > 0);
    assert.ok(presentation.label.length > 0);
    assert.equal(presentation.raw, value);
  }
});
