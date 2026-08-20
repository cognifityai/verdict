"""Bounded, process-local measurements of Verdict's capture path."""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Any


class RuntimeMetrics:
    """Record capture overhead without adding I/O or background work."""

    def __init__(self, *, max_samples: int = 2048) -> None:
        self._lock = threading.Lock()
        self._overhead_ms: deque[float] = deque(maxlen=max_samples)
        self._attempts = 0
        self._failures = 0

    def record_capture(self, overhead_ms: float, *, failed: bool) -> None:
        value = float(overhead_ms)
        with self._lock:
            self._attempts += 1
            self._failures += int(failed)
            if math.isfinite(value) and value >= 0:
                self._overhead_ms.append(value)

    def snapshot(self, storage: Any) -> dict[str, Any]:
        with self._lock:
            values = sorted(self._overhead_ms)
            attempts = self._attempts
            failures = self._failures
        buffer_snapshot = getattr(storage, "telemetry_snapshot", None)
        buffer = buffer_snapshot() if callable(buffer_snapshot) else {"enabled": False}
        return {
            "capture": {
                "attempts": attempts,
                "failures": failures,
                "overhead_ms": {
                    "samples": len(values),
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                    "p99": _percentile(values, 0.99),
                    "max": round(values[-1], 3) if values else None,
                },
            },
            "buffer": buffer,
        }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(fraction * len(values)) - 1)
    return round(values[index], 3)
