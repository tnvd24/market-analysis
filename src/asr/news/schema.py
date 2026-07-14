"""The shape every news source normalises into.

Two very different feeds land here — NSE corporate filings (primary source: the company
telling the exchange something) and Upstox's news wire (secondary source: a journalist
telling you about it). They are kept in one table with a `source` column rather than two
tables, because Phase 5 wants "everything known about RELIANCE this week" in one query,
and Phase 4's extractor treats them identically: text in, structured JSON out.

The distinction that *does* matter is preserved: `source` lets a synthesis agent weight a
filing above a headline, and `url` always points at the underlying document so any claim
can be traced back to it.
"""

from __future__ import annotations

import hashlib

import pandas as pd
from pydantic import BaseModel, Field

#: Sources, most authoritative first.
SOURCE_NSE = "nse_announcement"  # primary: the filing itself
SOURCE_UPSTOX = "upstox_news"  # secondary: reporting about the company

NEWS_COLUMNS = [
    "id",
    "instrument_key",
    "symbol",
    "source",
    "published_at",
    "category",
    "headline",
    "summary",
    "url",
    "fetched_at",
]


class NewsItem(BaseModel):
    """One article or filing. Validated on the way in, so Phase 4 never parses raw JSON."""

    instrument_key: str | None = None
    symbol: str
    source: str
    published_at: pd.Timestamp
    category: str | None = None  # NSE's filing type ("Credit Rating"); None for news
    headline: str
    summary: str | None = None
    url: str | None = None
    #: The source's own identifier, when it has one (NSE `seq_id`). Used for dedup.
    external_id: str | None = Field(default=None, exclude=True)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def id(self) -> str:
        """Stable content id, so re-fetching the same item never duplicates a row.

        Prefer the source's own id. Otherwise hash the fields that identify the item —
        the URL where there is one (news wires reuse headlines across stocks), else the
        headline plus its timestamp.
        """
        basis = self.external_id or self.url or f"{self.headline}|{self.published_at.isoformat()}"
        return hashlib.sha256(f"{self.source}|{self.symbol}|{basis}".encode()).hexdigest()[:32]

    def to_row(self, fetched_at: pd.Timestamp) -> dict:
        return {
            "id": self.id,
            "instrument_key": self.instrument_key,
            "symbol": self.symbol,
            "source": self.source,
            "published_at": self.published_at,
            "category": self.category,
            "headline": self.headline,
            "summary": self.summary,
            "url": self.url,
            "fetched_at": fetched_at,
        }


def to_frame(items: list[NewsItem], fetched_at: pd.Timestamp | None = None) -> pd.DataFrame:
    fetched_at = fetched_at or pd.Timestamp.now()
    if not items:
        return pd.DataFrame(columns=NEWS_COLUMNS)
    df = pd.DataFrame([i.to_row(fetched_at) for i in items])
    # A single fetch can return the same item twice (NSE paginates by time, and a filing
    # amended seconds later reappears); collapse before it reaches the database.
    return df.drop_duplicates(subset="id").reset_index(drop=True)
