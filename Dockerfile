FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    APP_CONFIG_DIR=/app/config \
    DATABASE_PATH=/data/pipeline_runs.sqlite3 \
    PREVIEW_DIRECTORY=/data/previews

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app/ ./app/
COPY config/ ./config/
COPY templates/ ./templates/

RUN python -m pip install --no-cache-dir . \
    && groupadd --system --gid 10001 newsagent \
    && useradd --system --uid 10001 --gid newsagent --home-dir /app newsagent \
    && mkdir -p /data \
    && chown newsagent:newsagent /data

USER 10001:10001

VOLUME ["/data"]

ENTRYPOINT ["personal-news-agent"]
CMD ["preview"]
