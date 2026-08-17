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
MT_BENCH_DATASET = "lmsys/mt_bench_human_judgments"
MT_BENCH_DATASET_REVISION = "f7d2896d2cc5d80f8b55c2bbc722613555233c25"
MIN_ALIGNMENT_PAIRS = 50
OFFLINE_SYNTHETIC_PAIRS = 120


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


def _integer(report: dict, *path: str) -> int:
    value = _number(report, *path)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"non-integer JSON field: {'.'.join(path)}")
    integer = int(value)
    if integer < 0:
        raise ValueError(f"negative JSON field: {'.'.join(path)}")
    return integer


def _ci(report: dict, *path: str) -> tuple[float, float] | None:
    value: object = report
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError(f"missing JSON field: {'.'.join(path)}")
        value = value[key]
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"invalid confidence interval: {'.'.join(path)}")
    holder = {"ci": {"lo": value[0], "hi": value[1]}}
    lo = _number(holder, "ci", "lo")
    hi = _number(holder, "ci", "hi")
    if lo > hi:
        raise ValueError(f"reversed confidence interval: {'.'.join(path)}")
    return float(lo), float(hi)


def _load_report(
    path: Path,
    provider: str,
    model: str,
    *,
    mode: str,
    expected_pairs: int,
) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read result JSON: {error}") from error
    if report.get("schemaVersion") != 1:
        raise ValueError("unsupported or missing schemaVersion")
    if report.get("mode") != mode:
        raise ValueError("result JSON identifies a different mode")
    if report.get("judge") != {"provider": provider, "model": model}:
        raise ValueError("result JSON identifies a different judge")
    expected_dataset = (
        {"name": MT_BENCH_DATASET, "revision": MT_BENCH_DATASET_REVISION}
        if mode == "online"
        else {"name": "synthetic", "revision": None}
    )
    if report.get("dataset") != expected_dataset:
        raise ValueError("result JSON identifies a different dataset or revision")
    if report.get("contextMode") != "full":
        raise ValueError("result JSON did not use the full-context comparison")
    available = _integer(report, "pairs", "available")
    scored = _integer(report, "pairs", "scored")
    required = expected_pairs if mode == "online" else OFFLINE_SYNTHETIC_PAIRS
    if available != required:
        raise ValueError(f"expected {required} available pairs, found {available}")
    if scored != available:
        raise ValueError(f"scored {scored} of {available} selected pairs")
    verdict = report.get("verdict")
    if not isinstance(verdict, dict):
        raise ValueError("missing JSON field: verdict")
    status = verdict.get("status")
    message = verdict.get("message")
    allowed_statuses = (
        {"acceptable", "preliminary", "inconclusive", "unreliable"}
        if mode == "online"
        else {"synthetic"}
    )
    if status not in allowed_statuses or not isinstance(message, str) or not message:
        raise ValueError("invalid verdict status or message")
    _number(report, "metrics", "threeWay", "cohensKappa")
    _number(report, "metrics", "threeWay", "gwetsAc2")
    ac2_3_ci = _ci(report, "metrics", "threeWay", "gwetsAc2Ci95")
    _number(report, "metrics", "binarized", "cohensKappa")
    kappa_bin_ci = _ci(report, "metrics", "binarized", "cohensKappaCi95")
    _number(report, "metrics", "binarized", "gwetsAc2")
    ac2_bin_ci = _ci(report, "metrics", "binarized", "gwetsAc2Ci95")
    _integer(report, "metrics", "binarized", "pairsKept")
    _number(report, "metrics", "nonTieAgreement")
    _integer(report, "metrics", "inconsistentCount")
    if status in {"acceptable", "preliminary", "synthetic"} and (
        ac2_3_ci is None or kappa_bin_ci is None or ac2_bin_ci is None
    ):
        raise ValueError("a successful evidence status requires computed confidence intervals")
    return report


def _failure_marker(result: SweepResult) -> str | None:
    if result.parse_error or result.report is None:
        return "RUN-FAIL" if result.returncode != 0 else "PARSE-FAIL"
    return None


def _format_score(result: SweepResult, metric: str) -> str:
    failure = _failure_marker(result)
    if failure:
        return failure
    assert result.report is not None
    n = int(_number(result.report, "metrics", "binarized", "pairsKept"))
    value = float(_number(result.report, "metrics", "binarized", metric))
    ci = _ci(result.report, "metrics", "binarized", f"{metric}Ci95")
    interval = "not computed" if ci is None else f"{ci[0]:.3f}, {ci[1]:.3f}"
    return f"{value:.3f} (n={n}; 95% CI {interval})"


def _format_counts(result: SweepResult) -> str:
    failure = _failure_marker(result)
    if failure:
        return failure
    assert result.report is not None
    scored = int(_number(result.report, "pairs", "scored"))
    available = int(_number(result.report, "pairs", "available"))
    return f"{scored}/{available}"


def _format_verdict(result: SweepResult) -> str:
    failure = _failure_marker(result)
    if failure:
        if result.parse_error:
            return f"{failure}: {result.parse_error}"
        return failure
    assert result.report is not None
    return str(result.report["verdict"]["status"]).upper()


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
    evidence_line = (
        f"Requested online pairs per judge: {pairs_per_judge}"
        if mode == "online"
        else "Evidence: 120 fixed synthetic pairs per judge; no provider judge or public dataset is used"
    )
    lines = [
        f"# Judge alignment sweep — {timestamp}",
        "",
        f"Mode: {mode}",
        evidence_line,
        "",
        "## Headline numbers",
        "",
        "| Judge | Evidence | Provider :: model | n scored/available | Binarized Gwet AC2 | Binarized Cohen κ | Gate |",
        "|---|---|---|---:|---|---|---|",
    ]
    for result in results:
        evidence = "SYNTHETIC WIRING ONLY" if mode == "offline" else "PINNED MT-BENCH + LIVE JUDGE"
        lines.append(
            f"| {result.label} | {evidence} | {result.provider} :: {result.model} | "
            f"{_format_counts(result)} | "
            f"{_format_score(result, 'gwetsAc2')} | "
            f"{_format_score(result, 'cohensKappa')} | "
            f"{_format_verdict(result)} |"
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
    if mode == "online":
        print(f"Requested online pairs per judge: {pairs_per_judge}\n")
    else:
        print(f"Synthetic wiring pairs per judge: {OFFLINE_SYNTHETIC_PAIRS} (fixed)\n")
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
        try:
            result.report = _load_report(
                json_path,
                provider,
                model,
                mode=mode,
                expected_pairs=pairs_per_judge,
            )
            status = result.report["verdict"]["status"]
            status_passed = status in {"acceptable", "preliminary", "synthetic"}
            if (completed.returncode == 0) != status_passed:
                raise ValueError(
                    f"verdict status {status!r} contradicts exit code {completed.returncode}"
                )
        except ValueError as error:
            result.parse_error = str(error)
        results.append(result)
        if completed.returncode != 0 and result.report is not None:
            print(f"   ✗ evidence gate not cleared (exit {completed.returncode}) → {text_path.name}")
        elif completed.returncode != 0:
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
    if args.mode == "online" and args.n < MIN_ALIGNMENT_PAIRS:
        parser.error(
            f"--n must be at least {MIN_ALIGNMENT_PAIRS} in online mode; "
            "smaller samples produce degenerate or unusably wide intervals"
        )
    return run_sweep(
        output=args.out,
        pairs_per_judge=args.n,
        mode=args.mode,
        verifier=args.verifier,
    )


if __name__ == "__main__":
    raise SystemExit(main())
