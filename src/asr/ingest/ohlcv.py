"""OHLCV ingestors: historical backfill and daily incremental.

Both funnel through :func:`ingest_range`, so there is exactly one code path that talks
to the API and writes candles. Writes are upserts keyed by ``(instrument_key, ts)``,
which makes any run safe to repeat — a re-run of an interrupted backfill costs API
calls, never duplicate rows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from ..storage.base import StorageAdapter, get_storage
from .upstox_client import UpstoxClient, UpstoxError

#: Days of history a first-time backfill pulls when no window is given.
DEFAULT_BACKFILL_YEARS = 3

#: How far back an incremental run looks for an instrument with no stored candles.
COLD_START_DAYS = 400


@dataclass
class IngestReport:
    instruments: int = 0
    rows: int = 0
    skipped: int = 0  # already up to date
    failures: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{self.rows} candles across {self.instruments} instruments"]
        if self.skipped:
            parts.append(f"{self.skipped} already current")
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        return ", ".join(parts)


def _symbol_map(storage: StorageAdapter) -> dict[str, str]:
    try:
        df = storage.read_sql("SELECT instrument_key, symbol FROM instruments")
    except Exception:  # table not created yet — symbol is a convenience column
        return {}
    return dict(zip(df["instrument_key"], df["symbol"], strict=True))


def ingest_range(
    keys: Iterable[str],
    from_date: date,
    to_date: date,
    client: UpstoxClient | None = None,
    storage: StorageAdapter | None = None,
    since: Callable[[str], date | None] | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> IngestReport:
    """Pull daily candles for ``keys`` over a date range and upsert them.

    ``since`` optionally overrides the start date per instrument (that is what makes an
    incremental run incremental); returning ``None`` means "nothing new to fetch".
    """
    client = client or UpstoxClient()
    storage = storage or get_storage()
    symbols = _symbol_map(storage)
    report = IngestReport()

    for key in keys:
        start = since(key) if since else from_date
        if start is None or start > to_date:
            report.skipped += 1
            continue
        try:
            df = client.daily_candles(key, start, to_date)
        except UpstoxError as exc:  # bad instrument/token — record and keep going
            report.failures[key] = str(exc)
            continue
        if df.empty:
            report.skipped += 1
            continue
        df["symbol"] = symbols.get(key, key.split("|")[-1])
        rows = storage.upsert_candles(df)
        report.instruments += 1
        report.rows += rows
        if on_progress:
            on_progress(key, rows)

    return report


def backfill(
    keys: Iterable[str],
    years: int = DEFAULT_BACKFILL_YEARS,
    to_date: date | None = None,
    **kw,
) -> IngestReport:
    """Full history load. Idempotent: safe to re-run after an interruption."""
    to_date = to_date or date.today()
    from_date = to_date - timedelta(days=365 * years)
    return ingest_range(keys, from_date, to_date, **kw)


def daily_incremental(
    keys: Iterable[str],
    to_date: date | None = None,
    storage: StorageAdapter | None = None,
    **kw,
) -> IngestReport:
    """Pull only what each instrument is missing, based on its newest stored candle."""
    to_date = to_date or date.today()
    storage = storage or get_storage()
    latest = storage.latest_candle_ts()
    cold_start = to_date - timedelta(days=COLD_START_DAYS)

    def since(key: str) -> date | None:
        last = latest.get(key)
        if last is None or pd.isna(last):
            return cold_start
        # Re-fetch the last stored day: an in-progress session's candle can be revised,
        # and the upsert makes the overlap free.
        return pd.Timestamp(last).date()

    return ingest_range(keys, cold_start, to_date, storage=storage, since=since, **kw)
