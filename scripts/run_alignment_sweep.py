"""Run and summarize the multi-provider judge-alignment sweep.

The verifier owns the metric calculations and emits a versioned JSON result.
This orchestrator never scrapes the verifier's human-formatted prose.
"""

from __future__ import annotations

import argparse
import json
import math
import os

# Commands use a fixed argv vector and never enable a shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = (
    ("01_haiku", "anthropic", "claude-haiku-4-5"),
    ("02_gpt4omini", "openai", "gpt-4o-mini"),
    ("03_gemini", "google", "gemini-2.5-flash"),
    ("04_sonnet", "anthropic", "claude-sonnet-4-5"),
)


@dataclass
class SweepResult:
    label: str
    provider: str
    model: str
    text_path: Path
    json_path: Path
    returncode: int
    report: dict | None = None
    parse_error: str | None = None


def _number(report: dict, *path: str) -> int | float:
    value: object = report
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"missing JSON field: {'.'.join(path)}")
        value = value[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"non-numeric JSON field: {'.'.join(path)}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite JSON field: {'.'.join(path)}")
    return value


def _load_report(path: Path, provider: str, model: str) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read result JSON: {error}") from error
    if report.get("schemaVersion") != 1:
        raise ValueError("unsupported or missing schemaVersion")
    if report.get("judge") != {"provider": provider, "model": model}:
        raise ValueError("result JSON identifies a different judge")
    _number(report, "metrics", "threeWay", "cohensKappa")
    _number(report, "metrics", "binarized", "cohensKappa")
    _number(report, "metrics", "nonTieAgreement")
    _number(report, "metrics", "inconsistentCount")
    return report


def _format_metric(result: SweepResult, *path: str) -> str:
    if result.returncode != 0:
        return "RUN-FAIL"
    if result.parse_error or result.report is None:
        return "PARSE-FAIL"
    value = _number(result.report, *path)
    return str(value) if isinstance(value, int) else f"{value:.3f}"


def _tail(path: Path, count: int = 25) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[-count:])
    except OSError as error:
        return f"MISSING: {error}"


def _write_summary(
    output: Path,
    *,
    timestamp: str,
    mode: str,
    pairs_per_judge: int,
    results: list[SweepResult],
) -> Path:
    lines = [
        f"# Judge alignment sweep — {timestamp}",
        "",
        f"Mode: {mode}",
        f"Requested pairs per judge: {pairs_per_judge}",
        "",
        "## Headline numbers",
        "",
        "| Judge | Provider :: model | 3-way κ | Binarized κ | Non-tie agree | Inconsistent |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.label} | {result.provider} :: {result.model} | "
            f"{_format_metric(result, 'metrics', 'threeWay', 'cohensKappa')} | "
            f"{_format_metric(result, 'metrics', 'binarized', 'cohensKappa')} | "
            f"{_format_metric(result, 'metrics', 'nonTieAgreement')} | "
            f"{_format_metric(result, 'metrics', 'inconsistentCount')} |"
        )
    lines.extend(["", "## Per-judge details", ""])
    for result in results:
        lines.extend([
            f"### {result.label}",
            "```",
            _tail(result.text_path),
        ])
        if result.parse_error:
            lines.append(f"PARSE-FAIL: {result.parse_error}")
        lines.extend(["```", ""])
    summary = output / "SUMMARY.md"
    summary.write_text("\n".join(lines), encoding="utf-8")
    return summary


def run_sweep(
    *,
    output: Path,
    pairs_per_judge: int,
    mode: str,
    verifier: Path,
    runs: tuple[tuple[str, str, str], ...] = DEFAULT_RUNS,
) -> int:
    output.mkdir(parents=True, exist_ok=True)
    print(f"Saving to: {output}")
    print(f"Pairs per judge: {pairs_per_judge}\n")
    results: list[SweepResult] = []
    for label, provider, model in runs:
        text_path = output / f"{label}.txt"
        json_path = output / f"{label}.json"
        # A verifier that exits zero without writing a result must be a parse
        # failure, never a false success backed by a previous sweep's file.
        json_path.unlink(missing_ok=True)
        print(f"── Running {label} ({provider} :: {model}) ──")
        command = [
            sys.executable,
            str(verifier),
            "--mode",
            mode,
            "--provider",
            provider,
            "--judge-model",
            model,
            "--n",
            str(pairs_per_judge),
            "--json-output",
            str(json_path),
        ]
        with text_path.open("w", encoding="utf-8") as stream:
            # The executable is the current interpreter and every argument is
            # passed as a discrete argv element; shell execution is disabled.
            completed = subprocess.run(  # nosec B603
                command,
                cwd=REPO_DIR,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        result = SweepResult(
            label=label,
            provider=provider,
            model=model,
            text_path=text_path,
            json_path=json_path,
            returncode=completed.returncode,
        )
        if completed.returncode == 0:
            try:
                result.report = _load_report(json_path, provider, model)
            except ValueError as error:
                result.parse_error = str(error)
        results.append(result)
        if completed.returncode != 0:
            print(f"   ✗ failed (exit {completed.returncode}) → {text_path.name}")
        elif result.parse_error:
            print(f"   ✗ invalid result JSON → {json_path.name}: {result.parse_error}")
        else:
            print(f"   ✓ done → {text_path.name}, {json_path.name}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary = _write_summary(
        output,
        timestamp=timestamp,
        mode=mode,
        pairs_per_judge=pairs_per_judge,
        results=results,
    )
    print(f"\n=== Done ===\nSummary: {summary}\n")
    print("\n".join(summary.read_text(encoding="utf-8").splitlines()[:20]))
    return 1 if any(
        result.returncode != 0 or result.parse_error
        for result in results
    ) else 0


def main() -> int:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["offline", "online"],
        default=os.environ.get("ALIGNMENT_MODE", "online"),
    )
    parser.add_argument(
        "--n",
        type=int,
        default=int(os.environ.get("ALIGNMENT_N", "50")),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(os.environ.get(
            "ALIGNMENT_OUT",
            REPO_DIR / "research" / "results" / f"alignment-sweep-{timestamp}",
        )),
    )
    parser.add_argument(
        "--verifier",
        type=Path,
        default=Path(os.environ.get(
            "ALIGNMENT_VERIFIER",
            REPO_DIR / "scripts" / "verify_judge_alignment.py",
        )),
    )
    args = parser.parse_args()
    if args.n <= 0:
        parser.error("--n must be greater than zero")
    return run_sweep(
        output=args.out,
        pairs_per_judge=args.n,
        mode=args.mode,
        verifier=args.verifier,
    )


if __name__ == "__main__":
    raise SystemExit(main())
