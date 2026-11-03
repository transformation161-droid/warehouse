# syntax=docker/dockerfile:1
# Build only after the application image; adds the local benchmark HTTP client, not a deployment service.
FROM ksu-amvera:issue10
RUN --mount=type=secret,id=proxy_ca \
    if [ -s /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -c /app/constraints.txt httpx==0.28.1
