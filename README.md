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
- **Phase 3 — Features (deterministic):** RSI/MACD/MA/ATR/Bollinger via `pandas-ta` → `features`.
- **Phase 4 — News (deterministic):** NSE filings + Upstox news → `news`, collected verbatim.
- **Phase 4b — Quality + pack:** assertions that make silent corruption loud; a research pack.
- **Phase 5 — Interactive read:** paste the pack into Claude with `prompts/analysis.md`.
- **Phase 6 — Backtest/eval:** measured performance before anything is trusted.

**No model touches the data.** Everything above is pure code, so there is no hallucination
surface at all; a human does the interpretation, over numbers a library computed.

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
Without Docker: `uv pip install -e ".[dev]"` then `asr info`.

Lean prod image (for Artifact Registry / Cloud Run later):
`docker build --target runtime -t asr:prod .`

## Ingestion (Phase 2)
```bash
asr ingest instruments         # Nifty 500 -> instrument keys
asr ingest prices --years 3    # daily OHLCV from NSE bhavcopy
asr ingest actions --years 3   # splits, bonuses, dividends
asr ingest adjust              # apply split/bonus adjustment to the stored prices
asr ingest prices --incremental  # only the missing days (schedule this)
asr ingest status
```
**No API token anywhere.** Prices come from **NSE bhavcopy** — the exchange's own end-of-day
file, one request per trading day for the whole market. Nothing to sign up for or renew.

**Splits are adjusted, not just detected.** Raw traded prices are stored untouched, and an
`adj_factor` computed from NSE's corporate-actions feed restates them, so a 1:2 split stops
looking like a 50% crash. Indicators, returns and the 52-week range all use adjusted prices.
A split whose ratio we *can't* read is an error, never a guess. Rights issues are flagged but
not adjusted; dividends are recorded but not adjusted.

Every write is an upsert keyed by `(instrument_key, ts)`, so an interrupted backfill is safe
to re-run, and fetched days are cached on disk — a re-run costs no network at all. Candle
timestamps are tz-naive **IST**.

## Indicators (Phase 3)
```bash
asr features build            # candles -> RSI, MACD, MAs, ATR, Bollinger, volume
asr features show RELIANCE    # spot-check one ticker
```
Real math via `pandas-ta`, never LLM-guessed. Indicators are computed per instrument (no
rolling window ever spans two stocks) and recomputed in full rather than appended, because
EMA and RSI carry state from every prior bar. Warmup rows are **NULL, not 0** — "not enough
history" must never read as "RSI 0".

## News & filings (Phase 4 — fetch layer)
```bash
asr news fetch --days 30            # NSE filings + Upstox news -> news
asr news fetch --source nse         # filings only (needs no token)
asr news show RELIANCE
```
Two sources, ranked by authority: **NSE corporate announcements** (primary — the company's
own filing to the exchange) and the **Upstox News API** (secondary — reporting about it).
No open-web scraping. Rows are deduped on a content id, because news windows always overlap
— you re-fetch "the last 30 days" every day.

News is **collected, never interpreted** — the pack quotes it verbatim and a human reads it.

## Research packs & data quality (the deliverable)
```bash
asr quality                      # data-quality assertions; exits non-zero on ERROR
asr pack build RELIANCE          # print a research pack (Markdown)
asr pack build --out packs/      # one pack per stock in the universe
```
A **research pack** is everything known about a stock — price summary, computed indicators,
rule-based signals *with the numbers that triggered them*, and news quoted verbatim — and
**nothing interpreted**. Paste it into Claude with [`prompts/analysis.md`](prompts/analysis.md)
for the qualitative read. That runs on your subscription; no API key, no cost.

**Why there's a quality layer:** in market data the dangerous failures don't throw. An
unadjusted split, a stale holiday candle, a timezone shift, a symbol dropped from the
universe — none of these raise an exception, they just produce a confident, wrong RSI.
`asr quality` turns each of those into a loud error, and its findings ride *inside* every
pack, so you can't read a stock's numbers without seeing that they may be suspect.

## What YOU need to provide
1. ~~**Upstox Analytics Token**~~ — **no longer needed.** Prices come from NSE bhavcopy,
   which needs no auth. (An Upstox token is only useful if you want their *news* feed on
   top of NSE's filings; the market-data client is retired.)
2. ~~**Anthropic API key**~~ — **no longer needed.** The system is deterministic end to
   end; the qualitative read happens by pasting a research pack into Claude on your
   subscription. (A Pro/Max plan does *not* cover the Developer API anyway — that's
   separate pay-as-you-go credit.) A key is only required if you later want a fully
   unattended brief with nobody at the keyboard.
