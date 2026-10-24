# Build only from a locally reviewed Verdict wheel. This image is not a release.
FROM python:3.12-slim

ARG VERDICT_WHEEL
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY ${VERDICT_WHEEL} /tmp/wheels/
RUN set -eu; wheel_file="/tmp/wheels/$(basename "$VERDICT_WHEEL")"; \
    python -m pip install --no-cache-dir "${wheel_file}[dashboard,postgres]"; \
    useradd --system --uid 10001 --no-create-home verdict

USER verdict
