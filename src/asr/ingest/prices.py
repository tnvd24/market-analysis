"""Price ingestion from NSE bhavcopy: backfill and daily incremental.

One request per trading day gives the whole market, so we filter each day's file down to
the universe and upsert. That inverts the usual shape — we iterate over *days*, not stocks —
which is why a 3-year backfill of 500 stocks costs ~750 requests instead of ~1,500.

Idempotent, like everything else: candles upsert on ``(instrument_key, ts)``, so an
interrupted backfill is safe to re-run. Downloaded days are cached on disk, so a re-run
costs no network at all.

Holidays need no calendar: a missing file *is* the holiday.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from ..storage.base import StorageAdapter, get_storage
from .bhavcopy import Bhavcopy, NoTradingDay, trading_days

DEFAULT_BACKFILL_YEARS = 3


@dataclass
class PriceReport:
    days: int = 0
    holidays: int = 0
    rows: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{self.rows:,} candles from {self.days} trading days"]
        if self.holidays:
            parts.append(f"{self.holidays} non-trading days skipped")
        if self.failures:
            parts.append(f"{len(self.failures)} days failed")
        return ", ".join(parts)


def _universe(storage: StorageAdapter) -> pd.DataFrame:
    df = storage.read_sql("SELECT instrument_key, symbol, isin FROM instruments")
    if df.empty:
        raise LookupError("No instruments stored. Run `asr ingest instruments` first.")
    return df


def _to_candles(day_df: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    """Filter a whole-market day down to our universe and attach instrument keys.

    Joined on ISIN where the file provides it (stable across ticker renames), falling back
    to symbol for the older bhavcopy format, which carries no ISIN.
    """
    by_isin = universe.dropna(subset=["isin"]).set_index("isin")["instrument_key"]
    by_symbol = universe.set_index("symbol")["instrument_key"]

    df = day_df.copy()
    df["instrument_key"] = df["isin"].map(by_isin) if "isin" in df else None
    missing = df["instrument_key"].isna()
    df.loc[missing, "instrument_key"] = df.loc[missing, "symbol"].map(by_symbol)

    df = df.dropna(subset=["instrument_key"])
    df["oi"] = 0
    return df[["instrument_key", "symbol", "ts", "open", "high", "low", "close", "volume", "oi"]]


def ingest_prices(
    since: date,
    until: date | None = None,
    storage: StorageAdapter | None = None,
    client: Bhavcopy | None = None,
    on_progress: Callable[[date, int], None] | None = None,
) -> PriceReport:
    storage = storage or get_storage()
    client = client or Bhavcopy()
    universe = _universe(storage)
    until = until or date.today()
    report = PriceReport()

    for day in trading_days(since, until):
        try:
            day_df = client.fetch_day(day)
        except NoTradingDay:
            report.holidays += 1
            continue
        except Exception as exc:  # one bad day must not sink a 3-year backfill
            report.failures[day.isoformat()] = str(exc)
            continue

        candles = _to_candles(day_df, universe)
        if candles.empty:
            report.holidays += 1
            continue

        report.rows += storage.upsert_candles(candles)
        report.days += 1
        if on_progress:
            on_progress(day, len(candles))

    return report


def backfill(years: int = DEFAULT_BACKFILL_YEARS, **kw) -> PriceReport:
    until = date.today()
    return ingest_prices(until - timedelta(days=365 * years), until, **kw)


def daily(storage: StorageAdapter | None = None, **kw) -> PriceReport:
    """Pull only the days missing since the newest stored candle."""
    storage = storage or get_storage()
    row = storage.read_sql("SELECT MAX(ts) AS last_ts FROM candles").iloc[0]
    if pd.isna(row["last_ts"]):
        return backfill(storage=storage, **kw)
    # Start the day after the last stored one; re-fetching it would be free but pointless.
    since = pd.Timestamp(row["last_ts"]).date() + timedelta(days=1)
    return ingest_prices(since, date.today(), storage=storage, **kw)
