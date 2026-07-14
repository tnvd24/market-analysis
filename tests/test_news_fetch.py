from datetime import date

import pandas as pd
import pytest

from asr.ingest.upstox_client import UpstoxError
from asr.news.fetch import fetch_news
from asr.news.nse import NseError
from asr.news.schema import SOURCE_NSE, SOURCE_UPSTOX, NewsItem
from asr.storage.duckdb_adapter import DuckDBAdapter


def _item(symbol="AAA", source=SOURCE_NSE, ext="1", headline="Board Meeting"):
    return NewsItem(
        symbol=symbol,
        source=source,
        published_at=pd.Timestamp("2026-07-10 10:00"),
        category="Board Meeting",
        headline=headline,
        summary="body",
        url=f"https://x/{ext}",
        external_id=ext,
    )


class FakeNse:
    def __init__(self, items=None, fail: set[str] | None = None):
        self.items = items if items is not None else [_item()]
        self.fail = fail or set()
        self.calls: list[str] = []

    def fetch(self, symbol, since, until=None):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise NseError(f"NSE rejected {symbol}")
        return [_item(symbol=symbol, ext=f"{symbol}-1") for _ in self.items]


class FakeUpstox:
    def __init__(self, fail=False):
        self.fail = fail
        self.batches: list[list[str]] = []

    def fetch(self, keys, symbols):
        self.batches.append(list(keys))
        if self.fail:
            raise UpstoxError("401 unauthorized")
        return [
            _item(symbol=symbols[k], source=SOURCE_UPSTOX, ext=f"news-{k}", headline="Wire story")
            for k in keys
        ]


@pytest.fixture
def storage(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    db.upsert_instruments(
        pd.DataFrame(
            [
                {"instrument_key": "NSE_EQ|A", "symbol": "AAA", "isin": "INE_A", "name": "A Ltd"},
                {"instrument_key": "NSE_EQ|B", "symbol": "BBB", "isin": "INE_B", "name": "B Ltd"},
            ]
        )
    )
    return db


def test_fetch_stores_both_sources(storage):
    report = fetch_news(storage=storage, nse=FakeNse(), upstox=FakeUpstox())

    assert report.by_source[SOURCE_NSE] == 2  # one per instrument
    assert report.by_source[SOURCE_UPSTOX] == 2
    stored = storage.read_sql("SELECT source, COUNT(*) AS n FROM news GROUP BY 1")
    assert set(stored["source"]) == {SOURCE_NSE, SOURCE_UPSTOX}


def test_refetching_an_overlapping_window_does_not_duplicate(storage):
    """The everyday case: 'last 30 days' re-pulled daily, mostly the same filings."""
    fetch_news(storage=storage, nse=FakeNse(), upstox=FakeUpstox())
    fetch_news(storage=storage, nse=FakeNse(), upstox=FakeUpstox())

    assert storage.read_sql("SELECT COUNT(*) AS n FROM news").iloc[0]["n"] == 4


def test_filings_carry_the_instrument_key_from_the_universe(storage):
    fetch_news(storage=storage, sources=(SOURCE_NSE,), nse=FakeNse())

    rows = storage.read_sql("SELECT symbol, instrument_key FROM news ORDER BY symbol")
    assert rows["instrument_key"].tolist() == ["NSE_EQ|A", "NSE_EQ|B"]


def test_a_missing_upstox_token_does_not_lose_the_nse_filings(storage):
    """The two sources are independent; one failing must not sink the run."""
    report = fetch_news(storage=storage, nse=FakeNse(), upstox=FakeUpstox(fail=True))

    assert report.by_source[SOURCE_NSE] == 2
    assert any("401" in e for e in report.failures.values())
    assert storage.read_sql("SELECT COUNT(*) AS n FROM news").iloc[0]["n"] == 2


def test_one_bad_symbol_does_not_abort_the_run(storage):
    nse = FakeNse(fail={"AAA"})
    report = fetch_news(storage=storage, sources=(SOURCE_NSE,), nse=nse)

    assert f"{SOURCE_NSE}:AAA" in report.failures
    assert report.by_source[SOURCE_NSE] == 1  # BBB still fetched


def test_symbol_filter_restricts_the_universe(storage):
    nse = FakeNse()
    fetch_news(storage=storage, symbols=["BBB"], sources=(SOURCE_NSE,), nse=nse)

    assert nse.calls == ["BBB"]


def test_upstox_is_called_in_batches_within_the_api_limit(storage):
    up = FakeUpstox()
    fetch_news(storage=storage, sources=(SOURCE_UPSTOX,), upstox=up)

    assert up.batches == [["NSE_EQ|A", "NSE_EQ|B"]]  # one batch, both keys
    assert all(len(b) <= 30 for b in up.batches)


def test_lookback_window_is_passed_through(storage):
    class Recording(FakeNse):
        def fetch(self, symbol, since, until=None):
            self.since = since
            return []

    nse = Recording()
    fetch_news(storage=storage, days=7, sources=(SOURCE_NSE,), nse=nse)

    assert (date.today() - nse.since).days == 7
