import assert from "node:assert/strict";
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

test("handles malformed dimension names defensively", () => {
  assert.equal(dimensionLabel(undefined), "Unknown dimension");
  assert.equal(dimensionLabel("  "), "Unknown dimension");
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

test("provider presentation handles malformed and unusual extension values", () => {
  for (const value of [undefined, null, "", "   ", "vendor/模型:beta", "x".repeat(300)]) {
    const presentation = providerPresentation(value, null);
    assert.equal(typeof presentation.color, "string");
    assert.ok(presentation.short.length > 0);
    assert.ok(presentation.label.length > 0);
    assert.equal(presentation.raw, value);
  }
});
