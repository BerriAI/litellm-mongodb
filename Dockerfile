FROM ghcr.io/astral-sh/uv:0.10.9@sha256:10902f58a1606787602f303954cea099626a4adb02acbac4c69920fe9d278f82 AS uv
FROM python:3.13.13-slim@sha256:aa938a849bcb82dce8f49480f056ab82bf5c1c3ebc294f0430f37b6820e7f286

LABEL org.opencontainers.image.source="https://github.com/BerriAI/litellm-mongodb" \
      org.opencontainers.image.description="BETA MongoDB Vector Search sidecar for LiteLLM" \
      org.opencontainers.image.licenses="MIT"

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY litellm_mongodb ./litellm_mongodb
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /root/.cache/uv \
    && groupadd --gid 10001 sidecar \
    && useradd --uid 10001 --gid 10001 --no-create-home sidecar
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/readiness', timeout=3)"
CMD ["uvicorn", "litellm_mongodb.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "35"]
