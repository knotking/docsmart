# Single image: the built dashboard is served by the API, so one Cloud Run service
# covers both. Multi-stage so node never ships in the runtime layer.

# ---- stage 1: build the dashboard ------------------------------------------
FROM node:22-slim AS web
WORKDIR /build
COPY web/package.json web/package-lock.json* ./
RUN npm ci --omit=dev 2>/dev/null || npm install
COPY web/ ./
RUN npm run build

# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# lxml needs no build tooling on slim thanks to wheels, but keep the layer explicit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxml2 libxslt1.1 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY termguard/ ./termguard/
RUN pip install --no-cache-dir ".[gcp]"

COPY data/rulebook.yaml ./data/rulebook.yaml
COPY --from=web /build/dist ./web/dist

# Run as a non-root user; Cloud Run does not require it but nothing here needs root.
RUN useradd --create-home --uid 1001 termguard && chown -R termguard /app
USER termguard

# Cloud Run injects PORT. Storage and database come from the environment, so the same
# image runs locally against SQLite and in GCP against Cloud SQL + GCS.
ENV PORT=8080 \
    TERMGUARD_STORAGE=gcs \
    TERMGUARD_OUT_DIR=/tmp/termguard-out
EXPOSE 8080

CMD exec uvicorn termguard.api:app --host 0.0.0.0 --port ${PORT} --workers 1
