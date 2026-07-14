"""NSE bhavcopy — the exchange's official end-of-day file. This is the price source.

One request returns the **whole market** for a trading day (~3,400 rows: OHLCV, volume,
ISIN, ticker), so a 3-year backfill is ~750 requests rather than 1,500 per-instrument calls
against a broker. No token, no account, nothing to renew.

Two formats, because NSE changed it:
  * **UDiFF** (`BhavCopy_NSE_CM_...csv.zip`) — current, from mid-2024.
  * **sec_bhavdata_full** (`sec_bhavdata_full_ddmmyyyy.csv`) — the older layout, still
    served for dates before that, which is what makes a multi-year backfill possible.
We try UDiFF first and fall back, so callers never think about the cutover.

**Prices here are raw and unadjusted** — that is a feature, not a gap. They are what
actually traded. Splits and bonuses are applied separately from NSE's corporate-actions
feed (`ingest/adjust.py`), which means the adjustment is ours, deterministic, and auditable,
rather than a broker's undocumented guess.

A 404 means "no trading that day" (weekend or holiday) — that is how the calendar is
discovered, so it is a normal result, not an error.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..ratelimit import RateLimiter

BASE = "https://nsearchives.nseindia.com"
UDIFF_URL = BASE + "/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
LEGACY_URL = BASE + "/products/content/sec_bhavdata_full_{dmy}.csv"
PRIMING_URL = "https://www.nseindia.com/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PRIMING_URL,
}

#: Where downloaded days are cached. A re-run of a backfill then costs nothing.
CACHE_DIR = Path("data/bhavcopy")

#: The columns every parsed day is normalised to.
COLUMNS = ["ts", "isin", "symbol", "open", "high", "low", "close", "volume"]


class BhavcopyTransientError(RuntimeError):
    """Blocked, rate-limited, or flaky — retry."""


class NoTradingDay(Exception):  # noqa: N818
    """No file for this date: a weekend or an exchange holiday. Expected, not a failure."""


def _norm_udiff(df: pd.DataFrame, day: date) -> pd.DataFrame:
    df = df[(df["SctySrs"] == "EQ") & (df["FinInstrmTp"] == "STK")].copy()
    out = pd.DataFrame(
        {
            "ts": pd.Timestamp(day),
            "isin": df["ISIN"].astype(str).str.strip().str.upper(),
            "symbol": df["TckrSymb"].astype(str).str.strip().str.upper(),
            "open": pd.to_numeric(df["OpnPric"], errors="coerce"),
            "high": pd.to_numeric(df["HghPric"], errors="coerce"),
            "low": pd.to_numeric(df["LwPric"], errors="coerce"),
            "close": pd.to_numeric(df["ClsPric"], errors="coerce"),
            "volume": pd.to_numeric(df["TtlTradgVol"], errors="coerce"),
        }
    )
    return out


def _norm_legacy(df: pd.DataFrame, day: date) -> pd.DataFrame:
    df = df.rename(columns={c: c.strip() for c in df.columns})
    df = df[df["SERIES"].astype(str).str.strip() == "EQ"].copy()
    out = pd.DataFrame(
        {
            "ts": pd.Timestamp(day),
            "isin": pd.NA,  # the legacy file carries no ISIN; we join on symbol instead
            "symbol": df["SYMBOL"].astype(str).str.strip().str.upper(),
            "open": pd.to_numeric(df["OPEN_PRICE"], errors="coerce"),
            "high": pd.to_numeric(df["HIGH_PRICE"], errors="coerce"),
            "low": pd.to_numeric(df["LOW_PRICE"], errors="coerce"),
            "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
            "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce"),
        }
    )
    return out


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["close", "symbol"])
    df = df[df["close"] > 0]
    df["volume"] = df["volume"].fillna(0).astype("int64")
    return df[COLUMNS].reset_index(drop=True)


class Bhavcopy:
    def __init__(
        self,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        cache_dir: Path = CACHE_DIR,
    ):
        self._c = client or httpx.Client(timeout=60.0, follow_redirects=True, headers=HEADERS)
        # NSE publishes no rate limit and is a public exchange site we are a guest on.
        self._limiter = limiter or RateLimiter(rate_per_sec=3.0, burst=3)
        self._primed = False
        self.cache_dir = Path(cache_dir)

    def _prime(self) -> None:
        self._limiter.acquire()
        try:
            self._c.get(PRIMING_URL)
        except httpx.TransportError as exc:
            raise BhavcopyTransientError(str(exc)) from exc
        self._primed = True

    @retry(
        retry=retry_if_exception_type(BhavcopyTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=20),
        reraise=True,
    )
    def _get(self, url: str) -> httpx.Response | None:
        """Returns the response, or None for a 404 (no trading that day)."""
        if not self._primed:
            self._prime()
        self._limiter.acquire()
        try:
            r = self._c.get(url)
        except httpx.TransportError as exc:
            raise BhavcopyTransientError(str(exc)) from exc

        if r.status_code == 404:
            return None
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            self._primed = False  # cookies went stale; re-prime on the retry
            raise BhavcopyTransientError(f"{r.status_code} from {url}")
        if r.status_code >= 400:
            raise BhavcopyTransientError(f"{r.status_code} from {url}")
        return r

    def fetch_day(self, day: date, use_cache: bool = True) -> pd.DataFrame:
        """One trading day, whole market, normalised. Raises NoTradingDay for holidays."""
        cache = self.cache_dir / f"{day.isoformat()}.parquet"
        if use_cache and cache.exists():
            return pd.read_parquet(cache)

        ymd = day.strftime("%Y%m%d")
        dmy = day.strftime("%d%m%Y")

        r = self._get(UDIFF_URL.format(ymd=ymd))
        if r is not None:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            raw = pd.read_csv(z.open(z.namelist()[0]))
            raw.columns = [c.strip() for c in raw.columns]
            df = _clean(_norm_udiff(raw, day))
        else:
            r = self._get(LEGACY_URL.format(dmy=dmy))
            if r is None:
                raise NoTradingDay(day.isoformat())
            raw = pd.read_csv(io.BytesIO(r.content))
            raw.columns = [c.strip() for c in raw.columns]
            df = _clean(_norm_legacy(raw, day))

        if use_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache, index=False)
        return df

    def close(self) -> None:
        self._c.close()


def trading_days(since: date, until: date) -> list[date]:
    """Weekdays in the range. Holidays are discovered by the 404, not guessed at."""
    days, d = [], since
    while d <= until:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days
