"""Bounded one-shot session evaluator for a local host scheduler."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from verdict.client import _resolve_storage
from verdict.sessions import MAX_RUBRIC_BYTES, key

from verdict_eval.session_evaluation import execute_evaluation, preview_evaluation


@contextmanager
def evaluation_lock(storage_url, tenant):
    """Host/process lock; deployment must designate one inference worker."""
    import fcntl

    identity = storage_url
    if storage_url.startswith("sqlite:///"):
        target = Path(storage_url[len("sqlite:///") :]).resolve()
        if target.exists():
            stat = target.stat()
            identity = f"sqlite:{stat.st_dev}:{stat.st_ino}"
        else:
            identity = f"sqlite:{target}"
    name = hashlib.sha256(f"{identity}\0{tenant}".encode()).hexdigest()
    path = Path(tempfile.gettempdir()) / f"verdict-evaluation-{name}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another evaluation worker is running") from None
        yield
    finally:
        os.close(fd)


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Preview or execute one bounded rubric evaluation and optional Monitor cycle. No credentials belong in rubric files."
    )
    p.add_argument(
        "--storage", default=os.environ.get("VERDICT_STORAGE_URL", "sqlite:///./verdict.db")
    )
    p.add_argument("--tenant", default="__verdict_local__")
    p.add_argument("--rubric-file", required=True)
    p.add_argument("--provider", choices=["openai", "anthropic", "google"], default="openai")
    p.add_argument("--model", required=True)
    p.add_argument("--max-calls", type=int, default=20)
    p.add_argument("--max-output-tokens", type=int, default=4096)
    p.add_argument("--execute", action="store_true")
    p.add_argument(
        "--approve-inference",
        action="store_true",
        help="Authorize selected transcript egress to configured provider/compatible endpoint",
    )
    p.add_argument(
        "--monitor",
        action="store_true",
        help="Advance the existing active conversation Monitor after judging",
    )
    args = p.parse_args(argv)
    storage = None
    try:
        key(args.tenant)
        raw = Path(args.rubric_file).read_bytes()
        if len(raw) > MAX_RUBRIC_BYTES:
            raise ValueError("rubric exceeds byte limit")
        config = {
            "rubric": json.loads(raw),
            "provider": args.provider,
            "model": args.model,
            "maxCalls": args.max_calls,
            "maxOutputTokens": args.max_output_tokens,
        }
        if args.execute and not args.approve_inference:
            raise ValueError("--execute requires --approve-inference")
        storage = _resolve_storage(args.storage)
        with evaluation_lock(args.storage, args.tenant):
            preview = preview_evaluation(storage, tenant_id=args.tenant, config=config)
            if args.execute:
                from verdict_eval.providers import get_provider

                result = execute_evaluation(
                    storage,
                    tenant_id=args.tenant,
                    config={
                        **config,
                        "plannedSessions": preview["plannedSessions"],
                        "planFingerprint": preview["planFingerprint"],
                    },
                    confirm_external_egress=True,
                    provider=get_provider(args.provider),
                )
            else:
                result = {
                    k: v for k, v in preview.items() if k not in {"rubric", "plannedSessions"}
                }
            if args.monitor:
                if not args.execute:
                    raise ValueError("--monitor requires --execute")
                from verdict.monitor_cli import run_active_monitor

                result["monitor"] = run_active_monitor(
                    storage, tenant_id=args.tenant, analysis_unit="conversation"
                )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, ImportError):
        print(
            json.dumps(
                {
                    "error": "evaluation cycle unavailable; check rubric, approval, budget, storage and configured provider"
                }
            )
        )
        return 2
    finally:
        if storage is not None:
            storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
