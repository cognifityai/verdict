const dateFields = ["referenceStart", "referenceEnd", "currentStart", "currentEnd"];

export function monitorRequest(form) {
  if (form.windowMode !== "explicit") return { ...form };
  return Object.fromEntries(Object.entries(form).map(([name, value]) => [
    name,
    dateFields.includes(name) && value ? new Date(value).toISOString() : value,
  ]));
}
