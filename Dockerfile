# Job Fit Agent: API + dashboard in one image.
#   docker compose up            (app + local pgvector; see docker-compose.yml)
#   docker build -t jobfit .     (CI builds this to prove it still builds)
# Stage 1 builds the React app; stage 2 is the Python runtime that serves it.

# ---- web ----------------------------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---- runtime ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS app
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

# Dependencies first (cached until pyproject/uv.lock change).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Chromium + its system libraries for PDF rendering, and the embedding model, baked in so the
# container starts without network downloads.
RUN playwright install --with-deps chromium \
    && python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" \
    && rm -rf /var/lib/apt/lists/*

COPY src/ src/
COPY config/ config/
COPY profile/ profile/
COPY db/ db/
COPY evals/cases/ evals/cases/
COPY docker/entrypoint.sh /entrypoint.sh
COPY --from=web /web/dist web/dist
# GOTCHA: editable install (uv's default) keeps the package in /app/src, so jobfit.config.ROOT
# resolves to /app and finds config/, profile/ and web/dist.
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 jobfit \
    && mkdir -p /app/output && chown -R jobfit /app/output /opt/fastembed

USER jobfit
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
