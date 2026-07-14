"""NSE corporate announcements — the primary-source news feed.

This is the company telling the exchange something (results, credit rating, restructuring,
board meetings), not a journalist's read on it. For a system whose whole premise is
grounding, a filing outranks a headline, so this is the source we lean on.

Two quirks of nseindia.com, both handled here:

* **Cookie priming.** Hitting the JSON endpoint cold returns 401/403. You must first load
  the corresponding HTML page so the server sets its cookies, then reuse that session.
* **`an_dt` is ``dd-Mon-yyyy HH:MM:SS`` in IST**, with no timezone marker. We parse it
  explicitly rather than letting pandas guess, and store tz-naive IST — the same clock the
  candles use, so a filing can be lined up against the day it moved the price.

No auth, no API key, no rate limit published — so we throttle ourselves on principle.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..ratelimit import RateLimiter
from .schema import SOURCE_NSE, NewsItem

BASE = "https://www.nseindia.com"
ANNOUNCEMENTS_URL = f"{BASE}/api/corporate-announcements"
PRIMING_URL = f"{BASE}/companies-listing/corporate-filings-announcements"

#: NSE serves the JSON only to something that looks like the browser that loaded the page.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PRIMING_URL,
}

NSE_DATE_FMT = "%d-%b-%Y %H:%M:%S"


class NseError(RuntimeError):
    """NSE said no in a way that retrying will not fix."""


class NseTransientError(RuntimeError):
    """Blocked, rate-limited or flaky — retry (re-priming the session first)."""


class NseAnnouncements:
    def __init__(self, client: httpx.Client | None = None, limiter: RateLimiter | None = None):
        self._c = client or httpx.Client(timeout=30.0, follow_redirects=True, headers=HEADERS)
        # NSE publishes no rate limit; 2 req/s is a deliberately quiet guest.
        self._limiter = limiter or RateLimiter(rate_per_sec=2.0, burst=2)
        self._primed = False

    def _prime(self) -> None:
        """Load the HTML page so NSE hands us the cookies its JSON endpoint demands."""
        self._limiter.acquire()
        try:
            self._c.get(PRIMING_URL)
        except httpx.TransportError as exc:
            raise NseTransientError(str(exc)) from exc
        self._primed = True

    @retry(
        retry=retry_if_exception_type(NseTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=20),
        reraise=True,
    )
    def _get(self, params: dict) -> list[dict]:
        if not self._primed:
            self._prime()
        self._limiter.acquire()
        try:
            r = self._c.get(ANNOUNCEMENTS_URL, params=params)
        except httpx.TransportError as exc:
            raise NseTransientError(str(exc)) from exc

        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            # Cookies go stale; force a re-prime so the retry starts from a fresh session.
            self._primed = False
            raise NseTransientError(f"{r.status_code} from NSE: {r.text[:150]}")
        if r.status_code >= 400:
            raise NseError(f"{r.status_code} from NSE: {r.text[:200]}")

        try:
            payload = r.json()
        except ValueError as exc:  # an HTML block page with a 200
            self._primed = False
            raise NseTransientError(f"NSE returned non-JSON: {r.text[:120]}") from exc
        # The endpoint returns a bare list; an error object would be a dict.
        return payload if isinstance(payload, list) else []

    def fetch(self, symbol: str, since: date, until: date | None = None) -> list[NewsItem]:
        """Announcements for one NSE symbol over a date range (inclusive)."""
        until = until or date.today()
        rows = self._get(
            {
                "index": "equities",
                "symbol": symbol.strip().upper(),
                "from_date": since.strftime("%d-%m-%Y"),
                "to_date": until.strftime("%d-%m-%Y"),
            }
        )
        return [item for row in rows if (item := self._parse(row, symbol)) is not None]

    @staticmethod
    def _parse(row: dict, symbol: str) -> NewsItem | None:
        published = pd.to_datetime(row.get("an_dt"), format=NSE_DATE_FMT, errors="coerce")
        if pd.isna(published):
            return None

        # `desc` is the filing type ("Credit Rating"); `attchmntText` is its substance.
        # Either can be blank, so the headline falls back rather than dropping the filing.
        desc = (row.get("desc") or "").strip()
        body = (row.get("attchmntText") or "").strip()
        headline = desc or body[:200]
        if not headline:
            return None

        return NewsItem(
            symbol=(row.get("symbol") or symbol).strip().upper(),
            source=SOURCE_NSE,
            published_at=published,
            category=desc or None,
            headline=headline,
            summary=body or None,
            url=(row.get("attchmntFile") or "").strip() or None,
            external_id=str(row.get("seq_id")) if row.get("seq_id") else None,
        )

    def close(self) -> None:
        self._c.close()


def default_since(days: int = 30) -> date:
    return date.today() - timedelta(days=days)
