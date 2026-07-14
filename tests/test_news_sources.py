from datetime import date

import httpx
import pandas as pd
import pytest

from asr.ingest.upstox_client import UpstoxError
from asr.news.nse import NseAnnouncements, NseTransientError
from asr.news.schema import SOURCE_NSE, SOURCE_UPSTOX, to_frame
from asr.news.upstox_news import MAX_KEYS_PER_REQUEST, UpstoxNews, batches
from asr.ratelimit import RateLimiter

FAST = RateLimiter(rate_per_sec=1e6, burst=1000)

NSE_ROW = {
    "symbol": "RELIANCE",
    "sm_name": "Reliance Industries Limited",
    "desc": "Credit Rating",
    "attchmntText": "CRISIL has reaffirmed its rating at AAA/Stable.",
    "attchmntFile": "https://nsearchives.nseindia.com/corporate/REL_123.pdf",
    "an_dt": "10-Jul-2026 17:46:25",
    "seq_id": "9001",
}

UPSTOX_PAYLOAD = {
    "data": {
        "NSE_EQ|INE002A01018": [
            {
                "heading": "Reliance beats Q1 estimates",
                "summary": "Net profit up 12% year on year.",
                "article_link": "https://news.example.com/ril-q1",
                "published_time": 1752148800000,
                "thumbnail": "https://img.example.com/a.png",
            }
        ]
    },
    "metadata": {"page_number": 1, "page_size": 100, "total_records": 1, "total_pages": 1},
}


def _nse(handler) -> NseAnnouncements:
    c = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return NseAnnouncements(client=c, limiter=FAST)


def _upstox(handler) -> UpstoxNews:
    c = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.upstox.com")
    return UpstoxNews(client=c, limiter=FAST)


# --- NSE ---------------------------------------------------------------------


def test_nse_primes_cookies_before_hitting_the_json_endpoint():
    """Cold-calling the JSON endpoint 403s; the HTML page must be loaded first."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if "api" in request.url.path:
            return httpx.Response(200, json=[NSE_ROW])
        return httpx.Response(200, text="<html>cookie jar</html>")

    _nse(handler).fetch("RELIANCE", date(2026, 6, 1))

    assert seen[0] == "/companies-listing/corporate-filings-announcements"
    assert seen[1] == "/api/corporate-announcements"


def test_nse_parses_a_filing_into_the_common_schema():
    items = _nse(lambda r: httpx.Response(200, json=[NSE_ROW])).fetch("RELIANCE", date(2026, 6, 1))

    item = items[0]
    assert item.source == SOURCE_NSE
    assert item.symbol == "RELIANCE"
    assert item.category == "Credit Rating"  # the filing type
    assert item.summary.startswith("CRISIL")  # its substance
    assert item.url.endswith(".pdf")
    # "10-Jul-2026 17:46:25" is IST, with no tz marker on it
    assert item.published_at == pd.Timestamp("2026-07-10 17:46:25")
    assert item.published_at.tzinfo is None


def test_nse_sends_the_date_range_in_the_format_it_expects():
    seen = {}

    def handler(request):
        if "api" in request.url.path:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json=[])
        return httpx.Response(200, text="ok")

    _nse(handler).fetch("tcs", date(2026, 6, 1), date(2026, 7, 14))

    assert seen["symbol"] == "TCS"  # normalised
    assert seen["from_date"] == "01-06-2026"  # dd-mm-yyyy, not ISO
    assert seen["to_date"] == "14-07-2026"


def test_nse_403_forces_a_re_prime_then_succeeds():
    """Cookies go stale. A retry must start from a fresh session, not reuse the dead one."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if "api" not in request.url.path:
            return httpx.Response(200, text="ok")
        api_hits = [c for c in calls if "api" in c]
        if len(api_hits) == 1:
            return httpx.Response(403, text="blocked")
        return httpx.Response(200, json=[NSE_ROW])

    items = _nse(handler).fetch("RELIANCE", date(2026, 6, 1))

    assert len(items) == 1
    assert calls.count("/companies-listing/corporate-filings-announcements") == 2  # re-primed


def test_nse_html_block_page_with_a_200_is_treated_as_transient():
    def handler(request):
        if "api" in request.url.path:
            return httpx.Response(200, text="<html>Access Denied</html>")
        return httpx.Response(200, text="ok")

    with pytest.raises(NseTransientError, match="non-JSON"):
        _nse(handler).fetch("RELIANCE", date(2026, 6, 1))


def test_nse_row_with_an_unparseable_date_is_skipped_not_crashed():
    bad = {**NSE_ROW, "an_dt": "not a date"}

    def handler(request):
        if "api" in request.url.path:
            return httpx.Response(200, json=[bad, NSE_ROW])
        return httpx.Response(200, text="ok")

    assert len(_nse(handler).fetch("RELIANCE", date(2026, 6, 1))) == 1


