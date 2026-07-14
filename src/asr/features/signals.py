"""Rule-based signals — deterministic, explainable, no model involved.

Each signal is a plain arithmetic rule over the `features` table, and each one carries the
**numbers that triggered it**. That evidence is the point: the research pack hands a human
(or a chat session) "golden_cross, because sma_50 crossed from 1412.30 below sma_200 to
1419.80 above it on 2026-07-10" — not a bare label to be taken on faith.

These are *observations, not recommendations*. `golden_cross` says two averages crossed. It
does not say buy. The interpretation happens later, by a human looking at the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

RSI_OVERSOLD = 30.0
RSI_OVERBOUGHT = 70.0
VOLUME_SPIKE = 2.0  # x the 20-day average
NEAR_EXTREME_PCT = 0.02  # within 2% of the 52-week high/low


@dataclass(frozen=True)
class Signal:
    name: str
    direction: str  # "bullish" | "bearish" | "neutral"
    description: str  # plain English, for a human
    evidence: dict  # the numbers behind it

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "direction": self.direction,
            "description": self.description,
            "evidence": self.evidence,
        }


def _num(value) -> float | None:
    """NULL indicators stay NULL. A missing value must never be read as 0."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _crossed(prev_a, prev_b, now_a, now_b) -> str | None:
    """Did series a cross series b between the two bars?"""
    if None in (prev_a, prev_b, now_a, now_b):
        return None
    if prev_a <= prev_b and now_a > now_b:
        return "up"
    if prev_a >= prev_b and now_a < now_b:
        return "down"
    return None


