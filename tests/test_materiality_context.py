import pandas as pd
import pytest

from asr.news.materiality import HIGH, LOW, MEDIUM, rank, tier
from asr.pack.context import describe, peer_context, universe_returns
from asr.storage.duckdb_adapter import DuckDBAdapter


# --- materiality: ranks the TYPE, never the content ---------------------------
def test_results_and_corporate_actions_are_material():
    assert tier("Financial Results") == HIGH
    assert tier("Board Meeting") == HIGH
    assert tier("Credit Rating") == HIGH
    assert tier("Amalgamation / Merger") == HIGH
    assert tier("Analysts/Institutional Investor Meet/Con. Call Updates") == HIGH


def test_statutory_boilerplate_sinks_to_the_bottom():
    """The noise that crowded out RELIANCE's Jio IPO filing in the first real pack."""
    assert tier("Trading Window") == LOW
    assert tier("Disclosure under SEBI Takeover Regulations") == LOW
    assert tier("Certificate under SEBI (Depositories and Participants) Regulations, 2018") == LOW


def test_a_regulations_name_does_not_promote_the_filing_that_quotes_it():
    """Statutory titles are full of material-sounding words. They must not fool the ranker.

    Both of these were wrongly promoted to the top of RELIANCE's pack once summaries were
    read: the takeover *regulation* is literally named "Substantial Acquisition of Shares",
    and an AGM's "voting results" contain the word "results". Neither reports an acquisition
    or an earnings figure.
    """
    assert (
        tier(
            "Disclosure under SEBI Takeover Regulations",
            "Disclosure under SEBI Takeover Regulations",
            "Submitted a copy of Disclosure under Regulation 31(4) of the SEBI (Substantial "
            "Acquisition of Shares and Takeovers) Regulations, 2011.",
        )
        == LOW
    )
    assert (
        tier(
            "Shareholders meeting",
            "Shareholders meeting",
            "The voting results along with the consolidated Scrutiniser's Report of the AGM.",
        )
        == LOW
    )


def test_meetings_and_releases_are_contextual():
    assert tier("Shareholders meeting") == MEDIUM
    assert tier("General Updates") == MEDIUM
    assert tier("Copy of Newspaper Publication") == MEDIUM


def test_good_and_bad_news_of_the_same_type_rank_identically():
    """The core discipline: this ranks how price-RELEVANT a type is, not whether it's good.

    A collapse in profits and a record quarter are both 'Financial Results'. Sorting them
    differently would be interpretation — and that is the reader's job, not ours.
    """
    assert tier("Financial Results", "Profit doubles") == tier(
        "Financial Results", "Profit collapses 80%"
    )


def test_an_unknown_category_falls_back_to_the_headline():
    assert tier(None, "Board Meeting Intimation") == HIGH
    assert tier("", "") == LOW  # nothing to go on -> assume it isn't news


def test_the_substance_in_the_summary_is_read_not_just_the_category():
    """The bug this caught on the first real pack.

    NSE files the *type* in the category and the *substance* in the attachment text. The Jio
    Platforms IPO — RELIANCE's biggest item that month — is categorised merely as "General
    Updates"; the words "Initial Public Offer" appear only in the body. Judging on the
    category alone demoted it below the dissolution of a shell subsidiary.
    """
    assert (
        tier("General Updates", "General Updates", "Proposed Initial Public Offer of Jio Platforms")
        == HIGH
    )
    # ...while a genuinely routine "General Updates" filing still ranks as contextual.
    assert tier("General Updates", "General Updates", "Disclosure under Regulation 30") == MEDIUM


def test_rank_orders_material_first_then_newest_and_drops_nothing():
    news = pd.DataFrame(
        [
            {
                "category": "Trading Window",
                "headline": "Trading Window",
                "published_at": pd.Timestamp("2026-07-12"),
            },
            {
                "category": "Financial Results",
                "headline": "Results",
                "published_at": pd.Timestamp("2026-07-01"),
            },
            {
                "category": "Board Meeting",
                "headline": "Board Meeting",
                "published_at": pd.Timestamp("2026-07-10"),
            },
        ]
    )

    out = rank(news)

    # The two HIGH items come first, newest of them leading; the boilerplate sinks.
    assert out["headline"].tolist() == ["Board Meeting", "Results", "Trading Window"]
    assert len(out) == 3  # nothing dropped: a statutory filing is still a fact


