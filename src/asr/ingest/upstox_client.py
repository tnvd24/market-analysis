"""Upstox market-data client (research-only).

Auth: the Analytics Token (1-year, read-only) from ``settings.upstox_access_token``.
No order-placement methods live here on purpose — this system never trades.

Uses the **v3** historical-candle API:
    GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}
Candle shape: ``[ts, open, high, low, close, volume, oi]`` with an IST-offset ISO
timestamp. We normalise to tz-naive IST before storing, so every timestamp in the
warehouse sits on one clock.

Upstox caps the range a single historical request may span, so long backfills are
split into windows here rather than by the caller.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import settings
from ..ratelimit import RateLimiter

__all__ = [
    "MAX_WINDOW_DAYS",
    "RateLimiter",
    "UpstoxClient",
    "UpstoxError",
    "UpstoxTransientError",
    "download_nse_master",
    "parse_candles",
    "read_master",
    "split_windows",
]

BASE = "https://api.upstox.com"

#: Instrument master (no auth). We join the universe against this instead of calling
#: the instrument-search API, which rejects Analytics Tokens (error UDAPI100050).
NSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "oi"]

#: Max span of a single historical request, per unit. Upstox rejects wider ranges.
MAX_WINDOW_DAYS = {"minutes": 30, "hours": 90, "days": 365, "weeks": 3650, "months": 3650}

IST = "Asia/Kolkata"


class UpstoxError(RuntimeError):
    """Non-retryable API failure (bad token, unknown instrument, malformed request)."""


class UpstoxTransientError(RuntimeError):
    """Rate limit / server hiccup / network blip — worth retrying."""


@dataclass(frozen=True)
class Window:
    from_date: date
    to_date: date


def split_windows(from_date: date, to_date: date, unit: str = "days") -> list[Window]:
    """Chop a date range into request-sized windows (inclusive bounds)."""
    if from_date > to_date:
        return []
    span = MAX_WINDOW_DAYS.get(unit, 365)
    out: list[Window] = []
    start = from_date
    while start <= to_date:
        end = min(start + timedelta(days=span - 1), to_date)
        out.append(Window(start, end))
        start = end + timedelta(days=1)
    return out


def parse_candles(candles: list[list], instrument_key: str) -> pd.DataFrame:
    """Turn the raw candle array into a typed, sorted, IST-naive frame."""
    if not candles:
        return pd.DataFrame(columns=["instrument_key", *CANDLE_COLUMNS])
    df = pd.DataFrame([row[: len(CANDLE_COLUMNS)] for row in candles], columns=CANDLE_COLUMNS)
    ts = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    df["ts"] = ts.dt.tz_convert(IST).dt.tz_localize(None)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    for col in ("volume", "oi"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")
    df.insert(0, "instrument_key", instrument_key)
    return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)


class UpstoxClient:
    def __init__(
        self,
        token: str | None = None,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
    ):
        self.token = token or settings.upstox_access_token
        if client is None and not self.token:
            raise UpstoxError(
                "No UPSTOX_ACCESS_TOKEN set. Generate a read-only Analytics Token in "
                "Upstox → Developer Apps and put it in .env."
            )
        self._c = client or httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=30.0,
        )
        self._limiter = limiter or RateLimiter()

    # --- transport -------------------------------------------------------
    @retry(
        retry=retry_if_exception_type(UpstoxTransientError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def _get(self, url: str) -> dict:
        self._limiter.acquire()
        try:
            r = self._c.get(url)
        except httpx.TransportError as exc:  # timeout, DNS, connection reset
            raise UpstoxTransientError(str(exc)) from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise UpstoxTransientError(f"{r.status_code} from {url}: {r.text[:200]}")
        if r.status_code >= 400:
            raise UpstoxError(f"{r.status_code} from {url}: {r.text[:300]}")
        return r.json()

    # --- market data -----------------------------------------------------
    def historical_candles(
        self,
        instrument_key: str,
        from_date: date,
        to_date: date,
        unit: str = "days",
        interval: str = "1",
    ) -> pd.DataFrame:
        """Historical OHLCV for one instrument, windowed to respect the API range cap."""
        frames = []
        for w in split_windows(from_date, to_date, unit):
            url = (
                f"/v3/historical-candle/{instrument_key}/{unit}/{interval}"
                f"/{w.to_date.isoformat()}/{w.from_date.isoformat()}"
            )
            payload = self._get(url)
            frames.append(parse_candles(payload.get("data", {}).get("candles", []), instrument_key))
        if not frames:
            return pd.DataFrame(columns=["instrument_key", *CANDLE_COLUMNS])
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    def daily_candles(self, instrument_key: str, from_date: date, to_date: date) -> pd.DataFrame:
        return self.historical_candles(instrument_key, from_date, to_date, "days", "1")

    def intraday_candles(self, instrument_key: str, interval: str = "1") -> pd.DataFrame:
        """Today's candles — the historical endpoint excludes the running session."""
        url = f"/v3/historical-candle/intraday/{instrument_key}/minutes/{interval}"
        payload = self._get(url)
        return parse_candles(payload.get("data", {}).get("candles", []), instrument_key)

    def close(self) -> None:
        self._c.close()


def download_nse_master(dest: str) -> str:
    """Fetch the Upstox NSE instrument master (gzipped JSON). No auth required."""
    with httpx.Client(timeout=120.0, follow_redirects=True) as c:
        r = c.get(NSE_MASTER_URL)
        r.raise_for_status()
        with open(dest, "wb") as fh:
            fh.write(r.content)
    return dest


def read_master(path: str) -> pd.DataFrame:
    """Read a master file written by :func:`download_nse_master` (or a plain .json)."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return pd.DataFrame(json.load(fh))
