"""Run the Verdict probe suite against a target model.

Designed to be scheduled (cron, GitHub Actions, k8s CronJob). Each run
writes a JSON file under `research/results/probe_runs/`; diff two runs
to detect drift.

Usage:
    .venv/bin/python scripts/run_probes.py \\
        --target-model anthropic/claude-haiku-4-5 \\
        --judge-model openai/gpt-4o-mini

    .venv/bin/python scripts/run_probes.py \\
        --suite /path/to/custom_suite.yaml \\
        --target-model gemini/gemini-2.5-flash \\
        --judge-model anthropic/claude-haiku-4-5

Schedule via cron:
    0 */6 * * *  cd /path/to/verdict && ./run_probes_cron.sh
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
    sys.exit(f"Unknown model prefix: {model!r}. Use anthropic/, openai/, or gemini/.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Verdict probe suite.")
    parser.add_argument("--suite", default=None,
                        help="YAML probe suite path. If omitted, uses the default bundled suite.")
    parser.add_argument("--target-model", required=True,
                        help="Target model (e.g. anthropic/claude-haiku-4-5)")
    parser.add_argument("--judge-model", required=True,
                        help="Judge model (different family from target recommended)")
    parser.add_argument("--out", default=None,
                        help="Output JSON path. Default: research/results/probe_runs/<timestamp>.json")
    args = parser.parse_args(argv)

    suite = load_suite_yaml(args.suite) if args.suite else default_suite()
    print(f"Suite: {suite.name} v{suite.version}  probes: {len(suite.probes)}")

    target_provider, target_model = _provider_for_model(args.target_model)
    judge_provider, judge_model = _provider_for_model(args.judge_model)

    runner = ProbeRunner(
        target_provider=target_provider,
        target_model=target_model,
        judge_provider=judge_provider,
        judge_model=judge_model,
    )
    print(f"Target: {args.target_model}  Judge: {args.judge_model}")
    print("Running probes...")
    run = runner.run_suite(suite)

    print(f"\nPass rate: {run.pass_rate:.2%}  ({sum(1 for r in run.results if r.overall_passed)}/{len(run.results)})")
    print("By category:")
    for cat, rate in run.pass_rate_by_category().items():
        print(f"  {cat:25s} {rate:.2%}")
    print()
    for r in run.results:
        mark = "PASS" if r.overall_passed else "FAIL"
        print(f"  [{mark}] {r.probe_id:35s} ({r.category})")
        for d in r.dimensions:
            if not d["passed"]:
                print(f"       └─ {d['name']}: expected={d['expected']} observed={d['observed']}")
                if d.get("judge_reasoning"):
                    print(f"          {d['judge_reasoning'][:150]}")

    # Persist. Default output dir is ./probe_runs/ at the repo root; override
    # with --out or the VERDICT_PROBE_DIR env var.
    if args.out:
        out_path = Path(args.out)
    else:
        repo_root = Path(__file__).resolve().parent.parent
        out_dir = Path(os.environ.get("VERDICT_PROBE_DIR", repo_root / "probe_runs"))
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / (
            f"{run.suite_name}_{run.target_model.replace('/', '_')}_{ts}.json"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(asdict(run), indent=2, default=str))
    print(f"\nWrote: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
