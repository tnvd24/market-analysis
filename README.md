# Stock Research — Indian Markets (research-only)

A **grounded** research system for the Nifty 500. The design rule:
**deterministic data and math on the outside; a human does the interpretation.**

**No model touches the data**, so there is no hallucination surface at all. The system
computes the facts — prices, indicators, rule-based signals, filings — and hands you a
**research pack**. You read it, with Claude's help, on your subscription.

> ⚠️ Research only. This system produces analysis you read, not orders it places.
> That keeps it outside SEBI's algo-trading (Algo-ID) mandate, which binds automated
> **order placement**, not research. Nothing here is investment advice.

**No API keys. No accounts. ₹0 to run.** Prices, corporate actions and company filings all
come from NSE, which serves them without authentication.

## How it works
```
NSE bhavcopy ─┐
              ├─> candles ──(split-adjusted)──> indicators ──> signals ─┐
corp actions ─┘                                                         ├─> research pack ─> you + Claude
NSE filings ─────────────────────────────────> news (verbatim) ────────┘
                                     ↑
                             quality assertions
                    (make the silent failures loud)
```

**Splits are fixed, not just detected.** Raw traded prices are stored untouched; an
adjustment factor computed from NSE's own corporate-action feed restates them. Without that,
a 1:2 split looks exactly like a 50% overnight crash — and *nothing errors*.

**In market data the dangerous failures don't throw.** A stale candle, a timezone shift, an
unreadable split ratio, a symbol dropped from the universe: none raise an exception, they
just produce a confident, wrong RSI. `asr quality` turns each into a loud error, and its
findings ride *inside* every pack.

Storage sits behind one interface: **DuckDB** locally (free), **BigQuery** in prod (flip
`STORAGE_BACKEND`). Same calling code either way.

📖 **[Setup guide — Linux, macOS, Windows](docs/setup.md)** ·
🗺️ [Roadmap](ROADMAP.md) · 🧠 [Why it's built this way](docs/decisions.md)

## Layout
```
src/asr/
  config.py       # settings (nothing is required)
  ingest/         # bhavcopy (prices), corporate_actions + adjust (splits), instruments
  features/       # indicators (pandas-ta) + rule-based signals
  news/           # NSE filings (primary) + optional Upstox wire
  quality/        # the assertions that make silent corruption loud
  pack/           # the research pack — computed facts, zero interpretation
  storage/        # duckdb (dev) + bigquery (prod), one interface
  backtest/       # Phase 6
  cli.py
prompts/          # analysis.md — the prompt you paste the pack into
infra/            # Phases 8-9 — deploy manifests, DAGs
```

## Quickstart
```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"

asr ingest instruments        # the Nifty 500 universe
asr ingest prices --years 3   # NSE bhavcopy -> candles
asr ingest actions --years 3  # splits, bonuses, dividends
asr ingest adjust             # restate prices for splits
asr features build            # indicators, on adjusted prices
asr quality                   # verify before you trust
asr pack build RELIANCE       # the thing you actually read
```
Then paste that pack into Claude with [`prompts/analysis.md`](prompts/analysis.md).

Full instructions for all three platforms (and Docker): **[docs/setup.md](docs/setup.md)**.

## Ingestion
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

## Indicators
```bash
asr features build            # candles -> RSI, MACD, MAs, ATR, Bollinger, volume
asr features show RELIANCE    # spot-check one ticker
```
Real math via `pandas-ta`, never LLM-guessed. Indicators are computed per instrument (no
rolling window ever spans two stocks) and recomputed in full rather than appended, because
EMA and RSI carry state from every prior bar. Warmup rows are **NULL, not 0** — "not enough
history" must never read as "RSI 0".

## News & filings
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

## Backtest — do the signals work?
```bash
asr backtest run RELIANCE --strategy sma_cross   # one stock vs buy-and-hold
asr backtest universe --strategy rsi_reversion   # the same rule across all 500
```
**No. Not as mechanical trading rules — and that's a finding worth having.** Across 500 stocks
and 3 years, *not one* rule beat buy-and-hold. The best won on 33% of stocks; MACD crossover on
14%. They sit out ~60% of a rising market, and that costs more than their timing saves. They
*do* cut drawdowns (−29% vs −41%) — they just don't earn their keep doing it.

The engine is built to be hard to fool: positions are **shifted one bar** (a signal from
today's close is traded tomorrow, never today), costs are real (25 bps/side), and every result
carries the **survivorship-bias** warning that can't be engineered away.

This is exactly why the pack presents signals as **observations, never recommendations**. Full
write-up: **[docs/backtest-results.md](docs/backtest-results.md)**.

## Credentials

**None required.** No broker account, no API key, no billing. That is a deliberate design
outcome, not a coincidence:

- **Prices, corporate actions and filings** come from NSE, unauthenticated.
- **No Anthropic key**, because no model is in the pipeline — you paste the pack into Claude
  on your subscription. (A Pro/Max plan does not cover the Developer API in any case; that's
  separate pay-as-you-go credit.)
- **Optional:** an Upstox read-only token adds their news wire on top of NSE's filings. Put
  it in `UPSTOX_ACCESS_TOKEN`. Nothing else changes if you don't.

## Development
```bash
pytest -q          # ~110 tests, fully offline (no network, no fixtures to refresh)
ruff check . && ruff format .
```
Tests never hit the network: HTTP is mocked at the transport layer, so the suite is fast and
runs on a plane. Indicator tests assert against math derived *independently* of `pandas-ta` —
comparing the library against itself would pass even if it were wired up wrong.
