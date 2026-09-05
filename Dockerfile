# Standalone packaged Verdict dashboard for Cloud Run or local Docker.
# Override VERDICT_VERSION only with another published, tested release.
FROM python:3.12-slim

ARG VERDICT_VERSION=0.1.0a16
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    VERDICT_STORAGE=sqlite:////app/verdict.db

WORKDIR /app

RUN pip install --no-cache-dir \
    "cognifity-verdict[dashboard,postgres]==${VERDICT_VERSION}"

# A maintainer may place verdict.db in docker-db/ before building. A clean
# checkout creates an empty schema through the real storage adapter instead of
# carrying a second copy of the schema in this Dockerfile.
COPY docker-db/ /tmp/verdict-db/
RUN python - <<'PY'
from pathlib import Path
from shutil import copy2

from verdict.storage import SQLiteStorage

provided = Path("/tmp/verdict-db/verdict.db")
target = Path("/app/verdict.db")
if provided.is_file():
    auxiliary = sorted(provided.parent.glob(f"{provided.name}-*"))
    if auxiliary:
        raise SystemExit("Checkpoint verdict.db before building; SQLite sidecar files exist")
    copy2(provided, target)
else:
    SQLiteStorage(str(target)).close()
PY

EXPOSE 8080

CMD ["sh", "-c", "exec verdict-dashboard --host 0.0.0.0 --port ${PORT:-8080}"]
