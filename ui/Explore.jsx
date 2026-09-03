import React from "react";
import { Monitor } from "./Monitor.jsx";
import { Registry } from "./Registry.jsx";

const panel = { borderColor: "#26332e", background: "#111715" };

export function Explore({ configUrl, registryUrl, operationsUrl, evaluation }) {
  return <div className="space-y-6">
    <section className="border p-5" style={panel}>
      <div className="text-xs font-mono" style={{ color: "#4ee1aa" }}>EXPLORE BEFORE YOU DEPLOY</div>
      <h2 className="text-lg font-semibold mt-1">Choose the simplest defensible comparison</h2>
      <p className="text-sm mt-2" style={{ color: "#94a39d" }}>Start with all eligible genuine model calls and an 80/20 count split. Provider/model is a factual facet. Clusters are optional, experimental for semantic grouping, and must be reviewed through exemplars, coverage, outliers, replay and activation history before a monitor can use them.</p>
      <div className="grid sm:grid-cols-3 gap-3 mt-4 text-sm"><div className="border p-3" style={{ borderColor: "#4ee1aa" }}><strong>No grouping</strong><div className="text-xs mt-1">Recommended default; avoids cluster instability.</div></div><div className="border p-3" style={{ borderColor: "#26332e" }}><strong>Provider / model</strong><div className="text-xs mt-1">Use when deployments differ by factual configuration.</div></div><div className="border p-3" style={{ borderColor: "#26332e" }}><strong>Reviewed clusters</strong><div className="text-xs mt-1">Use only when representatives and holdout behavior make sense.</div></div></div>
    </section>
    <Monitor configUrl={configUrl} evaluation={evaluation} />
    <Registry url={registryUrl} operationsUrl={operationsUrl} configUrl={configUrl} />
  </div>;
}
