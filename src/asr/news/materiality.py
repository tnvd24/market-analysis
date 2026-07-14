"""Rank filings by how price-relevant their *type* is. Not by whether they are good news.

The distinction is the whole design. This module answers **"how likely is a filing of this
kind to matter to the price?"** — a property of the category, knowable in advance. It never
answers "is this good or bad for the stock?", which is interpretation, requires reading the
document, and is the reader's job.

So: a results announcement ranks above a trading-window notice **whether the results are
splendid or dreadful**. Nothing here reads the content.

Why it exists: RELIANCE's pack carried a Jio Platforms IPO filing and, with identical
prominence, the dissolution of a subsidiary whose own filing put its contribution at
0.0000001% of consolidated net worth. Both are facts; only one is worth a reader's attention
first. Ordering by date alone buries the signal in statutory boilerplate.

Tiers are matched against NSE's filing *category*, which the exchange assigns — so this is
sorting NSE's own labels, not our reading of the text.
"""

from __future__ import annotations

import pandas as pd

HIGH = 1  # moves the price, or tells you when it will
MEDIUM = 2  # matters, but rarely on the day
LOW = 3  # statutory or procedural: filed because it must be, not because it's news
TIER_NAMES = {HIGH: "high", MEDIUM: "medium", LOW: "low"}

#: Checked FIRST, and forces LOW regardless of anything else.
#:
#: These are statutory filings whose *titles quote the regulation they are filed under*, and
#: those regulation names are full of otherwise-material words. "Disclosure under SEBI
#: (Substantial Acquisition of Shares and Takeovers) Regulations" contains "acquisition" but
#: reports no acquisition; the voting results of an AGM contain "results" but no earnings.
#: Matching keywords without this guard promoted both to the top of RELIANCE's pack.
_STATUTORY = (
    "takeover regulation",
    "substantial acquisition of shares",
    "regulation 31",
    "trading window",
    "insider trading",
    "certificate under",
    "depositories and participants",
    "share transfer",
    "reconciliation of share capital",
    "voting results",
    "scrutiniser",
)

#: Substrings matched (case-insensitively) against category + headline + summary.
#: Order matters: the first tier that matches wins, so HIGH is checked first.
_HIGH = (
    "financial result",
    "unaudited result",
    "audited result",
    "quarterly result",
    "board meeting",
    "dividend",
    "bonus",
    "split",
    "buyback",
    "amalgamation",
    "merger",
    "demerger",
    "acquisition",
    "scheme of arrangement",
    "restructuring",
    "credit rating",
    "initial public offer",
    "fund raising",
    "preferential issue",
    "rights issue",
    "open offer",
    "delisting",
    "resignation",
    "appointment",
    "investor meet",
    "con. call",
    "earnings call",
    "order win",
    "contract award",
    "analysts",
)

_MEDIUM = (
    "shareholders meeting",
    "agm",
    "egm",
    "postal ballot",
    "voting results",
    "annual report",
    "press release",
    "media release",
    "update",  # NSE's catch-all "Updates" / "General Updates"
    "clarification",
    "newspaper publication",
    "investor presentation",
)

# Everything else is LOW: trading-window closures, takeover-regulation disclosures,
# certificates under this-or-that regulation, share-transfer paperwork. Filed because the
# rules demand it, not because anything happened.


def tier(category: str | None, headline: str | None = None, summary: str | None = None) -> int:
    """Materiality tier for a filing.

    **The summary must be included**, and that is not a detail: NSE files the *type* in its
    category and the *substance* in the attachment text. RELIANCE's Jio Platforms IPO filing
    is categorised merely as "General Updates" — the words "Initial Public Offer" appear only
    in the body. Judging on the category alone buried the single biggest item in the pack.

    This still classifies the filing *type* from keywords — it does not read for sentiment.
    """
    text = f"{category or ''} {headline or ''} {summary or ''}".lower()
    if not text.strip():
        return LOW
    if any(k in text for k in _STATUTORY):  # checked first: regulation names are misleading
        return LOW
    if any(k in text for k in _HIGH):
        return HIGH
    if any(k in text for k in _MEDIUM):
        return MEDIUM
    return LOW


def rank(news: pd.DataFrame) -> pd.DataFrame:
    """Add a `tier` column and sort: most price-relevant type first, then newest.

    Nothing is dropped. A low-tier filing is still a fact, and a reader who wants the whole
    record should be able to see it — it just shouldn't be the first thing they read.
    """
    if news.empty:
        return news.assign(tier=pd.Series(dtype="int64"))
    out = news.copy()
    out["tier"] = [
        tier(
            getattr(row, "category", None),
            getattr(row, "headline", None),
            getattr(row, "summary", None),
        )
        for row in out.itertuples()
    ]
    return out.sort_values(["tier", "published_at"], ascending=[True, False]).reset_index(drop=True)
