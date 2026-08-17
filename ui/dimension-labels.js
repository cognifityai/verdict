export const DIM_LABEL = {
  groundedness: "Groundedness",
  relevance: "Relevance",
  completeness: "Completeness",
  safety: "Safety",
  instruction_following: "Instruction-following",
};

export function dimensionLabel(value) {
  if (typeof value !== "string") return "Unknown dimension";

  const key = value.trim();
  if (!key) return "Unknown dimension";
  if (DIM_LABEL[key]) return DIM_LABEL[key];

  const readable = key.replace(/[_-]+/g, " ").replace(/\s+/g, " ");
  return readable.charAt(0).toUpperCase() + readable.slice(1);
}

export function dimensionAxisLabel(value) {
  const label = dimensionLabel(value);
  return label === "Instruction-following" ? "Instruction" : label;
}