def test_nse_filing_with_no_desc_falls_back_to_the_attachment_text():
    row = {**NSE_ROW, "desc": ""}

    def handler(request):
        if "api" in request.url.path:
            return httpx.Response(200, json=[row])
        return httpx.Response(200, text="ok")

    item = _nse(handler).fetch("RELIANCE", date(2026, 6, 1))[0]
    assert item.headline.startswith("CRISIL")
    assert item.category is None


# --- Upstox news --------------------------------------------------------------


def test_upstox_news_parses_and_converts_epoch_ms_to_ist():
    items = _upstox(lambda r: httpx.Response(200, json=UPSTOX_PAYLOAD)).fetch(
        ["NSE_EQ|INE002A01018"], {"NSE_EQ|INE002A01018": "RELIANCE"}
    )

    item = items[0]
    assert item.source == SOURCE_UPSTOX
    assert item.symbol == "RELIANCE"
    assert item.headline == "Reliance beats Q1 estimates"
    assert item.url == "https://news.example.com/ril-q1"
    # 1752148800000 ms = 2025-07-10 12:00 UTC -> 17:30 IST (+05:30)
    assert item.published_at == pd.Timestamp("2025-07-10 17:30:00")
    assert item.published_at.tzinfo is None


def test_upstox_news_refuses_more_keys_than_the_api_allows():
    client = _upstox(lambda r: httpx.Response(200, json=UPSTOX_PAYLOAD))
    with pytest.raises(ValueError, match="30-key"):
        client.fetch([f"NSE_EQ|K{i}" for i in range(31)], {})


def test_batches_splits_the_universe_to_the_api_limit():
    keys = [f"NSE_EQ|K{i}" for i in range(70)]
    chunks = list(batches(keys))
    assert [len(c) for c in chunks] == [30, 30, 10]
    assert all(len(c) <= MAX_KEYS_PER_REQUEST for c in chunks)


def test_upstox_news_follows_pagination():
    pages = []

    def handler(request):
        page = int(request.url.params["page_number"])
        pages.append(page)
        art = {
            "heading": f"story {page}",
            "summary": "s",
            "article_link": f"https://n/{page}",
            "published_time": 1752148800000,
        }
        return httpx.Response(
            200,
            json={"data": {"NSE_EQ|A": [art]}, "metadata": {"total_pages": 3}},
        )

    items = _upstox(handler).fetch(["NSE_EQ|A"], {"NSE_EQ|A": "AAA"})

    assert pages == [1, 2, 3]
    assert len(items) == 3


def test_upstox_news_without_a_token_is_a_clear_error(monkeypatch):
    from asr import config

    monkeypatch.setattr(config.settings, "upstox_access_token", None)
    with pytest.raises(UpstoxError, match="Analytics Token"):
        UpstoxNews()


# --- dedup -------------------------------------------------------------------


def test_the_same_filing_twice_collapses_to_one_row():
    """News windows always overlap — you re-fetch 'the last 30 days' every day."""
    items = _nse(lambda r: httpx.Response(200, json=[NSE_ROW, NSE_ROW])).fetch(
        "RELIANCE", date(2026, 6, 1)
    )
    df = to_frame(items)
    assert len(df) == 1


def test_ids_are_stable_across_fetches_but_distinct_across_items():
    first = _nse(lambda r: httpx.Response(200, json=[NSE_ROW])).fetch("RELIANCE", date(2026, 6, 1))
    again = _nse(lambda r: httpx.Response(200, json=[NSE_ROW])).fetch("RELIANCE", date(2026, 6, 1))
    other = _nse(
        lambda r: httpx.Response(200, json=[{**NSE_ROW, "seq_id": "9002", "desc": "Board Meeting"}])
    ).fetch("RELIANCE", date(2026, 6, 1))

    assert first[0].id == again[0].id  # re-fetch must not create a new row
    assert first[0].id != other[0].id


def test_the_same_headline_on_two_stocks_stays_two_rows():
    """A wire story about a sector names several companies; each stock keeps its own row."""
    payload = {
        "data": {
            "NSE_EQ|A": [
                {"heading": "Banks rally", "article_link": "https://n/1", "published_time": 1}
            ],
            "NSE_EQ|B": [
                {"heading": "Banks rally", "article_link": "https://n/1", "published_time": 1}
            ],
        },
        "metadata": {"total_pages": 1},
    }
    items = _upstox(lambda r: httpx.Response(200, json=payload)).fetch(
        ["NSE_EQ|A", "NSE_EQ|B"], {"NSE_EQ|A": "AAA", "NSE_EQ|B": "BBB"}
    )
    assert len(to_frame(items)) == 2
