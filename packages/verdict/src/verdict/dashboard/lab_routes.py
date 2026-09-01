"""Evaluator and clustering-lab routes for the dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from verdict.dashboard.setup_routes import SetupRoutes

_log = logging.getLogger("verdict.dashboard")


def register_lab_routes(app, setup: SetupRoutes) -> None:
    @app.get("/api/evaluators")
    def evaluator_status():
        from verdict.dashboard.evaluator_lab import evaluator_environment

        return evaluator_environment()

    def evaluator_preview(request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse(
                {"error": "evaluator authorization required"}, status_code=403
            )
        writable = None
        try:
            from verdict.dashboard.evaluator_lab import preview_evaluation

            writable = setup.writable_storage()
            return preview_evaluation(
                writable, tenant_id="__verdict_local__", config=payload
            )
        except (ImportError, OSError, TypeError, UnicodeError, ValueError):
            return JSONResponse({"error": "invalid evaluator preview"}, status_code=400)
        finally:
            if writable is not None:
                writable.close()

    evaluator_preview.__annotations__["request"] = Request
    app.post("/api/evaluators/preview")(evaluator_preview)

    def evaluator_run(request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse(
                {"error": "evaluator authorization required"}, status_code=403
            )
        writable = None
        try:
            from verdict.dashboard.evaluator_lab import execute_evaluation

            writable = setup.writable_storage()
            return execute_evaluation(
                writable,
                tenant_id="__verdict_local__",
                config=payload,
                confirm_external_egress=payload.get("confirmExternalEgress") is True,
            )
        except (ImportError, OSError, TypeError, UnicodeError, ValueError):
            return JSONResponse({"error": "invalid evaluator run"}, status_code=400)
        finally:
            if writable is not None:
                writable.close()

    evaluator_run.__annotations__["request"] = Request
    app.post("/api/evaluators/run")(evaluator_run)

    def evaluator_calibration_preview(request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse(
                {"error": "evaluator authorization required"}, status_code=403
            )
        try:
            from verdict.dashboard.evaluator_lab import preview_calibration

            path = payload.get("labelSetPath")
            if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 4096:
                raise ValueError("invalid label set path")
            return preview_calibration(path=path, config=payload)
        except (ImportError, OSError, TypeError, UnicodeError, ValueError):
            return JSONResponse(
                {"error": "invalid calibration preview"}, status_code=400
            )

    evaluator_calibration_preview.__annotations__["request"] = Request
    app.post("/api/evaluators/calibration/preview")(evaluator_calibration_preview)

    def evaluator_calibration_run(request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse(
                {"error": "evaluator authorization required"}, status_code=403
            )
        writable = None
        try:
            from verdict.dashboard.evaluator_lab import execute_calibration

            path = payload.get("labelSetPath")
            if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 4096:
                raise ValueError("invalid label set path")
            writable = setup.writable_storage()
            return execute_calibration(
                writable,
                path=path,
                config=payload,
                confirm_external_egress=payload.get("confirmExternalEgress") is True,
                minimum_examples=int(payload.get("minimumExamples", 30)),
                agreement_threshold=float(payload.get("agreementThreshold", 0.8)),
            )
        except (ImportError, OSError, TypeError, UnicodeError, ValueError):
            return JSONResponse({"error": "invalid calibration run"}, status_code=400)
        finally:
            if writable is not None:
                writable.close()

    evaluator_calibration_run.__annotations__["request"] = Request
    app.post("/api/evaluators/calibration/run")(evaluator_calibration_run)

    def cluster_action(request, action: str, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse(
                {"error": "cluster authorization required"}, status_code=403
            )
        writable = None
        try:
            from verdict.dashboard.cluster_lab import execute_cluster_action

            writable = setup.writable_storage()
            return execute_cluster_action(writable, action=action, payload=payload)
        except (ImportError, OSError, TypeError, UnicodeError, ValueError):
            _log.exception("cluster action failed")
            return JSONResponse({"error": "cluster action unavailable"}, status_code=400)
        finally:
            if writable is not None:
                writable.close()

    cluster_action.__annotations__["request"] = Request
    app.post("/api/clusters/{action}")(cluster_action)
