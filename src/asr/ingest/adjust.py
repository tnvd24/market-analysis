"""Apply corporate actions to raw prices.

Raw bhavcopy prices are what actually traded. To compare a price across a split you must
restate the earlier ones in today's terms, or every window spanning the ex-date is nonsense.

The model, in one line: **``adj_factor(d)`` is the product of the ratios of every split and
bonus whose ex-date is after ``d``.**

    adjusted_price(d) = raw_price(d) / adj_factor(d)
    adjusted_volume(d) = raw_volume(d) * adj_factor(d)

So for a 1:2 split (factor 2), every price before the ex-date is halved — the fake 50%
"crash" disappears, and the series becomes continuous. After the last action the factor is
1.0, meaning recent prices are untouched and still equal what the market actually quoted.

**Raw prices are never overwritten.** The factor is stored alongside them, so the adjustment
is reversible, auditable, and recomputed from scratch whenever a new action lands — which
matters, because a split announced tomorrow changes the correct factor for every bar before
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..storage.base import StorageAdapter, get_storage

#: Corporate actions that mechanically restate the share count (and so the price).
ADJUSTING_TYPES = ("split", "bonus")


@dataclass
class AdjustReport:
    instruments: int = 0
    actions_applied: int = 0
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.actions_applied} actions applied across {self.instruments} instruments"]
        if self.needs_review:
            parts.append(f"{len(self.needs_review)} need review (unparsed ratio)")
        return ", ".join(parts)


def factors_for(candle_ts: pd.Series, actions: pd.DataFrame) -> pd.Series:
    """The cumulative adjustment factor for each candle timestamp.

    ``actions`` needs ``ex_date`` and ``factor``, already filtered to adjusting types with a
    known ratio. An action with an unknown ratio must never reach here — it would silently
    behave as 1.0, which is precisely the quiet wrongness we are trying to eliminate.
    """
    factor = pd.Series(1.0, index=candle_ts.index)
    for action in actions.itertuples():
        if pd.isna(action.factor) or action.factor <= 0:
            continue
        # Bars strictly before the ex-date are quoted in pre-split terms.
        factor *= pd.Series(
            [action.factor if ts < action.ex_date else 1.0 for ts in candle_ts],
            index=candle_ts.index,
        )
    return factor


def apply_adjustments(storage: StorageAdapter | None = None) -> AdjustReport:
    """Recompute ``adj_factor`` for every instrument from the stored corporate actions."""
    storage = storage or get_storage()
    report = AdjustReport()

    actions = storage.read_sql(
        "SELECT symbol, ex_date, action_type, factor, needs_review, subject "
        "FROM corporate_actions ORDER BY ex_date"
    )
    report.needs_review = [
        f"{r.symbol} {pd.Timestamp(r.ex_date).date()}: {r.subject}"
        for r in actions.itertuples()
        if bool(r.needs_review)
    ]

    usable = actions[
        actions["action_type"].isin(ADJUSTING_TYPES)
        & actions["factor"].notna()
        & (actions["factor"] > 0)
    ]

    # Every instrument is reset to 1.0 first: an action can be revoked or restated, and a
    # stale factor would keep silently distorting the series.
    storage.reset_adj_factors()
    if usable.empty:
        return report

    for symbol, group in usable.groupby("symbol"):
        candles = storage.read_sql(
            f"SELECT instrument_key, ts FROM candles WHERE symbol = '{symbol}' ORDER BY ts"  # noqa: S608
        )
        if candles.empty:
            continue
        candles["adj_factor"] = factors_for(candles["ts"], group)
        changed = candles[candles["adj_factor"] != 1.0]
        if changed.empty:
            continue
        storage.update_adj_factors(candles[["instrument_key", "ts", "adj_factor"]])
        report.instruments += 1
        report.actions_applied += len(group)

    return report


def adjusted(candles: pd.DataFrame) -> pd.DataFrame:
    """Restate raw OHLCV in today's share terms. Callers compute indicators on this."""
    if candles.empty or "adj_factor" not in candles:
        return candles
    df = candles.copy()
    factor = df["adj_factor"].fillna(1.0).replace(0, 1.0)
    for col in ("open", "high", "low", "close"):
        if col in df:
            df[col] = df[col] / factor
    if "volume" in df:
        df["volume"] = (df["volume"] * factor).round().astype("int64")
    return df
