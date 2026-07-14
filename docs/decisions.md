# Decisions Log

Why the code is the way it is — including the choices we **reversed**, and what it cost to
learn better. For *what is built and what is next*, see [`../ROADMAP.md`](../ROADMAP.md).

> **Reading order:** the two entries directly below are course corrections that supersede
> earlier decisions further down the file. The superseded sections are kept, marked, and not
> rewritten — a decision log that quietly erases its mistakes teaches nobody anything.

## Where we landed (the short version)

| | Choice | Because |
|---|---|---|
| **Prices** | **NSE bhavcopy** (no auth, ever) | It's the exchange's own file — the one brokers derive from. One request = the whole market for a day. |
| **Splits** | **We adjust, from NSE's corporate-action feed** | A broker's adjustment is undocumented and unknowable. Ours is deterministic and auditable. |
| **News** | **NSE filings** (primary) + optional Upstox wire | A company's filing outranks a journalist's headline. |
| **The LLM** | **None in the pipeline** | The automated track is pure code → zero hallucination surface. A human reads the pack on a Claude subscription. |
| **Universe** | Nifty 500 | Broad enough to be a real screen; bounded enough to be cheap. |
| **Storage** | DuckDB → BigQuery, one interface | ₹0 until it works. Calling code never changes. |
| **Orders** | **Never** | Keeps us outside SEBI's Algo-ID mandate, which binds order *placement*, not research. |

## Course correction — two tracks, no API key (supersedes parts of Phases 4-5)

The system splits into two tracks, and **the Anthropic API leaves the critical path.**

**Automated track** (pure code, no LLM, no API key): ingest → indicators → rule-based
signals → **research pack** (per stock: price summary, computed indicators, triggered
signals with their evidence, news collected *but not interpreted*). Runs on demand or on a
schedule. **Zero hallucination surface, because no model touches it.**

**Interactive track** (a human, on the Claude subscription): paste the pack into a chat for
the qualitative read — narrative, what-to-watch, risk framing — using `prompts/analysis.md`.
Debugging happens in Claude Code. Both are interactive use, covered by the plan.

Why: for a human-in-the-loop research tool where you are reviewing the output anyway,
paste-into-Claude costs nothing beyond the subscription and removes an entire class of
failure. The API key only comes back if we ever want a *truly unattended* brief — an email
at 8am with nobody at the keyboard. (Note: a Pro/Max subscription does **not** cover the
Developer API; that is separate pay-as-you-go credit. This decision sidesteps that spend.)

**Roadmap changes:**
- Phase 4: "LLM news extraction" → **news collection + research-pack exporter** (all code).
- Phase 5: "automated agent synthesis" → **a reusable analysis prompt** you paste the pack into.
- Phases 2, 3, 6 (ingest, indicators, backtest): unchanged — always deterministic.
- Optional later: bolt automated API synthesis back on if hands-off is ever wanted.

### The correction that matters more than the cost saving
"Whenever there's an error we look into it" is **not sufficient in market data**, because
the dangerous failures don't throw. A crash is easy: you see it, you fix it. What quietly
wrecks a research system is plausible-but-wrong data that runs clean —

- an unadjusted split halving a price overnight,
- a stale candle from a holiday,
- a timezone bug shifting every bar by a day,
- a symbol silently dropping out of the universe.

None of these raise an exception. They produce a confident, wrong RSI. So the automated
track carries **data-quality assertions** (`src/asr/quality/checks.py`) that turn those
states into loud errors. *That* is what makes "we look into errors" actually safe: you make
the silent failures into errors there is something to look into.

The findings ride **inside** the research pack, not merely in a log — so a reader cannot
study a stock's numbers without also seeing that its prices may be unadjusted.

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

### Data source — Upstox API  ⚠️ SUPERSEDED
> **Superseded by "Price source: NSE bhavcopy" below.** The Upstox market-data client is
> retired and there is no token in the system. Kept for the record; do not implement from it.
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

## Phase 2 — Ingestion (Upstox era)  ⚠️ SUPERSEDED
> **Superseded by "Price source: NSE bhavcopy" below.** The v3-candle client described here
> no longer exists. Two things from it survived the move and still hold: **timestamps are
> stored tz-naive IST**, and **idempotency (upsert on `(instrument_key, ts)`) is the
> ingestion contract**. Everything else is history.

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

### Deferred, deliberately (Phase 2)
- **Fundamentals and corporate actions** (both listed in the Phase 2 plan): Upstox exposes
  no endpoint for either. They need a separate source and are not blockers for Phases 3–6,
  so they are deferred rather than faked.
