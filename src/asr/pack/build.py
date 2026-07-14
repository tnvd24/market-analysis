"""The research pack: everything known about a stock, computed, never interpreted.

This is the handoff between the two tracks. The automated track (pure code, no model) puts
verified facts in; a human — or a chat session on a subscription — takes the qualitative
read out. The pack therefore has **zero hallucination surface**: every number in it was
computed by a library from stored rows, and every headline is quoted, never summarised.

The hard rule: **this file reports, it does not conclude.** Signals say "the 50-day crossed
the 200-day," never "buy." News is listed verbatim with its source and link, never
characterised as good or bad. Drawing the conclusion is the reader's job, and keeping that
boundary is what makes the output trustworthy.

Data-quality findings ride along *inside* the pack rather than being printed and forgotten,
so a reader can never study a stock without also seeing that its prices may be unadjusted.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd

from ..features.signals import Signal, detect, detect_52w
from ..ingest.adjust import adjusted
from ..quality.checks import QualityReport, run_checks
from ..storage.base import StorageAdapter, get_storage

DISCLAIMER = (
    "Research only. These are computed observations, not investment advice, and not a "
    "recommendation to buy or sell. No order is ever placed by this system."
)

#: Trailing windows reported as returns, in trading days.
RETURN_WINDOWS = {"1d": 1, "1w": 5, "1m": 21, "3m": 63, "1y": 252}


def _returns(candles: pd.DataFrame) -> dict:
    """Trailing returns. A window longer than the history available reports null, not zero."""
    closes = candles.sort_values("ts")["close"].astype(float).reset_index(drop=True)
    if closes.empty:
        return {}
    last = closes.iloc[-1]
    out = {}
    for label, bars in RETURN_WINDOWS.items():
        if len(closes) > bars:
            prior = closes.iloc[-(bars + 1)]
            out[label] = round((last / prior - 1) * 100, 2) if prior else None
        else:
            out[label] = None
    return out


def _price_block(candles: pd.DataFrame) -> dict:
    df = candles.sort_values("ts")
    last = df.iloc[-1]
    year = df.tail(252)
    high_52w = float(year["high"].max())
    low_52w = float(year["low"].min())
    close = float(last["close"])
    return {
        "as_of": str(pd.Timestamp(last["ts"]).date()),
        "close": round(close, 2),
        "open": round(float(last["open"]), 2),
        "high": round(float(last["high"]), 2),
        "low": round(float(last["low"]), 2),
        "volume": int(last["volume"]),
        "returns_pct": _returns(df),
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "pct_below_52w_high": round((high_52w - close) / high_52w * 100, 2) if high_52w else None,
        "pct_above_52w_low": round((close - low_52w) / low_52w * 100, 2) if low_52w else None,
        "bars_available": int(len(df)),
    }


def _indicator_block(feats: pd.Series) -> dict:
    """Latest indicator values. NULL stays null — never coerced to 0."""
    skip = {"instrument_key", "symbol", "ts"}
    out = {}
    for col, val in feats.items():
        if col in skip:
            continue
        out[col] = None if pd.isna(val) else round(float(val), 4)
    return out


def _news_block(storage: StorageAdapter, symbol: str, days: int, limit: int) -> list[dict]:
    """Headlines and filings, quoted verbatim. Collected, deliberately not interpreted."""
    since = (pd.Timestamp.now() - pd.Timedelta(days=days)).isoformat()
    df = storage.read_sql(
        "SELECT published_at, source, category, headline, summary, url FROM news "
        f"WHERE symbol = '{symbol}' AND published_at >= '{since}' "  # noqa: S608
        f"ORDER BY published_at DESC LIMIT {int(limit)}"
    )
    return [
        {
            "published_at": str(pd.Timestamp(r.published_at)),
            "source": r.source,
            "category": r.category,
            "headline": r.headline,
            "summary": r.summary,
            "url": r.url,
        }
        for r in df.itertuples()
    ]


def build_pack(
    symbol: str,
    storage: StorageAdapter | None = None,
    news_days: int = 30,
    news_limit: int = 25,
    quality: QualityReport | None = None,
) -> dict:
    """The full research pack for one stock, as a plain dict (JSON-serialisable)."""
    storage = storage or get_storage()
    symbol = symbol.strip().upper()

    meta = storage.read_sql(
        f"SELECT instrument_key, symbol, name, isin FROM instruments WHERE symbol = '{symbol}'"  # noqa: S608
    )
    if meta.empty:
        raise LookupError(f"{symbol} is not in the stored universe. Run `asr ingest instruments`.")
    key = meta.iloc[0]["instrument_key"]

    raw = storage.read_sql(
        "SELECT ts, open, high, low, close, volume, adj_factor FROM candles "
        f"WHERE instrument_key = '{key}' ORDER BY ts"  # noqa: S608
    )
    if raw.empty:
        raise LookupError(f"No candles stored for {symbol}. Run `asr ingest prices`.")
    # Returns and the 52-week range must be computed on adjusted prices, or any window
    # spanning a split reports a fake crash as if it were performance.
    candles = adjusted(raw)

    feats = storage.read_sql(
        f"SELECT * FROM features WHERE instrument_key = '{key}' ORDER BY ts DESC LIMIT 2"  # noqa: S608
    )
    latest = feats.iloc[0] if not feats.empty else pd.Series(dtype="float64")
    previous = feats.iloc[1] if len(feats) > 1 else None

    # Signals need the close alongside the indicators.
    close = float(candles.sort_values("ts").iloc[-1]["close"])
    latest_with_price = pd.concat([latest, pd.Series({"close": close})])
    prev_with_price = previous  # crossovers only compare indicator series

    signals: list[Signal] = detect(latest_with_price, prev_with_price) + detect_52w(candles)

    quality = quality if quality is not None else run_checks(storage)
    findings = [
        {"severity": f.severity, "check": f.check, "detail": f.detail}
        for f in quality.findings
        if f.symbol == symbol or f.symbol is None
    ]

    return {
        "meta": {
            "symbol": symbol,
            "name": meta.iloc[0]["name"],
            "isin": meta.iloc[0]["isin"],
            "instrument_key": key,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "disclaimer": DISCLAIMER,
        },
        "price": _price_block(candles),
        "indicators": _indicator_block(latest) if not feats.empty else {},
        "signals": [s.to_dict() for s in signals],
        "news": _news_block(storage, symbol, news_days, news_limit),
        "data_quality": findings,
    }


# --- rendering ----------------------------------------------------------------


def to_json(pack: dict) -> str:
    return json.dumps(pack, indent=2, default=str)


def to_markdown(pack: dict) -> str:
    """Markdown, because this is what gets pasted into a chat window."""
    m, p, ind = pack["meta"], pack["price"], pack["indicators"]
    lines = [
        f"# {m['symbol']} — {m['name']}",
        "",
        f"*Research pack generated {m['generated_at']} · {m['instrument_key']}*",
        "",
        f"> {m['disclaimer']}",
        "",
    ]

    if pack["data_quality"]:
        lines += ["## ⚠️ Data quality", ""]
        for f in pack["data_quality"]:
            lines.append(f"- **{f['severity']}** ({f['check']}): {f['detail']}")
        lines += ["", "*Treat the numbers below with this in mind.*", ""]

    r = p.get("returns_pct", {})
    lines += [
        "## Price",
        "",
        f"- **Close {p['close']}** on {p['as_of']} (O {p['open']} · H {p['high']} · L {p['low']} "
        f"· Vol {p['volume']:,})",
        "- Returns: "
        + " · ".join(f"{k} {v:+.2f}%" if v is not None else f"{k} n/a" for k, v in r.items()),
        f"- 52-week range: {p['low_52w']} – {p['high_52w']} "
        f"({p['pct_below_52w_high']}% below the high, {p['pct_above_52w_low']}% above the low)",
        f"- History: {p['bars_available']} bars",
        "",
        "## Indicators",
        "",
    ]

    if ind:
        for col, val in ind.items():
            lines.append(f"- `{col}`: {'null (insufficient history)' if val is None else val}")
    else:
        lines.append("- none computed — run `asr features build`")

    lines += ["", "## Signals", ""]
    if pack["signals"]:
        for s in pack["signals"]:
            lines.append(f"- **{s['name']}** ({s['direction']}) — {s['description']}")
    else:
        lines.append("- none triggered")

    lines += ["", "## News & filings (verbatim, uninterpreted)", ""]
    if pack["news"]:
        for n in pack["news"]:
            when = n["published_at"][:16]
            cat = f" · {n['category']}" if n["category"] else ""
            lines.append(f"- **{when}** [{n['source']}{cat}] {n['headline']}")
            if n["summary"]:
                lines.append(f"  - {n['summary']}")
            if n["url"]:
                lines.append(f"  - <{n['url']}>")
    else:
        lines.append("- nothing in the window")

    lines += ["", "---", "", f"_{m['disclaimer']}_", ""]
    return "\n".join(lines)


def build_many(
    symbols: list[str], storage: StorageAdapter | None = None, **kw
) -> tuple[list[dict], dict[str, str]]:
    """Packs for several stocks. Quality checks run once, not once per stock."""
    storage = storage or get_storage()
    quality = run_checks(storage)
    packs, failures = [], {}
    for sym in symbols:
        try:
            packs.append(build_pack(sym, storage=storage, quality=quality, **kw))
        except LookupError as exc:
            failures[sym] = str(exc)
    return packs, failures


def default_out_dir() -> str:
    return f"packs/{date.today().isoformat()}"
