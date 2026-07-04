"""End-to-end drift pipeline runner.

Reads every trace from the configured storage, runs intent clustering,
runs the judge ensemble on every (or sampled) trace, computes per-cluster
per-dimension drift signals across a current-vs-baseline window split, and
persists the resulting DriftSignal records back to storage.

This is the first script that wires the full pipeline together:

    Stored traces
        → IntentClusterer.partial_fit_predict (assigns cluster_id)
        → Judge / JudgeEnsemble (produces Judgment per trace)
        → DriftDetector (compares current window vs baseline)
        → DriftSignals persisted

Usage (offline, FakeProvider judge — runs without API keys):
    python scripts/run_drift_pipeline.py --storage sqlite:///./verdict.db \\
        --judge-provider fake

Usage (live judge):
    python scripts/run_drift_pipeline.py --storage sqlite:///./verdict.db \\
        --judge-provider anthropic --judge-model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "packages" / "verdict" / "src"))
sys.path.insert(0, str(HERE.parent / "packages" / "verdict_eval" / "src"))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--storage", default="sqlite:///./verdict.db",
                   help="Storage URL. SQLite, memory, or postgres URL.")
    p.add_argument("--judge-provider", default="fake",
                   choices=["fake", "anthropic", "openai", "google"],
                   help="Provider for the judge LLM.")
    p.add_argument("--judge-model", default="fake-judge",
                   help="Judge model name (use 'fake-judge' for offline).")
    p.add_argument("--current-hours", type=int, default=24,
                   help="Current window size in hours (default 24).")
    p.add_argument("--baseline-days", type=int, default=7,
                   help="Baseline window size in days (default 7).")
    p.add_argument("--baseline-lag-hours", type=int, default=24,
                   help="Gap between current and baseline windows (default 24).")
    p.add_argument("--min-sample-size", type=int, default=30,
                   help="Min judgments per (cluster, dimension) for stat test.")
    p.add_argument("--p-threshold", type=float, default=0.01,
                   help="BH-adjusted p-value threshold for emitting a signal.")
    p.add_argument("--sampling", choices=["stratified", "uniform"], default="stratified",
                   help="How to choose which traces to judge. 'stratified' "
                        "allocates per (cluster, window) so each cluster reaches "
                        "the n>=30 floor without over-spending on big clusters. "
                        "'uniform' is the legacy flat sample_rate.")
    p.add_argument("--target-per-cluster", type=int, default=40,
                   help="Stratified target: judged traces per (cluster, window). "
                        "Default 40 (margin above min-sample-size=30 for UNCLEARs).")
    p.add_argument("--sample-rate", type=float, default=1.0,
                   help="Uniform sampling fraction (0.0–1.0), and the rate used "
                        "for the uniform-vs-stratified contrast line. Default 1.0.")
    p.add_argument("--limit", type=int, default=10_000,
                   help="Max traces to pull from storage.")
    p.add_argument("--embedder", default="deterministic",
                   choices=["deterministic", "sentence-transformer"],
                   help="Embedder for clustering. 'deterministic' is stateless "
                        "(stable across runs, coarse); 'sentence-transformer' is "
                        "higher-quality but needs the optional dependency.")
    p.add_argument("--clustering-version", default="v1",
                   help="Registry/version key. Bump when you change embedder or "
                        "threshold so incompatible cluster definitions aren't mixed.")
    p.add_argument("--cluster-threshold", type=float, default=0.30,
                   help="Max cosine distance to join an existing cluster.")
    p.add_argument("--recluster", action="store_true",
                   help="Reassign cluster_id even for traces that already have one. "
                        "Default: assign only traces missing a cluster_id (capture-"
                        "time semantics) so IDs stay stable.")
    args = p.parse_args()

    # -- Step 1: load traces -------------------------------------------------
    from verdict.client import _resolve_storage
    storage = _resolve_storage(args.storage)
    print(f"Storage: {args.storage}")
    traces = storage.list_traces(limit=args.limit)
    print(f"Loaded {len(traces)} traces.")
    if not traces:
        print("No traces in storage. Run an instrumented example first "
              "(e.g. `python examples/basic_anthropic.py`).")
        return 0

    # -- Step 2: cluster (stable, persistent, assignment-based) --------------
    # Identity is ASSIGNED against a persisted registry, not recomputed from
    # scratch each run. A cluster keeps its ID forever, so the week-over-week
    # comparison in Step 4 lines up matching buckets. Clustering is done once,
    # at capture time: by default we only assign traces that lack a cluster_id
    # and never overwrite an existing one (use --recluster to override).
    from verdict_eval.clustering import DeterministicHashEmbedder, SentenceTransformerEmbedder
    from verdict_eval.stable_clustering import ClusterRegistry, StableIntentClusterer

    print(f"Clustering by intent (stable, version={args.clustering_version})...")
    if args.embedder == "sentence-transformer":
        embedder = SentenceTransformerEmbedder()
    else:
        # Stateless => same text embeds identically across runs (the property
        # the SVD-fitting HashingEmbedder lacks). Coarse but stable.
        embedder = DeterministicHashEmbedder()

    # Load the persisted registry for this version (empty on first run).
    registry = ClusterRegistry.from_json(
        storage.load_cluster_registry(args.clustering_version)
    )
    registry.version = args.clustering_version
    clusterer = StableIntentClusterer(
        embedder=embedder,
        threshold=args.cluster_threshold,
        registry=registry,
    )

    # Assign oldest-first so early traffic seeds the clusters deterministically.
    ordered = sorted(traces, key=lambda t: t.started_at)
    to_assign = [t for t in ordered if args.recluster or not t.cluster_id]
    if to_assign:
        # Cluster on the actual prompt text; fall back to an empty string (which
        # routes to UNCLUSTERED_ID) rather than deriving from request_model/trace_id
        # that used to fragment the space into singletons.
        new_ids = clusterer.assign([t.prompt_redacted or "" for t in to_assign])
        for t, cid in zip(to_assign, new_ids, strict=True):
            t.cluster_id = cid
            storage.insert_trace(t)
    # Persist the (possibly grown) registry so the next run is consistent.
    storage.save_cluster_registry(args.clustering_version, clusterer.registry.to_json())

    cluster_ids = [t.cluster_id for t in traces if t.cluster_id]
    print(f"  Assigned {len(to_assign)} trace(s); "
          f"registry now holds {len(clusterer.registry.ids)} cluster(s).")

    # -- Step 3: judge -------------------------------------------------------
    from verdict_eval.judge import Judge, DEFAULT_RUBRIC
    from verdict_eval.providers import FakeProvider

    if args.judge_provider == "fake":
        # Offline path: synthesize a judge that scores every dimension PASS.
        # Useful for exercising the pipeline before live keys are wired.
        import json
        provider = FakeProvider(
            json.dumps({
                d.name: {"reasoning": "fake-judge default PASS", "verdict": "PASS"}
                for d in DEFAULT_RUBRIC.dimensions
            })
        )
    elif args.judge_provider == "anthropic":
        from verdict_eval.providers import AnthropicAdapter
        provider = AnthropicAdapter()
    elif args.judge_provider == "openai":
        from verdict_eval.providers import OpenAIAdapter
        provider = OpenAIAdapter()
    else:
        from verdict_eval.providers import GoogleAdapter
        provider = GoogleAdapter()

    judge = Judge(provider=provider, model=args.judge_model, rubric=DEFAULT_RUBRIC)

    # Only judge-able traces (those with a response) are candidates.
    judgeable = [t for t in traces if (t.response_redacted or "")]

    if args.sampling == "stratified":
        # Allocate judgments PER (cluster, window) so each cluster reaches the
        # detector's n>=30 floor, instead of judging a flat % that starves
        # low-volume clusters and over-spends on high-volume ones.
        from verdict_eval.sampling import (
            StratifiedJudgeSampler,
            WindowSpec,
            uniform_coverage_estimate,
        )
        window = WindowSpec(
            now=datetime.now(timezone.utc),
            current_hours=args.current_hours,
            baseline_days=args.baseline_days,
            baseline_lag_hours=args.baseline_lag_hours,
        )
        # Which traces are already judged (so re-runs top up, not re-judge).
        already_judged: set[str] = set()
        for cid in set(t.cluster_id for t in judgeable if t.cluster_id):
            for j in storage.list_judgments_for_cluster(cid, limit=10**9):
                already_judged.add(j.trace_id)
        sampler = StratifiedJudgeSampler(target_per_cell=args.target_per_cluster)
        plan = sampler.plan(judgeable, window=window, already_judged_trace_ids=already_judged)
        print(plan.summary())
        # Contrast with what naive uniform sampling would have achieved.
        est = uniform_coverage_estimate(
            judgeable, window=window, sample_rate=args.sample_rate,
            min_sample_size=args.min_sample_size,
        )
        print(f"  (uniform @ rate={args.sample_rate} would reach the floor in "
              f"{est['cells_reaching_floor']}/{est['cells_total']} cells "
              f"for ~{est['expected_judgments']} judgments)")
        selected = set(plan.selected_trace_ids)
        to_judge = [t for t in judgeable if t.trace_id in selected]
    else:
        # Legacy uniform sampling, kept for comparison.
        import random
        rng = random.Random(42)
        to_judge = [t for t in judgeable if rng.random() <= args.sample_rate]

    print(f"Judging {len(to_judge)} traces (sampling={args.sampling})...")
    judged = 0
    for t in to_judge:
        try:
            j = judge.judge(query=t.prompt_redacted or "", response=t.response_redacted or "",
                            trace_id=t.trace_id)
            storage.insert_judgment(j)
            judged += 1
        except Exception as e:
            print(f"  WARN: judge failed for {t.trace_id}: {e}")
    print(f"  Persisted {judged} judgments.")

    # -- Step 4: compute drift ----------------------------------------------
    from verdict_eval.drift import (
        DriftDetector,
        split_windows_by_time,
    )

    print("Computing drift...")
    # Reload judgments per cluster to feed the detector
    all_judgments = []
    cluster_for_trace: dict[str, str] = {}
    for cid in set(cluster_ids):
        js = storage.list_judgments_for_cluster(cid, limit=100_000)
        all_judgments.extend(js)
        for j in js:
            cluster_for_trace[j.trace_id] = cid

    cur_windows, base_windows = split_windows_by_time(
        all_judgments,
        cluster_for_trace,
        current_hours=args.current_hours,
        baseline_days=args.baseline_days,
        baseline_lag_hours=args.baseline_lag_hours,
        now=datetime.now(timezone.utc),
    )
    print(f"  Current windows:  {len(cur_windows)}  "
          f"(total n = {sum(w.n for w in cur_windows)})")
    print(f"  Baseline windows: {len(base_windows)}  "
          f"(total n = {sum(w.n for w in base_windows)})")

    detector = DriftDetector(
        min_sample_size=args.min_sample_size,
        p_threshold=args.p_threshold,
    )
    signals = detector.detect(current=cur_windows, baseline=base_windows)
    print(f"  Detected {len(signals)} drift signal(s).")

    for sig in signals:
        storage.insert_drift_signal(sig)
        print(
            f"    • cluster={sig.cluster_id} dim={sig.dimension} "
            f"dir={sig.direction.value} d={sig.effect_size_cohens_d:.3f} "
            f"p_adj={sig.p_value_adjusted:.4f}"
        )

    print("\nDone. Inspect persisted signals with:")
    print(f"  python -c \"from verdict.client import _resolve_storage; "
          f"s=_resolve_storage('{args.storage}'); "
          f"[print(x) for x in s.list_drift_signals(limit=20)]\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
