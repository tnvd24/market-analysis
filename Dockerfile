# syntax=docker/dockerfile:1
#
#   builder  -> resolve + install deps into an isolated prefix
#   runtime  -> the lean image you actually run and deploy
#   dev      -> runtime + tests/linters, for `docker compose`
#
# The image needs NO credentials. Prices, corporate actions and filings all come from NSE
# unauthenticated, so `docker run asr:prod asr pipeline` works on a clean machine.

# ---- builder -----------------------------------------------------------------
FROM python:3.12-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
# uv.lock pins the exact resolution; the glob keeps this working if it's ever absent.
COPY pyproject.toml uv.lock* ./
COPY src ./src
RUN uv pip install --prefix=/install "."

# ---- runtime -----------------------------------------------------------------
FROM python:3.12-slim AS runtime
COPY --from=builder /install /usr/local
WORKDIR /app

COPY src ./src
COPY pyproject.toml README.md ./
# The universe snapshot (so a cold container needn't hit NSE just to know what to research)
# and the analysis prompt (so the pack and the prompt that reads it ship together).
COPY universe ./universe
COPY prompts ./prompts

# Non-root, and it must own the data dir: the pipeline writes DuckDB, the bhavcopy cache
# and the packs. uid 1000 matches the typical desktop user, so a bind-mounted ./data on the
# host stays writable without a chown dance.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/data /app/packs \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    DUCKDB_PATH=/app/data/asr.duckdb

# Default: the daily run. Override with any `asr ...` command.
CMD ["asr", "pipeline"]

# ---- dev ---------------------------------------------------------------------
FROM runtime AS dev
USER root
RUN pip install --no-cache-dir uv && uv pip install --system -e ".[dev]"
USER appuser
CMD ["sleep", "infinity"]
