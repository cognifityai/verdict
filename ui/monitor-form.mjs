const dateFields = ["referenceStart", "referenceEnd", "currentStart", "currentEnd"];

export function monitorRequest(form) {
  if (form.windowMode !== "explicit") return { ...form };
  return Object.fromEntries(Object.entries(form).map(([name, value]) => [
    name,
    dateFields.includes(name) && value && Number.isFinite(new Date(value).getTime())
      ? new Date(value).toISOString() : value,
  ]));
}

export function conversationMonitorRequest(form) {
  const values = monitorRequest({ ...form, windowMode: "explicit" });
  return {
    analysisUnit: "conversation",
    referenceStart: values.referenceStart, referenceEnd: values.referenceEnd,
    currentStart: values.currentStart, currentEnd: values.currentEnd,
    evaluatorFingerprint: values.evaluatorFingerprint,
    dimension: values.dimension, labelKey: values.labelKey,
  };
}
