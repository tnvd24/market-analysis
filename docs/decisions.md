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
