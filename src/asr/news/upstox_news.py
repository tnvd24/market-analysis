"""Upstox News API — the secondary (journalism) feed.

``GET /v2/news`` with ``category=instrument_keys``, up to **30 instrument keys per
request**, paginated. Returns ``heading`` / ``summary`` / ``article_link`` /
``published_time`` (Unix ms) — headline and summary only, never the article body.

We do not follow ``article_link`` to fetch the full text: that would be scraping publisher
sites, which this project rules out. So the extractor in Phase 4 reasons over the summary
and cites the link. That caps how much signal is available here, which is exactly why the
NSE filings feed carries the weight.

Auth is the same bearer token as market data. Whether the read-only Analytics Token is
accepted on this endpoint is undocumented; ``category=instrument_keys`` needs no portfolio
scope, so it should be — but this is the same class of quirk as the instrument-search
failure (UDAPI100050), so treat it as unverified until a real call succeeds.
"""

from __future__ import annotations

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import settings
from ..ingest.upstox_client import UpstoxError, UpstoxTransientError
from ..ratelimit import RateLimiter
from .schema import SOURCE_UPSTOX, NewsItem

BASE = "https://api.upstox.com"
NEWS_URL = "/v2/news"

#: Hard limit from the API (error UDAPI1193 above this).
MAX_KEYS_PER_REQUEST = 30
MAX_PAGE_SIZE = 100


class UpstoxNews:
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

    @retry(
        retry=retry_if_exception_type(UpstoxTransientError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, max=30),
        reraise=True,
    )
    def _get(self, params: dict) -> dict:
        self._limiter.acquire()
        try:
            r = self._c.get(NEWS_URL, params=params)
        except httpx.TransportError as exc:
            raise UpstoxTransientError(str(exc)) from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise UpstoxTransientError(f"{r.status_code} from news: {r.text[:200]}")
        if r.status_code >= 400:
            raise UpstoxError(f"{r.status_code} from news: {r.text[:300]}")
        return r.json()

    def fetch(self, instrument_keys: list[str], symbols: dict[str, str]) -> list[NewsItem]:
        """News for a batch of instrument keys.

        ``symbols`` maps instrument_key -> symbol, so rows carry a human ticker without a
        second lookup. Callers must respect :data:`MAX_KEYS_PER_REQUEST`; use
        :func:`batches` to split.
        """
        if not instrument_keys:
            return []
        if len(instrument_keys) > MAX_KEYS_PER_REQUEST:
            raise ValueError(
                f"{len(instrument_keys)} keys exceeds the API's {MAX_KEYS_PER_REQUEST}-key "
                "limit; split with news.upstox_news.batches()."
            )

        items: list[NewsItem] = []
        page = 1
        while True:
            payload = self._get(
                {
                    "category": "instrument_keys",
                    "instrument_keys": ",".join(instrument_keys),
                    "page_number": page,
                    "page_size": MAX_PAGE_SIZE,
                }
            )
            data = payload.get("data") or {}
            items.extend(self._parse_page(data, symbols))

            meta = payload.get("metadata") or payload.get("meta") or {}
            total_pages = int(meta.get("total_pages") or 1)
            if page >= total_pages or page >= 100:
                break
            page += 1
        return items

    @staticmethod
    def _parse_page(data, symbols: dict[str, str]) -> list[NewsItem]:
        """The payload is keyed by instrument_key -> list of articles."""
        out: list[NewsItem] = []
        if not isinstance(data, dict):
            return out

        for key, articles in data.items():
            for art in articles or []:
                headline = (art.get("heading") or "").strip()
                if not headline:
                    continue
                published = pd.to_datetime(art.get("published_time"), unit="ms", errors="coerce")
                if pd.isna(published):
                    continue
                # published_time is epoch ms (UTC). Store IST, like every other timestamp.
                published = (
                    published.tz_localize("UTC").tz_convert("Asia/Kolkata").tz_localize(None)
                )

                out.append(
                    NewsItem(
                        instrument_key=key,
                        symbol=symbols.get(key, key.split("|")[-1]),
                        source=SOURCE_UPSTOX,
                        published_at=published,
                        headline=headline,
                        summary=(art.get("summary") or "").strip() or None,
                        url=(art.get("article_link") or "").strip() or None,
                    )
                )
        return out

    def close(self) -> None:
        self._c.close()


def batches(keys: list[str], size: int = MAX_KEYS_PER_REQUEST):
    for i in range(0, len(keys), size):
        yield keys[i : i + size]
