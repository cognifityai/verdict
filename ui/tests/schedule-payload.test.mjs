import assert from "node:assert/strict";
import test from "node:test";

import { normalizeSchedulePayload } from "../schedule-payload.js";

test("blank schedule roots are omitted while configured paths remain intact", () => {
  assert.deepEqual(normalizeSchedulePayload({
    intervalHours: 24, claudeRoot: "  ", codexRoot: "", analyzeOnly: true,
  }), { intervalHours: 24, analyzeOnly: true });
  assert.deepEqual(normalizeSchedulePayload({
    claudeRoot: "~/history", codexRoot: " /tmp/synthetic-history ", analyzeOnly: true,
  }), { claudeRoot: "~/history", codexRoot: " /tmp/synthetic-history ", analyzeOnly: true });
});
