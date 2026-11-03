# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KSU_DB=/data/ksu.sqlite3 KSU_SECURE_COOKIES=1 KSU_REQUIRE_INITIALIZED=1 \
    KSU_DB_MAX_BYTES=268435456 KSU_DATA_MAX_BYTES=2147483648 KSU_BACKUP_KEEP=2 KSU_CONCURRENCY_LIMIT=16
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ksu && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ksu
COPY requirements.txt constraints.txt ./
# Optional build-only CA for the managed environment; never baked into image layers.
RUN --mount=type=secret,id=proxy_ca \
    if [ -s /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY app.js sync.js decisions.js calendar.js index.html styles.css ./
COPY server/ ./server/
COPY deploy/amvera/ ./deploy/amvera/
COPY deploy/rotate_backups.py ./deploy/rotate_backups.py
# Git checkouts may have mode 0600; public source must be readable after privilege drop.
# Runtime data/credentials are never part of /app or the image context.
RUN chmod -R a+rX /app
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD python -m deploy.amvera.healthcheck
ENTRYPOINT ["python", "-m", "deploy.amvera.runtime"]
