import assert from "node:assert/strict";
import test from "node:test";

import { normalizeSchedulePayload, schedulePlanKey } from "../schedule-payload.js";

test("blank schedule roots are omitted while configured paths remain intact", () => {
  assert.deepEqual(normalizeSchedulePayload({
    intervalHours: 24, claudeRoot: "  ", codexRoot: "", analyzeOnly: true,
  }), { intervalHours: 24, analyzeOnly: true });
  assert.deepEqual(normalizeSchedulePayload({
    claudeRoot: "~/history", codexRoot: " /tmp/synthetic-history ", analyzeOnly: true,
  }), { claudeRoot: "~/history", codexRoot: " /tmp/synthetic-history ", analyzeOnly: true });
});

test("schedule identity survives server key sorting and blank-root removal", () => {
  const drafted = { intervalHours: 24, claudeRoot: "", codexRoot: "", analyzeOnly: true,
    runMonitor: false, includeApprovedEvaluations: false };
  const returned = { analyzeOnly: true, includeApprovedEvaluations: false,
    intervalHours: 24, runMonitor: false };
  assert.equal(schedulePlanKey(drafted), schedulePlanKey(returned));
  assert.notEqual(schedulePlanKey(drafted), schedulePlanKey({ ...returned, intervalHours: 12 }));
});
