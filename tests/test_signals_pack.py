import numpy as np
import pandas as pd
import pytest

from asr.features.build import build_features
from asr.features.signals import detect, detect_52w
from asr.pack.build import build_pack, to_markdown
from asr.storage.duckdb_adapter import DuckDBAdapter
from tests.test_indicators import make_candles


def row(**kw) -> pd.Series:
    return pd.Series(kw)


# --- signals ------------------------------------------------------------------


def test_rsi_bands_fire_on_the_right_side():
    assert detect(row(rsi_14=25.0))[0].name == "rsi_oversold"
    assert detect(row(rsi_14=75.0))[0].name == "rsi_overbought"
    assert detect(row(rsi_14=50.0)) == []


def test_a_null_indicator_fires_nothing():
    """The whole reason warmup rows are NULL: a missing RSI must not read as 0 (oversold!)."""
    assert detect(row(rsi_14=np.nan)) == []
    assert detect(row(rsi_14=None)) == []


def test_golden_cross_needs_an_actual_crossing_not_just_an_ordering():
    crossed = detect(
        row(sma_50=110.0, sma_200=100.0),
        previous=row(sma_50=95.0, sma_200=100.0),  # was below, now above
    )
    assert any(s.name == "golden_cross" for s in crossed)

    already_above = detect(
        row(sma_50=110.0, sma_200=100.0),
        previous=row(sma_50=105.0, sma_200=100.0),  # was already above
    )
    assert not any(s.name == "golden_cross" for s in already_above)


def test_death_cross_is_the_mirror():
    signals = detect(
        row(sma_50=90.0, sma_200=100.0),
        previous=row(sma_50=105.0, sma_200=100.0),
    )
    assert any(s.name == "death_cross" and s.direction == "bearish" for s in signals)


def test_no_crossover_signals_without_a_previous_bar():
    assert detect(row(sma_50=110.0, sma_200=100.0)) == []


def test_macd_crossover_carries_the_numbers_that_triggered_it():
    signals = detect(
        row(macd=1.5, macd_signal=1.0),
        previous=row(macd=0.5, macd_signal=1.0),
    )
    sig = next(s for s in signals if s.name == "macd_bullish_cross")
    assert sig.evidence == {"macd": 1.5, "macd_signal": 1.0}  # evidence, not a bare label


def test_volume_spike_is_neutral_not_bullish():
    """Unusual volume says something happened, not which way. Direction is not implied."""
    sig = next(s for s in detect(row(rel_volume=3.0)) if s.name == "volume_spike")
    assert sig.direction == "neutral"


def test_trend_regime_reports_which_side_of_the_200dma():
    above = next(s for s in detect(row(close=120.0, sma_200=100.0)) if "200dma" in s.name)
    below = next(s for s in detect(row(close=80.0, sma_200=100.0)) if "200dma" in s.name)
    assert above.name == "above_200dma" and above.direction == "bullish"
    assert below.name == "below_200dma" and below.direction == "bearish"


def test_52w_high_proximity_needs_enough_history():
    short = make_candles([100.0] * 30)
    assert detect_52w(short) == []  # 30 bars can't establish a 52-week extreme


def test_near_52w_high_fires_when_close_to_the_top():
    closes = list(np.linspace(50, 100, 200))  # steady climb, ends at the high
    signals = detect_52w(make_candles(closes))
    assert any(s.name == "near_52w_high" for s in signals)


