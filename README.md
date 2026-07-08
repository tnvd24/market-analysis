# Market Analysis — Grounded Agentic Stock Research (Indian Markets)

A **research-only** agentic stock research system for Indian markets.

Design principle: **deterministic data and math on the outside, Claude only for
reasoning and synthesis on the inside.** GCP-flavored, containerized to run on a
local CachyOS box and lift to the cloud.

> **Research-only boundary:** no automated order placement anywhere in the system.
> This keeps the project outside SEBI's Algo-ID mandate. Every output carries a
> "not investment advice" disclaimer.

## Architecture at a glance

- **Deterministic edges** — data ingestion, technical indicators, and backtests are
  real code and real math, never LLM-guessed.
- **LLM core** — Claude handles news/sentiment extraction (to a strict schema) and
  the agentic reasoning layer, and must cite the data rows behind any claim or
  refuse when data is missing rather than hallucinate.
- **Same interface, two backends** — DuckDB + Parquet locally (near-zero cost),
  BigQuery in production.

## Planned monorepo layout

```
ingest/     # broker API client + historical/incremental ingestors
features/   # RSI, MACD, MAs, ATR, Bollinger, volume (pandas-ta) + tests
news/       # Claude extractor: articles -> structured JSON (Pydantic)
agents/     # LangGraph supervisor + constrained tool-calling agents
backtest/   # vectorbt/backtrader harness + reasoning-fidelity evals
app/        # daily research brief generator / Streamlit dashboard
infra/      # Dockerfiles, compose, Cloud Run/GKE, scheduling DAG
```

## Roadmap

The full phased plan (Phase 0 scope → Phase 11 compliance checkpoint), with each
step tagged `[YOU]` / `[ME]` / `[BOTH]`, lives in [`tasks.txt`](tasks.txt).

## Status

Planning stage. Scaffolding begins once the Phase 0 decisions (data/broker source,
stock universe, GCP project + Anthropic API key) are made.

## License

MIT — see [`LICENSE`](LICENSE).
