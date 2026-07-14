"""Peer and index context: is this stock falling, or is everything falling?

"RELIANCE is down 8.5% over a year" means something quite different depending on whether the
index is up 10% or down 15%. On its own the number invites the wrong conclusion, and a reader
— human or model — will supply the missing comparison from imagination if we don't supply it
from data.

We have all 500 stocks and their industries, so the comparison is cheap:

* the **median** stock's return over the same window (the index, equal-weighted),
* the **median stock in the same industry**,
* the stock's **percentile** within each (50 = exactly typical, 90 = only 10% did better).

All computed from split-adjusted closes, over the same bars, with no interpretation attached.
A percentile is a fact about a distribution, not a verdict.
"""

from __future__ import annotations

import pandas as pd

from ..ingest.adjust import adjusted
from ..storage.base import StorageAdapter, get_storage

#: Trailing windows, in trading days — the same ones the price block reports.
WINDOWS = {"1m": 21, "3m": 63, "1y": 252}

#: An industry with fewer than this many stocks having data can't support a useful median.
MIN_PEERS = 3


def _universe_closes(storage: StorageAdapter, bars: int) -> pd.DataFrame:
    """Adjusted closes for every stock, for the last `bars` sessions."""
    df = storage.read_sql(
        f"""
        WITH ranked AS (
            SELECT symbol, ts, close, adj_factor,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) AS rn
            FROM candles
        )
        SELECT symbol, ts, close, adj_factor FROM ranked WHERE rn <= {int(bars)}
        """  # noqa: S608
    )
    return adjusted(df)


def universe_returns(storage: StorageAdapter | None = None) -> pd.DataFrame:
    """Trailing returns for every stock, plus its industry. One query, whole universe."""
    storage = storage or get_storage()
    longest = max(WINDOWS.values()) + 1
    closes = _universe_closes(storage, longest)
    if closes.empty:
        return pd.DataFrame()

    rows = []
    for symbol, grp in closes.groupby("symbol"):
        g = grp.sort_values("ts")
        last = float(g["close"].iloc[-1])
        row = {"symbol": symbol}
        for label, bars in WINDOWS.items():
            # A window longer than the stock's history is null, never zero — a recent listing
            # has no 1-year return, and pretending it is 0% would drag every median it enters.
            row[label] = (
                round((last / float(g["close"].iloc[-(bars + 1)]) - 1) * 100, 2)
                if len(g) > bars
                else None
            )
        rows.append(row)

    returns = pd.DataFrame(rows)
    industries = storage.read_sql("SELECT symbol, industry FROM instruments")
    return returns.merge(industries, on="symbol", how="left")


def peer_context(
    symbol: str,
    storage: StorageAdapter | None = None,
    universe: pd.DataFrame | None = None,
) -> dict:
    """Where one stock sits against the index and its industry, per window."""
    storage = storage or get_storage()
    uni = universe if universe is not None else universe_returns(storage)
    if uni.empty or symbol not in set(uni["symbol"]):
        return {}

    me = uni[uni["symbol"] == symbol].iloc[0]
    industry = me.get("industry")
    peers = uni[uni["industry"] == industry] if pd.notna(industry) else uni.iloc[0:0]

    out: dict = {
        "industry": industry if pd.notna(industry) else None,
        "peers_in_industry": int(len(peers)),
        "windows": {},
    }

    for label in WINDOWS:
        mine = me[label]
        if pd.isna(mine):
            continue

        index_series = uni[label].dropna()
        block = {
            "stock_pct": float(mine),
            "index_median_pct": round(float(index_series.median()), 2),
            "index_percentile": round(float((index_series < mine).mean() * 100), 0),
        }

        peer_series = peers[label].dropna()
        if len(peer_series) >= MIN_PEERS:
            block["industry_median_pct"] = round(float(peer_series.median()), 2)
            block["industry_percentile"] = round(float((peer_series < mine).mean() * 100), 0)

        out["windows"][label] = block

    return out


def describe(context: dict) -> list[str]:
    """Plain-English lines for the Markdown pack. States the comparison; draws no conclusion."""
    if not context or not context.get("windows"):
        return []

    lines = []
    industry = context.get("industry")
    for label, w in context["windows"].items():
        parts = [
            f"**{label}**: {w['stock_pct']:+.1f}% "
            f"vs index median {w['index_median_pct']:+.1f}% "
            f"({w['index_percentile']:.0f}th percentile of the 500)"
        ]
        if "industry_median_pct" in w:
            parts.append(
                f"{industry} median {w['industry_median_pct']:+.1f}% "
                f"({w['industry_percentile']:.0f}th percentile of "
                f"{context['peers_in_industry']} peers)"
            )
        lines.append(" · ".join(parts))
    return lines