# --- pack ---------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    db.upsert_instruments(
        pd.DataFrame(
            [
                {
                    "instrument_key": "NSE_EQ|A",
                    "symbol": "AAA",
                    "isin": "INE_A",
                    "name": "A Industries",
                }
            ]
        )
    )
    rng = np.random.default_rng(9)
    closes = list(100 + np.cumsum(rng.normal(size=260)))
    db.upsert_candles(make_candles(closes, key="NSE_EQ|A", symbol="AAA").assign(oi=0))
    build_features(storage=db)
    db.upsert_news(
        pd.DataFrame(
            [
                {
                    "id": "n1",
                    "instrument_key": "NSE_EQ|A",
                    "symbol": "AAA",
                    "source": "nse_announcement",
                    "published_at": pd.Timestamp.now() - pd.Timedelta(days=2),
                    "category": "Credit Rating",
                    "headline": "Credit Rating",
                    "summary": "CRISIL reaffirmed AAA/Stable.",
                    "url": "https://x/1.pdf",
                    "fetched_at": pd.Timestamp.now(),
                }
            ]
        )
    )
    return db


def test_pack_contains_facts_signals_news_and_quality(storage):
    pack = build_pack("AAA", storage=storage)

    assert pack["meta"]["symbol"] == "AAA"
    assert pack["price"]["close"] > 0
    assert pack["indicators"]["rsi_14"] is not None
    assert isinstance(pack["signals"], list)
    assert pack["news"][0]["headline"] == "Credit Rating"
    assert "data_quality" in pack


def test_pack_quotes_news_verbatim_and_never_interprets_it(storage):
    """The pack collects; it does not characterise. No sentiment field exists, by design."""
    item = build_pack("AAA", storage=storage)["news"][0]

    assert item["summary"] == "CRISIL reaffirmed AAA/Stable."
    assert item["url"] == "https://x/1.pdf"  # always traceable to the source
    assert "sentiment" not in item
    assert "interpretation" not in item


def test_every_pack_carries_the_disclaimer(storage):
    pack = build_pack("AAA", storage=storage)
    assert "not investment advice" in pack["meta"]["disclaimer"]
    assert "not investment advice" in to_markdown(pack)


def test_null_indicators_stay_null_in_the_pack(storage):
    """sma_200 on a 260-bar series exists; on a short one it must be null, not 0."""
    db = DuckDBAdapter(path=str(storage.path))
    db.upsert_instruments(
        pd.DataFrame(
            [{"instrument_key": "NSE_EQ|B", "symbol": "BBB", "isin": "I", "name": "B Ltd"}]
        )
    )
    db.upsert_candles(make_candles([50.0] * 30, key="NSE_EQ|B", symbol="BBB").assign(oi=0))
    build_features(["NSE_EQ|B"], storage=db)

    pack = build_pack("BBB", storage=db)

    assert pack["indicators"]["sma_200"] is None  # not 0.0
    assert "null (insufficient history)" in to_markdown(pack)


def test_returns_are_null_when_the_window_exceeds_history(storage):
    db = DuckDBAdapter(path=str(storage.path))
    db.upsert_instruments(
        pd.DataFrame(
            [{"instrument_key": "NSE_EQ|C", "symbol": "CCC", "isin": "I", "name": "C Ltd"}]
        )
    )
    db.upsert_candles(make_candles([50.0] * 10, key="NSE_EQ|C", symbol="CCC").assign(oi=0))

    pack = build_pack("CCC", storage=db)

    assert pack["price"]["returns_pct"]["1d"] is not None
    assert pack["price"]["returns_pct"]["1y"] is None  # 10 bars can't span a year


def test_data_quality_findings_ride_inside_the_pack(storage):
    """A reader must not be able to study the numbers without seeing that they're suspect."""
    db = DuckDBAdapter(path=str(storage.path))
    db.upsert_candles(
        pd.DataFrame(
            [
                {
                    "instrument_key": "NSE_EQ|A",
                    "symbol": "AAA",
                    "ts": pd.Timestamp("2027-01-01"),  # future-dated: a timezone bug
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1,
                    "oi": 0,
                }
            ]
        )
    )

    pack = build_pack("AAA", storage=db)

    assert any(f["check"] == "future_timestamp" for f in pack["data_quality"])
    assert "⚠️ Data quality" in to_markdown(pack)


def test_unknown_symbol_says_what_to_run(storage):
    with pytest.raises(LookupError, match="not in the stored universe"):
        build_pack("NOPE", storage=storage)
