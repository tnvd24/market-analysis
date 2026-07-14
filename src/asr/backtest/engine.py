"""The backtest engine: measure a rule instead of believing it.

A backtest's job is to be *hard to fool*. Three things do almost all the fooling, and each is
handled explicitly here rather than left to a library default:

**1. Lookahead.** A signal computed from a bar's close cannot be traded at that same close —
you did not know it until the bar ended. `run()` shifts every position forward one bar,
always, and no strategy is trusted to do it for itself. Without that shift most rules look
brilliant, because they are quietly buying the day *before* the rise they detected.

**2. Costs.** Zero-cost backtests make high-turnover rules look free. Every change in position
pays `cost_bps` (brokerage + STT + fees + slippage, one way). At ~25 bps a side, a rule that
flips weekly cannot outrun its own friction — and that is a finding, not a nuisance.

**3. Survivorship bias — the one we cannot fix.** The universe is *today's* Nifty 500: a list
of companies successful enough to still be in it. Every "buy the dip" rule looks better on
survivors, because the firms whose dips never recovered are precisely the ones that fell out
of the index and out of this data. **Every result carries the warning.** It is not a caveat to
bury; it is the reason a good number here is not a good number in the market.

Long-only, one stock at a time, fully invested or flat. No shorting, no leverage, no position
sizing — those add ways to be wrong faster than they add realism.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..ingest.adjust import adjusted
from ..storage.base import StorageAdapter, get_storage
from . import metrics as M
from .strategies import STRATEGIES

#: One-way cost in basis points: brokerage + STT + exchange fees + slippage.
#: 25 bps a side (0.25%) is a realistic Indian retail round trip of ~0.5%.
DEFAULT_COST_BPS = 25.0

SURVIVORSHIP_WARNING = (
    "Survivorship bias: the universe is TODAY's Nifty 500, so every stock tested is one that "
    "survived to still be in the index. Real results would be worse — the companies whose "
    "declines never recovered are exactly the ones missing from this data."
)


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    metrics: M.Metrics
    benchmark: M.Metrics  # buy & hold over the same bars
    equity: pd.Series = field(repr=False)
    position: pd.Series = field(repr=False)
    cost_bps: float = DEFAULT_COST_BPS

    @property
    def excess_return_pct(self) -> float:
        """The only number that matters: did the rule beat simply owning the stock?"""
        return self.metrics.total_return_pct - self.benchmark.total_return_pct

    def summary(self) -> str:
        m = self.metrics
        return (
            f"{self.symbol} / {self.strategy}: {m.total_return_pct:+.1f}% "
            f"(buy&hold {self.benchmark.total_return_pct:+.1f}%, "
            f"excess {self.excess_return_pct:+.1f}%) · "
            f"Sharpe {m.sharpe:.2f} · maxDD {m.max_drawdown_pct:.1f}% · "
            f"{m.n_trades} trades · {m.exposure_pct:.0f}% exposed"
        )


def _load(symbol: str, storage: StorageAdapter) -> pd.DataFrame:
    """Adjusted prices joined to their indicators, one row per bar."""
    candles = storage.read_sql(
        "SELECT instrument_key, ts, open, high, low, close, volume, adj_factor "
        f"FROM candles WHERE symbol = '{symbol}' ORDER BY ts"  # noqa: S608
    )
    if candles.empty:
        raise LookupError(f"No candles for {symbol}. Run `asr ingest prices`.")
    candles = adjusted(candles)  # a split left raw would fabricate a 50% loss

    feats = storage.read_sql(
        f"SELECT * FROM features WHERE symbol = '{symbol}' ORDER BY ts"  # noqa: S608
    )
    if feats.empty:
        raise LookupError(f"No features for {symbol}. Run `asr features build`.")

    df = candles.merge(feats.drop(columns=["instrument_key", "symbol"]), on="ts", how="inner")
    return df.set_index("ts").sort_index()


def run(
    symbol: str,
    strategy: str = "sma_cross",
    since: str | None = None,
    until: str | None = None,
    cost_bps: float = DEFAULT_COST_BPS,
    storage: StorageAdapter | None = None,
    data: pd.DataFrame | None = None,
) -> BacktestResult:
    """Backtest one rule on one stock."""
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}. Known: {', '.join(STRATEGIES)}")

    df = data if data is not None else _load(symbol, storage or get_storage())
    if since:
        df = df[df.index >= pd.Timestamp(since)]
    if until:
        df = df[df.index <= pd.Timestamp(until)]
    if len(df) < 2:
        raise LookupError(f"Not enough bars for {symbol} in that window.")

    wanted = STRATEGIES[strategy](df).fillna(0.0)

    # THE SHIFT. A signal from today's close is acted on tomorrow. Everything else in this
    # file is bookkeeping; this line is what makes the number honest.
    position = wanted.shift(1).fillna(0.0)

    returns = df["close"].pct_change().fillna(0.0)
    turnover = position.diff().abs().fillna(position.abs())
    costs = turnover * (cost_bps / 10_000.0)

    strat_returns = position * returns - costs
    equity = (1.0 + strat_returns).cumprod()

    hold = pd.Series(1.0, index=df.index)
    hold_equity = (1.0 + hold * returns).cumprod()

    return BacktestResult(
        symbol=symbol,
        strategy=strategy,
        metrics=M.compute(equity, strat_returns, position),
        benchmark=M.compute(hold_equity, returns, hold),
        equity=equity,
        position=position,
        cost_bps=cost_bps,
    )


def run_universe(
    strategy: str = "sma_cross",
    symbols: list[str] | None = None,
    limit: int | None = None,
    storage: StorageAdapter | None = None,
    **kw,
) -> pd.DataFrame:
    """The same rule across many stocks.

    One stock beating buy-and-hold is noise. The question is whether a rule holds up across
    the universe, so this returns a row per stock and lets the aggregate speak.
    """
    storage = storage or get_storage()
    if symbols is None:
        symbols = storage.read_sql("SELECT DISTINCT symbol FROM features ORDER BY symbol")[
            "symbol"
        ].tolist()
    if limit:
        symbols = symbols[:limit]

    rows = []
    for sym in symbols:
        try:
            r = run(sym, strategy, storage=storage, **kw)
        except LookupError:
            continue  # too little history to judge; not a failure worth shouting about
        rows.append(
            {
                "symbol": sym,
                "return_pct": r.metrics.total_return_pct,
                "benchmark_pct": r.benchmark.total_return_pct,
                "excess_pct": r.excess_return_pct,
                "sharpe": r.metrics.sharpe,
                "max_dd_pct": r.metrics.max_drawdown_pct,
                "trades": r.metrics.n_trades,
                "exposure_pct": r.metrics.exposure_pct,
            }
        )
    return pd.DataFrame(rows)


def aggregate(results: pd.DataFrame) -> dict:
    """The universe-level verdict on a rule.

    The headline is `beat_benchmark_pct`: on what share of stocks did the rule actually beat
    just owning them? A rule that wins on half the universe has told you nothing.
    """
    if results.empty:
        return {}
    return {
        "stocks": int(len(results)),
        "median_return_pct": round(float(results["return_pct"].median()), 2),
        "median_benchmark_pct": round(float(results["benchmark_pct"].median()), 2),
        "median_excess_pct": round(float(results["excess_pct"].median()), 2),
        "beat_benchmark_pct": round(float((results["excess_pct"] > 0).mean() * 100), 1),
        "median_sharpe": round(float(results["sharpe"].median()), 2),
        "median_max_dd_pct": round(float(results["max_dd_pct"].median()), 2),
        "median_trades": int(results["trades"].median()),
        "median_exposure_pct": round(float(results["exposure_pct"].median()), 1),
    }