- **Split/bonus adjustment — open question.** It is not yet verified whether Upstox
  historical candles are adjusted for splits and bonuses. If they are not, indicators
  (Phase 3) and backtests (Phase 6) will see false gaps. **To verify:** pull a stock with a
  known split and check for an unexplained price discontinuity on the ex-date. This must be
  settled before any backtest result is trusted.

## Price source: NSE bhavcopy, not the broker (supersedes Phase 2)

**The Upstox market-data client is retired. There is no token anywhere in the system.**

Verified live before deciding:

| | Upstox API | **NSE bhavcopy** |
|---|---|---|
| Auth | Analytics Token, renewed yearly | **none** |
| Coverage | one instrument per request | **whole market per request** |
| 3-year backfill | ~1,500 requests | **~750** (one per trading day) |
| Split adjustment | undocumented — unknowable | **ours, from NSE's corporate-actions feed** |
| Churn | v2→v3 deprecation mid-project; `UDAPI100050` | format changed once (2024); both handled |

Bhavcopy *is* the file the broker's prices derive from, so this is a move toward the primary
source, consistent with the rest of the design. Weaknesses are real but different: the site
is bot-hostile (cookie priming, already proven by the news feed) and end-of-day only — which
costs us nothing, since we only ever use daily candles.

**The decisive argument was never the token — it was the splits.** Bhavcopy gives raw traded
prices, and NSE separately publishes every corporate action with its ex-date. Together they
let us *fix* the split problem deterministically instead of merely detecting it, which was
all the quality check could ever do while a broker owned the adjustment.

### How adjustment works
`adj_factor(d)` = product of the ratios of every split/bonus with an ex-date after `d`.
Then `adjusted_price = raw / adj_factor` and `adjusted_volume = raw * adj_factor`. **Raw
prices are never overwritten** — the factor sits beside them, so the adjustment is
reversible, auditable, and fully recomputed whenever a new action lands (a split announced
tomorrow changes the correct factor for every bar before it).

Indicators, signals, returns and the 52-week range are all computed on **adjusted** prices.

### What adjusts, and what deliberately does not
- **Splits and bonuses** adjust: they mechanically restate the share count.
- **Dividends** are recorded, not adjusted. Standard technical analysis works on
  split/bonus-adjusted prices; adjusting for dividends changes what the chart means.
- **Rights issues** are recorded as a **WARN**, never adjusted: doing it properly needs the
  issue price and the ex-date market price, and the effect is far smaller than a split.
- **A split or bonus whose ratio we cannot read is an ERROR** and is *never guessed at*.
  Guessing would leave prices looking continuous while being silently wrong — the exact
  failure this project exists to prevent.

Severity is calibrated so the alarm stays meaningful: if every rights issue were an ERROR,
`asr quality` would fail forever on any stock that ever raised rights, and an alarm that is
always on gets ignored.

### The layer justified itself on its first live run
The parser flagged 27 splits it could not read rather than assuming 1.0. All 27 were NSE
writing the *singular* **"To Re 1/-"** (not "Rs") — the single commonest split there is.
**One of them was KOTAKBANK's 5:1.** A regex that quietly matched nothing would have
corrupted its entire price history with no error anywhere; instead the refusal-to-guess made
it visible in minutes. That is the whole thesis of the quality layer, demonstrated on day one.

**Full 3-year backfill (verified):** 356,865 candles · 500/500 instruments ·
2023-07-17 → 2026-07-14 · 7,174 corporate actions · **0 needing review** · 30,197 candles
adjusted · **0 quality errors**.

### The residual: 42 unexplained jumps, and what they turned out to be
After adjustment, 42 overnight moves >20% remain flagged. Inspected, they fall into three
groups — and the split is instructive:

- **Real market history.** ADANIPORTS −21.1% on 2024-06-04 (election results day);
  ADANIENT −22.6% on 2024-11-21 (the US indictment). The check cannot distinguish a genuine
  crash from a data error, and *should not try* — that is why it is a WARN that says
  "unexplained, verify", not an ERROR.
- **Demergers — a genuine gap.** ABFRL −66.6% on 2025-05-22 is the Aditya Birla Lifestyle
  demerger. NSE files these as a "Scheme of Arrangement", and adjusting for one requires the
  value split between the parent and the demerged entity, which the feed does not give.
  **We do not adjust for demergers, and the WARN is the safety net.** Fixing it properly
  needs a value ratio from the scheme document; deferred rather than guessed at.
- **Suspensions/relistings**, which show up alongside the 39 `candle_gap` warnings.

