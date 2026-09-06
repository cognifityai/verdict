"""One-shot scheduled monitor runner over durable policy state."""

from __future__ import annotations

import argparse
import json
import sys

from verdict.client import _resolve_storage
from verdict.monitor_inputs import LOCAL_TENANT, LOCAL_TRACE_SCOPE, advance_monitor

_LOCAL_TRACE_SCOPE = LOCAL_TRACE_SCOPE


def run_active_monitor(storage) -> dict[str, object]:
    policy = storage.get_active_monitor_policy(_LOCAL_TRACE_SCOPE)
    if policy is None:
        raise ValueError("no active monitor")
    manifest, comparison = advance_monitor(
        storage,
        policy,
        tenant_id=LOCAL_TENANT,
    )
    return {
        "policy_id": policy.policy_id,
        "snapshot_id": manifest.snapshot_id,
        "status": comparison.status.value,
        "reference_units": len(manifest.reference_unit_ids),
        "current_units": len(manifest.current_unit_ids),
        "late_units": manifest.late_unit_count,
        "alerts": sum(metric.alert for metric in comparison.metrics),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verdict-monitor",
        description="Run one idempotent cycle of the active Verdict monitor.",
    )
    parser.add_argument("run", nargs="?")
    parser.add_argument("--storage", default="sqlite:///./verdict.db")
    args = parser.parse_args(argv)
    storage = None
    try:
        storage = _resolve_storage(args.storage)
        print(json.dumps(run_active_monitor(storage), sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if storage is not None:
            storage.close()
