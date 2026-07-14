"""Backtest correctness.

The tests that matter here are the ones that catch a backtest *lying*: lookahead, ignored
costs, and metrics that flatter. A backtest nobody can trust is worse than no backtest — it
launders a hunch into a number.
"""

import numpy as np
import pandas as pd
import pytest

from asr.backtest import metrics as M
from asr.backtest.engine import DEFAULT_COST_BPS, aggregate, run, run_universe
from asr.backtest.strategies import STRATEGIES, rsi_reversion


def frame(closes, **cols) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    df = pd.DataFrame({"close": pd.Series(closes, dtype="float64", index=idx)}, index=idx)
    for name, values in cols.items():
        df[name] = pd.Series(values, dtype="float64", index=idx)
    return df


# --- the shift: the line that makes the number honest -------------------------


def test_a_signal_is_acted_on_the_bar_AFTER_it_appears():
    """The whole game. If the position weren't shifted, the strategy would buy on the very
    day of the rise it 'predicted' — turning hindsight into performance."""
    # Flat, then a 10% jump on the last bar. sma-cross turns on at bar 2.
    df = frame([100, 100, 110], sma_50=[1, 2, 2], sma_200=[2, 1, 1])

    r = run("X", "sma_cross", data=df, cost_bps=0)

    # The signal is true from bar 2 (index 1), so we are only in the market from bar 3.
    assert r.position.tolist() == [0.0, 0.0, 1.0]
    # We captured the 10% move that happened ON bar 3 — not a move we could not have known.
    assert r.metrics.total_return_pct == pytest.approx(10.0)


def test_a_strategy_cannot_capture_a_move_that_precedes_its_signal():
    """A rule that only turns on AFTER the jump must earn nothing from that jump."""
    df = frame([100, 130, 130], sma_50=[1, 1, 2], sma_200=[2, 2, 1])  # signal on the last bar

    r = run("X", "sma_cross", data=df, cost_bps=0)

    assert r.position.tolist() == [0.0, 0.0, 0.0]  # never in the market in-window
    assert r.metrics.total_return_pct == pytest.approx(0.0)
    assert r.benchmark.total_return_pct == pytest.approx(30.0)  # buy&hold got the jump
    assert r.excess_return_pct < 0  # and the rule rightly shows as having missed it


# --- costs --------------------------------------------------------------------


def test_costs_are_charged_on_every_change_of_position():
    df = frame([100, 100, 100], sma_50=[2, 1, 2], sma_200=[1, 2, 1])  # in, out, in

    free = run("X", "sma_cross", data=df, cost_bps=0)
    charged = run("X", "sma_cross", data=df, cost_bps=100)  # 1% a side

    assert free.metrics.total_return_pct == pytest.approx(0.0)  # flat prices, no cost
    assert charged.metrics.total_return_pct < 0  # churn costs money even when price is flat


def test_buy_and_hold_pays_one_entry_cost_not_a_stream_of_them():
    df = frame([100, 110, 121])

    r = run("X", "buy_and_hold", data=df, cost_bps=DEFAULT_COST_BPS)

    # One entry (0 -> 1), no exit. Roughly the 21% move, less a single 25bp entry.
    assert 20.5 < r.metrics.total_return_pct < 21.0
    assert r.metrics.n_trades == 1


# --- metrics ------------------------------------------------------------------


def test_max_drawdown_is_the_worst_peak_to_trough():
    equity = pd.Series([1.0, 1.5, 0.75, 1.2])  # peak 1.5 -> trough 0.75 = -50%
    assert M.max_drawdown(equity) == pytest.approx(-50.0)


def test_no_drawdown_on_a_series_that_only_rises():
    assert M.max_drawdown(pd.Series([1.0, 1.1, 1.2])) == pytest.approx(0.0)


def test_sharpe_of_a_constant_return_series_is_not_infinite():
    """Zero variance must not blow up into inf/NaN and poison every ranking downstream."""
    assert M.sharpe(pd.Series([0.01] * 50)) == 0.0


