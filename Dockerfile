# Verdict dashboard — container image for Google Cloud Run.
# Build context is the verdict/ project root.
#
# The validation database (verdict_experiment.db) is gitignored and is NOT
# present in a clean public clone. The build therefore treats a baked DB as
# OPTIONAL: if one is in the build context it is used; otherwise an empty but
# schema-valid SQLite DB is initialized at build time so the image still builds
# and the server starts (the dashboard then renders an empty-but-valid bundle).
#   docker build -t verdict-dashboard .
#   docker run -p 8080:8080 -e VERDICT_USER=demo -e VERDICT_PASS=secret verdict-dashboard
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    VERDICT_DB=/app/verdict_experiment.db

WORKDIR /app

# Dependencies first for layer caching.
COPY ui/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application (server + both served pages: public landing, gated dashboard).
COPY ui/server.py ui/landing.html ui/dashboard.html ./

# Optionally bake the validation database into the image.
#
# verdict_experiment.db is gitignored, so a clean public clone has no such file.
# We must NOT make the build depend on it. The `verdict` package is also NOT
# installed in this runtime image (only fastapi/uvicorn are — see
# ui/requirements.txt) and `packages/` is excluded by .dockerignore, so we keep
# this step fully self-contained. Strategy:
#   1. COPY the committed `docker-db/` directory (always present — it ships a
#      .gitkeep). A maintainer who wants a populated image drops a copy of
#      verdict_experiment.db into docker-db/ before building; a clean clone
#      leaves it empty. Copying a *directory* never fails on a missing optional
#      file the way a bare `COPY foo.db*` glob would.
#   2. RUN an init step with the schema DDL inlined below (kept in sync with
#      packages/verdict/src/verdict/storage/sqlite.py:_SCHEMA): if a real DB was
#      provided, fold its WAL into the main file and switch to a rollback
#      journal so it opens cleanly on Cloud Run's read-mostly filesystem;
#      otherwise create an empty but schema-valid DB so the server still starts.
COPY docker-db/ ./db_optional/
RUN python - <<'PY'
import glob, os, shutil, sqlite3

db = "/app/verdict_experiment.db"

# Move any DB files that were actually provided into place.
for f in glob.glob("/app/db_optional/verdict_experiment.db*"):
    shutil.move(f, os.path.join("/app", os.path.basename(f)))
shutil.rmtree("/app/db_optional", ignore_errors=True)

# Minimal schema-valid DDL. Mirrors verdict.storage.sqlite._SCHEMA — only the
# tables/columns the read-only dashboard server queries need to exist for an
# empty bundle to build. Keep in sync if the storage schema changes.
SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
    provider TEXT, operation TEXT, request_model TEXT, response_model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, temperature REAL,
    max_tokens INTEGER, finish_reason TEXT, error TEXT, latency_ms REAL,
    prompt_redacted TEXT, response_redacted TEXT, raw_messages_json TEXT,
    tenant_id TEXT, session_id TEXT, user_id_hash TEXT, cluster_id TEXT,
    tags_json TEXT, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS judgments (
    judgment_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, rubric_name TEXT,
    rubric_version TEXT, created_at TEXT NOT NULL, judge_models_json TEXT,
    dimensions_json TEXT, position_swap_consistent INTEGER
);
CREATE TABLE IF NOT EXISTS drift_signals (
    signal_id TEXT PRIMARY KEY, detected_at TEXT NOT NULL, cluster_id TEXT,
    dimension TEXT, direction TEXT, statistic_name TEXT, statistic_value REAL,
    p_value REAL, p_value_adjusted REAL, effect_size_cohens_d REAL,
    effect_size_cliffs_delta REAL DEFAULT 0.0, wasserstein_distance REAL DEFAULT 0.0,
    psi REAL DEFAULT 0.0, sample_size_current INTEGER, sample_size_baseline INTEGER,
    contributing_layers_json TEXT, example_trace_ids_json TEXT, recommended_action TEXT
);
"""

if os.path.exists(db):
    # A real validation DB was baked in: consolidate WAL/SHM and use a rollback
    # journal so the file opens cleanly on a read-mostly filesystem.
    c = sqlite3.connect(db)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("PRAGMA journal_mode=DELETE")
    c.commit()
    c.close()
    for f in glob.glob(db + "-wal") + glob.glob(db + "-shm"):
        os.remove(f)
    print("Using baked-in validation database.")
else:
    # No DB provided (e.g. clean public clone): initialize an empty schema-valid DB.
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    c.execute("PRAGMA journal_mode=DELETE")
    c.commit()
    c.close()
    print("No validation DB provided — initialized an empty schema-valid DB.")
PY

EXPOSE 8080

# Cloud Run injects $PORT; shell form expands it. exec for clean signal handling.
CMD exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
