# Setup & how-to — Linux, macOS, Windows

From a fresh clone to a research pack in your hands.

**You need no accounts, no API keys and no credit card.** Prices, corporate actions and
company filings all come from NSE, which serves them without authentication. Everything
below runs on a laptop, offline after the first pull, and costs nothing.

---

## 0. Prerequisites

| | Requirement |
|---|---|
| **Python** | **3.12 or newer** (3.11 will *not* work — `pandas-ta` requires 3.12+) |
| **Disk** | ~1 GB for three years of Nifty 500 daily data plus the bhavcopy cache |
| **Network** | Only for ingestion. Analysis works offline. |
| **Git** | To clone. |

We use [**uv**](https://docs.astral.sh/uv/) — one fast tool that installs Python, creates the
virtualenv and resolves dependencies. Plain `pip` works too (§6).

---

## 1. Install uv

**Linux / macOS**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close and reopen the terminal so `uv` lands on your `PATH`, then check:
```
uv --version
```

> **macOS/Homebrew alternative:** `brew install uv`
> **Arch/CachyOS alternative:** `sudo pacman -S uv`

---

## 2. Clone and install

**Linux / macOS**
```bash
git clone https://github.com/tnvd24/market-analysis.git
cd market-analysis
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

**Windows (PowerShell)**
```powershell
git clone https://github.com/tnvd24/market-analysis.git
cd market-analysis
uv venv --python 3.12
.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"
```

> If PowerShell refuses to run the activate script, allow local scripts once:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
> (Or use `cmd.exe` and run `.venv\Scripts\activate.bat`.)

Confirm the CLI is live — this works identically on all three platforms:
```
asr --help
asr info
```

`asr info` should report `price_source: NSE bhavcopy (no auth)`.

---

## 3. Configuration (optional)

```bash
cp .env.example .env        # Windows: copy .env.example .env
```

**You can skip this entirely.** The defaults are enough for a full run. `.env` only matters
if you want the optional Upstox *news* feed, or the BigQuery backend.

---

## 4. Load the data

Run these in order. The first two touch the network; everything after is local.

```bash
asr ingest instruments        # 1. the Nifty 500 universe (auto-downloads from NSE)
asr ingest prices --years 3   # 2. daily OHLCV from NSE bhavcopy  (~10-15 min)
asr ingest actions --years 3  # 3. splits, bonuses, dividends     (~1 min)
asr ingest adjust             # 4. restate prices for splits/bonuses
asr features build            # 5. RSI, MACD, moving averages, ATR, Bollinger...
asr news fetch --days 30      # 6. corporate filings from NSE
asr quality                   # 7. verify the data before you trust it
```

**Why step 4 matters.** Bhavcopy gives *raw* traded prices. Without adjustment, a 1:2 split
looks exactly like a 50% overnight crash — no error is raised, and every indicator spanning
it is quietly wrong. Step 4 fixes that from NSE's own corporate-action feed.

**Never skip step 7.** `asr quality` exits non-zero if it finds something that would make the
numbers untrustworthy — an unreadable split ratio, a hole in a price series, a future-dated
candle (the fingerprint of a timezone bug). In market data the dangerous failures don't
crash; they produce a confident, wrong answer. This is what turns them into errors you can
actually see.

Progress check at any point:
```
asr ingest status
```

### It's slower than you'd like — why, and why that's fine
The price backfill makes one request per *trading day* (~750 for three years), throttled to
be a polite guest on NSE's servers. Each day is cached to `data/bhavcopy/`, so **a re-run
costs no network at all** — and an interrupted backfill is safe to simply run again. Nothing
duplicates: every write is an upsert.

---

## 5. Daily use

```bash
asr ingest prices --incremental   # just the days you're missing
asr ingest actions --years 1
asr ingest adjust
asr features build
asr quality
asr pack build RELIANCE           # <- the thing you actually read
```

`asr pack build SYMBOL` prints a **research pack**: price summary, computed indicators,
rule-based signals *with the numbers that triggered them*, recent filings quoted verbatim,
and any data-quality caveats. It contains **no interpretation whatsoever** — that's the point.

**Then paste it into Claude** together with [`prompts/analysis.md`](../prompts/analysis.md)
for the qualitative read. That runs on your Claude subscription; there is no API key and no
per-token cost anywhere in this system.

Write packs for the whole universe to disk:
```bash
asr pack build --out packs/
```

---

## 6. Without uv (plain pip)

```bash
python3.12 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 7. With Docker (any platform)

Avoids the Python-version question entirely, and gives you one command for the whole chain:

```bash
docker compose run --rm pipeline
```

Full guide, including scheduling it: **[docker.md](docker.md)**.

## 8. The whole pipeline in one command

Rather than running the seven steps in §4 by hand:

```bash
asr pipeline --full --years 3    # first run
asr pipeline                     # daily: only the days you're missing
```

The stage order is load-bearing — **`adjust` must run before `features`** (or indicators are
computed on prices where a split still looks like a crash), and **`quality` gates the packs**
(the run stops non-zero rather than writing numbers nobody should trust). `asr pipeline`
encodes that order in code, where it can't rot the way a hand-written cron line does.

---

## Troubleshooting

**`pandas-ta` fails to install / `requires Python >=3.12`**
You're on 3.11 or older. Check with `python --version`. `uv venv --python 3.12` will fetch
3.12 for you even if your system Python is older.

**`asr: command not found`**
The virtualenv isn't active. Re-run the activate line from §2. (Or call it directly:
`.venv/bin/asr --help`, or on Windows `.venv\Scripts\asr --help`.)

**NSE requests return 401/403, or hang**
NSE is bot-hostile and rate-limits. The client handles this — it primes session cookies and
retries with backoff — but a heavy backfill from a flaky connection can still trip it. Just
run the command again: cached days are skipped, so it resumes where it stopped.

**`No instruments stored`**
Run `asr ingest instruments` first. Everything keys off the universe.

**A stock's chart looks insane / a 50% crash that never happened**
Run `asr ingest actions` then `asr ingest adjust`, then `asr features build` again. That's an
unadjusted split. If `asr quality` reports `unparsed_action`, NSE described the split in a
format the parser couldn't read — the ratio is deliberately never guessed at, so tell me and
it gets fixed properly.

**Windows: `\` vs `/` in paths**
The CLI takes care of it. If you set `DUCKDB_PATH` by hand in `.env`, forward slashes are
safest: `./data/asr.duckdb`.

---

## A note on the APIs

NSE has changed its file formats before (the bhavcopy layout changed in 2024, and the client
handles *both* formats). It will change again. When a fetch starts failing in a way that
retrying doesn't fix, that's what happened — the fix is a parser update, not a redesign.
The design deliberately keeps every external assumption in one small module per source
(`ingest/bhavcopy.py`, `ingest/corporate_actions.py`, `news/nse.py`) so that when it happens,
there's exactly one place to look.

---

*Research only. This system produces analysis you read, not orders it places. Nothing it
outputs is investment advice.*
