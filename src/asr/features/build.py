"""The feature job: read ``candles`` -> compute indicators -> write ``features``.

Like ingestion, this is idempotent — features are upserted on ``(instrument_key, ts)``,
so re-running after new candles land simply refreshes the affected rows.

Recomputation is deliberately *not* incremental. Indicators like EMA and RSI carry state
from every prior bar, so appending only new rows would produce values that quietly
disagree with a full recompute. We recompute each instrument's whole series; on a
Nifty-500 / 3-year dataset that is seconds of local CPU, which is far cheaper than
debugging a subtly wrong EMA.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import pandas as pd

from ..storage.base import StorageAdapter, get_storage
from .indicators import compute_features
from .schema import MIN_HISTORY


@dataclass
class FeatureReport:
    instruments: int = 0
    rows: int = 0
    thin: list[str] = field(default_factory=list)  # too little history for the slow MAs

    def summary(self) -> str:
        parts = [f"{self.rows} feature rows across {self.instruments} instruments"]
        if self.thin:
            parts.append(f"{len(self.thin)} with <{MIN_HISTORY} bars (slow MAs will be NULL)")
        return ", ".join(parts)


def build_features(
    keys: Iterable[str] | None = None,
    storage: StorageAdapter | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> FeatureReport:
    """Recompute indicators for ``keys`` (default: every instrument that has candles)."""
    storage = storage or get_storage()
    keys = list(keys) if keys is not None else _keys_with_candles(storage)
    report = FeatureReport()

    for key in keys:
        candles = storage.read_sql(
            "SELECT instrument_key, symbol, ts, open, high, low, close, volume "
            f"FROM candles WHERE instrument_key = '{key}' ORDER BY ts"  # noqa: S608
        )
        if candles.empty:
            continue
        if len(candles) < MIN_HISTORY:
            report.thin.append(key)

        feats = compute_features(candles)
        rows = storage.upsert_features(feats)
        report.instruments += 1
        report.rows += rows
        if on_progress:
            on_progress(key, rows)

    return report


def _keys_with_candles(storage: StorageAdapter) -> list[str]:
    df = storage.read_sql("SELECT DISTINCT instrument_key FROM candles ORDER BY 1")
    return df["instrument_key"].tolist()


def latest_features(symbols: Iterable[str] | None = None, storage=None) -> pd.DataFrame:
    """Newest feature row per instrument — the screener's input (Phase 5)."""
    storage = storage or get_storage()
    sql = """
        SELECT f.* FROM features f
        JOIN (
            SELECT instrument_key, MAX(ts) AS ts FROM features GROUP BY 1
        ) m ON f.instrument_key = m.instrument_key AND f.ts = m.ts
    """
    df = storage.read_sql(sql)
    if symbols:
        wanted = {s.strip().upper() for s in symbols}
        df = df[df["symbol"].isin(wanted)]
    return df.sort_values("symbol").reset_index(drop=True)
