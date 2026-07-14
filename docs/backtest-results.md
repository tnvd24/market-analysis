# Backtest results — do the signals actually work?

**Short answer: no. Not as mechanical trading rules.**

Every rule in `features/signals.py`, tested on **500 stocks over 3 years** of split-adjusted
NSE data (2023-07-17 → 2026-07-14), with realistic costs (25 bps a side), signals acted on
the **bar after** they appear.

| strategy | median return | buy & hold | excess | **beat b&h** | Sharpe | max DD | trades | exposure |
|---|---|---|---|---|---|---|---|---|
| **buy_and_hold** | **+53.6%** | +54.0% | −0.4% | — | **0.60** | −41.3% | 1 | 100% |
| rsi_reversion | +18.8% | +54.0% | −25.8% | 33% | 0.41 | −28.7% | 3 | 41% |
| sma_cross | −0.4% | +54.0% | −46.2% | 15% | 0.01 | −31.1% | 2 | 39% |
| trend_200 | −1.9% | +54.0% | −49.7% | 16% | 0.00 | −32.5% | 9 | 38% |
| macd_cross | −3.7% | +54.0% | −53.1% | 14% | 0.07 | −38.5% | 29 | 47% |

*(`buy_and_hold`'s −0.4% "excess" is its single entry cost against a zero-cost benchmark —
a sanity check that the cost model is wired up.)*

## What this means

**Not one rule beat simply owning the stock.** The best of them (`rsi_reversion`) beat
buy-and-hold on **33% of stocks** — worse than a coin flip. `macd_cross` managed 14%.

The mechanism is visible in the table: these rules are **in the market ~40% of the time**, and
the market rose ~54% over the window. **Being out of a rising market is the whole cost.** No
amount of clever entry timing recovers the 60% of days you sat in cash. `macd_cross` compounds
that with 29 round trips, each paying 50 bps.

**They are not worthless, though — read the risk columns.** Every rule cut the maximum
drawdown (−29% to −38%, vs **−41%** for buy-and-hold) while holding only ~40% exposure. They
sidestep some pain. They just don't earn their keep doing it: `rsi_reversion` gets a 0.41
Sharpe for 41% exposure, where buy-and-hold gets 0.60 for being fully invested.

## The honest caveats

**This is one regime.** Three years, and a strongly rising one (median stock +54%). Trend and
reversion rules are *supposed* to lag in a bull market; the question they exist to answer is
what happens in a 2008 or a 2020, and this window contains neither. **A rule that loses in a
bull market has not been disproved — it has been shown to cost something.** The right
conclusion is "this is what it costs," not "technical analysis is bunk."

**Survivorship bias makes these numbers flattering, not harsh.** The universe is *today's*
Nifty 500 — every stock tested survived to still be in the index. Buy-and-hold looks
especially good under that bias, because the companies whose declines never recovered are
precisely the ones missing from the data. In reality the benchmark would be lower... and so
would the rules that hold those same stocks.

**No parameter tuning was done, deliberately.** RSI 30/70, MA 50/200, MACD 12/26/9 are the
textbook defaults. Searching for the settings that *would* have won these three years is how
you manufacture a backtest that works perfectly right up to the day you fund it.

## Why this changes nothing about the design — and validates it

The research pack presents signals as **observations, never recommendations**: "the 50-day
crossed the 200-day, here are the numbers." It has never told you to buy anything.

This backtest is *why* that boundary matters. Had the pack said "golden cross → BUY", it would
have been confidently wrong on 85% of stocks. Instead the signal is one input a human weighs
against news, filings, and context — and now it comes with a measured price tag.

**That is the point of Phase 6.** A signal you have never backtested is a story. A signal you
have backtested is a fact with a known cost — including, as here, the fact that trading it
mechanically would have lost you money.

---

## Reproduce it

```bash
asr backtest run RELIANCE --strategy sma_cross      # one stock
asr backtest universe --strategy rsi_reversion      # all 500
asr backtest strategies                             # what's available
```

Costs are adjustable (`--cost_bps 0` to see how much friction alone explains — it is not
most of it).

*Research only. Not investment advice. Past performance of a rule on survivor data says
little about its future.*
