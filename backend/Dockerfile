FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/app.db \
    SIGNING_KEY_DIR=/data/keys

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN python -m pip install -r backend/requirements.txt
# Optional Timestral forecast band (spec 45): CPU-only torch, timestral at the pinned commit, official weights.
COPY backend/requirements-forecast.txt ./backend/requirements-forecast.txt
RUN python -m pip install --index-url https://download.pytorch.org/whl/cpu torch \
 && python -m pip install "timestral @ https://github.com/mistralai/timestral/archive/a534bdbd244d64f3d9d743f46c7bf6fc330ed4ff.zip" huggingface_hub \
 && HF_HUB_DISABLE_IMPLICIT_TOKEN=1 python -c "from huggingface_hub import snapshot_download; snapshot_download('mistralai/Timestral-28M', local_dir='/app/models/Timestral-28M', token=False)"
ENV FORECAST_ENABLED=1 FORECAST_MODEL_PATH=/app/models/Timestral-28M

# Both images use the repository root as context. Copy runtime sources explicitly:
# backend/.dockerignore does not filter a repository-root build context.
COPY backend/app/ ./backend/app/
COPY backend/scripts/ ./backend/scripts/
COPY backend/vendor/ ./backend/vendor/
# /developers, /developers/tools.json and /llms.txt read these generated files on every request.
COPY backend/static/ ./backend/static/
COPY backend/sandbox/ ./backend/sandbox/
COPY backend/suppliers/ ./backend/suppliers/
COPY backend/docs/ ./backend/docs/
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
