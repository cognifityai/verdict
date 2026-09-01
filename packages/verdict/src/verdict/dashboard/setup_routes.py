"""Setup and historical-import routes for the local dashboard.

This capability owns the in-process preview approvals.  It deliberately keeps
those ephemeral approvals separate from durable schedule configuration.
"""

from __future__ import annotations

import secrets
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.analysis_service import run_analysis
from verdict.telemetry.files import SUPPORTED_FORMATS, iter_telemetry_file
from verdict.telemetry.local_agents import capture_local_agents
from verdict.telemetry.model import ImportContext
from verdict.telemetry.runner import ImportRunError, import_into_storage

_MAX_FILES_PREVIEW = 10_000
LOCAL_SCOPE = "__verdict_local__"


def _is_postgres(storage_url: str) -> bool:
    return storage_url.startswith(("postgres://", "postgresql://"))


def _sqlite_path(storage_url: str) -> Path:
    return Path(
        storage_url[len("sqlite:///") :]
        if storage_url.startswith("sqlite:///")
        else storage_url
    )


class SetupRoutes:
    """Register setup routes and expose their shared authorization boundary."""

    def __init__(self, storage_url: str, setup_token: str) -> None:
        self.storage_url = storage_url
        self.setup_token = setup_token
        self._previewed_local_roots: set[tuple[str | None, str | None]] = set()
        self._previewed_imports: set[tuple[str, str]] = set()

    def authorized(self, request: Request) -> bool:
        supplied = request.headers.get("x-verdict-setup", "")
        return bool(supplied) and secrets.compare_digest(supplied, self.setup_token)

    def roots(self, payload: dict[str, Any]) -> tuple[Path | None, Path | None]:
        roots: list[Path | None] = []
        for key in ("claudeRoot", "codexRoot"):
            value = payload.get(key)
            if value is None:
                roots.append(None)
                continue
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 4096
            ):
                raise ValueError("invalid source path")
            roots.append(Path(value).expanduser())
        return roots[0], roots[1]

    def writable_storage(self):
        if _is_postgres(self.storage_url):
            from verdict.storage.postgres import PostgresStorage

            return PostgresStorage(self.storage_url)
        from verdict.storage.sqlite import SQLiteStorage

        return SQLiteStorage(str(_sqlite_path(self.storage_url)))

    @staticmethod
    def _approved_path(path: Path | None) -> str | None:
        return str(path.resolve()) if path is not None else None

    @staticmethod
    def _import_candidates(payload: dict[str, Any]) -> tuple[Path, str, list[Path]]:
        raw_path = payload.get("path")
        file_format = payload.get("format", "auto")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or len(raw_path.encode("utf-8")) > 4096
            or file_format not in SUPPORTED_FORMATS
        ):
            raise ValueError("invalid import request")
        path = Path(raw_path).expanduser()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("approved import path is unavailable")
        if path.is_dir():
            candidates = [
                item
                for item in sorted(path.rglob("*"))
                if item.is_file()
                and not item.is_symlink()
                and item.suffix.lower() in {".json", ".jsonl", ".ndjson"}
            ]
            if len(candidates) > _MAX_FILES_PREVIEW:
                raise ValueError("approved import directory exceeds the file limit")
        else:
            candidates = [path]
        return path, file_format, candidates

    def register(self, app) -> None:
        @app.get("/api/setup/token")
        def setup_token_response():
            return {"setupToken": self.setup_token}

        def setup_preview(request, payload: dict[str, Any]):
            if not self.authorized(request):
                return JSONResponse(
                    {"error": "setup authorization required"}, status_code=403
                )
            try:
                claude_root, codex_root = self.roots(payload)
            except (TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "invalid setup request"}, status_code=400)

            def preview(root: Path | None) -> dict[str, Any]:
                if root is None:
                    return {"approvedPath": None, "exists": False, "files": 0}
                files: list[Path] = []
                file_limit_reached = False
                if root.is_dir() and not root.is_symlink():
                    for path in root.rglob("*.jsonl"):
                        if not path.is_file() or path.is_symlink():
                            continue
                        if len(files) == _MAX_FILES_PREVIEW:
                            file_limit_reached = True
                            break
                        files.append(path)
                modified = [path.stat().st_mtime for path in files]
                return {
                    "approvedPath": str(root),
                    "exists": root.is_dir(),
                    "files": len(files),
                    "modifiedFrom": (
                        datetime.fromtimestamp(
                            min(modified), timezone.utc
                        ).isoformat()
                        if modified
                        else None
                    ),
                    "modifiedTo": (
                        datetime.fromtimestamp(
                            max(modified), timezone.utc
                        ).isoformat()
                        if modified
                        else None
                    ),
                    "fileLimitReached": file_limit_reached,
                }

            result = {
                "claude": preview(claude_root),
                "codex": preview(codex_root),
            }
            self._previewed_local_roots.clear()
            self._previewed_local_roots.add(
                (self._approved_path(claude_root), self._approved_path(codex_root))
            )
            return result

        setup_preview.__annotations__["request"] = Request
        app.post("/api/setup/preview")(setup_preview)

        def setup_capture(request, payload: dict[str, Any]):
            if not self.authorized(request):
                return JSONResponse(
                    {"error": "setup authorization required"}, status_code=403
                )
            try:
                claude_root, codex_root = self.roots(payload)
                if claude_root is None and codex_root is None:
                    raise ValueError("source approval required")
                root_key = (
                    self._approved_path(claude_root),
                    self._approved_path(codex_root),
                )
                if root_key not in self._previewed_local_roots:
                    return JSONResponse(
                        {"error": "preview these exact source paths before capture"},
                        status_code=409,
                    )
                capture_content = payload.get("captureContent", True)
                if not isinstance(capture_content, bool):
                    raise ValueError("captureContent must be boolean")
                writable = self.writable_storage()
                try:
                    summary = capture_local_agents(
                        writable,
                        tenant_id=LOCAL_SCOPE,
                        claude_root=claude_root,
                        codex_root=codex_root,
                        capture_content=capture_content,
                    )
                finally:
                    writable.close()
                self._previewed_local_roots.discard(root_key)
                analysis = self._run_analysis()
                return {
                    "summary": summary.as_dict(),
                    "analysis": analysis["analysisState"],
                }
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "invalid setup request"}, status_code=400)

        setup_capture.__annotations__["request"] = Request
        app.post("/api/setup/capture")(setup_capture)

        def setup_import_preview(request, payload: dict[str, Any]):
            if not self.authorized(request):
                return JSONResponse(
                    {"error": "setup authorization required"}, status_code=403
                )
            try:
                path, file_format, candidates = self._import_candidates(payload)
                modified = [candidate.stat().st_mtime for candidate in candidates]
                total_bytes = sum(candidate.stat().st_size for candidate in candidates)
                self._previewed_imports.clear()
                self._previewed_imports.add((str(path.resolve()), file_format))
                return {
                    "approvedPath": str(path),
                    "format": file_format,
                    "files": len(candidates),
                    "bytes": total_bytes,
                    "modifiedFrom": (
                        datetime.fromtimestamp(
                            min(modified), timezone.utc
                        ).isoformat()
                        if modified
                        else None
                    ),
                    "modifiedTo": (
                        datetime.fromtimestamp(
                            max(modified), timezone.utc
                        ).isoformat()
                        if modified
                        else None
                    ),
                }
            except (OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "invalid import request"}, status_code=400)

        setup_import_preview.__annotations__["request"] = Request
        app.post("/api/setup/import/preview")(setup_import_preview)

        def setup_import(request, payload: dict[str, Any]):
            if not self.authorized(request):
                return JSONResponse(
                    {"error": "setup authorization required"}, status_code=403
                )
            try:
                path, file_format, candidates = self._import_candidates(payload)
                import_key = (str(path.resolve()), file_format)
                if import_key not in self._previewed_imports:
                    return JSONResponse(
                        {"error": "preview this exact import path and format first"},
                        status_code=409,
                    )
                writable = self.writable_storage()
                try:
                    seen = stored = skipped = 0
                    reasons: Counter[str] = Counter()
                    for candidate in candidates:
                        context = ImportContext(
                            adapter="file", source_scope=str(candidate.resolve())
                        )
                        summary = import_into_storage(
                            iter_telemetry_file(
                                candidate, file_format=file_format, context=context
                            ),
                            writable,
                        )
                        seen += summary.seen
                        stored += summary.stored
                        skipped += summary.skipped
                        reasons.update(summary.skip_reasons)
                finally:
                    writable.close()
                self._previewed_imports.discard(import_key)
                analysis = self._run_analysis()
                return {
                    "summary": {
                        "files": len(candidates),
                        "seen": seen,
                        "stored": stored,
                        "skipped": skipped,
                        "skipReasons": dict(sorted(reasons.items())),
                    },
                    "analysis": analysis["analysisState"],
                }
            except (ImportRunError, OSError, TypeError, UnicodeError, ValueError):
                return JSONResponse({"error": "invalid import request"}, status_code=400)

        setup_import.__annotations__["request"] = Request
        app.post("/api/setup/import")(setup_import)

    def _run_analysis(self) -> dict[str, Any]:
        # Import at execution time to avoid coupling the route capability back
        # to the application factory during module initialization.
        from verdict.dashboard.app import build_agent_insights_bundle

        return run_analysis(
            self.storage_url,
            tenant=LOCAL_SCOPE,
            build=lambda: build_agent_insights_bundle(
                self.storage_url,
                tenant=LOCAL_SCOPE,
                _include_input_fingerprint=True,
            ),
        )
