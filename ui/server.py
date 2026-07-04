"""Verdict dashboard — read-only FastAPI service over a Verdict SQLite store.

Serves the dashboard HTML at `/` and a single aggregated data bundle at
`/api/data`, computed live from the database on every request. Point it at any
`verdict.db` produced by the SDK; defaults to the validation experiment DB.

Run:
    pip install fastapi "uvicorn[standard]"
    python server.py                       # serves http://127.0.0.1:8000
    VERDICT_DB=/path/to/verdict.db python server.py

This is a v0 read layer. It is intentionally dependency-light and does no
writes. The aggregation in build_bundle() mirrors the dashboard's data shape.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent  # .../verdict

_log = logging.getLogger("verdict.dashboard")

# Security headers applied to every HTTP response. The CSP is deliberately
# strict but allows exactly the CDNs the dashboard page actually loads
# (tailwind, cdnjs for React/Recharts/lucide/babel) plus the inline styles and
# in-browser Babel transform the research-preview page relies on. It is kept in
# sync with the <meta http-equiv="Content-Security-Policy"> tag in
# dashboard.html. Tighten/remove 'unsafe-inline'/'unsafe-eval' once the page is
# served from a pre-built bundle instead of CDN + in-browser Babel.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://cdn.tailwindcss.com https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "font-src 'self' data:; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
PRETTY = {
    "claude-haiku-4-5": "Claude Haiku 4.5",
    "gpt-4o-mini": "GPT-4o-mini",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
}
PROVIDER_ORDER = ["anthropic", "openai", "google"]
# drift signals tag the regressed stream in cluster_id; map those aliases back
# to a provider key so the UI can attribute the signal.
PROVIDER_ALIAS = {"haiku": "anthropic", "gpt": "openai", "gpt-4o-mini": "openai",
                  "gemini": "google", "flash": "google"}
DIM_ORDER = ["groundedness", "relevance", "completeness", "safety", "instruction_following"]


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _label_for(provider: str, model: str) -> str:
    return PRETTY.get(model, model or provider)


def resolve_db() -> Path:
    env = os.environ.get("VERDICT_DB")
    if env:
        return Path(env).expanduser().resolve()
    for name in ("verdict_experiment.db", "verdict.db"):
        cand = REPO / name
        if cand.exists():
            return cand
    return REPO / "verdict.db"


# --------------------------------------------------------------------------- #
#  Aggregation — produces the exact shape the dashboard consumes
# --------------------------------------------------------------------------- #
def build_bundle(db_path: str | os.PathLike) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    try:
        return _build(cur)
    finally:
        con.close()


def _provider_order(keys) -> list[str]:
    ordered = [p for p in PROVIDER_ORDER if p in keys]
    ordered += sorted(k for k in keys if k not in PROVIDER_ORDER)
    return ordered


def _build(cur) -> dict:
    t0row = cur.execute("SELECT MIN(started_at) m, MAX(started_at) x FROM traces").fetchone()
    if not t0row or not t0row["m"]:
        # empty DB — return a minimal, valid-but-empty bundle
        return {"meta": {"runStart": None, "durationHours": 0, "totalTraces": 0,
                         "totalJudged": 0, "totalCost": 0.0, "regressionHour": 0,
                         "providers": 0, "clusters": 0},
                "providers": [], "clusters": [], "driftSignals": [],
                "dimensionOverall": [], "tsRows": [], "passrate": [], "haikuDim": [],
                "samples": [], "providerDimension": []}
    t0, tmax = _dt(t0row["m"]), _dt(t0row["x"])

    # trace_id -> (provider, started_at) and provider model
    tp, ttime, model_of = {}, {}, {}
    for r in cur.execute("SELECT trace_id, provider, request_model, started_at FROM traces"):
        tp[r["trace_id"]] = r["provider"]
        ttime[r["trace_id"]] = r["started_at"]
        model_of.setdefault(r["provider"], r["request_model"])

    # judgments -> per provider / per dimension tallies + per-trace dims
    dim_overall = defaultdict(Counter)
    prov_dim = defaultdict(lambda: defaultdict(Counter))
    judg_by_trace = {}
    for r in cur.execute("SELECT trace_id, dimensions_json, judge_models_json FROM judgments"):
        dims = json.loads(r["dimensions_json"])
        prov = tp.get(r["trace_id"], "?")
        judg_by_trace[r["trace_id"]] = {
            "judges": json.loads(r["judge_models_json"] or "[]"),
            "dims": [{"name": d["name"], "verdict": d["verdict"], "reasoning": d.get("reasoning", "")} for d in dims],
        }
        for d in dims:
            dim_overall[d["name"]][d["verdict"]] += 1
            prov_dim[prov][d["name"]][d["verdict"]] += 1

    keys = _provider_order({r["provider"] for r in cur.execute("SELECT DISTINCT provider FROM traces")})

    # ---- providers ----
    providers = []
    for p in keys:
        r = cur.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN error IS NOT NULL AND error<>'' THEN 1 ELSE 0 END) errors,
                      AVG(latency_ms) lat, SUM(input_tokens) it, SUM(output_tokens) ot,
                      SUM(cost_usd) cost
               FROM traces WHERE provider=?""", (p,)).fetchone()
        pas = tot = 0
        for c in prov_dim[p].values():
            for v, cnt in c.items():
                tot += cnt
                if v == "pass":
                    pas += cnt
        providers.append({
            "key": p, "label": _label_for(p, model_of.get(p, "")), "model": model_of.get(p, ""),
            "n": r["n"], "errors": r["errors"] or 0,
            "errorRate": round(100 * (r["errors"] or 0) / r["n"], 1) if r["n"] else 0.0,
            "avgLatency": round((r["lat"] or 0) / 1000, 2),
            "inTok": r["it"] or 0, "outTok": r["ot"] or 0, "cost": round(r["cost"] or 0, 4),
            "passRate": round(100 * pas / tot, 1) if tot else None, "judged": tot,
        })

    # ---- clusters ----
    clusters = [dict(cluster_id=r["cluster_id"], n=r["n"]) for r in
                cur.execute("SELECT cluster_id, COUNT(*) n FROM traces GROUP BY cluster_id ORDER BY n DESC")]

    # ---- drift signals ----
    drift = []
    for s in cur.execute("SELECT * FROM drift_signals"):
        s = dict(s)
        alias = s["cluster_id"]
        prov = PROVIDER_ALIAS.get(alias, alias if alias in keys else "anthropic")
        drift.append({
            "id": s["signal_id"][:8], "dimension": s["dimension"], "direction": s["direction"],
            "provider": prov, "providerLabel": _label_for(prov, model_of.get(prov, "")),
            "statName": s["statistic_name"], "stat": round(s["statistic_value"], 1),
            "p": s["p_value"], "pAdj": s["p_value_adjusted"],
            "cliffsDelta": round(s["effect_size_cliffs_delta"], 3) if s["effect_size_cliffs_delta"] is not None else None,
            "cohensD": round(s["effect_size_cohens_d"], 2),
            "nCur": s["sample_size_current"], "nBase": s["sample_size_baseline"],
            "layers": json.loads(s["contributing_layers_json"] or "[]"),
            "action": s["recommended_action"], "detectedAt": s["detected_at"],
        })
    # Sort by the primary effect size (Cliff's δ) when present, else Cohen's d.
    drift.sort(key=lambda x: x["cliffsDelta"] if x["cliffsDelta"] is not None else x["cohensD"])

    # ---- dimension overall ----
    dims_present = [d for d in DIM_ORDER if d in dim_overall] + \
                   [d for d in dim_overall if d not in DIM_ORDER]
    dimensionOverall = []
    for d in dims_present:
        c = dim_overall[d]
        tot = sum(c.values())
        dimensionOverall.append({"dim": d, "passRate": round(100 * c.get("pass", 0) / tot, 1) if tot else None,
                                 "pass": c.get("pass", 0), "fail": c.get("fail", 0),
                                 "unclear": c.get("unclear", 0), "tot": tot})

    # ---- provider x dimension ----
    providerDimension = []
    for d in dims_present:
        row = {"dim": d}
        for p in keys:
            c = prov_dim[p].get(d, {})
            tot = sum(c.values())
            row[p] = round(100 * c.get("pass", 0) / tot, 1) if tot else None
        providerDimension.append(row)

    # ---- time series: 30-min bins ----
    BIN = 30 * 60
    bins = defaultdict(lambda: defaultdict(lambda: {"n": 0, "err": 0, "lat": []}))
    for r in cur.execute("SELECT provider, started_at, latency_ms, error FROM traces"):
        b = int((_dt(r["started_at"]) - t0).total_seconds() // BIN)
        cell = bins[r["provider"]][b]
        cell["n"] += 1
        if r["error"]:
            cell["err"] += 1
        if r["latency_ms"] is not None:
            cell["lat"].append(r["latency_ms"])
    maxbin = max((b for p in bins for b in bins[p]), default=0)
    tsRows = []
    for b in range(maxbin + 1):
        row = {"hour": round(b * 0.5, 1)}
        for p in keys:
            cell = bins[p].get(b)
            if cell and cell["n"]:
                row[f"{p}_lat"] = round(sum(cell["lat"]) / len(cell["lat"]) / 1000, 2) if cell["lat"] else None
                row[f"{p}_err"] = round(100 * cell["err"] / cell["n"], 1)
                row[f"{p}_n"] = cell["n"]
            else:
                row[f"{p}_lat"] = None
                row[f"{p}_err"] = None
                row[f"{p}_n"] = 0
        tsRows.append(row)

    # ---- pass rate over time (hourly, all dims) + per-dim for first provider w/ drift ----
    HB = 60 * 60
    pr = defaultdict(lambda: defaultdict(lambda: {"p": 0, "t": 0}))
    dim_hr = defaultdict(lambda: defaultdict(lambda: {"p": 0, "t": 0}))
    focus = drift[0]["provider"] if drift else (keys[0] if keys else None)
    for tid, j in judg_by_trace.items():
        st = ttime.get(tid)
        if not st:
            continue
        prov = tp.get(tid)
        hb = int((_dt(st) - t0).total_seconds() // HB)
        for d in j["dims"]:
            pr[prov][hb]["t"] += 1
            if d["verdict"] == "pass":
                pr[prov][hb]["p"] += 1
            if prov == focus:
                dim_hr[d["name"]][hb]["t"] += 1
                if d["verdict"] == "pass":
                    dim_hr[d["name"]][hb]["p"] += 1
    maxh = max((h for p in pr for h in pr[p]), default=0)
    passrate = []
    for h in range(maxh + 1):
        row = {"hour": h}
        for p in keys:
            cell = pr[p].get(h)
            row[p] = round(100 * cell["p"] / cell["t"], 1) if cell and cell["t"] else None
        passrate.append(row)
    haikuDim = []
    for h in range(maxh + 1):
        row = {"hour": h}
        for d in DIM_ORDER:
            cell = dim_hr[d].get(h)
            row[d] = round(100 * cell["p"] / cell["t"], 1) if cell and cell["t"] else None
        haikuDim.append(row)

    # ---- sample traces for the explorer ----
    rows = [dict(r) for r in cur.execute(
        """SELECT trace_id, provider, request_model, cluster_id, input_tokens, output_tokens,
                  latency_ms, cost_usd, finish_reason, error, started_at, prompt_redacted, response_redacted
           FROM traces WHERE prompt_redacted IS NOT NULL AND prompt_redacted<>'' ORDER BY started_at""")]
    errs = [r for r in rows if r["error"]]
    late = [r for r in rows if r["provider"] == focus and (_dt(r["started_at"]) - t0).total_seconds() > 4 * 3600]
    pick, seen = [], set()
    for r in errs[:4] + late[:6]:
        if r["trace_id"] not in seen:
            pick.append(r)
            seen.add(r["trace_id"])
    for r in rows:
        if len(pick) >= 40:
            break
        if r["trace_id"] not in seen:
            pick.append(r)
            seen.add(r["trace_id"])
    samples = []
    for r in pick:
        s = dict(r)
        s["hour"] = round((_dt(r["started_at"]) - t0).total_seconds() / 3600, 2)
        s["latency_ms"] = round(r["latency_ms"]) if r["latency_ms"] else None
        if s.get("response_redacted"):
            s["response_redacted"] = s["response_redacted"][:600]
        j = judg_by_trace.get(r["trace_id"])
        if j:
            s["judgment"] = j
        samples.append(s)

    total_traces = sum(p["n"] for p in providers)
    return {
        "meta": {
            "runStart": t0row["m"],
            "durationHours": max(1, round((tmax - t0).total_seconds() / 3600)),
            "totalTraces": total_traces,
            "totalJudged": len(judg_by_trace),
            "totalCost": round(sum(p["cost"] for p in providers), 3),
            "regressionHour": int(os.environ.get("VERDICT_REGRESSION_HOUR", "4")),
            "providers": len(providers),
            "clusters": len(clusters),
        },
        "providers": providers,
        "clusters": clusters,
        "driftSignals": drift,
        "dimensionOverall": dimensionOverall,
        "tsRows": tsRows,
        "passrate": passrate,
        "haikuDim": haikuDim,
        "samples": samples,
        "providerDimension": providerDimension,
    }


# --------------------------------------------------------------------------- #
#  FastAPI app
# --------------------------------------------------------------------------- #
def create_app():
    import base64
    import secrets

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    app = FastAPI(title="Verdict Dashboard", version="0.1.0")
    # CORS is locked down by default (same-origin only). The dashboard is served
    # from this same origin, so it needs nothing. Set VERDICT_CORS_ORIGINS to a
    # comma-separated allowlist only if a separate frontend must read /api/data.
    _cors = [o.strip() for o in os.environ.get("VERDICT_CORS_ORIGINS", "").split(",") if o.strip()]
    if _cors:
        app.add_middleware(CORSMiddleware, allow_origins=_cors,
                           allow_methods=["GET"], allow_headers=["*"])

    # Only these paths require the password. The landing page (/) and health
    # check stay public; the dashboard view and the live data are gated.
    def _is_gated(path: str) -> bool:
        return path == "/dashboard" or path.startswith("/api/data")

    @app.middleware("http")
    async def basic_auth(request, call_next):
        """Gate the dashboard + live data behind HTTP Basic Auth when configured.

        Set VERDICT_USER and VERDICT_PASS to require a login. If either is unset
        (local dev), the gate is disabled. Only /dashboard and /api/data are
        protected; the marketing landing at / and /api/health stay public.
        """
        user = os.environ.get("VERDICT_USER")
        pw = os.environ.get("VERDICT_PASS")
        if user and pw and _is_gated(request.url.path):
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    raw = base64.b64decode(header[6:]).decode("utf-8")
                    u, _, p = raw.partition(":")
                    ok = secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)
                except Exception:
                    ok = False
            if not ok:
                resp = Response("Authentication required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Verdict"'})
                for k, v in _SECURITY_HEADERS.items():
                    resp.headers.setdefault(k, v)
                return resp
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        """Apply baseline security headers (CSP, nosniff, frame-deny, no-referrer)
        to every response. Kept in sync with the <meta> CSP in dashboard.html."""
        resp = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    def _serve(filename: str) -> HTMLResponse:
        html = HERE / filename
        if not html.exists():
            return HTMLResponse(f"<h1>{filename} not found</h1>"
                                "<p>Run <code>python ui/build.py</code> to generate it.</p>",
                                status_code=404)
        return HTMLResponse(html.read_text())

    @app.get("/api/health")
    def health():
        # Public endpoint — do not leak the filesystem path of the database.
        db = resolve_db()
        return {"status": "ok", "db_configured": db.exists()}

    @app.get("/api/data")
    def data():
        db = resolve_db()
        if not db.exists():
            # Do NOT leak the absolute DB path in the HTTP body — log it
            # server-side and return a generic 503 so the dashboard degrades
            # gracefully instead of crashing or exposing the filesystem layout.
            _log.warning("data unavailable: database not found at %s", db)
            return JSONResponse({"error": "data unavailable"}, status_code=503)
        try:
            return build_bundle(db)
        except Exception:  # pragma: no cover - defensive: corrupt/locked DB
            _log.exception("failed to build data bundle from %s", db)
            return JSONResponse({"error": "data unavailable"}, status_code=503)

    @app.get("/", response_class=HTMLResponse)
    def index():
        # Public marketing page.
        return _serve("landing.html")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        # Gated by the middleware above.
        return _serve("dashboard.html")

    return app


app = None
try:  # only construct if FastAPI is installed; allows importing build_bundle standalone
    app = create_app()
except Exception:  # pragma: no cover
    pass


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve the Verdict dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", help="path to a verdict SQLite db (overrides VERDICT_DB)")
    args = parser.parse_args()
    if args.db:
        os.environ["VERDICT_DB"] = args.db

    db = resolve_db()
    print(f"Verdict dashboard → http://{args.host}:{args.port}")
    print(f"Reading database  → {db} ({'found' if db.exists() else 'MISSING'})")

    # Loud warning if exposed beyond localhost without auth: /dashboard and the
    # full /api/data bundle would otherwise be world-readable.
    if args.host not in ("127.0.0.1", "localhost", "::1") and not (
        os.environ.get("VERDICT_USER") and os.environ.get("VERDICT_PASS")
    ):
        print("WARNING: binding to a non-localhost host without VERDICT_USER/VERDICT_PASS — "
              "the dashboard and /api/data will be publicly accessible. Set both to require auth.")

    import uvicorn
    uvicorn.run(create_app(), host=args.host, port=args.port)
