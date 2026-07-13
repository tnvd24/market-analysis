# syntax=docker/dockerfile:1
# Multi-stage build:
#   builder  -> installs runtime deps into an isolated prefix
#   runtime  -> lean prod image (deploy this: Artifact Registry -> Cloud Run/GKE)
#   dev      -> runtime + dev tools + editable install (used by docker-compose)

# ---- builder: build/install the package + runtime extras ----
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
# uv.lock is optional; the glob makes the COPY a no-op when it's absent.
COPY pyproject.toml uv.lock* ./
COPY src ./src
# Runtime deps only (no dev), installed into a relocatable prefix.
RUN uv pip install --prefix=/install "."

# ---- runtime: minimal production image ----
FROM python:3.12-slim AS runtime
# Node is cheap insurance for an MCP-based news tool later (Phase 4).
# Debian bookworm ships Node 18, which is adequate for MCP servers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        nodejs \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
WORKDIR /app
COPY src ./src
COPY pyproject.toml README.md ./
RUN useradd -m appuser && chown -R appuser /app
USER appuser
ENV PYTHONUNBUFFERED=1
# Default: show CLI help. Compose overrides this for the dev shell.
CMD ["asr", "--help"]

# ---- dev: editable install + dev tools, for docker-compose ----
FROM runtime AS dev
USER root
RUN pip install --no-cache-dir uv
RUN uv pip install --system -e ".[dev]"
USER appuser
