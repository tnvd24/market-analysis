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
asr ingest instruments        # Nifty 500 -> Upstox instrument keys (no token needed)
asr ingest smoke              # one stock, 30 days — auth + storage check
asr ingest backfill --years 3 # full daily history for the universe
asr ingest daily              # incremental: only what's missing (schedule this)
asr ingest status             # universe size, candle coverage, date range
```
Every write is an upsert keyed by `(instrument_key, ts)`, so an interrupted backfill is
safe to re-run — it costs API calls, never duplicate rows. Candle timestamps are stored
tz-naive **IST**.

`asr ingest instruments` downloads both the NSE Nifty 500 constituents CSV and the Upstox
instrument master by itself, and joins them on ISIN. Nothing to download by hand.

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
1. **Upstox Analytics Token** — Upstox → Developer Apps → generate the 1-year,
   read-only **Analytics Token**. Paste into `UPSTOX_ACCESS_TOKEN` in `.env`.
   This is the only thing standing between you and real candles.
2. ~~**Anthropic API key**~~ — **no longer needed.** The system is deterministic end to
   end; the qualitative read happens by pasting a research pack into Claude on your
   subscription. (A Pro/Max plan does *not* cover the Developer API anyway — that's
   separate pay-as-you-go credit.) A key is only required if you later want a fully
   unattended brief with nobody at the keyboard.
