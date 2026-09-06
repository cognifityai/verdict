const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function timelineTick(start, elapsedHours) {
  const origin = new Date(start);
  if (!Number.isFinite(origin.getTime()) || !Number.isFinite(elapsedHours)) return `${elapsedHours}h`;
  const date = new Date(origin.getTime() + elapsedHours * 60 * 60 * 1000);
  return `${MONTHS[date.getUTCMonth()]} ${date.getUTCDate()}, ${date.getUTCFullYear()} ${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}`;
}

export function formatLatency(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "Unavailable";
  if (seconds > 0 && seconds < 1) return `${Math.round(seconds * 10000) / 10} ms`;
  return `${Math.round(seconds * 100) / 100} s`;
}

export function formatCaptureRange(start, end, hours) {
  const first = new Date(start); const last = new Date(end);
  if (!Number.isFinite(first.getTime()) || !Number.isFinite(last.getTime())) return `${hours} hour window`;
  return `${MONTHS[first.getUTCMonth()]} ${first.getUTCDate()}, ${first.getUTCFullYear()} – ${MONTHS[last.getUTCMonth()]} ${last.getUTCDate()}, ${last.getUTCFullYear()}`;
}
