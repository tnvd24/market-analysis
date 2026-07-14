# Roadmap

What this system is, what's built, and what's left. The *reasoning* behind each choice —
including the ones we reversed — lives in [`docs/decisions.md`](docs/decisions.md).

Ownership:
- **[PRASAD]** — accounts, decisions, running things, judging whether output is useful
- **[CLAUDE]** — code, schemas, docs
- **[BOTH]** — Claude writes it, Prasad runs and verifies it

---

## The design in one line

**Deterministic data and math on the outside; a human does the interpretation.**
No model touches the data, so there is no hallucination surface. The system computes facts
and hands them over; you read them, with Claude's help, on your subscription.

### Two tracks

**Automated track** (pure code, no LLM, no API key, ₹0):
`ingest → adjust → indicators → rule-based signals → research pack`

**Interactive track** (you, on the Claude subscription):
paste a research pack into a chat with [`prompts/analysis.md`](prompts/analysis.md) for the
qualitative read. Debug in Claude Code.

### Non-negotiables
1. **Research only. No order placement anywhere.** This keeps the project outside SEBI's
   Algo-ID mandate, which binds automated *order placement*, not research. Every output
   carries a "not investment advice" disclaimer.
2. **The dangerous failures don't throw.** An unadjusted split, a stale candle, a timezone
   shift, a symbol dropped from the universe — none raise an exception; they produce a
   confident, wrong number. Every one of them is an explicit assertion in `asr quality`.
3. **Never guess.** A corporate action whose ratio we can't read is an *error*, not an
   assumption. Missing indicators are NULL, never 0.

---

## Done

### Phase 0-1 — Scope & scaffolding ✅
`src/asr/` package, multi-stage Docker, ruff + pre-commit, `uv.lock`. **Python 3.12+.**
Universe: **Nifty 500**. Storage: **DuckDB** locally → **BigQuery** in prod, behind one
`StorageAdapter`, so calling code never changes.

### Phase 2 — Ingestion ✅  *(price source: NSE, not a broker)*
- **[CLAUDE]** `ingest/bhavcopy.py` — NSE's official end-of-day file. **No token, ever.**
  One request = the whole market for a day. Handles both NSE file formats and discovers
  holidays from the 404. Days are cached, so re-runs cost no network.
- **[CLAUDE]** `ingest/corporate_actions.py` + `adjust.py` — **splits are fixed, not just
  detected.** We adjust from NSE's own feed, so the adjustment is ours and auditable. Raw
  prices are never overwritten.
- **[CLAUDE]** `ingest/instruments.py` — Nifty 500 → instrument keys, joined on ISIN. All
  500 resolve; unresolved rows are reported, never dropped silently.
- ~~Broker API client~~ — **retired.** Kite has no news endpoint; Upstox's candle adjustment
  is undocumented. Bhavcopy is the file both derive from.
- **Deferred:** fundamentals (no free source; doesn't block anything).
- **Known gap: demergers are not adjusted.** NSE files them as a "Scheme of Arrangement" and
  the feed carries no value-split ratio, so the parent's price drop looks like a crash (e.g.
  ABFRL −66.6%). It is *flagged*, never silently assumed — fixing it needs a ratio from the
  scheme document.

**Verified at full scale:** 356,865 candles · 500/500 instruments · 3 years ·
7,174 corporate actions · 0 needing review · **0 quality errors**.

### Phase 3 — Indicators ✅
`features/indicators.py` — RSI, MACD, moving averages, ATR, Bollinger, OBV, volume features
via `pandas-ta`. Real math, never LLM-guessed. Computed **per instrument on adjusted prices**;
warmup rows are NULL. Tests assert against math derived independently of pandas-ta.

### Phase 4 — News & filings ✅  *(deterministic; the LLM extractor was dropped)*
`news/nse.py` — **NSE corporate announcements** (primary: the company's own filing).
`news/upstox_news.py` — optional news wire (secondary; the only thing a token buys).
Collected **verbatim, never interpreted**. No sentiment field exists, by design.

### Phase 4b — Quality & the research pack ✅
`quality/checks.py` — the assertions that make silent corruption loud.
`features/signals.py` — rule-based signals, each carrying **the numbers that triggered it**.
`pack/build.py` — the research pack (JSON/Markdown).
`prompts/analysis.md` — the paste-in prompt.

### Phase 5 — Interactive analysis ✅
- **[CLAUDE]** the analysis prompt. Forbids adding facts from memory, requires citing the
  pack's numbers, refuses rather than guesses, reads data-quality first, surfaces
  contradictions between signals instead of smoothing them into a story.
- **[PRASAD]** run a pack through Claude and say whether the read is useful. **That feedback
  shapes what the pack should carry next.**

---

### Phase 6 — Backtest & eval ✅
`backtest/` — every signal now has *measured* performance instead of a plausible story.
Guards against the three things that fool a backtest: **lookahead** (positions are always
shifted a bar — a signal from today's close is traded tomorrow), **costs** (25 bps a side),
and **survivorship bias** (can't be fixed; every result carries the warning).

**The result is negative, and it matters: not one rule beat buy-and-hold.**
The best (`rsi_reversion`) won on 33% of stocks; `macd_cross` on 14%. They're in the market
~40% of the time, and the market rose 54% — being out of a rising market is the whole cost.
They *do* cut drawdowns (−29% vs −41%), they just don't earn their keep doing it.
**Full write-up: [`docs/backtest-results.md`](docs/backtest-results.md).**

This validates the pack's design rather than undermining it: signals are presented as
**observations, never recommendations**. Had the pack said "golden cross → BUY", it would
have been confidently wrong on 85% of stocks.

---

## Next

### Phase 7 — Delivery
- **[CLAUDE]** Scheduled pack generation (a dated folder of Markdown packs). Possibly a small
  Streamlit view over the warehouse. Read-only, disclaimers baked in.

### Phase 8-9 — Orchestration & deploy *(optional — only if you want it hands-off)*
- **[CLAUDE]** A DAG: ingest → adjust → features → news → quality → packs. Cloud Scheduler →
  Cloud Run is the light option; Composer/Airflow the heavy one.
- **[PRASAD]** GCP project + billing (only needed here, not before), run the deploy.

### Phase 10 — Hardening
- **[CLAUDE]** Structured logging, retry/backoff (partly done), data-quality checks (done —
  `quality/`), secret rotation (only if a token is ever introduced).

### Phase 11 — Compliance checkpoint
- **[BOTH]** Confirm no order-placement path exists anywhere, and that the disclaimer ships
  in every output. (Both hold today: no broker order endpoint is wired, and `pack/build.py`
  puts the disclaimer in every pack.)

---

## Deliberately not doing

- **An LLM in the pipeline.** It was the original Phase 4-5 plan and it was dropped. The
  automated track stays pure code, which is what gives it zero hallucination surface.
- **Automated order placement.** Ever. See non-negotiable #1.
- **Open-web scraping.** News comes from NSE's filings and an optional broker wire, not from
  scraping publishers.
- **Guessing a corporate-action ratio.** An unreadable split is an error. This has already
  paid for itself — see the KOTAKBANK story in `docs/decisions.md`.
