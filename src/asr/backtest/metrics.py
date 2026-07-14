"""Performance metrics.

Deliberately plain arithmetic over a return series — no library, so there is nothing to
misconfigure and every number can be checked by hand. Each one is defined here rather than
assumed, because the same word means different things in different places (a "Sharpe" with
the wrong annualisation factor is off by 4x and still looks plausible).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

#: NSE trades ~250 days a year. Used to annualise daily figures.
TRADING_DAYS = 252


@dataclass
class Metrics:
    total_return_pct: float
    cagr_pct: float
    ann_volatility_pct: float
    sharpe: float
    max_drawdown_pct: float
    exposure_pct: float  # share of days actually in the market
    n_trades: int
    win_rate_pct: float | None  # None when there are no closed trades to judge
    bars: int

    def to_dict(self) -> dict:
        return asdict(self)


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough fall, as a negative percentage."""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min() * 100)


def sharpe(returns: pd.Series, periods: int = TRADING_DAYS) -> float:
    """Annualised Sharpe, risk-free = 0.

    Zero risk-free is a simplification worth stating: with Indian rates near 6-7%, this
    flatters every strategy equally, so it stays useful for *ranking* while overstating the
    absolute number.
    """
    r = returns.dropna()
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / r.std(ddof=1) * np.sqrt(periods))


def cagr(equity: pd.Series, bars: int, periods: int = TRADING_DAYS) -> float:
    if equity.empty or bars <= 0 or equity.iloc[-1] <= 0:
        return 0.0
    years = bars / periods
    if years <= 0:
        return 0.0
    return float((equity.iloc[-1] ** (1 / years) - 1) * 100)


def trade_stats(position: pd.Series, returns: pd.Series) -> tuple[int, float | None]:
    """Count round-trip trades and the share that made money.

    A trade is an unbroken run of being in the market. Its P&L is the compounded return over
    that run — not the sum of daily returns, which would quietly misstate it.
    """
    pos = position.fillna(0)
    in_market = pos > 0
    if not in_market.any():
        return 0, None

    # Each contiguous block of in-market bars is one trade.
    block = (in_market != in_market.shift()).cumsum()
    wins = 0
    trades = 0
    for _, grp in returns[in_market].groupby(block[in_market]):
        trades += 1
        if (1 + grp).prod() - 1 > 0:
            wins += 1

    return trades, (wins / trades * 100 if trades else None)


def compute(equity: pd.Series, strat_returns: pd.Series, position: pd.Series) -> Metrics:
    bars = len(strat_returns.dropna())
    n_trades, win_rate = trade_stats(position, strat_returns)
    return Metrics(
        total_return_pct=float((equity.iloc[-1] - 1) * 100) if not equity.empty else 0.0,
        cagr_pct=cagr(equity, bars),
        ann_volatility_pct=float(strat_returns.std(ddof=1) * np.sqrt(TRADING_DAYS) * 100)
        if bars > 1
        else 0.0,
        sharpe=sharpe(strat_returns),
        max_drawdown_pct=max_drawdown(equity),
        exposure_pct=float((position.fillna(0) > 0).mean() * 100) if len(position) else 0.0,
        n_trades=n_trades,
        win_rate_pct=win_rate,
        bars=bars,
    )
