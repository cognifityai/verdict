"""Format an InspectReport as Markdown or JSON."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime

from verdict_inspect.pipeline import InspectReport


def to_markdown(report: InspectReport) -> str:
    lines: list[str] = []
    lines.append("# Verdict Inspect report")
    lines.append("")
    lines.append(f"Generated: {datetime.utcnow().isoformat()}Z  ")
    lines.append(f"Format: `{report.format_name}`  ")
    lines.append(f"Conversations: **{report.n_conversations}**  ")
    lines.append(f"Total assistant turns: {report.n_turns_total}  ")
    lines.append(f"Substantive turns analyzed: **{report.n_turns_substantive}**  ")
    if report.embedder_name != "(skipped)":
        lines.append(f"Embedder: `{report.embedder_name}`  ")
    if report.judge_model:
        lines.append(f"Judge: `{report.judge_model}`")
    lines.append("")

    if report.notes:
        lines.append("## Notes")
        for n in report.notes:
            lines.append(f"- {n}")
        lines.append("")

    # Structural
    lines.append("## Structural metrics by window")
    lines.append("")
    if not report.structural:
        lines.append("_No structural metrics computed (no turns)._")
    else:
        lines.append("| window | n_turns | mean words/turn | hedge density | refusal rate | apology rate |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for r in report.structural:
            lines.append(
                f"| {r.window} | {r.n_turns} | {r.mean_words:.1f} | "
                f"{r.hedge_density:.2f} | {r.refusal_rate:.2%} | {r.apology_rate:.2%} |"
            )
    lines.append("")

    # Semantic drift
    lines.append("## Semantic drift")
    lines.append("")
    if not report.semantic_drift:
        lines.append("_Not computed (not enough turns or embedder unavailable)._")
    else:
        lines.append("| comparison | drift? | effect (centroid dist) | p-value | max_wasserstein | psi | n_cur | n_base |")
        lines.append("|---|:---:|---:|---:|---:|---:|---:|---:|")
        for s in report.semantic_drift:
            import math
            mark = "**YES**" if s.triggered else "no"
            pval = "n/a" if (s.p_value is None or math.isnan(s.p_value)) else f"{s.p_value:.3f}"
            lines.append(
                f"| {s.comparison} | {mark} | {s.centroid_distance:.4f} | {pval} | "
                f"{s.max_wasserstein:.4f} | {s.cluster_psi:.4f} | "
                f"{s.n_current} | {s.n_baseline} |"
            )
        lines.append("")
        lines.append("_Drift fires only when the shift is both statistically significant "
                     "(permutation p < 0.05) and larger than the effect-size floor. "
                     "It is informational — cross-reference judge/structural signals before "
                     "treating it as a regression._")
        for s in report.semantic_drift:
            if s.triggered and s.recommended_action:
                lines.append("")
                lines.append(f"- {s.comparison}: {s.recommended_action}")
    lines.append("")

    # Judge
    lines.append("## Judge sample (LLM-as-judge)")
    lines.append("")
    if not report.judge:
        lines.append("_Judge layer not run (see Notes above)._")
    else:
        all_dims: list[str] = []
        for r in report.judge:
            for d in r.pass_rate:
                if d not in all_dims:
                    all_dims.append(d)
        def _pct(v: object) -> str:
            return "n/a" if v is None else f"{v:.2%}"

        header_cells = " | ".join(all_dims)
        align_cells = "---:|" * len(all_dims)
        lines.append(f"| window | n_judged | {header_cells} |")
        lines.append(f"|---|---:|{align_cells}")
        for r in report.judge:
            cells = " | ".join(_pct(r.pass_rate.get(d)) for d in all_dims)
            lines.append(f"| {r.window} | {r.n_judged} | {cells} |")
        lines.append("")
        lines.append("_Pass rate = PASS / (PASS + FAIL); `n/a` = judge marked the "
                     "dimension UNCLEAR (e.g. groundedness with no retrieved context)._")
        if report.judge_deltas_vs_early:
            lines.append("")
            lines.append("### Delta vs early window (negative = degradation)")
            lines.append("")
            for wname, deltas in report.judge_deltas_vs_early.items():
                items = ", ".join(
                    (f"{k}={v:+.2%}" if v is not None else f"{k}=n/a")
                    for k, v in deltas.items()
                )
                lines.append(f"- **{wname}**: {items}")
    lines.append("")

    return "\n".join(lines)


def to_json(report: InspectReport) -> str:
    return json.dumps(asdict(report), indent=2, default=str)