This is the intended end state: everything the system cannot explain is *visible*, and
nothing is silently assumed.

## Phase 4 — News sources (fetch layer)

### Upstox, not Kite — and Kite wouldn't have worked anyway
Upstox ships a **News API** (`GET /v2/news`, `category=instrument_keys`, ≤30 keys per
request, paginated). **Kite Connect has no news endpoint at all** — so switching brokers
for news would not have solved the problem, while costing a second account, a second auth
flow, Kite's *daily* token handshake, and an order-capable session that cuts against the
research-only boundary. We stay single-broker on one read-only Analytics Token.

### Two sources, ranked by authority
| Source | What it is | Auth |
|--------|-----------|------|
| `nse_announcement` | **Primary** — the company's own filing to the exchange | none |
| `upstox_news` | **Secondary** — a journalist's reporting about the company | token |

Both normalise into one `news` table with a `source` column, because Phase 5 wants
"everything known about RELIANCE this week" in one query. Keeping `source` lets a synthesis
agent weight a filing above a headline, and `url` always points at the underlying document
so any claim can be traced back.

### NSE filings carry the weight
For a system whose premise is grounding, a filing outranks a headline. NSE's endpoint
filters by symbol and date and returns a filing type (`desc`), its gist (`attchmntText`),
a PDF link, and a `seq_id` we dedup on. **Verified live: 65 filings across 3 tickers.**

Two quirks, both handled in `news/nse.py`:
- **Cookie priming.** The JSON endpoint 403s if called cold; the HTML page must be loaded
  first to obtain cookies. Cookies also go stale, so a 401/403/429 forces a *re-prime*
  before the retry rather than reusing the dead session.
- **`an_dt` is `dd-Mon-yyyy HH:MM:SS` in IST with no timezone marker.** Parsed explicitly
  and stored tz-naive IST — the same clock as the candles, so a filing lines up against the
  day it moved the price.

### The known ceiling on text quality
- **Upstox news returns headline + summary + link, never the article body.** Following the
  link would be scraping publisher sites, which this project rules out. So the extractor
  reasons over the summary and cites the link.
