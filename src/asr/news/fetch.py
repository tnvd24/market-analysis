"""The news job: pull both feeds for the universe -> ``news`` table.

Idempotent like everything else: rows are upserted on a content id, so re-running a fetch
that overlaps a previous window refreshes rows instead of duplicating them. That matters
more here than for candles — news windows *always* overlap, because you re-fetch "the last
30 days" every day.

Sources are independent. If the Upstox token is missing or the endpoint rejects it, the
NSE filings still land, and the run reports the failure rather than aborting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from ..ingest.upstox_client import UpstoxError
from ..storage.base import StorageAdapter, get_storage
from .nse import NseAnnouncements, NseError
from .schema import SOURCE_NSE, SOURCE_UPSTOX, NewsItem, to_frame
from .upstox_news import MAX_KEYS_PER_REQUEST, UpstoxNews, batches

DEFAULT_LOOKBACK_DAYS = 30


@dataclass
class NewsReport:
    by_source: dict[str, int] = field(default_factory=dict)
    rows: int = 0
    failures: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        got = ", ".join(f"{n} from {s}" for s, n in sorted(self.by_source.items())) or "nothing"
        out = f"{self.rows} new/updated rows ({got})"
        if self.failures:
            out += f", {len(self.failures)} failed"
        return out


def _universe(storage: StorageAdapter, symbols: list[str] | None, limit: int | None):
    df = storage.read_sql("SELECT instrument_key, symbol FROM instruments ORDER BY symbol")
    if symbols:
        wanted = {s.strip().upper() for s in symbols}
        df = df[df["symbol"].isin(wanted)]
    if limit:
        df = df.head(limit)
    return df


def fetch_news(
    symbols: list[str] | None = None,
    limit: int | None = None,
    days: int = DEFAULT_LOOKBACK_DAYS,
    sources: tuple[str, ...] = (SOURCE_NSE, SOURCE_UPSTOX),
    storage: StorageAdapter | None = None,
    nse: NseAnnouncements | None = None,
    upstox: UpstoxNews | None = None,
    on_progress: Callable[[str, int], None] | None = None,
) -> NewsReport:
    storage = storage or get_storage()
    universe = _universe(storage, symbols, limit)
    report = NewsReport()
    if universe.empty:
        return report

    since = date.today() - timedelta(days=days)
    fetched_at = pd.Timestamp.now()
    items: list[NewsItem] = []

    if SOURCE_NSE in sources:
        client = nse or NseAnnouncements()
        for row in universe.itertuples():
            try:
                got = client.fetch(row.symbol, since)
            except (NseError, RuntimeError) as exc:
                report.failures[f"{SOURCE_NSE}:{row.symbol}"] = str(exc)
                continue
            for item in got:
                item.instrument_key = row.instrument_key
            items.extend(got)
            report.by_source[SOURCE_NSE] = report.by_source.get(SOURCE_NSE, 0) + len(got)
            if on_progress:
                on_progress(row.symbol, len(got))

    if SOURCE_UPSTOX in sources:
        symbol_of = dict(zip(universe["instrument_key"], universe["symbol"], strict=True))
        try:
            client = upstox or UpstoxNews()
        except UpstoxError as exc:  # no token — NSE results still stand
            report.failures[SOURCE_UPSTOX] = str(exc)
            client = None
        if client is not None:
            for batch in batches(list(universe["instrument_key"]), MAX_KEYS_PER_REQUEST):
                try:
                    got = client.fetch(batch, symbol_of)
                except UpstoxError as exc:
                    report.failures[f"{SOURCE_UPSTOX}:{batch[0]}..."] = str(exc)
                    continue
                items.extend(got)
                report.by_source[SOURCE_UPSTOX] = report.by_source.get(SOURCE_UPSTOX, 0) + len(got)
                if on_progress:
                    on_progress(f"{len(batch)} keys", len(got))

    df = to_frame(items, fetched_at)
    report.rows = storage.upsert_news(df) if not df.empty else 0
    return report
