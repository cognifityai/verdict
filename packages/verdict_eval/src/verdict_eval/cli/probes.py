"""Run the Verdict probe suite against a target model.

Designed to be scheduled (cron, GitHub Actions, k8s CronJob). Each run
writes a JSON file under `./probe_runs/` by default; diff two runs
to detect drift.

Usage:
    verdict-probes \\
        --target-model anthropic/claude-haiku-4-5 \\
        --judge-model openai/gpt-4o-mini

    verdict-probes \\
        --suite /path/to/custom_suite.yaml \\
        --target-model gemini/gemini-2.5-flash \\
        --judge-model anthropic/claude-haiku-4-5

"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from verdict_eval.probes import (
    ProbeRunner,
    default_suite,
    load_suite_yaml,
)
from verdict_eval.providers import (
    AnthropicAdapter,
    GoogleAdapter,
    OpenAIAdapter,
)


def _pass_rate(value: str) -> float:
    rate = float(value)
    if not 0.0 <= rate <= 1.0:
        raise argparse.ArgumentTypeError("pass-rate threshold must be between 0 and 1")
    return rate


def probe_run_exit_code(run, *, min_pass_rate: float) -> int:
    """Return 2 for execution errors, 1 for a failed quality gate, else 0."""
    has_execution_error = any(
        result.error
        or any(
            isinstance(dimension, dict)
            and dimension.get("observed") == "ERROR"
            for dimension in (
                result.dimensions if isinstance(result.dimensions, list) else []
            )
        )
        for result in run.results
    )
    if has_execution_error:
        return 2
    return 0 if run.pass_rate >= min_pass_rate else 1


def probe_run_payload(run) -> dict[str, object]:
    """Serialize a run with the derived metrics that operators gate on."""
    passed_weight, total_weight = run.weighted_probe_counts()
    expectation_passed, expectation_total = run.weighted_expectation_counts()
    payload = asdict(run)
    payload["summary"] = {
        "probe_pass_rate": run.pass_rate,
        "probe_passed_weight": passed_weight,
        "probe_total_weight": total_weight,
        "probe_pass_rate_by_category": run.pass_rate_by_category(),
        "expectation_agreement": run.expectation_agreement,
        "expectation_passed_weight": expectation_passed,
        "expectation_total_weight": expectation_total,
        "expectation_agreement_by_dimension": run.pass_rate_by_dimension(),
    }
    return payload


def _provider_for_model(model: str):
    """Map "vendor/model-name" → adapter instance."""
    if model.startswith("anthropic/"):
        api_key = os.environ.get("ANTHROPIC_API_KEY") or ""
        return AnthropicAdapter(api_key=api_key), model.split("/", 1)[1]
    if model.startswith("openai/"):
        api_key = os.environ.get("OPENAI_API_KEY") or ""
        return OpenAIAdapter(api_key=api_key), model.split("/", 1)[1]
    if model.startswith("gemini/"):
        api_key = os.environ.get("GOOGLE_API_KEY") or ""
        return GoogleAdapter(api_key=api_key), model.split("/", 1)[1]
    raise ValueError(
        f"Unknown model prefix: {model!r}. Use anthropic/, openai/, or gemini/."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Verdict probe suite.")
    parser.add_argument("--suite", default=None,
                        help="YAML probe suite path. If omitted, uses the default bundled suite.")
    parser.add_argument("--target-model", required=True,
                        help="Target model (e.g. anthropic/claude-haiku-4-5)")
    parser.add_argument("--judge-model", required=True,
                        help="Judge model (different family from target recommended)")
    parser.add_argument("--out", default=None,
                        help="Output JSON path. Default: ./probe_runs/<timestamp>.json")
    parser.add_argument(
        "--min-pass-rate",
        type=_pass_rate,
        default=1.0,
        help=(
            "Required weighted probe pass rate (default 1.0). Below it "
            "exits 1; provider/judge execution errors exit 2."
        ),
    )
    args = parser.parse_args(argv)

    suite = load_suite_yaml(args.suite) if args.suite else default_suite()
    print(f"Suite: {suite.name} v{suite.version}  probes: {len(suite.probes)}")

    try:
        target_provider, target_model = _provider_for_model(args.target_model)
        judge_provider, judge_model = _provider_for_model(args.judge_model)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    runner = ProbeRunner(
        target_provider=target_provider,
        target_model=target_model,
        judge_provider=judge_provider,
        judge_model=judge_model,
    )
    print(f"Target: {args.target_model}  Judge: {args.judge_model}")
    print("Running probes...")
    run = runner.run_suite(suite)

    passed_weight, total_weight = run.weighted_probe_counts()
    expectation_passed, expectation_total = run.weighted_expectation_counts()
    print(
        f"\nWeighted probe pass rate: {run.pass_rate:.2%} "
        f"({passed_weight:g}/{total_weight:g} weight passed)"
    )
    print(
        f"Weighted expectation agreement (diagnostic): "
        f"{run.expectation_agreement:.2%} "
        f"({expectation_passed:g}/{expectation_total:g} weight passed)"
    )
    print(
        "Methodology: metric schema "
        f"v{run.metric_schema_version}; judge method v{run.judge_method_version}"
    )
    print("Weighted by category:")
    for cat, rate in run.pass_rate_by_category().items():
        print(f"  {cat:25s} {rate:.2%}")
    print("Weighted expectation agreement by dimension:")
    for dimension, rate in run.pass_rate_by_dimension().items():
        print(f"  {dimension:25s} {rate:.2%}")
    print()
    for r in run.results:
        mark = "PASS" if r.overall_passed else "FAIL"
        print(f"  [{mark}] {r.probe_id:35s} ({r.category})")
        for d in r.dimensions:
            if not d["passed"]:
                print(f"       └─ {d['name']}: expected={d['expected']} observed={d['observed']}")
                if d.get("judge_reasoning"):
                    print(f"          {d['judge_reasoning'][:150]}")

    # Persist beside the operator's working directory unless explicitly set.
    if args.out:
        out_path = Path(args.out)
    else:
        out_dir = Path(os.environ.get("VERDICT_PROBE_DIR", "probe_runs"))
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / (
            f"{run.suite_name}_{run.target_model.replace('/', '_')}_{ts}.json"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(probe_run_payload(run), indent=2, default=str))
    print(f"\nWrote: {out_path}")

    exit_code = probe_run_exit_code(run, min_pass_rate=args.min_pass_rate)
    if exit_code == 2:
        print("ERROR: one or more probes had target/judge execution errors.")
    elif exit_code == 1:
        print(
            "ERROR: weighted probe pass rate "
            f"{run.pass_rate:.2%} is below the required "
            f"{args.min_pass_rate:.2%}."
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
