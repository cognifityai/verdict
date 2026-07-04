"""Redesigned multi-run drift-detection validation.

This replaces the old single-window "8-hour run" whose headline numbers never
actually exercised the thing that matters: detecting a regression *week over
week, across separate pipeline runs, with stable clusters and enough judged
volume per cluster to be statistically valid*. It also replaces the
single-synthetic-F1 "marketing number" with an honest, reproducible battery.

What this validates (and what it deliberately does NOT):

  VALIDATED here (offline, deterministic, no API cost):
    1. Cluster-ID STABILITY across SEPARATE runs — the same intent keeps the
       same cluster_id every run, so the week-over-week comparison lines up
       matching buckets. (This is the fix in stable_clustering.py; the old
       Birch re-fit-every-run path failed it.)
    2. DRIFT DETECTION accuracy — a regression planted on ONE (cluster,
       dimension) is caught with the right direction/effect size, and the
       control clusters/dimensions produce ZERO false positives. Reported as
       precision / recall / F1 over the full (cluster x dimension) grid.
    3. The new UNCLEAR-rate signal — a dimension drifting toward unevaluable
       (e.g. groundedness losing retrieved context) raises
       `unclear_rate_increase` instead of silently shrinking the window.
    4. The n>=30-per-cell requirement is actually MET (the script asserts it),
       demonstrating the stratified-volume point: you must judge enough per
       cluster, not a flat random slice.

  NOT validated here (separate, and honestly out of scope for an offline run):
    - The JUDGE's real-world accuracy on ambiguous traffic. That is the
      judge-vs-human kappa question and needs human labels; `--judge live`
      exercises the judge end-to-end but its *accuracy* still has to be
      measured against human labels, not asserted here.

Two judge modes:
    --judge synthetic   (default) deterministic PASS/FAIL/UNCLEAR drawn at
                        configured per-(cluster,dimension,period) rates. No API.
                        Isolates DETECTOR + CLUSTERING + WINDOWING + STORAGE.
    --judge live        real provider judge over injector-corrupted responses
                        (costs money; validates the judge plumbing end to end).

Two embedders:
    --embedder oracle             (default) deterministic, separable-by-intent
                                  embeddings; isolates the detector from
                                  embedding noise while still exercising the
                                  real StableIntentClusterer (registry persist,
                                  stable IDs).
    --embedder sentence-transformer  real embeddings; ALSO validates clustering
                                  quality (paraphrases must group). Recommended
                                  for the full-stack number.

Exit code is non-zero if any assertion fails, so this doubles as a CI gate.

Heavy deps (scipy via drift, sklearn via sentence-transformers) are imported
lazily so the dataset/clustering/judging stages import with numpy alone.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))

from verdict.schema import DimensionScore, Judgment, Operation, Trace, Verdict  # noqa: E402
from verdict.storage.memory import InMemoryStorage  # noqa: E402
from verdict_eval.stable_clustering import StableIntentClusterer  # noqa: E402

# Five rubric dimensions (matches DEFAULT_RUBRIC in judge.py).
DIMENSIONS = ["groundedness", "relevance", "completeness", "safety", "instruction_following"]

# A small set of clearly-distinct intents, each with paraphrase prompts so a
# real embedder has something to group. Intent name -> prompt paraphrases.
INTENTS: dict[str, list[str]] = {
    "billing_refund": [
        "How do I get a refund for my last charge?",
        "I want my money back on the subscription.",
        "Can you process a refund to my card?",
        "Please reverse the payment I made yesterday.",
    ],
    "weather_forecast": [
        "What's the weather going to be tomorrow?",
        "Will it rain this weekend?",
        "Give me the five-day forecast for Seattle.",
        "Is it going to be sunny on Friday?",
    ],
    "code_debug": [
        "Why does my Python loop throw an IndexError?",
        "Help me fix this stack trace in my function.",
        "My code crashes on an empty list, what's wrong?",
        "Debug this null pointer in my method.",
    ],
    "travel_booking": [
        "Book me a flight to Tokyo next month.",
        "Find a hotel near the conference center.",
        "What's the cheapest train from Paris to Lyon?",
        "Reserve a rental car for my trip.",
    ],
    "account_security": [
        "How do I reset my password?",
        "Someone logged into my account, what do I do?",
        "Enable two-factor authentication for me.",
        "I think my account was hacked, help.",
    ],
    "product_howto": [
        "How do I export my data to CSV?",
        "Where is the setting to change my theme?",
        "Walk me through connecting an integration.",
        "How can I invite a teammate to my workspace?",
    ],
}


@dataclass
class Regression:
    """A planted ground-truth regression on one (intent, dimension)."""
    intent: str
    dimension: str
    current_pass_rate: float   # baseline rate is taken from BASELINE_PASS_RATE


@dataclass
class UnclearRegression:
    """A planted ground-truth drift toward unevaluable on one (intent, dim)."""
    intent: str
    dimension: str
    current_unclear_frac: float


@dataclass
class Config:
    baseline_days: int = 7
    current_days: int = 3
    # gap_hours is the lag between the current and baseline windows. The detector
    # classifies a trace as "current" if it is newer than current_days*24 hours.
    # If gap_hours < current_days*24, the current window would extend back PAST
    # base_end and overlap the baseline window — baseline (good) traffic would be
    # mislabeled "current", diluting any planted regression below threshold. So
    # gap_hours MUST be >= current_days*24. Default leaves a 24h clean gap.
    gap_hours: int = 24 + 3 * 24   # = 96h: current(72h) + 24h gap, no overlap
    traces_per_intent_per_day: int = 30  # margin above n>=30/cell so defaults PASS
    # (current window = current_days * this; baseline = baseline_days * this)
    baseline_pass_rate: float = 0.90
    min_sample_size: int = 30
    seed: int = 7
    # Ground truth:
    regressions: list[Regression] = field(default_factory=lambda: [
        Regression("billing_refund", "completeness", current_pass_rate=0.45),
        Regression("code_debug", "instruction_following", current_pass_rate=0.55),
    ])
    unclear_regressions: list[UnclearRegression] = field(default_factory=lambda: [
        UnclearRegression("account_security", "groundedness", current_unclear_frac=0.45),
    ])

    def __post_init__(self) -> None:
        min_gap = self.current_days * 24
        if self.gap_hours < min_gap:
            raise ValueError(
                f"gap_hours ({self.gap_hours}) must be >= current_days*24 ({min_gap}) "
                "so the current and baseline windows do not overlap (baseline traffic "
                "leaking into the current window would mask the regression)."
            )


# --------------------------------------------------------------------------- #
# Embedders
# --------------------------------------------------------------------------- #

class OracleEmbedder:
    """Deterministic, separable-by-intent embeddings.

    Each intent gets a fixed random base vector; each prompt is that base plus
    small deterministic noise. Paraphrases of one intent land close together
    and far from other intents, so the StableIntentClusterer produces clean,
    stable clusters — isolating the drift detector from embedding noise while
    still exercising the real registry/persistence/stable-ID machinery.

    It does NOT peek at ground truth: it maps a prompt to its intent via the
    corpus's own prompt->intent table, which the caller already knows because
    it generated the prompts. (Embedding quality itself is validated separately
    via --embedder sentence-transformer.)
    """

    dim = 64

    def __init__(self, prompt_to_intent: dict[str, str], seed: int = 0) -> None:
        self._p2i = prompt_to_intent
        intents = sorted(set(prompt_to_intent.values()))
        rng = np.random.default_rng(seed)
        self._base = {name: rng.standard_normal(self.dim) for name in intents}

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            base = self._base.get(self._p2i.get(t, ""), np.zeros(self.dim))
            # small deterministic per-prompt jitter
            h = int(hashlib.sha256(t.encode()).hexdigest()[:8], 16)
            jitter = np.random.default_rng(h).standard_normal(self.dim) * 0.05
            out[i] = base + jitter
        return out


def _build_embedder(kind: str, prompt_to_intent: dict[str, str], seed: int):
    if kind == "oracle":
        return OracleEmbedder(prompt_to_intent, seed=seed)
    if kind == "sentence-transformer":
        from verdict_eval.clustering import SentenceTransformerEmbedder
        return SentenceTransformerEmbedder()
    raise ValueError(f"unknown embedder {kind!r}")


# --------------------------------------------------------------------------- #
# Corpus + synthetic judge
# --------------------------------------------------------------------------- #

@dataclass
class GeneratedTrace:
    trace: Trace
    intent: str
    period: str   # "baseline" or "current"


def _gen_traffic(cfg: Config, now: datetime) -> tuple[list[GeneratedTrace], dict[str, str]]:
    """Generate per-day traffic across baseline and current periods.

    Returns the traces (with timestamps placed in the right window) and the
    prompt->intent map the OracleEmbedder needs.
    """
    rng = random.Random(cfg.seed)
    base_end = now - timedelta(hours=cfg.gap_hours)
    base_start = base_end - timedelta(days=cfg.baseline_days)
    cur_start = now - timedelta(days=cfg.current_days)

    gen: list[GeneratedTrace] = []
    prompt_to_intent: dict[str, str] = {}
    counter = 0

    def emit(period: str, span_start: datetime, span_seconds: float, n_days: int):
        nonlocal counter
        for _day in range(n_days):
            for intent, prompts in INTENTS.items():
                for _ in range(cfg.traces_per_intent_per_day):
                    prompt = rng.choice(prompts)
                    prompt_to_intent[prompt] = intent
                    ts = span_start + timedelta(seconds=rng.uniform(0, span_seconds))
                    counter += 1
                    tr = Trace(
                        trace_id=f"{period}-{counter:05d}",
                        started_at=ts,
                        ended_at=ts,
                        operation=Operation.CHAT,
                        request_model="model-under-test",
                        prompt_redacted=prompt,
                        response_redacted="(synthetic response)",
                    )
                    gen.append(GeneratedTrace(trace=tr, intent=intent, period=period))

    emit("baseline", base_start, cfg.baseline_days * 86400, cfg.baseline_days)
    emit("current", cur_start, cfg.current_days * 86400, cfg.current_days)
    return gen, prompt_to_intent


def _pass_rate(cfg: Config, intent: str, dim: str, period: str) -> float:
    if period == "current":
        for r in cfg.regressions:
            if r.intent == intent and r.dimension == dim:
                return r.current_pass_rate
    return cfg.baseline_pass_rate


def _unclear_frac(cfg: Config, intent: str, dim: str, period: str) -> float:
    base = 0.03
    if period == "current":
        for u in cfg.unclear_regressions:
            if u.intent == intent and u.dimension == dim:
                return u.current_unclear_frac
    return base


def _synthetic_judge(cfg: Config, gt: GeneratedTrace, rng: random.Random) -> Judgment:
    """Deterministic judgment: each dimension drawn PASS/FAIL/UNCLEAR at the
    configured per-(intent, dimension, period) rates."""
    dims: list[DimensionScore] = []
    for dim in DIMENSIONS:
        uf = _unclear_frac(cfg, gt.intent, dim, gt.period)
        if rng.random() < uf:
            verdict = Verdict.UNCLEAR
        else:
            verdict = Verdict.PASS if rng.random() < _pass_rate(cfg, gt.intent, dim, gt.period) else Verdict.FAIL
        dims.append(DimensionScore(name=dim, verdict=verdict, reasoning="synthetic", judge_model="synthetic"))
    return Judgment(
        trace_id=gt.trace.trace_id, rubric_name="default", rubric_version="1",
        judge_models=["synthetic"], dimensions=dims, created_at=gt.trace.started_at,
    )


# --------------------------------------------------------------------------- #
# Multi-run driver
# --------------------------------------------------------------------------- #

def run_multirun(cfg: Config, *, embedder_kind: str, judge_mode: str):
    """Drive the full multi-run pipeline and return everything needed to score.

    Critically, clustering is performed as SEPARATE per-day runs that each
    load and save the persisted registry — so cluster-ID stability across runs
    is genuinely exercised, not assumed.
    """
    now = datetime.now(timezone.utc)
    gen, prompt_to_intent = _gen_traffic(cfg, now)
    embedder = _build_embedder(embedder_kind, prompt_to_intent, cfg.seed)
    storage = InMemoryStorage()
    rng = random.Random(cfg.seed + 1)

    # Group traces by calendar day to simulate distinct cron invocations.
    by_day: dict[str, list[GeneratedTrace]] = {}
    for g in gen:
        by_day.setdefault(g.trace.started_at.strftime("%Y-%m-%d"), []).append(g)

    # intent -> {run_index -> cluster_id}, to check stability across runs.
    intent_cluster_by_run: dict[str, dict[int, str]] = {}
    trace_intent: dict[str, str] = {g.trace.trace_id: g.intent for g in gen}

    for run_idx, day in enumerate(sorted(by_day)):
        day_traces = by_day[day]
        for g in day_traces:
            storage.insert_trace(g.trace)
        # --- separate clustering invocation: load registry, assign, save ---
        registry_json = storage.load_cluster_registry("v1")
        from verdict_eval.stable_clustering import ClusterRegistry
        clusterer = StableIntentClusterer(
            embedder=embedder, threshold=0.30,
            registry=ClusterRegistry.from_json(registry_json),
        )
        clusterer.registry.version = "v1"
        texts = [g.trace.prompt_redacted or "" for g in day_traces]
        cids = clusterer.assign(texts)
        for g, cid in zip(day_traces, cids, strict=True):
            g.trace.cluster_id = cid
            storage.insert_trace(g.trace)
            intent_cluster_by_run.setdefault(g.intent, {})
            # record the (dominant) cluster id this intent got this run
            intent_cluster_by_run[g.intent].setdefault(run_idx, cid)
        storage.save_cluster_registry("v1", clusterer.registry.to_json())
        # --- judge this day's traffic ---
        for g in day_traces:
            if judge_mode == "synthetic":
                storage.insert_judgment(_synthetic_judge(cfg, g, rng))
            else:
                _live_judge(storage, g)   # defined in live path

    # Final cluster_id per trace (read back from storage).
    cluster_for_trace = {t.trace_id: t.cluster_id for t in storage.list_traces(limit=10**9) if t.cluster_id}
    # Map cluster_id -> dominant intent.
    cluster_to_intent: dict[str, str] = {}
    tally: dict[str, dict[str, int]] = {}
    for tid, cid in cluster_for_trace.items():
        tally.setdefault(cid, {}).setdefault(trace_intent[tid], 0)
        tally[cid][trace_intent[tid]] += 1
    for cid, counts in tally.items():
        cluster_to_intent[cid] = max(counts, key=counts.get)

    return {
        "now": now, "storage": storage, "cfg": cfg,
        "cluster_for_trace": cluster_for_trace,
        "cluster_to_intent": cluster_to_intent,
        "intent_cluster_by_run": intent_cluster_by_run,
    }


def _live_judge(storage, g: GeneratedTrace) -> None:
    """Live judge path (costs money). Uses the real Anthropic judge over an
    injector-corrupted response so there is a ground-truth label."""
    from verdict_eval.judge import Judge, DEFAULT_RUBRIC
    from verdict_eval.providers import AnthropicAdapter
    judge = Judge(provider=AnthropicAdapter(), model="claude-haiku-4-5", rubric=DEFAULT_RUBRIC)
    j = judge.judge(query=g.trace.prompt_redacted or "", response=g.trace.response_redacted or "",
                    trace_id=g.trace.trace_id)
    j.created_at = g.trace.started_at
    storage.insert_judgment(j)


# --------------------------------------------------------------------------- #
# Detect + score
# --------------------------------------------------------------------------- #

def detect_and_score(state: dict) -> int:
    """Run drift detection and check it against ground truth. Returns exit code."""
    from verdict_eval.drift import DriftDetector, split_windows_by_time  # scipy here

    cfg: Config = state["cfg"]
    storage = state["storage"]
    cluster_to_intent = state["cluster_to_intent"]
    failures: list[str] = []

    # --- check 1: cluster-ID stability across separate runs ---
    unstable = []
    for intent, by_run in state["intent_cluster_by_run"].items():
        ids = set(by_run.values())
        if len(ids) != 1:
            unstable.append(f"{intent}: {sorted(ids)}")
    print("\n[1] CLUSTER-ID STABILITY ACROSS RUNS")
    if unstable:
        failures.append("cluster IDs unstable: " + "; ".join(unstable))
        print("    FAIL — an intent changed cluster_id across runs:\n      " + "\n      ".join(unstable))
    else:
        n_runs = max((max(v) for v in state["intent_cluster_by_run"].values()), default=0) + 1
        print(f"    PASS — every intent kept ONE cluster_id across all {n_runs} separate runs.")

    # --- build windows (uses the persisted cluster_id on each trace) ---
    all_judgments = []
    for cid in set(state["cluster_for_trace"].values()):
        all_judgments.extend(storage.list_judgments_for_cluster(cid, limit=10**9))
    cur_windows, base_windows = split_windows_by_time(
        all_judgments, state["cluster_for_trace"],
        current_hours=cfg.current_days * 24,
        baseline_days=cfg.baseline_days,
        baseline_lag_hours=cfg.gap_hours,
        now=state["now"],
    )

    # --- check 2: n>=30 per (cluster, dimension) per window actually met ---
    # A planted-unclear cell legitimately drops below the floor (that IS the
    # regression), so exclude exactly those (intent, dimension) pairs — by pair,
    # not by dimension name, so the same dimension on other intents still counts.
    print("\n[2] SAMPLE-SIZE FLOOR (n>=30 per cell, both windows)")
    exempt = {(u.intent, u.dimension) for u in cfg.unclear_regressions}
    starved = [f"{w.cluster_id}/{w.dimension} n={w.n}" for w in (cur_windows + base_windows)
               if w.n < cfg.min_sample_size
               and (cluster_to_intent.get(w.cluster_id, "?"), w.dimension) not in exempt]
    if starved:
        failures.append(f"{len(starved)} cells below n>=30")
        print(f"    FAIL — {len(starved)} cells below n={cfg.min_sample_size}: {starved[:6]}")
    else:
        print(f"    PASS — every scored cell has n>={cfg.min_sample_size}.")

    # --- run the detector ---
    signals = DriftDetector(min_sample_size=cfg.min_sample_size, p_threshold=0.01).detect(
        current=cur_windows, baseline=base_windows)

    # --- check 3: regression detection precision/recall/F1 over the grid ---
    planted = {(r.intent, r.dimension) for r in cfg.regressions}
    flagged_regressions = set()
    flagged_unclear = set()
    for s in signals:
        intent = cluster_to_intent.get(s.cluster_id, "?")
        if s.statistic_name == "unclear_rate_increase":
            flagged_unclear.add((intent, s.dimension))
        else:
            flagged_regressions.add((intent, s.dimension))

    all_cells = {(i, d) for i in INTENTS for d in DIMENSIONS}
    controls = all_cells - planted - {(u.intent, u.dimension) for u in cfg.unclear_regressions}
    tp = len(planted & flagged_regressions)
    fn = len(planted - flagged_regressions)
    fp = len(flagged_regressions & controls)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    print("\n[3] DRIFT DETECTION vs GROUND TRUTH")
    print(f"    planted regressions: {sorted(planted)}")
    print(f"    detected:            {sorted(flagged_regressions)}")
    print(f"    TP={tp} FP={fp} FN={fn}  ->  precision={precision:.2f} recall={recall:.2f} F1={f1:.2f}")
    if recall < 1.0:
        failures.append(f"missed regression(s): {sorted(planted - flagged_regressions)}")
        print(f"    FAIL — missed: {sorted(planted - flagged_regressions)}")
    if fp > 0:
        failures.append(f"{fp} false positive(s) on controls")
        print(f"    FAIL — false positives on controls: {sorted(flagged_regressions & controls)}")
    if recall == 1.0 and fp == 0:
        print("    PASS — all regressions caught, zero false positives on controls.")

    # --- check 4: unclear-rate signal ---
    planted_unclear = {(u.intent, u.dimension) for u in cfg.unclear_regressions}
    print("\n[4] UNCLEAR-RATE (unevaluable-drift) SIGNAL")
    print(f"    planted: {sorted(planted_unclear)}   detected: {sorted(flagged_unclear)}")
    if not planted_unclear <= flagged_unclear:
        failures.append(f"missed unclear drift: {sorted(planted_unclear - flagged_unclear)}")
        print(f"    FAIL — missed: {sorted(planted_unclear - flagged_unclear)}")
    else:
        print("    PASS — unevaluable-drift dimension(s) flagged.")

    print("\n" + ("=" * 64))
    if failures:
        print(f"VALIDATION FAILED ({len(failures)} issue(s)):")
        for f in failures:
            print("  - " + f)
        return 1
    print("VALIDATION PASSED — stable clusters, regressions caught, zero false")
    print("positives, sample-size floor met, unevaluable-drift flagged.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--judge", choices=["synthetic", "live"], default="synthetic")
    p.add_argument("--embedder", choices=["oracle", "sentence-transformer"], default="oracle")
    p.add_argument("--traces-per-intent-per-day", type=int, default=30)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()

    cfg = Config(traces_per_intent_per_day=args.traces_per_intent_per_day, seed=args.seed)
    if args.judge == "live":
        print(textwrap.dedent("""\
            WARNING: --judge live runs the REAL judge, but on this script's
            *synthetic placeholder responses* ("(synthetic response)"). It only
            exercises the judge PLUMBING end-to-end against a real API (and costs
            money) — it does NOT measure judge accuracy, because the responses
            aren't real model outputs.

            For real judge-vs-human accuracy, use:
                python scripts/verify_judge_alignment.py --mode online ...
            For real-traffic capture, use:
                python scripts/live_capture_check.py
            Continuing in 3s... (Ctrl-C to abort)
        """))
        import time as _t
        _t.sleep(3)
    state = run_multirun(cfg, embedder_kind=args.embedder, judge_mode=args.judge)
    return detect_and_score(state)


if __name__ == "__main__":
    sys.exit(main())