def test_sharpe_is_annualised_from_daily_returns():
    rng = np.random.default_rng(0)
    daily = pd.Series(rng.normal(0.001, 0.01, 1000))

    expected = daily.mean() / daily.std(ddof=1) * np.sqrt(252)

    assert M.sharpe(daily) == pytest.approx(expected)


def test_cagr_of_a_doubling_over_one_year_is_100pct():
    equity = pd.Series([1.0, 2.0])
    assert M.cagr(equity, bars=252) == pytest.approx(100.0)


def test_a_trade_is_a_run_of_being_in_the_market_not_a_single_bar():
    position = pd.Series([0, 1, 1, 1, 0, 1, 0])  # two distinct trades
    returns = pd.Series([0, 0.01, 0.01, 0.01, 0, -0.05, 0])

    n_trades, win_rate = M.trade_stats(position, returns)

    assert n_trades == 2
    assert win_rate == pytest.approx(50.0)  # one winner, one loser


def test_win_rate_is_none_when_the_rule_never_traded():
    n, win = M.trade_stats(pd.Series([0, 0, 0]), pd.Series([0.1, 0.1, 0.1]))
    assert n == 0
    assert win is None


def test_trade_pnl_compounds_rather_than_summing_daily_returns():
    """+50% then -50% is a LOSS (0.75), not break-even. Summing would call it a win."""
    position = pd.Series([1, 1])
    returns = pd.Series([0.5, -0.5])

    n_trades, win_rate = M.trade_stats(position, returns)

    assert n_trades == 1
    assert win_rate == 0.0


# --- strategies ---------------------------------------------------------------


def test_rsi_reversion_holds_from_oversold_until_overbought():
    df = frame([1, 2, 3, 4, 5], rsi_14=[25, 40, 50, 75, 50])

    pos = rsi_reversion(df)

    # buys at 25 (oversold), holds through the middle, sells when 75 (overbought) prints
    assert pos.tolist() == [1.0, 1.0, 1.0, 0.0, 0.0]


def test_a_strategy_stays_flat_while_its_indicator_is_null():
    """Warmup bars have NULL indicators. Flat is the only honest position there."""
    df = frame([1, 2, 3], sma_50=[np.nan, np.nan, 2.0], sma_200=[np.nan, np.nan, 1.0])

    assert STRATEGIES["sma_cross"](df).tolist() == [0.0, 0.0, 1.0]


def test_unknown_strategy_names_itself():
    with pytest.raises(ValueError, match="Unknown strategy"):
        run("X", "moon_phase", data=frame([1, 2]))


# --- universe -----------------------------------------------------------------


def test_aggregate_reports_the_share_that_beat_buy_and_hold():
    results = pd.DataFrame(
        {
            "symbol": ["A", "B", "C", "D"],
            "return_pct": [10.0, 5.0, 20.0, 1.0],
            "benchmark_pct": [5.0, 10.0, 10.0, 10.0],
            "excess_pct": [5.0, -5.0, 10.0, -9.0],
            "sharpe": [1.0, 0.2, 1.5, 0.1],
            "max_dd_pct": [-10.0, -20.0, -5.0, -30.0],
            "trades": [3, 4, 2, 5],
            "exposure_pct": [50.0, 60.0, 40.0, 70.0],
        }
    )

    agg = aggregate(results)

    assert agg["stocks"] == 4
    assert agg["beat_benchmark_pct"] == 50.0  # 2 of 4 — i.e. the rule told us nothing
    assert agg["median_excess_pct"] == pytest.approx(0.0)


def test_aggregate_of_nothing_is_empty_not_a_crash():
    assert aggregate(pd.DataFrame()) == {}


def test_run_universe_skips_stocks_with_too_little_history(monkeypatch):
    import asr.backtest.engine as eng

    def fake_load(symbol, storage):
        if symbol == "SHORT":
            return frame([100])  # one bar: unjudgeable
        return frame([100, 110], sma_50=[2, 2], sma_200=[1, 1])

    monkeypatch.setattr(eng, "_load", fake_load)

    out = run_universe("sma_cross", symbols=["GOOD", "SHORT"], storage=object())

    assert out["symbol"].tolist() == ["GOOD"]  # SHORT dropped, run did not crash
