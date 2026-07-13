# Decisions Log

Records the choices that shape the code, so the plan doc (`tasks.txt`) and the
implementation stay in agreement. Newest phase last.

## Phase 0 — Scope & decisions

| Decision | Choice | Status |
|----------|--------|--------|
| Research-only boundary | No automated order placement, anywhere | Confirmed |
| Data / broker source | **Upstox API** (read-only Analytics Token preferred) | Decided |
| Stock universe | **Nifty 500** | Decided |
| Storage / dev strategy | **DuckDB + Parquet local → BigQuery prod** | Decided |
| GCP project + Anthropic key | — | Pending (Prasad) |

### Research-only boundary — CONFIRMED
The system produces analysis a human reads; it never places orders. This keeps the
project outside SEBI's Algo-ID mandate (which binds automated **order placement**, not
research). A "not investment advice" disclaimer ships in every output. No broker
order/trade endpoint is wired anywhere.

### Data source — Upstox API
- **Why:** Upstox exposes an OHLCV/historical-candle API and a 1-year, read-only
  **Analytics Token**, so we get market data without an order-capable session.
- **Auth:** prefer the Analytics Token (`UPSTOX_ACCESS_TOKEN`) over the OAuth handshake —
  simpler, read-only, and it sidesteps the instrument-search API quirk (error
  `UDAPI100050`). We resolve instruments from the **downloadable NSE instrument master**
  instead (join on ISIN).
- **Code impact:** `src/asr/ingest/upstox_client.py`, `src/asr/config.py` (Upstox fields).

### Stock universe — Nifty 500
- **Why:** broad enough to be a real screen (large + mid caps) while staying a bounded,
  well-defined list. Bigger than Nifty 50 (more research surface), far cheaper than
  all-of-NSE (data volume, API rate limits, LLM tokens).
- **Inputs Prasad provides:** NSE's Nifty 500 constituents CSV (`Symbol` + `ISIN Code`) →
  `universe/nifty500.csv`, plus the Upstox NSE instrument master to map ISIN →
  `instrument_key`.
- **Code impact:** `src/asr/ingest/instruments.py` (`UNIVERSE_CSV = universe/nifty500.csv`).

### Dev strategy / storage — DuckDB first, BigQuery later  *(the `[CLAUDE]` Phase 0 item)*
Goal: **keep cost at ~₹0 until the system actually works**, then lift to the cloud
without rewriting calling code.

- **Local (now, Phases 2–7):** DuckDB file (`./data/asr.duckdb`) + Parquet. Zero cloud
  spend, fast, runs entirely on the CachyOS box in the dev container. All of ingest,
  indicators, news extraction, agents, backtest, and the research brief can be built and
  iterated here.
- **Prod (later, Phases 8–9):** BigQuery, selected by flipping `STORAGE_BACKEND=bigquery`.
  Both backends implement the same `StorageAdapter` interface
  (`src/asr/storage/base.py`), so **calling code never changes** — only the adapter and
  a few env vars (`GCP_PROJECT`, `BQ_DATASET`).
- **Cost controls that follow from this:**
  - No GCP project or billing is required to build Phases 2–3 — defer that spend.
  - The Anthropic key is only needed from **Phase 4** onward; Phases 2–3 are pure
    deterministic Python, so early iteration costs nothing but local compute.
  - When the LLM layer comes online, cap spend with token budgeting + spend caps
    (Phase 10) and cache/dedup news extraction so we don't re-pay for the same article.
  - BigQuery only for what needs scale/sharing; keep exploratory queries on DuckDB.

### Still pending (Prasad, not blocking near-term work)
- **GCP project + billing** — needed only at Phase 8–9 (BigQuery / deploy). DuckDB-first
  means Phases 2–3 proceed with none of it.
- **Anthropic API key** — needed from Phase 4 (news/agents). Paste into `ANTHROPIC_API_KEY`.

## Phase 1 — Scaffolding (already implemented)
See the repo layout in `README.md`. Notable choices made during scaffolding:
- **`src/asr/` src-layout** — cleaner on GitHub, packages cleanly into the multi-stage
  Docker image.
- **Python 3.12** — forced by the current `pandas-ta` (needs `numpy>=2.2` and Python
  ≥3.12); the earlier "pin numpy<2" note applied only to the old 2021 beta.
- **Secrets** — local `.env` now; GCP Secret Manager hook noted in `config.py` for Phase 9.

## Phase 2 — Ingestion

### Historical candles: the **v3** API, windowed
`/v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}` (v2 is being deprecated).
Upstox caps how wide a single request may be, so `upstox_client.split_windows()` chops a
range into ≤1-year windows for daily candles and stitches the results. Backfill length is
therefore a parameter, not an API limit.

### Timestamps are stored tz-naive **IST**
Upstox returns `+05:30`-offset ISO timestamps. We convert to Asia/Kolkata and drop the
tzinfo, so every timestamp in the warehouse is on one clock and DuckDB/BigQuery
comparisons never silently cross a timezone. Indicators (Phase 3) can assume this.

### Idempotency is the ingestion contract
Every write is an upsert keyed by `(instrument_key, ts)`. Re-running an interrupted
backfill costs API calls, never duplicate rows — which is what makes `asr ingest daily`
safe to schedule (Phase 8) and safe to retry.

### Instrument resolution: no manual downloads
Both inputs are fetched automatically, neither needs auth:
- NSE's Nifty 500 constituents CSV (`nsearchives.nseindia.com`; NSE 403s a default client
  UA, so we send a browser one).
- The Upstox NSE instrument master (`assets.upstox.com`), filtered to `segment=NSE_EQ`,
  `instrument_type=EQ`.

The join is on **ISIN** (stable across NSE symbol renames), falling back to symbol.
All 500 constituents currently resolve. Rows that resolve to nothing are *reported*, not
silently dropped — a short universe should be visible, not inferred later from missing data.

### Retries distinguish "try again" from "you're wrong"
`UpstoxTransientError` (429 / 5xx / network) retries with exponential backoff;
`UpstoxError` (4xx — bad token, unknown instrument) fails immediately. A single bad
instrument is recorded in the run's `failures` and the run continues, so one delisted
symbol can't abort a 500-stock backfill.

### Deferred, deliberately
- **Fundamentals and corporate actions** (both listed in the Phase 2 plan): Upstox exposes
  no endpoint for either. They need a separate source and are not blockers for Phases 3–6,
  so they are deferred rather than faked.
- **Split/bonus adjustment — open question.** It is not yet verified whether Upstox
  historical candles are adjusted for splits and bonuses. If they are not, indicators
  (Phase 3) and backtests (Phase 6) will see false gaps. **To verify:** pull a stock with a
  known split and check for an unexplained price discontinuity on the ex-date. This must be
  settled before any backtest result is trusted.
