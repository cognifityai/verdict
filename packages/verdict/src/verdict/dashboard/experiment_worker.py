"""Run one explicitly configured local task function in a child process."""

from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
import re
import sys
import time

_RUNNER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*\Z")


def main() -> int:
    spec = os.environ.get("VERDICT_EXPERIMENT_RUNNER", "")
    if _RUNNER.fullmatch(spec) is None:
        return 2
    raw = sys.stdin.read(16_385)
    if len(raw) > 16_384:
        return 2
    try:
        task = json.loads(raw)
        module_name, function_name = spec.split(":", 1)
        with contextlib.redirect_stdout(sys.stderr):
            runner = getattr(importlib.import_module(module_name), function_name)
            started = time.perf_counter()
            result = runner(task["input"], task.get("context"), task["configuration"])
            elapsed = round((time.perf_counter() - started) * 1_000, 3)
        if isinstance(result, str):
            result = {"output": result}
        if not isinstance(result, dict) or not isinstance(result.get("output"), str):
            return 2
        output = result["output"]
        if len(output.encode("utf-8")) > 2_048:
            return 2
        usage = result.get("usage")
        if usage is not None and (
            not isinstance(usage, dict) or set(usage) - {"inputCount", "outputCount", "basis", "model"}
            or any(type(usage.get(name)) is not int or usage[name] < 0
                   for name in ("inputCount", "outputCount") if name in usage)
        ):
            return 2
        cost = result.get("costUsd")
        cost_basis = result.get("costBasis")
        if cost is not None and (
            isinstance(cost, bool) or not isinstance(cost, (int, float))
            or not math.isfinite(cost) or not 0 <= cost <= 10_000
            or not isinstance(cost_basis, str) or not 1 <= len(cost_basis) <= 64
        ):
            return 2
        payload = {"output": output, "taskElapsedMs": elapsed, "usage": usage,
                   "costUsd": cost, "costBasis": cost_basis if cost is not None else None}
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, OSError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
