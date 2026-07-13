from datetime import date

import httpx
import pandas as pd
import pytest

from asr.ingest.upstox_client import (
    RateLimiter,
    UpstoxClient,
    UpstoxError,
    UpstoxTransientError,
    parse_candles,
    split_windows,
)

# Upstox returns newest-first, IST-offset timestamps, numbers as JSON numbers.
RAW = [
    ["2026-01-02T00:00:00+05:30", 101.0, 105.5, 100.0, 104.0, 12345, 0],
    ["2026-01-01T00:00:00+05:30", 100.0, 102.0, 99.0, 101.0, 9876, 0],
]


def _client(handler, **kw) -> UpstoxClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.upstox.com")
    # burst high + rate high so tests never actually sleep
    return UpstoxClient(client=http, limiter=RateLimiter(rate_per_sec=1e6, burst=1000), **kw)


def test_parse_candles_normalises_types_and_order():
    df = parse_candles(RAW, "NSE_EQ|INE001A01036")

    assert list(df.columns) == [
        "instrument_key",
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "oi",
    ]
    assert df["ts"].is_monotonic_increasing
    assert df["ts"].dt.tz is None  # stored as tz-naive IST
    assert df["ts"].iloc[0] == pd.Timestamp("2026-01-01 00:00:00")
    assert df["close"].dtype == "float64"
    assert df["volume"].dtype == "int64"
    assert (df["instrument_key"] == "NSE_EQ|INE001A01036").all()


def test_parse_candles_empty_still_has_schema():
    df = parse_candles([], "NSE_EQ|X")
    assert df.empty
    assert "close" in df.columns


def test_split_windows_respects_the_range_cap():
    windows = split_windows(date(2023, 1, 1), date(2026, 1, 1), "days")
    assert len(windows) == 4  # 3 years + a day -> four <=365d windows
    assert windows[0].from_date == date(2023, 1, 1)
    assert windows[-1].to_date == date(2026, 1, 1)
    # contiguous, non-overlapping
    for a, b in zip(windows, windows[1:], strict=False):
        assert (b.from_date - a.to_date).days == 1


def test_split_windows_empty_when_inverted():
    assert split_windows(date(2026, 1, 2), date(2026, 1, 1)) == []


def test_historical_candles_stitches_windows():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"candles": RAW}})

    df = _client(handler).historical_candles(
        "NSE_EQ|X", date(2024, 1, 1), date(2026, 1, 1), "days", "1"
    )

    assert len(seen) == 3  # two years + a day -> three requests
    assert "/v3/historical-candle/NSE_EQ|X/days/1/" in seen[0]
    # identical candles across windows must collapse, not duplicate
    assert len(df) == 2
    assert df["ts"].is_unique


def test_4xx_raises_and_does_not_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="UDAPI100050 bad instrument")

    with pytest.raises(UpstoxError):
        _client(handler).daily_candles("NSE_EQ|X", date(2026, 1, 1), date(2026, 1, 2))
    assert len(calls) == 1


def test_429_retries_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"data": {"candles": RAW}})

    df = _client(handler).daily_candles("NSE_EQ|X", date(2026, 1, 1), date(2026, 1, 2))
    assert len(calls) == 2
    assert len(df) == 2


def test_5xx_exhausts_retries_and_raises_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    with pytest.raises(UpstoxTransientError):
        _client(handler).daily_candles("NSE_EQ|X", date(2026, 1, 1), date(2026, 1, 2))


def test_no_token_is_a_clear_error(monkeypatch):
    from asr import config

    monkeypatch.setattr(config.settings, "upstox_access_token", None)
    with pytest.raises(UpstoxError, match="Analytics Token"):
        UpstoxClient()
