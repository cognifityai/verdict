"""Command-line entry point for importing existing telemetry."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from verdict.client import _resolve_storage
from verdict.telemetry.files import SUPPORTED_FORMATS, iter_telemetry_file
from verdict.telemetry.http import JsonHttpClient
from verdict.telemetry.local_agents import capture_local_agents
from verdict.telemetry.model import ImportContext
from verdict.telemetry.normalize import parse_datetime
from verdict.telemetry.receiver import OtlpHttpReceiver
from verdict.telemetry.runner import ImportRunError, import_into_storage
from verdict.telemetry.sources.datadog import DatadogApiSource
from verdict.telemetry.sources.langfuse import LangfuseApiSource
from verdict.telemetry.sources.langsmith import LangSmithApiSource
from verdict.telemetry.sources.opik import OpikApiSource
from verdict.telemetry.sources.phoenix import PhoenixApiSource


def _add_storage(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--storage", default="sqlite:///./verdict.db")
    parser.add_argument("--tenant-id")
    parser.add_argument("--source-scope")


def _add_api_window(parser: argparse.ArgumentParser) -> None:
    _add_storage(parser)
    parser.add_argument("--from", dest="start_time", required=True, help="Inclusive ISO-8601 start")
    parser.add_argument("--to", dest="end_time", required=True, help="Exclusive ISO-8601 end")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--page-size", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verdict-import",
        description="Import existing LLM telemetry into Verdict's current Trace storage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    file_parser = sub.add_parser("file", help="Import JSON, JSONL, or NDJSON telemetry")
    file_parser.add_argument("path", type=Path)
    file_parser.add_argument("--format", choices=sorted(SUPPORTED_FORMATS), default="auto")
    _add_storage(file_parser)

    local = sub.add_parser("local", help="Capture local Claude Code and Codex histories")
    local.add_argument("--claude-root", type=Path, default=Path("~/.claude/projects"))
    local.add_argument("--codex-root", type=Path, default=Path("~/.codex/sessions"))
    local.set_defaults(capture_content=True)
    local.add_argument(
        "--no-capture-content",
        action="store_false",
        dest="capture_content",
        help="Store metadata only; bounded redacted content is retained by default",
    )
    _add_storage(local)

    langfuse = sub.add_parser("langfuse", help="Read Langfuse observations API v2")
    langfuse.add_argument("--base-url", default="https://cloud.langfuse.com")
    _add_api_window(langfuse)

    langsmith = sub.add_parser("langsmith", help="Read LangSmith LLM runs")
    langsmith.add_argument("--base-url", default="https://api.smith.langchain.com")
    langsmith.add_argument("--project", required=True)
    _add_api_window(langsmith)

    datadog = sub.add_parser("datadog", help="Read Datadog LLM Observability spans")
    datadog.add_argument("--base-url", default="https://api.datadoghq.com")
    _add_api_window(datadog)

    phoenix = sub.add_parser("phoenix", help="Read Phoenix OpenInference traces")
    phoenix.add_argument("--base-url", required=True)
    phoenix.add_argument("--project", required=True)
    _add_api_window(phoenix)

    opik = sub.add_parser("opik", help="Read Opik LLM spans")
    opik.add_argument("--base-url", default="https://www.comet.com/opik/api")
    opik.add_argument("--project", required=True)
    _add_api_window(opik)

    receiver = sub.add_parser("receive-otlp", help="Run a loopback OTLP/HTTP trace receiver")
    receiver.add_argument("--host", default="127.0.0.1")
    receiver.add_argument("--port", type=int, default=4318)
    receiver.add_argument("--max-request-bytes", type=int, default=16 * 1024 * 1024)
    _add_storage(receiver)
    return parser


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required environment variable {name} is not set")
    return value


def _window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    start = parse_datetime(args.start_time)
    end = parse_datetime(args.end_time)
    if start is None or end is None:
        raise ValueError("--from and --to must be valid ISO-8601 timestamps")
    if end <= start:
        raise ValueError("--to must be after --from")
    return start, end


def _scope(args: argparse.Namespace) -> str:
    if args.source_scope:
        return args.source_scope
    if args.command == "file":
        return str(args.path.resolve())
    if args.command == "local":
        return "local-agent-history"
    base = getattr(args, "base_url", "otlp")
    project = getattr(args, "project", "")
    return f"{base}|{project}" if project else str(base)


def _open_storage(url: str):
    try:
        return _resolve_storage(url)
    except Exception as exc:
        backend = url.split(":", 1)[0] if ":" in url else "configured"
        if backend not in {"memory", "postgres", "postgresql", "sqlite"}:
            backend = "configured"
        raise ValueError(f"cannot open {backend} storage ({type(exc).__name__})") from exc


def _api_source(args: argparse.Namespace, context: ImportContext):
    start, end = _window(args)
    client = JsonHttpClient(timeout_seconds=args.timeout)
    common = {
        "client": client,
        "context": context,
        "base_url": args.base_url,
        "start_time": start,
        "end_time": end,
    }
    if args.command == "langfuse":
        return LangfuseApiSource(
            **common,
            public_key=_required_env("LANGFUSE_PUBLIC_KEY"),
            secret_key=_required_env("LANGFUSE_SECRET_KEY"),
            page_size=args.page_size or 100,
        )
    if args.command == "langsmith":
        return LangSmithApiSource(
            **common,
            api_key=_required_env("LANGSMITH_API_KEY"),
            project_name=args.project,
            page_size=args.page_size or 100,
        )
    if args.command == "datadog":
        return DatadogApiSource(
            **common,
            api_key=_required_env("DD_API_KEY"),
            app_key=_required_env("DD_APP_KEY"),
            page_size=args.page_size or 100,
        )
    if args.command == "phoenix":
        return PhoenixApiSource(
            **common,
            project=args.project,
            api_key=os.environ.get("PHOENIX_API_KEY"),
            page_size=args.page_size or 100,
        )
    if args.command == "opik":
        return OpikApiSource(
            **common,
            project=args.project,
            api_key=os.environ.get("OPIK_API_KEY"),
            workspace=os.environ.get("OPIK_WORKSPACE"),
            page_size=args.page_size or 500,
        )
    raise ValueError("unsupported API source")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "local":
            storage = _open_storage(args.storage)
            try:
                summary = capture_local_agents(
                    storage,
                    tenant_id=args.tenant_id or "__verdict_local__",
                    claude_root=args.claude_root.expanduser(),
                    codex_root=args.codex_root.expanduser(),
                    capture_content=args.capture_content,
                )
            finally:
                storage.close()
            print(json.dumps(summary.as_dict(), sort_keys=True))
            return 0
        context = ImportContext(
            adapter="otlp" if args.command == "receive-otlp" else args.command,
            source_scope=_scope(args),
            tenant_id=args.tenant_id,
        )
        if args.command == "receive-otlp":
            storage = _open_storage(args.storage)
            try:
                receiver = OtlpHttpReceiver(
                    storage=storage,
                    context=context,
                    host=args.host,
                    port=args.port,
                    max_request_bytes=args.max_request_bytes,
                )
                print(f"Verdict OTLP/HTTP listening on http://{args.host}:{args.port}/v1/traces")
                try:
                    receiver.serve_forever()
                except KeyboardInterrupt:
                    pass
            finally:
                storage.close()
            return 0
        if args.command == "file":
            results = iter_telemetry_file(args.path, file_format=args.format, context=context)
        else:
            results = _api_source(args, context)
        storage = _open_storage(args.storage)
        try:
            summary = import_into_storage(results, storage)
        finally:
            storage.close()
        print(
            json.dumps(
                {
                    "seen": summary.seen,
                    "stored": summary.stored,
                    "skipped": summary.skipped,
                    "skip_reasons": summary.skip_reasons,
                },
                sort_keys=True,
            )
        )
        return 0
    except (ImportRunError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
