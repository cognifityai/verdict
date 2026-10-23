export function normalizeSchedulePayload(schedule) {
  const payload = { ...schedule };
  for (const key of ["claudeRoot", "codexRoot"]) {
    if (typeof payload[key] === "string" && !payload[key].trim()) delete payload[key];
  }
  return payload;
}

export function schedulePlanKey(schedule) {
  return JSON.stringify(Object.entries(normalizeSchedulePayload(schedule))
    .sort(([left], [right]) => left.localeCompare(right)));
}