def detect(latest: pd.Series, previous: pd.Series | None = None) -> list[Signal]:
    """Signals for one instrument, from its newest feature row (and the one before it).

    ``previous`` is needed for crossovers — a cross is an event between two bars, not a
    state. Without it, only state-based signals fire.
    """
    out: list[Signal] = []

    rsi = _num(latest.get("rsi_14"))
    close = _num(latest.get("close"))
    sma_50 = _num(latest.get("sma_50"))
    sma_200 = _num(latest.get("sma_200"))
    macd = _num(latest.get("macd"))
    macd_signal = _num(latest.get("macd_signal"))
    bb_upper = _num(latest.get("bb_upper"))
    bb_lower = _num(latest.get("bb_lower"))
    rel_volume = _num(latest.get("rel_volume"))
    atr = _num(latest.get("atr_14"))

    # --- momentum state ---
    if rsi is not None:
        if rsi < RSI_OVERSOLD:
            out.append(
                Signal(
                    "rsi_oversold",
                    "bullish",
                    f"RSI-14 at {rsi:.1f} is below {RSI_OVERSOLD:.0f} (oversold territory).",
                    {"rsi_14": rsi, "threshold": RSI_OVERSOLD},
                )
            )
        elif rsi > RSI_OVERBOUGHT:
            out.append(
                Signal(
                    "rsi_overbought",
                    "bearish",
                    f"RSI-14 at {rsi:.1f} is above {RSI_OVERBOUGHT:.0f} (overbought territory).",
                    {"rsi_14": rsi, "threshold": RSI_OVERBOUGHT},
                )
            )

    # --- trend regime ---
    if close is not None and sma_200 is not None:
        above = close > sma_200
        out.append(
            Signal(
                "above_200dma" if above else "below_200dma",
                "bullish" if above else "bearish",
                f"Close {close:.2f} is {'above' if above else 'below'} the 200-day "
                f"average ({sma_200:.2f}).",
                {"close": close, "sma_200": sma_200, "distance_pct": (close / sma_200 - 1) * 100},
            )
        )

    # --- crossovers (need the previous bar) ---
    if previous is not None:
        cross = _crossed(
            _num(previous.get("sma_50")),
            _num(previous.get("sma_200")),
            sma_50,
            sma_200,
        )
        if cross == "up":
            out.append(
                Signal(
                    "golden_cross",
                    "bullish",
                    f"50-day average ({sma_50:.2f}) crossed above the 200-day ({sma_200:.2f}).",
                    {"sma_50": sma_50, "sma_200": sma_200},
                )
            )
        elif cross == "down":
            out.append(
                Signal(
                    "death_cross",
                    "bearish",
                    f"50-day average ({sma_50:.2f}) crossed below the 200-day ({sma_200:.2f}).",
                    {"sma_50": sma_50, "sma_200": sma_200},
                )
            )

        macd_cross = _crossed(
            _num(previous.get("macd")),
            _num(previous.get("macd_signal")),
            macd,
            macd_signal,
        )
        if macd_cross == "up":
            out.append(
                Signal(
                    "macd_bullish_cross",
                    "bullish",
                    f"MACD ({macd:.3f}) crossed above its signal line ({macd_signal:.3f}).",
                    {"macd": macd, "macd_signal": macd_signal},
                )
            )
        elif macd_cross == "down":
            out.append(
                Signal(
                    "macd_bearish_cross",
                    "bearish",
                    f"MACD ({macd:.3f}) crossed below its signal line ({macd_signal:.3f}).",
                    {"macd": macd, "macd_signal": macd_signal},
                )
            )

    # --- volatility bands ---
    if close is not None and bb_upper is not None and close > bb_upper:
        out.append(
            Signal(
                "bollinger_breakout_up",
                "bullish",
                f"Close {close:.2f} is above the upper Bollinger band ({bb_upper:.2f}).",
                {"close": close, "bb_upper": bb_upper},
            )
        )
    if close is not None and bb_lower is not None and close < bb_lower:
        out.append(
            Signal(
                "bollinger_breakout_down",
                "bearish",
                f"Close {close:.2f} is below the lower Bollinger band ({bb_lower:.2f}).",
                {"close": close, "bb_lower": bb_lower},
            )
        )

    # --- participation ---
    if rel_volume is not None and rel_volume >= VOLUME_SPIKE:
        out.append(
            Signal(
                "volume_spike",
                "neutral",
                f"Volume is {rel_volume:.1f}x its 20-day average — unusual participation. "
                "Direction is not implied; check the news.",
                {"rel_volume": rel_volume, "threshold": VOLUME_SPIKE},
            )
        )

    # --- context, not a trigger ---
    if atr is not None and close is not None and close > 0:
        out.append(
            Signal(
                "volatility_context",
                "neutral",
                f"ATR-14 is {atr:.2f}, i.e. {atr / close * 100:.1f}% of price — the typical "
                "daily range.",
                {"atr_14": atr, "atr_pct_of_price": atr / close * 100},
            )
        )
    return out


def detect_52w(candles: pd.DataFrame) -> list[Signal]:
    """52-week extremes — computed from candles, since features hold no rolling extremes."""
    if candles.empty or "close" not in candles:
        return []
    window = candles.sort_values("ts").tail(252)  # ~one trading year
    if len(window) < 60:  # too little history to call anything a 52-week extreme
        return []

    close = float(window["close"].iloc[-1])
    high = float(window["high"].max())
    low = float(window["low"].min())
    out: list[Signal] = []

    if high > 0 and (high - close) / high <= NEAR_EXTREME_PCT:
        out.append(
            Signal(
                "near_52w_high",
                "bullish",
                f"Close {close:.2f} is within {(high - close) / high * 100:.1f}% of the "
                f"52-week high ({high:.2f}).",
                {"close": close, "high_52w": high, "bars": len(window)},
            )
        )
    if low > 0 and (close - low) / low <= NEAR_EXTREME_PCT:
        out.append(
            Signal(
                "near_52w_low",
                "bearish",
                f"Close {close:.2f} is within {(close - low) / low * 100:.1f}% of the "
                f"52-week low ({low:.2f}).",
                {"close": close, "low_52w": low, "bars": len(window)},
            )
        )
    return out
