"""End-to-end drift pipeline runner.

Reads traces from the configured storage, assigns stable intent clusters,
runs the configured judge on a stratified or uniform sample, computes per-cluster
per-dimension drift signals across a current-vs-baseline window split, and
persists one atomic DriftRun snapshot and its exact DriftSignal set to storage,
including a completed zero-signal run.

This is the first script that wires the full pipeline together:

    Stored traces
        → StableIntentClusterer.assign (persists stable cluster IDs)
        → Judge (produces Judgment per selected trace)
        → DriftDetector (compares current window vs baseline)
        → DriftRun + exact DriftSignals persisted atomically

Usage (offline, FakeProvider judge — runs without API keys):
    verdict-pipeline --storage sqlite:///./verdict.db \\
        --judge-provider fake

Usage (live judge):
    verdict-pipeline --storage sqlite:///./verdict.db \\
        --judge-provider anthropic --judge-model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--storage",
        default=os.environ.get("VERDICT_STORAGE", "sqlite:///./verdict.db"),
        help="Storage URL. Defaults to VERDICT_STORAGE, then sqlite:///./verdict.db.",
    )
    p.add_argument("--judge-provider", default="fake",
                   choices=["fake", "anthropic", "openai", "google"],
                   help="Provider for the judge LLM.")
    p.add_argument("--judge-model", default="fake-judge",
                   help="Judge model name (use 'fake-judge' for offline).")
    p.add_argument(
        "--judge-sentinel-file",
        default="",
        help="Optional JSONL human-labeled anchor set. Agreement is persisted "
             "as judge health, separately from production drift.",
    )
    p.add_argument(
        "--judge-health-min-examples",
        type=int,
        default=30,
        help="Minimum independently judged examples before health can be certified.",
    )
    p.add_argument(
        "--judge-health-threshold",
        type=float,
        default=0.8,
        help="Minimum 95%% Wilson-CI lower bound for healthy sentinel status "
             "after the independently judged example floor.",
    )
    p.add_argument("--current-hours", type=int, default=24,
                   help="Current window size in hours (default 24).")
    p.add_argument("--baseline-days", type=int, default=7,
                   help="Baseline window size in days (default 7).")
    p.add_argument("--baseline-lag-hours", type=int, default=24,
                   help="Gap between current and baseline windows (default 24).")
    p.add_argument("--as-of", default="",
                   help="Analysis time as an ISO-8601 timestamp. Defaults to now. "
                        "Useful for reproducible reruns and backfills.")
    p.add_argument("--min-sample-size", type=int, default=30,
                   help="Min judgments per (cluster, dimension) for stat test.")
    p.add_argument("--p-threshold", type=float, default=0.01,
                   help="BH-adjusted p-value threshold for emitting a signal.")
    p.add_argument("--effect-size-threshold", type=float, default=0.147,
                   help="Minimum absolute Cliff's delta. On PASS/FAIL data this "
                        "is the minimum detectable pass-rate change (default 0.147 = 14.7pp).")
    p.add_argument("--sampling", choices=["stratified", "uniform"], default="stratified",
                   help="How to choose which traces to judge. 'stratified' "
                        "allocates per (cluster, window) so each cluster reaches "
                        "the n>=30 floor without over-spending on big clusters. "
                        "'uniform' is the legacy flat sample_rate.")
    p.add_argument("--target-per-cluster", type=int, default=40,
                   help="Stratified target: judged traces per (cluster, window). "
                        "Default 40 (margin above min-sample-size=30 for UNCLEARs).")
    p.add_argument("--sample-rate", type=float, default=1.0,
                   help="Uniform sampling fraction (0.0-1.0), and the rate used "
                        "for the uniform-vs-stratified contrast line. Default 1.0.")
    p.add_argument("--limit", type=int, default=10_000,
                   help="Max traces to pull from storage.")
    p.add_argument("--embedder", default="sentence-transformer",
                   choices=["sentence-transformer", "hashing", "deterministic"],
                   help="Embedder for clustering. The semantic model is the default "
                        "and needs cognifity-verdict-eval[semantic]. 'hashing' is an "
                        "explicit lexical fallback; 'deterministic' is for tests only.")
    p.add_argument("--clustering-version", default="v2",
                   help="Registry/version key. Bump when you change embedder or "
                        "threshold so incompatible cluster definitions aren't mixed.")
    p.add_argument("--cluster-threshold", type=float, default=0.50,
                   help="Max cosine distance to join an existing cluster. The "
                        "MiniLM starting point is 0.50; validate it on your prompts.")
    cluster_mode = p.add_mutually_exclusive_group()
    cluster_mode.add_argument(
        "--recluster", action="store_true",
        help="Reassign cluster_id even for traces that already have one. Default: "
             "assign only traces missing a cluster_id during analysis so IDs stay stable.",
    )
    cluster_mode.add_argument(
        "--trust-existing-clusters",
        action="store_true",
        help="Use preassigned external cluster IDs without a Verdict registry. All "
             "judgeable traces must already have a cluster_id. Do not use this to "
             "bypass an embedder/version migration; use --recluster for that.",
    )
    return p


def _parse_analysis_time(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid --as-of timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include a timezone offset, for example +00:00")
    return parsed.astimezone(timezone.utc)


def _storage_backend(storage: str) -> str:
    """Name a backend without echoing a credential-bearing storage value."""
    if storage.startswith(("postgres://", "postgresql://")):
        return "postgresql"
    if storage.startswith("memory:"):
        return "memory"
    if storage.startswith("sqlite:") or "://" not in storage:
        return "sqlite"
    return "configured"


def _drift_run_id(
    args,
    analysis_time: datetime,
    tenant_scope: str,
    evaluator_fingerprint: str,
) -> str:
    """Return the deterministic identity of one completed hourly snapshot."""
    window_bucket = analysis_time.replace(minute=0, second=0, microsecond=0)
    identity = "|".join([
        "verdict-drift-run-v1",
        tenant_scope,
        evaluator_fingerprint,
        args.clustering_version,
        args.embedder,
        str(args.cluster_threshold),
        str(args.recluster),
        str(args.trust_existing_clusters),
        args.sampling,
        str(args.target_per_cluster),
        str(args.sample_rate),
        str(args.current_hours),
        str(args.baseline_days),
        str(args.baseline_lag_hours),
        str(args.min_sample_size),
        str(args.p_threshold),
        str(args.effect_size_threshold),
        window_bucket.isoformat(),
    ])
    return uuid5(NAMESPACE_URL, identity).hex


def _signal_id_for_run(signal, run_id: str) -> str:
    """Return a stable ID for one statistical cell within a run snapshot.

    Run identity includes evaluator, scope, configuration, and analysis bucket,
    so signals from distinct snapshots cannot overwrite one another.
    """
    identity = "|".join([
        "verdict-drift-signal-v2",
        run_id,
        signal.cluster_id,
        signal.dimension,
        signal.direction.value,
        signal.statistic_name,
    ])
    return uuid5(NAMESPACE_URL, identity).hex


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        analysis_time = _parse_analysis_time(args.as_of)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    # -- Step 1: load traces -------------------------------------------------
    from verdict.client import _resolve_storage

    try:
        storage = _resolve_storage(args.storage)
    except Exception:
        print(f"ERROR: cannot open {_storage_backend(args.storage)} storage")
        return 2
    print(f"Storage backend: {_storage_backend(args.storage)}")
    traces = storage.list_traces(limit=args.limit)
    print(f"Loaded {len(traces)} traces.")
    if not traces:
        print("No traces in storage. Run an instrumented example first "
              "(e.g. `python examples/basic_anthropic.py`).")
        return 0

    # The v0 runner has one cluster registry and one set of statistical cells
    # per store. Refuse mixed tenants instead of silently pooling them and
    # implying isolation that the analysis path does not provide yet.
    tenant_values = {t.tenant_id for t in traces}
    if len(tenant_values) > 1:
        shown = sorted("<unset>" if t is None else t for t in tenant_values)
        print(
            "ERROR: this database contains multiple tenant scopes "
            f"({', '.join(shown)}). The v0 drift runner supports one tenant "
            "per store; use separate stores until tenant-scoped registries and "
            "signals are implemented."
        )
        return 2
    only_tenant = next(iter(tenant_values))
    tenant_scope = only_tenant or "single-store-unset-tenant"

    completed = [t for t in traces if not t.error]
    judgeable = [
        t for t in completed
        if (t.prompt_redacted or "").strip() and (t.response_redacted or "").strip()
    ]
    if not judgeable:
        print(
            "ERROR: no completed trace has both captured prompt and response "
            "content. Quality clustering and judging require "
            "verdict.init(capture_content=True). Metadata-only traces still "
            "support cost, latency, token, and error inspection."
        )
        return 2
    missing_content = len(completed) - len(judgeable)
    if missing_content:
        print(
            f"  WARN: skipping {missing_content} completed trace(s) without both "
            "captured prompt and response content."
        )

    # -- Step 2: cluster (stable, persistent, assignment-based) --------------
    # Identity is ASSIGNED against a persisted registry, not recomputed from
    # scratch each run. A cluster keeps its ID forever, so the week-over-week
    # comparison in Step 4 lines up matching buckets. Assignment happens on
    # the first analysis run after capture: by default we only assign traces
    # that lack a cluster_id and never overwrite an existing one (use
    # --recluster to override).
    from verdict_eval.clustering import (
        DeterministicHashEmbedder,
        HashingEmbedder,
        SentenceTransformerEmbedder,
    )
    from verdict_eval.stable_clustering import (
        UNCLUSTERED_ID,
        ClusterRegistry,
        StableIntentClusterer,
        assess_cluster_health,
    )

    print(f"Clustering by intent (stable, version={args.clustering_version})...")
    if args.embedder == "sentence-transformer":
        try:
            embedder = SentenceTransformerEmbedder()
        except Exception as exc:
            print(
                "ERROR: semantic intent clustering is unavailable. Install "
                "`cognifity-verdict-eval[semantic]` and ensure the MiniLM model can "
                f"load, or explicitly choose `--embedder hashing`. Cause: {exc}"
            )
            return 2
    elif args.embedder == "hashing":
        print(
            "  WARN: using lexical HashingEmbedder fallback. It is deterministic but "
            "does not reliably group semantic paraphrases."
        )
        embedder = HashingEmbedder()
    else:
        print(
            "  WARN: DeterministicHashEmbedder is a test utility, not a production "
            "intent model. Expect paraphrase fragmentation."
        )
        embedder = DeterministicHashEmbedder()

    # Load the persisted registry for this version (empty on first run).
    registry = ClusterRegistry.from_json(
        storage.load_cluster_registry(args.clustering_version)
    )
    registry.version = args.clustering_version

    # Trace rows do not yet carry a separate clustering-version column. Verify
    # every persisted ID belongs to this registry rather than silently mixing
    # old/default or externally assigned IDs with a new embedding definition.
    persisted_ids = {
        t.cluster_id
        for t in traces
        if t.cluster_id and t.cluster_id != UNCLUSTERED_ID
    }
    unknown_ids = persisted_ids - set(registry.ids)
    if unknown_ids and not args.recluster and not args.trust_existing_clusters:
        preview = ", ".join(sorted(unknown_ids)[:5])
        print(
            "ERROR: stored traces contain cluster IDs that do not belong to "
            f"registry {args.clustering_version!r} ({preview}). This commonly "
            "happens after changing the embedder, threshold, or clustering "
            "version. Re-run once with --recluster, or use "
            "--trust-existing-clusters only when those IDs come from a stable "
            "external clustering system."
        )
        return 2
    if args.trust_existing_clusters:
        missing_external = [t.trace_id for t in judgeable if not t.cluster_id]
        if missing_external:
            print(
                "ERROR: --trust-existing-clusters requires every judgeable trace "
                "to have a preassigned cluster_id; "
                f"{len(missing_external)} trace(s) are missing one."
            )
            return 2
    try:
        clusterer = StableIntentClusterer(
            embedder=embedder,
            threshold=args.cluster_threshold,
            registry=registry,
        )
    except ValueError as exc:
        print(f"ERROR: incompatible cluster registry: {exc}")
        return 2

    # Assign oldest-first so early traffic seeds the clusters deterministically.
    ordered = sorted(traces, key=lambda t: t.started_at)
    to_assign = [] if args.trust_existing_clusters else [
        t for t in ordered if args.recluster or not t.cluster_id
    ]
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
    cluster_health = assess_cluster_health(
        cluster_ids, min_sample_size=args.min_sample_size,
    )
    print(
        f"  Cluster health: {cluster_health.n_clusters} cluster(s), median size "
        f"{cluster_health.median_cluster_size:g}, "
        f"{cluster_health.clusters_meeting_sample_floor} meeting the "
        f"{args.min_sample_size}-sample floor."
    )
    for message in cluster_health.messages:
        print(f"  WARN: {message}")

    # -- Step 3: judge -------------------------------------------------------
    from verdict_eval.judge import DEFAULT_RUBRIC, Judge
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
    current_evaluator = judge.evaluator_identity(context=None)

    # A fixed human-labeled sentinel set is the independent anchor for judge
    # behavior. Its aggregate is stored in evaluator_health and never mixed into
    # production judgments or target-model drift windows.
    if args.judge_sentinel_file:
        from verdict_eval.judge_health import (
            evaluate_judge_health,
            load_sentinel_set,
        )

        try:
            set_name, sentinels = load_sentinel_set(args.judge_sentinel_file)
            health = evaluate_judge_health(
                judge,
                sentinels,
                set_name=set_name,
                minimum_examples=args.judge_health_min_examples,
                agreement_threshold=args.judge_health_threshold,
            )
        except (OSError, ValueError) as exc:
            print(f"ERROR: invalid judge sentinel set: {exc}")
            return 2
        storage.insert_evaluator_health(health)
        low = health.example_confidence_low or 0.0
        high = health.example_confidence_high or 0.0
        print(
            f"Judge health ({health.sentinel_set_name}): {health.status.value}; "
            f"examples={health.correct_examples}/{health.total_examples} "
            f"({health.example_agreement:.1%}, 95% CI {low:.1%}-{high:.1%}); "
            f"labels={health.correct_labels}/{health.total_labels} "
            f"({health.label_agreement:.1%}); "
            f"errors={health.error_count}."
        )
        print(
            "  Sentinel agreement is monitored separately from production drift; "
            "it cannot rule out silent behavior changes outside the anchor set."
        )
        if health.status.value != "healthy":
            print(
                "ERROR: production judging and drift detection are blocked because "
                f"judge health is {health.status.value}, not healthy."
            )
            storage.close()
            return 2

    def matches_current_evaluator(judgment) -> bool:
        """Keep one statistical run on one judge/rubric definition."""
        return (
            judgment.evaluator_provider
            == current_evaluator["evaluator_provider"]
            and judgment.judge_models == current_evaluator["judge_models"]
            and judgment.rubric_name == current_evaluator["rubric_name"]
            and judgment.rubric_version == current_evaluator["rubric_version"]
            and judgment.evaluator_config == current_evaluator["evaluator_config"]
            and judgment.expected_dimensions
            == current_evaluator["expected_dimensions"]
            and judgment.evaluator_fingerprint
            == current_evaluator["evaluator_fingerprint"]
        )

    # Re-runs top up only the current evaluator definition. A judgment from a
    # different model or rubric is valid historical evidence, but pooling it
    # into this run would change the measuring instrument mid-window.
    latest_existing_by_trace = {}
    for cid in set(t.cluster_id for t in judgeable if t.cluster_id):
        for existing in storage.list_judgments_for_cluster(cid, limit=10**9):
            if not matches_current_evaluator(existing):
                continue
            previous = latest_existing_by_trace.get(existing.trace_id)
            if previous is None or (existing.created_at, existing.judgment_id) > (
                previous.created_at,
                previous.judgment_id,
            ):
                latest_existing_by_trace[existing.trace_id] = existing
    already_judged = {
        trace_id
        for trace_id, judgment in latest_existing_by_trace.items()
        if judgment.status.value == "completed"
    }

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
            now=analysis_time,
            current_hours=args.current_hours,
            baseline_days=args.baseline_days,
            baseline_lag_hours=args.baseline_lag_hours,
        )
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
        to_judge = [
            t for t in judgeable
            if t.trace_id not in already_judged and rng.random() <= args.sample_rate
        ]

    print(f"Judging {len(to_judge)} traces (sampling={args.sampling})...")
    judged = 0
    judge_errors = 0
    for t in to_judge:
        try:
            j = judge.judge(query=t.prompt_redacted or "", response=t.response_redacted or "",
                            trace_id=t.trace_id)
            storage.insert_judgment(j)
            judged += 1
        except Exception as e:
            from verdict.redaction import redact
            from verdict.schema import Judgment, JudgmentStatus

            safe_error = redact(str(e)) or "judge error"
            storage.insert_judgment(Judgment(
                trace_id=t.trace_id,
                status=JudgmentStatus.ERROR,
                error=safe_error,
                **current_evaluator,
            ))
            judge_errors += 1
            print(f"  WARN: judge failed for {t.trace_id}: {safe_error}")
    print(
        f"  Persisted {judged} completed judgment(s) and "
        f"{judge_errors} error record(s)."
    )

    # -- Step 4: compute drift ----------------------------------------------
    from verdict_eval.drift import (
        DriftDetector,
        split_windows_by_time,
    )

    print("Computing drift...")
    # Reload judgments per cluster to feed the detector
    latest_judgment_for_trace = {}
    cluster_for_trace: dict[str, str] = {}
    for cid in set(cluster_ids):
        js = storage.list_judgments_for_cluster(cid, limit=100_000)
        for j in js:
            if not matches_current_evaluator(j):
                continue
            previous = latest_judgment_for_trace.get(j.trace_id)
            if previous is None or (j.created_at, j.judgment_id) > (
                previous.created_at,
                previous.judgment_id,
            ):
                latest_judgment_for_trace[j.trace_id] = j
                cluster_for_trace[j.trace_id] = cid
    all_judgments = [
        judgment
        for judgment in latest_judgment_for_trace.values()
        if judgment.status.value == "completed"
    ]

    cur_windows, base_windows = split_windows_by_time(
        all_judgments,
        cluster_for_trace,
        {t.trace_id: t.started_at for t in traces},
        current_hours=args.current_hours,
        baseline_days=args.baseline_days,
        baseline_lag_hours=args.baseline_lag_hours,
        now=analysis_time,
    )
    print(f"  Current windows:  {len(cur_windows)}  "
          f"(total n = {sum(w.n for w in cur_windows)})")
    print(f"  Baseline windows: {len(base_windows)}  "
          f"(total n = {sum(w.n for w in base_windows)})")

    detector = DriftDetector(
        min_sample_size=args.min_sample_size,
        p_threshold=args.p_threshold,
        effect_size_threshold=args.effect_size_threshold,
    )
    print(
        f"  Sensitivity floor: {args.effect_size_threshold:.1%} absolute pass-rate "
        "change for binary rubric dimensions (before significance gating)."
    )
    signals = detector.detect(current=cur_windows, baseline=base_windows)
    for diagnostic in detector.last_diagnostics:
        print(f"  WARN: {diagnostic}")
    print(f"  Detected {len(signals)} drift signal(s).")

    # Persist one completed snapshot even when no signal clears the gates. The
    # adapter replaces its marker and exact signal set atomically.
    from verdict.schema import DriftRun

    evaluator_fingerprint = current_evaluator["evaluator_fingerprint"]
    run_id = _drift_run_id(
        args,
        analysis_time,
        tenant_scope,
        evaluator_fingerprint,
    )
    for sig in signals:
        sig.evaluator_fingerprint = evaluator_fingerprint
        sig.run_id = run_id
        sig.signal_id = _signal_id_for_run(sig, run_id)
        sig.detected_at = analysis_time
    storage.replace_drift_run(
        DriftRun(
            run_id=run_id,
            analysis_time=analysis_time,
            completed_at=datetime.now(timezone.utc),
            evaluator_fingerprint=evaluator_fingerprint,
            signal_count=len(signals),
        ),
        signals,
    )
    for sig in signals:
        print(
            f"    • cluster={sig.cluster_id} dim={sig.dimension} "
            f"dir={sig.direction.value} delta={sig.effect_size_cliffs_delta:.3f} "
            f"p_adj={sig.p_value_adjusted:.4f}"
        )

    print("\nDone. Inspect persisted signals with the Verdict dashboard or storage API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