- **NSE's gist is sometimes boilerplate.** "…has informed the Exchange about Credit Rating"
  — the actual rating lives in the linked PDF. Others are genuinely rich ("Board meeting on
  July 17 to consider results").
- **If that proves too thin, the next step is parsing the NSE PDFs** — those are the
  exchange's own documents, not a publisher's, so fetching them is not scraping. Deferred
  until we see whether the extractor needs it.

### Idempotency matters more here than for candles
News windows *always* overlap: you re-fetch "the last 30 days" every day. Rows are upserted
on a content id (the source's own id where it has one, else a hash of source+symbol+URL).
The same wire story about two stocks stays two rows — each stock keeps its own evidence.
Verified live: re-running an identical fetch leaves 65 rows, not 130.

### Anthropic API billing is separate from the Claude subscription
A Pro/Max plan covers claude.ai and Claude Code; the Developer API is pay-as-you-go credits
on the same login. **This is why the LLM extractor was dropped** in favour of the two-track
design above: the news is collected and shipped verbatim in the pack, and a human does the
interpretation on the subscription. No key, no spend.

## Phase 4b/5 — Research pack, signals, and the quality layer

### Signals are observations, not recommendations
`features/signals.py` computes plain arithmetic rules (golden/death cross, RSI bands, MACD
crossover, Bollinger breakout, volume spike, 200-DMA regime, 52-week proximity). Each signal
carries **the numbers that triggered it**, so the pack says "golden_cross, because sma_50
crossed from 1412.30 below sma_200 to 1419.80 above it" rather than a bare label to be taken
on faith. `golden_cross` means two averages crossed; it does not mean buy. A crossover is an
*event between two bars*, so it needs the previous row — a mere ordering is not a cross.

`volume_spike` is deliberately **neutral**: unusual volume says something happened, not which
way.

### The pack reports; it does not conclude
`pack/build.py` emits JSON or Markdown containing only computed facts, rule-based signals
with evidence, and **news quoted verbatim** — there is deliberately no `sentiment` field and
no interpretation anywhere. Every item keeps its source and URL so any claim is traceable.
Drawing the conclusion is the reader's job, and keeping that boundary is what makes the
output trustworthy. Every pack carries the "not investment advice" disclaimer (Phase 11).

NULL indicators stay NULL through the whole chain — features → signals → pack. A missing RSI
must never read as 0, which a signal layer would happily interpret as *maximally oversold*.

### News is ranked by TYPE, never by content (`news/materiality.py`)
The first real pack gave a Jio Platforms IPO filing and the dissolution of a ₹0.0009-crore
shell subsidiary identical prominence, and buried both under statutory boilerplate. So filings
are now tiered — **material / contextual / statutory** — and ordered accordingly.

The discipline: this ranks **how price-relevant a filing's *type* is**, a property knowable in
advance. It never asks "is this good or bad for the stock?" — that requires reading the
document, and it is the reader's job. A results announcement outranks a trading-window notice
*whether the results are splendid or dreadful*. There is a test asserting exactly that.

Two bugs that only real data exposed, both worth keeping in mind for any keyword classifier:

1. **The substance is in the summary, not the category.** NSE files the *type* in `desc` and
   the *content* in the attachment text. The Jio IPO is categorised merely as "General
   Updates" — the words "Initial Public Offer" appear only in the body. Ranking on the
   category alone demoted the biggest item in the pack below a shell-company dissolution.
2. **Statutory titles quote the regulation they are filed under, and those names are full of
   material-sounding words.** Once summaries were read, "Disclosure under SEBI (Substantial
   *Acquisition* of Shares and Takeovers) Regulations" matched *acquisition*, and an AGM's
   "voting *results*" matched *results* — promoting pure boilerplate to the top. A statutory
   exclusion list is now checked **first**, before any keyword.

**Known limit, accepted:** a trivial restructuring still ranks as material, because *type* is
all we judge on. The reader sees the filing's own text immediately below it ("0.0000001% of
consolidated net worth"), which is the right place for that judgement to happen.

### Peer & index context (`pack/context.py`)
"RELIANCE is down 8.5% over a year" invites the wrong conclusion on its own; a reader will
supply the missing comparison from imagination if we don't supply it from data. Every pack now
carries the stock against **the index median** and **its own industry median** (NSE's sector
from the Nifty 500 CSV), with percentiles.

For RELIANCE it changes the reading materially: −8.5% over 1y against an index median of
−3.5%, but its Oil & Gas peers are at −6.8% — so it is lagging the market while sitting mid-
pack in its own sector. A percentile is a fact about a distribution, not a verdict. An industry
with fewer than 3 stocks reports no median: one stock is not a distribution.

### The prompt is part of the system
`prompts/analysis.md` is the other half of the handoff: it forbids the model from adding
prices or events from memory, requires every claim to cite a value in the pack, tells it to
refuse rather than guess when data is missing, to read the data-quality section *first*, and
to surface contradictions between signals rather than smoothing them into a tidy story.

## Phase 3 — Indicators

### Stable column names, not pandas-ta's
pandas-ta names output after its parameters (`MACDh_12_26_9`, `BBL_20_2.0_2.0`). We rename
to fixed names (`macd_hist`, `bb_lower`) in `features/indicators.py`. Retuning a parameter
or upgrading pandas-ta must never rename a column that a Phase 5 agent tool queries — and
the suffix format *did* already change between releases, so bands are selected by prefix.

### Indicators are computed per instrument, never across
Rolling and recursive windows are stateful along time. A window spanning two stocks is
silent nonsense (B's SMA quietly averaging in A's prices), and it produces *plausible*
numbers, which is the worst kind of wrong. `compute_features()` takes one instrument;
`compute_all()` is the only thing that groups. There is a test for exactly this bug.

### Full recompute, not incremental append
EMA and RSI carry state from every prior bar, so appending only the new rows would yield
values that quietly disagree with a full recompute. We recompute each instrument's whole
series. On Nifty 500 × 3 years that is seconds of local CPU — much cheaper than debugging
a subtly wrong EMA six weeks later.

### Warmup rows are NULL, never 0
The first `length-1` bars of any window have no defined value. Storing 0 there would let a
Phase 5 agent read "RSI 0" (maximally oversold!) where the truth is "not enough history".
Missing must read as missing all the way up to the agent layer, where the guardrail is to
refuse rather than guess. `build_features()` also reports instruments with fewer than 200
bars, whose slow moving averages are legitimately NULL.

### Tests assert against independent math
`tests/test_indicators.py` checks closed forms (SMA of 1..60), analytic edge cases (RSI is
100 on an unbroken uptrend, 0 on a downtrend, 50 on symmetric moves; ATR of a constant true
range equals that range) and definitional identities (MACD = EMA12 − EMA26; hist = MACD −
signal). Comparing pandas-ta against itself would pass even if the library were wired up
wrong — which is the one failure this layer exists to prevent.
