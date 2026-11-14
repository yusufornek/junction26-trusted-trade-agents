FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/app.db \
    SIGNING_KEY_DIR=/data/keys

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install -r backend/requirements.txt

# Both images use the repository root as context. Copy runtime sources explicitly:
# backend/.dockerignore does not filter a repository-root build context.
COPY backend/app/ ./backend/app/
COPY backend/scripts/ ./backend/scripts/
COPY backend/vendor/ ./backend/vendor/
COPY backend/policy.yaml ./backend/policy.yaml
COPY backend/guardrail_presets.yaml ./backend/guardrail_presets.yaml
COPY data/ ./data/
COPY contracts/ ./contracts/

RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home gateway \
    && mkdir -p /data \
    && chown gateway:gateway /data \
    && ln -s /data /app/backend/var

# Keep the entrypoint in the Dockerfile so packaging needs no extra host script.
COPY --chmod=755 <<'SH' /usr/local/bin/gateway-entrypoint
#!/bin/sh
set -eu
umask 077
if [ -z "${OPERATOR_TOKEN:-}" ]; then
    echo 'OPERATOR_TOKEN is required; run scripts/gen_keys.py --write first.' >&2
    exit 1
fi
mkdir -p "$(dirname "$DB_PATH")" "$SIGNING_KEY_DIR" /data/buyer-keys /data/archive
if [ ! -f "$DB_PATH" ]; then
    python -m app.catalog build --db "$DB_PATH"
fi
exec "$@"
SH

WORKDIR /app/backend
USER gateway
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/gateway-entrypoint"]
# The backend is private to the Compose network; only Caddy publishes ports.
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