def test_ranking_empty_news_is_safe():
    assert rank(pd.DataFrame(columns=["category", "headline", "published_at"])).empty


# --- peer context -------------------------------------------------------------
@pytest.fixture
def storage(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    db.upsert_instruments(
        pd.DataFrame(
            [
                {
                    "instrument_key": f"NSE_EQ|{s}",
                    "symbol": s,
                    "isin": f"I{s}",
                    "name": s,
                    "industry": ind,
                }
                for s, ind in [
                    ("BANKA", "Financial Services"),
                    ("BANKB", "Financial Services"),
                    ("BANKC", "Financial Services"),
                    ("PHARMA", "Healthcare"),
                ]
            ]
        )
    )
    # 260 sessions. Each stock ends at a different multiple of where it started.
    idx = pd.date_range("2025-07-01", periods=260, freq="B")
    rows = []
    for symbol, growth in [("BANKA", 1.5), ("BANKB", 1.2), ("BANKC", 0.9), ("PHARMA", 2.0)]:
        prices = [100 * (1 + (growth - 1) * i / 259) for i in range(260)]
        for ts, px in zip(idx, prices, strict=True):
            rows.append(
                {
                    "instrument_key": f"NSE_EQ|{symbol}",
                    "symbol": symbol,
                    "ts": ts,
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "volume": 1000,
                    "oi": 0,
                }
            )
    db.upsert_candles(pd.DataFrame(rows))
    return db


def test_universe_returns_covers_every_stock_with_its_industry(storage):
    uni = universe_returns(storage)

    assert set(uni["symbol"]) == {"BANKA", "BANKB", "BANKC", "PHARMA"}
    assert uni.set_index("symbol").loc["BANKA", "industry"] == "Financial Services"
    # PHARMA doubles over the full 260-bar fixture, but the 1y window measures the last
    # 252 bars, so it captures 252/259 of that rise. Checking the exact figure keeps the
    # window definition honest.
    assert uni.set_index("symbol").loc["PHARMA", "1y"] == pytest.approx(94.7, abs=0.5)


def test_context_places_a_stock_against_the_index_and_its_peers(storage):
    ctx = peer_context("BANKA", storage)

    assert ctx["industry"] == "Financial Services"
    assert ctx["peers_in_industry"] == 3
    w = ctx["windows"]["1y"]
    assert w["stock_pct"] == pytest.approx(48.0, abs=0.5)  # +50% over 260 bars, measured over 252
    # It beat both banks but not PHARMA: 2nd of 4 overall, 1st of 3 in its industry.
    assert w["index_percentile"] == pytest.approx(50.0)
    assert w["industry_percentile"] == pytest.approx(67.0, abs=1.0)
    assert w["industry_median_pct"] == pytest.approx(19.2, abs=0.5)  # BANKB is the median bank


def test_the_laggard_of_its_own_sector_is_visible_as_such(storage):
    """The point of the block: 'down 10%' reads differently when peers are up 20%."""
    w = peer_context("BANKC", storage)["windows"]["1y"]

    assert w["stock_pct"] < 0
    assert w["industry_median_pct"] > 0  # it fell while its sector rose
    assert w["industry_percentile"] == pytest.approx(0.0)  # worst in its industry


def test_a_lone_stock_in_its_industry_gets_no_peer_median(storage):
    """One stock is not a distribution. Better silent than falsely precise."""
    ctx = peer_context("PHARMA", storage)

    assert ctx["peers_in_industry"] == 1
    assert "industry_median_pct" not in ctx["windows"]["1y"]  # index comparison still stands
    assert "index_median_pct" in ctx["windows"]["1y"]


def test_unknown_symbol_returns_no_context_rather_than_guessing(storage):
    assert peer_context("NOPE", storage) == {}


def test_describe_renders_the_comparison_without_a_verdict(storage):
    lines = describe(peer_context("BANKC", storage))

    text = " ".join(lines)
    assert "index median" in text
    assert "percentile" in text
    # It states where the stock sits. It never says what to do about it.
    for word in ("buy", "sell", "should", "recommend", "undervalued"):
        assert word not in text.lower()
