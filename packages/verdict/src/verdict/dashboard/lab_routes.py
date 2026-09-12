"""Evaluator and clustering-lab routes for the dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

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
        except ValueError as exc:
            if str(exc) == "no rubric dimensions are evaluable without context":
                return JSONResponse(
                    {
                        "error": (
                            "No rubric dimensions can be evaluated because Verdict "
                            "traces do not include retrieved context."
                        )
                    },
                    status_code=400,
                )
            return JSONResponse({"error": "invalid evaluator preview"}, status_code=400)
        except (ImportError, OSError, TypeError, UnicodeError):
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

    async def evaluator_inspect(request: Request):
        if not setup.authorized(request):
            return JSONResponse(
                {"error": "evaluator authorization required"}, status_code=403
            )
        from verdict.dashboard.inspect_lab import MAX_INSPECT_BYTES

        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_INSPECT_BYTES:
                return JSONResponse(
                    {"error": "Inspect input exceeds the 4 MiB limit."}, status_code=413
                )
        params = request.query_params
        for name in ("semantic", "judge", "confirm_external_egress"):
            if params.get(name, "0") not in {"0", "1"}:
                return JSONResponse(
                    {"error": f"{name} must be 0 or 1."}, status_code=400
                )
        enable_judge = params.get("judge", "0") == "1"
        confirm_egress = params.get("confirm_external_egress", "0") == "1"
        if enable_judge and not confirm_egress:
            return JSONResponse(
                {"error": "Confirm external judge egress before analysis."},
                status_code=400,
            )
        try:
            from verdict.dashboard.inspect_lab import inspect_export

            return await run_in_threadpool(
                inspect_export,
                bytes(content),
                format_name=params.get("format", "auto"),
                enable_semantic=params.get("semantic", "0") == "1",
                enable_judge=enable_judge,
                judge_model=params.get("judge_model", "claude-haiku-4-5-20251001"),
                confirm_external_egress=confirm_egress,
            )
        except ImportError:
            return JSONResponse(
                {
                    "error": (
                        "Install cognifity-verdict-inspect and any selected "
                        "optional dependencies to analyze exports."
                    )
                },
                status_code=503,
            )
        except RuntimeError as exc:
            if str(exc) == "another inspect analysis is already running":
                return JSONResponse(
                    {"error": "Another Inspect analysis is already running."},
                    status_code=409,
                )
            return JSONResponse(
                {"error": "The export could not be analyzed."}, status_code=400
            )
        except (OSError, TypeError, UnicodeError, ValueError):
            return JSONResponse(
                {"error": "The export is empty, malformed, or unsupported."},
                status_code=400,
            )

    app.post("/api/evaluators/inspect")(evaluator_inspect)

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
        except ValueError as exc:
            _log.exception("cluster action failed")
            known = {
                "model_unavailable": (
                    "Semantic clustering needs local MiniLM support. Install the "
                    "semantic extra and retry."
                ),
                "semantic_fit_too_small": (
                    "Not enough eligible prompt evidence was found in the selected range."
                ),
                "no eligible traces are available for clustering": (
                    "No completed eligible traces are available for clustering."
                ),
                "cluster registry version is not validated": (
                    "These clusters did not pass activation checks. Refresh the analysis "
                    "and review the reported warnings."
                ),
            }
            return JSONResponse(
                {"error": known.get(str(exc), "Cluster action could not be completed.")},
                status_code=400,
            )
        except (ImportError, OSError, TypeError, UnicodeError):
            _log.exception("cluster action failed")
            return JSONResponse({"error": "Cluster action could not be completed."}, status_code=400)
        finally:
            if writable is not None:
                writable.close()

    cluster_action.__annotations__["request"] = Request
    app.post("/api/clusters/{action}")(cluster_action)
