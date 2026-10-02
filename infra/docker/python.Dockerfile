# syntax=docker/dockerfile:1.7
# One image for the FastAPI service, RQ worker and scheduler (different commands).
# Multi-stage: dependencies are resolved from uv.lock (reproducible), the runtime runs as non-root.

# Builder uses the full image (compilers/runtime libs present); runtime stays slim and apt-free.
FROM python:3.12 AS builder
# Optional extra CA for TLS-inspecting proxies (build with --secret id=extra_ca,src=<bundle>).
# No-op when the secret is absent; only the builder stage sees it, never the runtime image.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then cat /run/secrets/extra_ca >> /etc/ssl/certs/ca-certificates.crt; fi \
    && pip install --no-cache-dir uv==0.8.17
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# 1) third-party dependencies only (cached layer)
COPY pyproject.toml uv.lock ./
COPY packages/domain/pyproject.toml packages/domain/pyproject.toml
COPY packages/storage/pyproject.toml packages/storage/pyproject.toml
COPY packages/forecasting/pyproject.toml packages/forecasting/pyproject.toml
COPY packages/simulation/pyproject.toml packages/simulation/pyproject.toml
COPY packages/optimizer/pyproject.toml packages/optimizer/pyproject.toml
COPY packages/decision/pyproject.toml packages/decision/pyproject.toml
COPY packages/backtesting/pyproject.toml packages/backtesting/pyproject.toml
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml
COPY services/feature-store/pyproject.toml services/feature-store/pyproject.toml
COPY services/notifications/pyproject.toml services/notifications/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/worker/pyproject.toml apps/worker/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace
# 2) first-party code
COPY packages packages
COPY services services
COPY apps/api apps/api
COPY apps/worker apps/worker
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
# LightGBM needs the OpenMP runtime; ship it without apt in the runtime image.
RUN mkdir -p /opt/runtime-libs && cp -L "$(find /usr/lib -name 'libgomp.so.1' | head -n1)" /opt/runtime-libs/

FROM python:3.12-slim AS runtime
COPY --from=builder /opt/runtime-libs/ /usr/local/lib/
RUN ldconfig && groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app config config
COPY --chown=app:app db db
COPY --chown=app:app alembic.ini alembic.ini
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 FPL_CONFIG_DIR=/app/config
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "fpl_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
