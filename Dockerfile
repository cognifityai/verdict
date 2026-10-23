# Standalone packaged Verdict dashboard for Cloud Run or local Docker.
# Override VERDICT_VERSION only with another published, tested release.
FROM python:3.12-slim

ARG VERDICT_VERSION=0.1.0b2
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    VERDICT_STORAGE=sqlite:////app/verdict.db

WORKDIR /app

RUN pip install --no-cache-dir \
    "cognifity-verdict[dashboard,postgres,eval,anthropic,openai,google]==${VERDICT_VERSION}"

# Initialize an empty local schema only. Capture stores belong outside the
# image and are supplied at runtime through VERDICT_STORAGE.
RUN python -c 'from verdict.storage import SQLiteStorage; SQLiteStorage("/app/verdict.db").close()'

EXPOSE 8080

CMD ["sh", "-c", "exec verdict-dashboard --host 0.0.0.0 --port ${PORT:-8080}"]
