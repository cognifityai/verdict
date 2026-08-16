const BUILTIN_PROVIDERS = {
  anthropic: { color: "#f39a62", label: "Anthropic", short: "Anthropic" },
  openai: { color: "#4ee1aa", label: "OpenAI", short: "OpenAI" },
  google: { color: "#82aaff", label: "Google", short: "Google" },
};

const FALLBACK_COLORS = ["#b8c0cc", "#b39ddb", "#80cbc4", "#ffcc80", "#90caf9", "#c5e1a5"];

function displayValue(value, fallback) {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (value !== null && value !== undefined && String(value).trim()) return String(value).trim();
  return fallback;
}

function compact(value, limit = 32) {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`;
}

function stableColor(value) {
  let hash = 2166136261;
  for (const character of value) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return FALLBACK_COLORS[(hash >>> 0) % FALLBACK_COLORS.length];
}

export function providerPresentation(provider, model = "", preferredLabel = "") {
  const raw = provider;
  const providerText = displayValue(provider, "Unknown provider");
  const modelText = displayValue(model, "Unknown model");
  const key = providerText.toLowerCase();
  const builtin = BUILTIN_PROVIDERS[key];
  const providerLabel = builtin?.label || providerText;
  const modelLabel = displayValue(
    preferredLabel,
    modelText,
  );
  const label = modelLabel === "Unknown model" || modelLabel === providerLabel
    ? providerLabel
    : `${providerLabel} · ${modelLabel}`;
  return {
    raw,
    modelRaw: model,
    color: builtin?.color || stableColor(providerText),
    icon: "●",
    label,
    short: builtin?.short || compact(providerText),
  };
}
