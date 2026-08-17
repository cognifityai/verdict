"""The Verdict Inspect analysis pipeline.

Given a list of `ParsedConversation`s (from any format parser), this runs:

  1. Structural metrics per temporal window
  2. Semantic drift across windows (Layer 1 — SemanticDriftDetector)
  3. Judge sample on stride-sampled turns from each window (Layer 3-ish)

The result is an `InspectReport` that the report formatter renders into
either Markdown or JSON.

Factored as a library so it can be reused by the verdict-inspect CLI and by
anyone embedding the engine into their own tool.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from verdict_eval.structural import (
    count_hedges as _count_hedges,
)
from verdict_eval.structural import (
    is_apology_start as _is_apology_start,
)
from verdict_eval.structural import (
    is_refusal as _is_refusal,
)

from verdict_inspect.parsers.base import ParsedConversation, ParsedTurn, flatten

# Default thresholds — these are configurable on `run_inspect`
MIN_TURN_WORDS = 10
MIN_TURNS_PER_WINDOW = 8
# 25 per window gives ±~10pp CIs on the judge pass-rate; n=6 was too small to
# read deltas. Still cheap (~75 judge calls per 3-window run).
JUDGE_SAMPLE_PER_WINDOW = 25


# Structural Layer 2 metrics live in verdict_eval.structural — that module
# owns the canonical pattern libraries and per-window summary logic so the
# same checks are usable from outside this pipeline (e.g. the streaming SDK).


@dataclass
class Window:
    name: str
    turns: list[ParsedTurn] = field(default_factory=list)


@dataclass
class StructuralRow:
    window: str
    n_turns: int
    mean_words: float
    hedge_density: float
    refusal_rate: float
    apology_rate: float


@dataclass
class SemanticDriftRow:
    comparison: str
    triggered: bool
    centroid_distance: float
    p_value: float
    mean_wasserstein: float
    max_wasserstein: float
    cluster_psi: float
    n_current: int
    n_baseline: int
    recommended_action: str = ""


@dataclass
class JudgeRow:
    window: str
    n_judged: int
    # value is None when the judge marked the dimension UNCLEAR for every
    # sampled turn (e.g. groundedness with no retrieved context)
    pass_rate: dict[str, float | None]


@dataclass
class InspectReport:
    format_name: str
    n_conversations: int
    n_turns_total: int
    n_turns_substantive: int
    embedder_name: str
    judge_model: str | None
    structural: list[StructuralRow]
    semantic_drift: list[SemanticDriftRow]
    judge: list[JudgeRow]
    judge_deltas_vs_early: dict[str, dict[str, float]]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Structural — Layer 2
# --------------------------------------------------------------------------- #

def _structural_for_window(window: Window) -> StructuralRow:
    if not window.turns:
        return StructuralRow(window=window.name, n_turns=0, mean_words=0.0,
                             hedge_density=0.0, refusal_rate=0.0, apology_rate=0.0)
    n_words = [len(t.assistant_text.split()) for t in window.turns]
    hedge = [_count_hedges(t.assistant_text) for t in window.turns]
    refusals = sum(1 for t in window.turns if _is_refusal(t.assistant_text))
    apologies = sum(1 for t in window.turns if _is_apology_start(t.assistant_text))
    return StructuralRow(
        window=window.name,
        n_turns=len(window.turns),
        mean_words=sum(n_words) / len(window.turns),
        hedge_density=sum(hedge) / len(window.turns),
        refusal_rate=refusals / len(window.turns),
        apology_rate=apologies / len(window.turns),
    )


# --------------------------------------------------------------------------- #
# Windowing
# --------------------------------------------------------------------------- #

def _split_windows(turns: list[ParsedTurn], n_windows: int = 3) -> list[Window]:
    if len(turns) < n_windows * MIN_TURNS_PER_WINDOW:
        n_windows = 2
    if len(turns) < n_windows * MIN_TURNS_PER_WINDOW:
        n_windows = 1
    chunk = max(1, len(turns) // n_windows)
    labels = {3: ["early", "middle", "late"], 2: ["early", "late"], 1: ["whole"]}[n_windows]
    out: list[Window] = []
    for i, label in enumerate(labels):
        lo = i * chunk
        hi = (i + 1) * chunk if i < n_windows - 1 else len(turns)
        out.append(Window(name=label, turns=turns[lo:hi]))
    return out


# --------------------------------------------------------------------------- #
# Semantic drift — Layer 1
# --------------------------------------------------------------------------- #

def _make_embedder() -> tuple[object, str]:
    # SentenceTransformerEmbedder can fail two ways: ImportError (the dep isn't
    # installed) OR a runtime error at construction (model not cached and the
    # network is down, torch/CUDA mismatch, corrupt cache, etc.). Both must fall
    # back to the stateless HashingEmbedder, so catch Exception, not just
    # ImportError. HashingEmbedder is stateless with a fixed dim=128 and takes
    # NO constructor args.
    try:
        from verdict_eval.clustering import SentenceTransformerEmbedder
        return SentenceTransformerEmbedder(), "sentence-transformers/all-MiniLM-L6-v2"
    except Exception as e:
        from verdict_eval.clustering import HashingEmbedder
        print(f"  [embedder] SentenceTransformerEmbedder unavailable ({e}); "
              "falling back to lexical HashingEmbedder (not semantic).")
        return HashingEmbedder(), "HashingEmbedder (lexical fallback; not semantic)"


def _semantic_drift(windows: list[Window], embedder: object) -> list[SemanticDriftRow]:
    """Run SemanticDriftDetector pairwise: each later window vs the early baseline."""
    if len(windows) < 2:
        return []
    from verdict_eval.semantic_drift import SemanticDriftDetector

    detector = SemanticDriftDetector(
        embedder=embedder,
        centroid_distance_threshold=0.10,
        min_sample_size=MIN_TURNS_PER_WINDOW,
    )

    baseline = windows[0]
    baseline_texts = [t.assistant_text for t in baseline.turns]
    rows: list[SemanticDriftRow] = []
    for w in windows[1:]:
        cur_texts = [t.assistant_text for t in w.turns]
        analysis = detector.analyze(
            cluster_id=f"{baseline.name}->{w.name}",
            current_responses=cur_texts,
            baseline_responses=baseline_texts,
        )
        if analysis is None:
            continue
        rows.append(SemanticDriftRow(
            comparison=f"{baseline.name}->{w.name}",
            triggered=analysis.triggered,
            centroid_distance=analysis.centroid_distance,
            p_value=analysis.p_value,
            mean_wasserstein=analysis.mean_wasserstein,
            max_wasserstein=analysis.max_wasserstein,
            cluster_psi=analysis.cluster_psi,
            n_current=analysis.sample_size_current,
            n_baseline=analysis.sample_size_baseline,
            recommended_action=analysis.recommended_action,
        ))
    return rows


# --------------------------------------------------------------------------- #
# Judge — Layer 3
# --------------------------------------------------------------------------- #

def _stride_sample(turns: list[ParsedTurn], k: int) -> list[ParsedTurn]:
    if len(turns) <= k:
        return turns
    step = len(turns) / k
    return [turns[int(i * step)] for i in range(k)]


def _judge_windows(windows: list[Window], judge_model: str) -> tuple[list[JudgeRow], dict[str, dict[str, float]]]:
    try:
        from verdict.metrics import count_scores
        from verdict_eval.judge import DEFAULT_RUBRIC, Judge
        from verdict_eval.providers import AnthropicAdapter
    except ImportError:
        return [], {}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return [], {}

    provider = AnthropicAdapter(api_key=api_key)
    judge = Judge(provider=provider, model=judge_model,
                  rubric=DEFAULT_RUBRIC, temperature=0.0, max_tokens=1024,
                  # Chat exports carry no retrieved context — skip groundedness
                  # rather than score unverifiable claims as failures.
                  skip_context_dependent_when_missing=True)

    dim_names = [d.name for d in DEFAULT_RUBRIC.dimensions]
    rows: list[JudgeRow] = []
    for w in windows:
        sampled = _stride_sample(w.turns, JUDGE_SAMPLE_PER_WINDOW)
        verdicts = {name: [] for name in dim_names}
        judged = 0
        for t in sampled:
            try:
                j = judge.judge(
                    query=t.user_text[:4000],
                    response=t.assistant_text[:4000],
                )
            except Exception:
                continue
            judged += 1
            for d in j.dimensions:
                if d.name in verdicts:
                    verdicts[d.name].append(d.verdict)
            time.sleep(0.2)
        # Pass rate = PASS / (PASS + FAIL); None when no applicable judgments
        rows.append(JudgeRow(
            window=w.name,
            n_judged=judged,
            pass_rate={
                name: count_scores(verdicts[name]).pass_rate
                for name in dim_names
            },
        ))

    deltas: dict[str, dict[str, float]] = {}
    if rows and rows[0].n_judged > 0:
        early = rows[0].pass_rate
        for r in rows[1:]:
            deltas[r.window] = {
                n: (r.pass_rate.get(n) - early.get(n))
                if (r.pass_rate.get(n) is not None and early.get(n) is not None)
                else None
                for n in dim_names
            }
    return rows, deltas


# --------------------------------------------------------------------------- #
# Top-level pipeline
# --------------------------------------------------------------------------- #

def run_inspect(
    conversations: list[ParsedConversation],
    *,
    format_name: str = "unknown",
    judge_model: str = "claude-haiku-4-5-20251001",
    enable_semantic: bool = True,
    enable_judge: bool = True,
) -> InspectReport:
    """Run the full inspect pipeline. Returns an InspectReport."""
    all_turns = flatten(conversations)
    substantive = [t for t in all_turns if len(t.assistant_text.split()) >= MIN_TURN_WORDS]

    windows = _split_windows(substantive)

    notes: list[str] = []
    if not substantive:
        notes.append("No substantive assistant turns found.")
    elif len(substantive) < 2 * MIN_TURNS_PER_WINDOW:
        notes.append(
            f"Only {len(substantive)} substantive turns; need >= "
            f"{2 * MIN_TURNS_PER_WINDOW} for windowed comparisons. "
            "Reporting whole-conversation aggregate only."
        )

    structural_rows = [_structural_for_window(w) for w in windows]

    semantic_rows: list[SemanticDriftRow] = []
    embedder_name = "(skipped)"
    if enable_semantic and len(windows) >= 2 and len(substantive) >= 2 * MIN_TURNS_PER_WINDOW:
        embedder, embedder_name = _make_embedder()
        if "not semantic" in embedder_name:
            notes.append(
                "Semantic model unavailable: embedding drift used the lexical hash "
                "fallback. Do not interpret these rows as semantic similarity."
            )
        semantic_rows = _semantic_drift(windows, embedder)

    judge_rows: list[JudgeRow] = []
    judge_deltas: dict[str, dict[str, float]] = {}
    used_judge_model: str | None = None
    if enable_judge:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            notes.append("Judge layer skipped: ANTHROPIC_API_KEY not set.")
        elif len(substantive) < MIN_TURNS_PER_WINDOW:
            notes.append("Judge layer skipped: not enough turns to sample.")
        else:
            judge_rows, judge_deltas = _judge_windows(windows, judge_model)
            used_judge_model = judge_model
            if not judge_rows:
                notes.append("Judge layer ran but produced no rows.")

    return InspectReport(
        format_name=format_name,
        n_conversations=len(conversations),
        n_turns_total=len(all_turns),
        n_turns_substantive=len(substantive),
        embedder_name=embedder_name,
        judge_model=used_judge_model,
        structural=structural_rows,
        semantic_drift=semantic_rows,
        judge=judge_rows,
        judge_deltas_vs_early=judge_deltas,
        notes=notes,
    )
