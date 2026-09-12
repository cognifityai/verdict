import React from "react";
import {
  buildManagementReport, managementReportCsv, managementReportHtml,
} from "./management-report.mjs";

const C = {
  panel: "#111715", border: "#26332e", sub: "#94a39d", faint: "#68766f",
  green: "#4ee1aa", amber: "#f2b84b", grid: "#202925",
};

function download(filename, content, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function Card({ label, value, note }) {
  return <div className="border rounded-lg p-4" style={{ borderColor: C.border, background: C.panel }}>
    <div className="text-[11px] uppercase tracking-wide font-mono" style={{ color: C.faint }}>{label}</div>
    <div className="text-2xl font-semibold mt-2 tabular-nums">{value}</div>
    <div className="text-xs mt-1" style={{ color: C.sub }}>{note}</div>
  </div>;
}

function Fact({ label, value }) {
  return <div className="border-t pt-3" style={{ borderColor: C.border }}>
    <div className="text-xs" style={{ color: C.faint }}>{label}</div>
    <div className="text-sm mt-1">{value}</div>
  </div>;
}

function formatNumber(value) {
  return value.toLocaleString("en-US");
}

function formatPercent(value) {
  return value == null ? "—" : `${value.toLocaleString("en-US")}%`;
}

function formatLatency(value) {
  return value == null ? "—" : `${value.toLocaleString("en-US")} ms`;
}

function formatCost(row) {
  if (row.costKnownCalls === 0 || row.costUsd == null) return "—";
  return `$${row.costUsd.toFixed(4)}${row.costKnownCalls < row.calls ? " partial" : ""}`;
}

function VolumeChart({ timeline }) {
  const rows = timeline.rows;
  if (!rows.length) return <div className="text-sm mt-5" style={{ color: C.sub }}>No application activity is available.</div>;
  const max = Math.max(1, ...rows.map((point) => point.calls));
  const half = Math.round(max / 2);
  return <div className="mt-5 overflow-x-auto pb-1" data-report-chart>
    <div className="grid gap-3" style={{ gridTemplateColumns: "36px 1fr", minWidth: Math.max(440, rows.length * 34 + 48) }}>
      <div className="h-44 pb-7 flex flex-col justify-between text-right text-[10px] tabular-nums" style={{ color: C.faint }}>
        <span>{formatNumber(max)}</span><span>{formatNumber(half)}</span><span>0</span>
      </div>
      <div className="h-44 grid items-end border-b relative" style={{ borderColor: C.border, gridTemplateColumns: `repeat(${rows.length}, minmax(26px, 1fr))` }} aria-label="Daily application LLM requests">
        <div className="absolute inset-x-0 top-0 border-t" style={{ borderColor: C.grid }} />
        <div className="absolute inset-x-0 top-[50%] border-t" style={{ borderColor: C.grid }} />
        {rows.map((point) => <div key={point.date} className="h-full px-1 grid relative" style={{ gridTemplateRows: "18px 1fr 28px" }} title={`${point.date}: ${point.calls} application calls`}>
          <b className="text-[10px] text-center tabular-nums z-10" style={{ color: C.green }}>{formatNumber(point.calls)}</b>
          <div className="flex items-end min-h-0">
            <div data-report-bar className="w-full rounded-t-sm" style={{ height: `${Math.max(4, Math.round(point.calls / max * 100))}%`, background: C.green }} />
          </div>
          <span className="text-[9px] text-center whitespace-nowrap pt-1" style={{ color: C.faint }}>{point.label}</span>
        </div>)}
      </div>
    </div>
  </div>;
}

function UtilizationTable({ rows, kind }) {
  const application = kind === "application";
  return <div className="overflow-x-auto mt-4">
    <table className="w-full min-w-[760px] text-sm">
      <thead><tr className="text-xs" style={{ color: C.faint }}>
        <th className="text-left p-2">{application ? "Application / service" : "Model / provider"}</th>
        <th className="text-right p-2">Requests</th><th className="text-right p-2">Success</th>
        <th className="text-right p-2">Tokens</th><th className="text-right p-2">Avg latency</th>
        <th className="text-right p-2">Cost</th>
      </tr></thead>
      <tbody>{rows.length ? rows.map((row) => {
        const name = application ? row.name : row.model;
        const detail = application
          ? [row.environments.join(" · "), row.providers.join(" · "), row.models.join(" · ")].filter(Boolean).join(" | ")
          : row.provider;
        return <tr key={application ? row.name : `${row.provider}:${row.model}`} className="border-t" style={{ borderColor: C.border }}>
          <td className="p-2"><div className="font-medium">{name}</div><div className="text-xs mt-0.5" style={{ color: C.faint }}>{detail}</div></td>
          <td className="text-right p-2 tabular-nums">{formatNumber(row.calls)}</td>
          <td className="text-right p-2 tabular-nums" style={{ color: row.failedCalls ? C.amber : undefined }}>{formatPercent(row.successRatePct)}</td>
          <td className="text-right p-2 tabular-nums">{formatNumber(row.totalTokens)}{row.tokenKnownCalls < row.calls ? <div className="text-[10px]" style={{ color: C.amber }}>partial</div> : null}</td>
          <td className="text-right p-2 tabular-nums">{formatLatency(row.averageLatencyMs)}</td>
          <td className="text-right p-2 tabular-nums">{formatCost(row)}</td>
        </tr>;
      }) : <tr><td className="p-3" colSpan="6" style={{ color: C.sub }}>No data is available.</td></tr>}</tbody>
    </table>
  </div>;
}

export function ManagementReport({ data, source }) {
  const report = buildManagementReport(data, { source });
  const facts = [
    ["Success rate", report.summary.success], ["Estimated cost", report.summary.cost],
    ["Token coverage", report.summary.tokenCoverage], ["Latency coverage", report.summary.latencyCoverage],
    ["First capture", report.summary.firstCapture], ["Latest capture", report.summary.latestCapture],
  ];
  const status = [
    ["Evaluator", report.quality.evaluator], ["Evaluation coverage", report.quality.evaluationCoverage],
    ["Current monitor", report.quality.monitor], ["Deterministic analysis", report.quality.deterministicAnalysis],
    ["Legacy change history", report.quality.legacyChange],
  ];
  return <article className="verdict-report-print space-y-4">
    <header className="border-b pb-5 flex flex-col xl:flex-row xl:items-start justify-between gap-5" style={{ borderColor: C.border }}>
      <div>
        <div className="text-[11px] uppercase tracking-wide font-mono" style={{ color: C.green }}>{report.source}</div>
        <h2 className="text-2xl font-semibold mt-1">{report.title}</h2>
        <div className="text-xs mt-2" style={{ color: C.sub }}>{report.range}</div>
        <div className="text-xs mt-1" style={{ color: C.faint }}>Generated {report.generatedAt}</div>
      </div>
      <div className="verdict-no-print flex flex-wrap gap-2">
        <button className="border rounded px-3 py-2 text-sm" style={{ borderColor: C.green, color: C.green }} onClick={() => download("verdict-management-report.html", managementReportHtml(report), "text/html;charset=utf-8")}>Export HTML</button>
        <button className="border rounded px-3 py-2 text-sm" style={{ borderColor: C.border }} onClick={() => download("verdict-management-report.csv", managementReportCsv(report), "text/csv;charset=utf-8")}>Download CSV</button>
        <button className="border rounded px-3 py-2 text-sm" style={{ borderColor: C.border }} onClick={() => window.print()}>Print / Save PDF</button>
      </div>
    </header>

    <section className="grid sm:grid-cols-2 xl:grid-cols-4 gap-3">
      {report.kpis.map((item) => <Card key={item.label} {...item} />)}
    </section>

    <section className="border rounded-lg p-5 min-w-0" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">LLM request volume</h2>
      <p className="text-xs mt-1" style={{ color: C.sub }}>{report.timeline.scope} · application calls only · UTC</p>
      <VolumeChart timeline={report.timeline} />
    </section>

    <section className="border rounded-lg p-5" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">Report scope</h2>
      <div className="grid sm:grid-cols-2 xl:grid-cols-3 gap-4 mt-4">{facts.map(([label, value]) => <Fact key={label} label={label} value={value} />)}</div>
    </section>

    <section className="border rounded-lg p-5" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">Application-level LLM utilization</h2>
      <p className="text-xs mt-1" style={{ color: C.sub }}>{report.applications.scope}. Historical traces without service identity appear as Unattributed.</p>
      <UtilizationTable rows={report.applications.rows} kind="application" />
    </section>

    <section className="border rounded-lg p-5" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">Model performance and throughput</h2>
      <p className="text-xs mt-1" style={{ color: C.sub }}>{report.models.scope}. Totals retain partial-coverage labels.</p>
      <UtilizationTable rows={report.models.rows} kind="model" />
    </section>

    <section className="border rounded-lg p-5" style={{ borderColor: C.border, background: C.panel }}>
      <h2 className="font-semibold">Verdict status</h2>
      <p className="text-xs mt-1" style={{ color: C.sub }}>Evaluation and change evidence is reported separately from operational utilization.</p>
      <div className="grid sm:grid-cols-2 xl:grid-cols-3 gap-4 mt-4">{status.map(([label, value]) => <Fact key={label} label={label} value={value} />)}</div>
    </section>

    <section className="border rounded-lg p-5" style={{ borderColor: C.amber, background: C.panel }}>
      <div className="text-[11px] tracking-wide font-mono" style={{ color: C.amber }}>MANAGEMENT ATTENTION</div>
      <ul className="mt-3 space-y-2 text-sm list-disc pl-5">{report.attention.map((item) => <li key={item}>{item}</li>)}</ul>
    </section>
  </article>;
}
