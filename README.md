# Agentic Stock Research — Indian Markets (research-only)

A **grounded** agentic research system for NSE/BSE. Design rule:
**deterministic data + math on the outside, Claude only for reasoning/synthesis inside.**
The model never invents prices, indicators, or targets — it reasons over numbers a
library computed and cites the rows behind every claim.

> ⚠️ Research only. This system produces analysis you read, not orders it places.
> That keeps it outside SEBI's algo-trading (Algo-ID) mandate, which binds automated
> **order placement**, not research. Nothing here is investment advice.

## Architecture (by phase)
- **Phase 2 — Ingest (deterministic):** Upstox market data → `candles` table.
- **Phase 3 — Features (deterministic):** RSI/MACD/MA/ATR via `pandas-ta` → `features`.
- **Phase 4 — News (Claude, grounded):** articles → structured JSON (sentiment/catalysts).
- **Phase 5 — Agents (Claude):** LangGraph supervisor; tools query the DB with typed inputs.
- **Phase 6 — Backtest/eval:** measured performance before anything is trusted.

Storage is behind one interface: **DuckDB** locally (free), **BigQuery** in prod (flip
`STORAGE_BACKEND`). Same calling code either way.

**Phase 0 decisions** (data source = Upstox, universe = Nifty 500, storage = DuckDB-first)
are logged with rationale in [`docs/decisions.md`](docs/decisions.md).

## Layout
```
src/asr/
  config.py            # env-driven settings
  storage/             # base + duckdb (dev) + bigquery (prod)
  ingest/              # upstox_client.py, instruments.py (Nifty 500 → instrument keys)
  features/            # Phase 3
  news/                # Phase 4
  agents/              # Phase 5
  backtest/            # Phase 6
  app/                 # Phase 7 — research brief / dashboard
  cli.py               # `asr ingest smoke`, `asr info`
infra/                 # Phases 8-9 — Cloud Run/GKE, Secret Manager, DAGs
```

## Quickstart (local, CachyOS + Docker/Rancher)
```bash
cp .env.example .env          # then fill in tokens (see below)
docker compose up -d --build
docker compose exec asr asr info
docker compose exec asr asr ingest smoke   # auth + storage check
```
Without Docker: `uv pip install -e ".[upstox,dev]"` then `asr info`.

Lean prod image (for Artifact Registry / Cloud Run later):
`docker build --target runtime -t asr:prod .`

## What YOU need to provide (unblocks Phase 2)
1. **Upstox Analytics Token** — Upstox → Developer Apps → generate the 1-year,
   read-only **Analytics Token**. Paste into `UPSTOX_ACCESS_TOKEN` in `.env`.
2. **Anthropic API key** — paste into `ANTHROPIC_API_KEY` (only needed from Phase 4).
3. **Nifty 500 CSV** — download NSE's Nifty 500 constituents CSV (has `Symbol` +
   `ISIN Code`) and save as `universe/nifty500.csv`.
4. **Upstox NSE instrument master** — download the NSE master (json/json.gz) from
   Upstox docs; we join on ISIN to get `instrument_key`s. (We avoid the
   instrument-search API — known Analytics-Token quirk.)

Once (1) and (3)+(4) are in place, `asr ingest smoke` should store real candles,
and I'll build out full backfill + the Phase 3 indicators.
