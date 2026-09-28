"""Evaluator and clustering-lab routes for the dashboard."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from verdict.agent_judgment import AgentTurnJudgmentStoreError
from verdict.dashboard.setup_routes import SetupRoutes

_log = logging.getLogger("verdict.dashboard")


def register_lab_routes(app, setup: SetupRoutes) -> None:
    @app.post("/api/evaluators/rubric/validate")
    def rubric_validate(request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "evaluator authorization required"}, status_code=403)
        try:
            from verdict.sessions import validate_rubric
            return validate_rubric(payload.get("document"))
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid executable rubric JSON"}, status_code=400)

    @app.post("/api/evaluators/import")
    def import_assessment(request: Request, payload: dict[str, Any]):
        if not setup.authorized(request):
            return JSONResponse({"error": "evaluator authorization required"}, status_code=403)
        writable = None
        try:
            from verdict_eval.providers import FakeProvider
            from verdict_eval.session_evaluation import assess_snapshot

            from verdict.sessions import validate_rubric
            writable = setup.writable_storage()
            row = writable.get_session(setup.tenant_id, payload["sessionId"])
            if row is None or row["revision"] != payload["revision"]:
                return JSONResponse({"error": "session revision changed; review current evidence"}, status_code=409)
            result = assess_snapshot(row, validate_rubric(payload["rubric"]), provider=FakeProvider(), model=payload["model"],
                imported_findings=payload["findings"], source_provider=payload["provider"], target_message_id=payload.get("targetMessageId"))
            if not writable.save_session_assessment(result):
                return JSONResponse({"error": "session revision changed"}, status_code=409)
            return result
        except (KeyError, TypeError, ValueError, UnicodeError):
            return JSONResponse({"error": "invalid imported grader findings"}, status_code=400)
        finally:
            if writable is not None:
                writable.close()

    @app.get("/api/data/sessions")
    def sessions_data(request: Request, evaluator: str | None = None):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error": "data unavailable"}, status_code=403)
        writable = setup.writable_storage()
        try:
            from verdict.sessions import MAX_SESSIONS, select_assessments
            rows = writable.list_sessions(setup.tenant_id, limit=MAX_SESSIONS + 1)
            assessments = writable.list_session_assessments(setup.tenant_id, limit=MAX_SESSIONS + 1)
            if len(rows) > MAX_SESSIONS or len(assessments) > MAX_SESSIONS:
                return JSONResponse({"error": "session data exceeds bounded review limit"}, status_code=503)
            identities = {}
            for result in assessments:
                identities[result["evaluator_fingerprint"]] = {"fingerprint": result["evaluator_fingerprint"],
                    "rubric": result["rubric"]["name"], "version": result["rubric"]["version"],
                    "target": result["rubric"]["target"], "provider": result["evaluator"].get("provider"),
                    "model": result["evaluator"].get("model"), "source": result["source"]}
            if evaluator is None and len(identities) == 1:
                evaluator = next(iter(identities))
            selected = [a for a in select_assessments(assessments) if a["evaluator_fingerprint"] == evaluator]
            issues = {}
            summaries = []
            for row in rows:
                matched = [a for a in selected if a["session_id"] == row["id"] and a["session_revision"] == row["revision"]]
                for result in matched:
                    if result["status"] != "completed":
                        continue
                    for finding in result["findings"]:
                        deficient = finding.get("adequacy") in {"borderline", "inadequate", "critical"} or result["dimensions"][finding["dimension"]]["state"] == "fail"
                        if deficient:
                            issue = finding.get("issue", finding["dimension"])
                            issues.setdefault(issue, set()).add(row["id"])
                summaries.append({**{k:v for k,v in row.items() if k != "messages"}, "messageCount": len(row["messages"]),
                                  "assessments": matched})
            return {"sessions": summaries, "evaluatorIdentities": list(identities.values()), "selectedEvaluator": evaluator,
                    "issues": [{"issue": issue, "conversations": len(ids), "sessionIds": sorted(ids)} for issue, ids in sorted(issues.items(), key=lambda p:-len(p[1]))],
                    "coverage": {"captured": len(rows), "untimed": sum(r["event_at"] is None for r in rows),
                                 "unknownEnding": sum(r["end_status"] in {"unknown", "open"} for r in rows),
                                 "judged": sum(any(a["status"] == "completed" for a in r["assessments"]) for r in summaries)}}
        finally:
            writable.close()

    @app.get("/api/data/sessions/{session_id}")
    def session_detail(request: Request, session_id: str, revision: str | None = None):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error": "session unavailable"}, status_code=403)
        writable = setup.writable_storage()
        try:
            row = writable.get_session(setup.tenant_id, session_id, revision)
            if row is None:
                return JSONResponse({"error": "session unavailable"}, status_code=404)
            assessments = writable.list_session_assessments(setup.tenant_id, session_id=session_id, limit=5001)
            if len(assessments) > 5000:
                return JSONResponse({"error": "session history exceeds bounded review limit"}, status_code=503)
            return {"session": row, "assessments": assessments}
        except (TypeError, ValueError):
            return JSONResponse({"error": "invalid session request"}, status_code=400)
        finally:
            writable.close()

    @app.get("/api/compare/matched")
    def matched_replies(request: Request, left: str | None = None, right: str | None = None, evaluator: str | None = None):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error":"comparison unavailable"},status_code=403)
        writable=setup.writable_storage()
        try:
            from verdict.matched_comparison import compare_saved_replies
            traces=writable.list_traces(tenant_id=setup.tenant_id,limit=5001)
            if len(traces)>5000:
                raise ValueError("matched comparison exceeds bounded input limit")
            models=sorted({f"{t.provider}/{t.response_model or t.request_model}" for t in traces})
            if evaluator is None or left is None or right is None:
                return {"models":models,"status":"select_models_and_evaluator"}
            if left not in models or right not in models:
                raise ValueError("selected model is unavailable")
            from verdict.monitor_inputs import _evaluator_judgments
            judgments=_evaluator_judgments(writable,tenant_id=setup.tenant_id,evaluator_fingerprint=evaluator)
            return {"models":models,"evaluatorFingerprint":evaluator,**compare_saved_replies(traces,judgments,left,right)}
        except (TypeError,ValueError):
            return JSONResponse({"error":"comparison requires two captured models and one exact stored evaluator"},status_code=400)
        finally:
            writable.close()

    @app.get("/api/evaluators")
    def evaluator_status(request: Request):
        if not setup.request_matches_tenant(request):
            return JSONResponse({"error": "evaluator unavailable"}, status_code=403)
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
                writable, tenant_id=setup.tenant_id, config=payload
            )
        except ValueError as exc:
            if str(exc) == "no rubric dimensions are evaluable without context":
                explanation = (
                    "Verdict does not have retrieved context for this evaluation unit."
                    if payload.get("unit") == "agent_turn" else
                    "Verdict traces do not include retrieved context."
                )
                return JSONResponse(
                    {
                        "error": (
                            "No rubric dimensions can be evaluated because " + explanation
                        )
                    },
                    status_code=400,
                )
            return JSONResponse({"error": "invalid evaluator preview"}, status_code=400)
        except AgentTurnJudgmentStoreError:
            _log.exception("invalid persisted Turn result")
            return JSONResponse({"error": "stored Turn evaluation unavailable"}, status_code=503)
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
                tenant_id=setup.tenant_id,
                config=payload,
                confirm_external_egress=payload.get("confirmExternalEgress") is True,
            )
        except AgentTurnJudgmentStoreError:
            _log.exception("invalid persisted Turn result")
            return JSONResponse({"error": "stored Turn evaluation unavailable"}, status_code=503)
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
                tenant_id=setup.tenant_id,
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
            return execute_cluster_action(
                writable,
                action=action,
                payload=payload,
                tenant_id=setup.tenant_id,
            )
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
