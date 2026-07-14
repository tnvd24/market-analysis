import io
import zipfile
from datetime import date

import httpx
import pandas as pd
import pytest

from asr.ingest.adjust import adjusted, apply_adjustments, factors_for
from asr.ingest.bhavcopy import Bhavcopy, NoTradingDay, trading_days
from asr.ingest.corporate_actions import DIVIDEND, RIGHTS, SPLIT, classify, parse_actions
from asr.ingest.prices import ingest_prices
from asr.ratelimit import RateLimiter
from asr.storage.duckdb_adapter import DuckDBAdapter

FAST = RateLimiter(rate_per_sec=1e6, burst=1000)

UDIFF_CSV = (
    "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,"
    "OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol\n"
    "2026-07-10,2026-07-10,CM,NSE,STK,1,INE002A01018,RELIANCE,EQ,100,110,95,105,50000\n"
    "2026-07-10,2026-07-10,CM,NSE,STK,2,INE467B01029,TCS,EQ,200,205,198,203,20000\n"
    "2026-07-10,2026-07-10,CM,NSE,STK,3,INE999Z01011,NOTOURS,EQ,10,11,9,10,100\n"
    "2026-07-10,2026-07-10,CM,NSE,IDX,4,INEIDX,NIFTY,EQ,1,1,1,1,0\n"
)

LEGACY_CSV = (
    "SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,LAST_PRICE,"
    "CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY\n"
    "RELIANCE,EQ,10-Jul-2026,99,100,110,95,105,105,102,50000\n"
    "SOMEBOND,GS,10-Jul-2026,99,100,110,95,105,105,102,10\n"
)


def _zip(csv: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("bhav.csv", csv)
    return buf.getvalue()


def _bhav(handler, tmp_path) -> Bhavcopy:
    c = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Bhavcopy(client=c, limiter=FAST, cache_dir=tmp_path / "cache")


# --- bhavcopy -----------------------------------------------------------------


def test_udiff_is_parsed_and_non_equities_are_dropped(tmp_path):
    def handler(request):
        if "BhavCopy" in str(request.url):
            return httpx.Response(200, content=_zip(UDIFF_CSV))
        return httpx.Response(200, text="ok")

    df = _bhav(handler, tmp_path).fetch_day(date(2026, 7, 10))

    assert set(df["symbol"]) == {"RELIANCE", "TCS", "NOTOURS"}  # the index row is gone
    assert df.loc[df["symbol"] == "RELIANCE", "close"].iloc[0] == 105
    assert df.loc[df["symbol"] == "RELIANCE", "isin"].iloc[0] == "INE002A01018"
    assert df["ts"].iloc[0] == pd.Timestamp("2026-07-10")


def test_falls_back_to_the_legacy_format_when_udiff_is_absent(tmp_path):
    """UDiFF only exists from mid-2024; older dates must still backfill."""

    def handler(request):
        url = str(request.url)
        if "BhavCopy" in url:
            return httpx.Response(404)
        if "sec_bhavdata_full" in url:
            return httpx.Response(200, content=LEGACY_CSV.encode())
        return httpx.Response(200, text="ok")

    df = _bhav(handler, tmp_path).fetch_day(date(2022, 7, 14))

    assert df["symbol"].tolist() == ["RELIANCE"]  # the GS series row is dropped
    assert pd.isna(df["isin"].iloc[0])  # legacy file carries no ISIN


def test_a_holiday_is_not_an_error(tmp_path):
    """Both formats 404 -> the exchange was shut. That's how we learn the calendar."""

    def handler(request):
        if "nseindia.com/" in str(request.url) and "archives" not in str(request.url):
            return httpx.Response(200, text="ok")
        return httpx.Response(404)

    with pytest.raises(NoTradingDay):
        _bhav(handler, tmp_path).fetch_day(date(2026, 1, 26))


def test_a_fetched_day_is_cached_so_reruns_cost_nothing(tmp_path):
    calls = []

    def handler(request):
        if "BhavCopy" in str(request.url):
            calls.append(1)
            return httpx.Response(200, content=_zip(UDIFF_CSV))
        return httpx.Response(200, text="ok")

    client = _bhav(handler, tmp_path)
    client.fetch_day(date(2026, 7, 10))
    client.fetch_day(date(2026, 7, 10))

    assert len(calls) == 1  # second read came from disk


def test_trading_days_skips_weekends():
    days = trading_days(date(2026, 7, 10), date(2026, 7, 13))  # Fri, Sat, Sun, Mon
    assert days == [date(2026, 7, 10), date(2026, 7, 13)]


# --- corporate action parsing -------------------------------------------------


def test_face_value_split_gives_the_right_ratio():
    kind, factor, review = classify(
        "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
    )
    assert kind == SPLIT
    assert factor == 5.0  # five shares replace one
    assert not review


def test_split_to_a_face_value_of_one_says_Re_not_Rs():
    """NSE writes the singular "Re 1" — the commonest split of all, and the one a naive
    "Rs"-only regex silently misses (it cost us KOTAKBANK's 5:1 on the first live run)."""
    kind, factor, review = classify(
        "Face Value Split (Sub-Division) - From Rs 5/- Per Share To Re 1/- Per Share"
    )
    assert kind == SPLIT
    assert factor == 5.0
    assert not review


def test_bonus_ratio_counts_the_shares_that_now_exist():
    assert classify("Bonus 1:1")[1] == 2.0  # one free per one held -> price halves
    assert classify("Bonus issue 1:2")[1] == 1.5  # one free per two held


def test_dividends_are_recorded_but_never_adjust_the_price():
    kind, factor, review = classify("Dividend - Rs 23 Per Share")
    assert kind == DIVIDEND
    assert factor is None
    assert not review


def test_an_unreadable_split_refuses_to_guess_and_asks_for_review():
    """The dangerous case: it IS a split, but we can't read the ratio. Never assume 1.0."""
    kind, factor, review = classify("Face Value Split - details in the notice")
    assert kind == SPLIT
    assert factor is None
    assert review is True


def test_rights_are_a_known_caveat_not_a_data_failure():
    """Rights dilute, but we don't adjust: the effect is small and needs the issue price.

    Deliberately NOT needs_review — that is reserved for splits/bonuses whose ratio we
    couldn't read. If every rights issue were an ERROR, `asr quality` would fail forever on
    any stock that ever raised rights, and an alarm that is always on gets ignored.
    """
    kind, factor, review = classify("Rights 1:4 @ Premium Rs 100")
    assert kind == RIGHTS
    assert factor is None
    assert review is False


def test_parse_actions_builds_stable_rows():
    df = parse_actions(
        [
            {
                "symbol": "reliance",
                "isin": "INE002A01018",
                "exDate": "10-Jul-2026",
                "subject": "Bonus 1:1",
            },
            {"symbol": "X", "exDate": "not-a-date", "subject": "Bonus 1:1"},  # dropped
        ]
    )
    assert len(df) == 1
    assert df.iloc[0]["symbol"] == "RELIANCE"
    assert df.iloc[0]["factor"] == 2.0
    assert df.iloc[0]["ex_date"] == pd.Timestamp("2026-07-10")


# --- adjustment ---------------------------------------------------------------


def test_prices_before_an_ex_date_are_restated_and_later_ones_are_not():
    ts = pd.Series(pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03"]))
    actions = pd.DataFrame([{"ex_date": pd.Timestamp("2026-01-03"), "factor": 2.0}])

    factors = factors_for(ts, actions)

    assert factors.tolist() == [2.0, 2.0, 1.0]  # the ex-date bar is already post-split


def test_two_actions_compound():
    ts = pd.Series(pd.to_datetime(["2026-01-01", "2026-06-01"]))
    actions = pd.DataFrame(
        [
            {"ex_date": pd.Timestamp("2026-03-01"), "factor": 2.0},
            {"ex_date": pd.Timestamp("2026-09-01"), "factor": 5.0},
        ]
    )

    assert factors_for(ts, actions).tolist() == [10.0, 5.0]


def test_an_unparsed_action_never_silently_acts_as_one():
    ts = pd.Series(pd.to_datetime(["2026-01-01"]))
    actions = pd.DataFrame([{"ex_date": pd.Timestamp("2026-02-01"), "factor": None}])

    assert factors_for(ts, actions).tolist() == [1.0]  # untouched, and flagged elsewhere


def test_adjusted_removes_the_fake_crash_and_scales_volume():
    candles = pd.DataFrame(
        {
            "ts": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "open": [1000.0, 500.0],
            "high": [1010.0, 505.0],
            "low": [990.0, 495.0],
            "close": [1000.0, 500.0],  # a 1:2 split, not a 50% crash
            "volume": [1000, 2000],
            "adj_factor": [2.0, 1.0],
        }
    )

    out = adjusted(candles)

    assert out["close"].tolist() == [500.0, 500.0]  # continuous: the crash is gone
    assert out["volume"].tolist() == [2000, 2000]  # volume scales the other way


# --- end to end ---------------------------------------------------------------


@pytest.fixture
def storage(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    db.upsert_instruments(
        pd.DataFrame(
            [
                {
                    "instrument_key": "NSE_EQ|INE002A01018",
                    "symbol": "RELIANCE",
                    "isin": "INE002A01018",
                    "name": "Reliance",
                },
                {
                    "instrument_key": "NSE_EQ|INE467B01029",
                    "symbol": "TCS",
                    "isin": "INE467B01029",
                    "name": "TCS",
                },
            ]
        )
    )
    return db


def test_ingest_filters_the_whole_market_down_to_the_universe(storage, tmp_path):
    def handler(request):
        if "BhavCopy" in str(request.url):
            return httpx.Response(200, content=_zip(UDIFF_CSV))
        return httpx.Response(200, text="ok")

    report = ingest_prices(
        date(2026, 7, 10),
        date(2026, 7, 10),
        storage=storage,
        client=_bhav(handler, tmp_path),
    )

    assert report.days == 1
    assert report.rows == 2  # NOTOURS is not in our universe
    stored = storage.read_sql("SELECT symbol FROM candles ORDER BY symbol")
    assert stored["symbol"].tolist() == ["RELIANCE", "TCS"]


def test_apply_adjustments_writes_factors_and_features_see_corrected_prices(storage):
    storage.upsert_candles(
        pd.DataFrame(
            [
                {
                    "instrument_key": "NSE_EQ|INE002A01018",
                    "symbol": "RELIANCE",
                    "ts": pd.Timestamp("2026-01-01"),
                    "open": 1000.0,
                    "high": 1000.0,
                    "low": 1000.0,
                    "close": 1000.0,
                    "volume": 100,
                    "oi": 0,
                },
                {
                    "instrument_key": "NSE_EQ|INE002A01018",
                    "symbol": "RELIANCE",
                    "ts": pd.Timestamp("2026-01-05"),
                    "open": 500.0,
                    "high": 500.0,
                    "low": 500.0,
                    "close": 500.0,
                    "volume": 200,
                    "oi": 0,
                },
            ]
        )
    )
    storage.upsert_corporate_actions(
        pd.DataFrame(
            [
                {
                    "id": "a1",
                    "symbol": "RELIANCE",
                    "isin": "INE002A01018",
                    "ex_date": pd.Timestamp("2026-01-05"),
                    "action_type": "split",
                    "subject": "Face Value Split From Rs 10 To Rs 5",
                    "factor": 2.0,
                    "needs_review": False,
                }
            ]
        )
    )

    report = apply_adjustments(storage)

    assert report.actions_applied == 1
    rows = storage.read_sql("SELECT ts, close, adj_factor FROM candles ORDER BY ts")
    assert rows["adj_factor"].tolist() == [2.0, 1.0]
    assert rows["close"].tolist() == [1000.0, 500.0]  # raw prices are never overwritten

    corrected = adjusted(rows)
    assert corrected["close"].tolist() == [500.0, 500.0]  # the series is continuous


def test_rerunning_adjust_is_idempotent(storage):
    storage.upsert_candles(
        pd.DataFrame(
            [
                {
                    "instrument_key": "NSE_EQ|INE002A01018",
                    "symbol": "RELIANCE",
                    "ts": pd.Timestamp("2026-01-01"),
                    "open": 1000.0,
                    "high": 1000.0,
                    "low": 1000.0,
                    "close": 1000.0,
                    "volume": 100,
                    "oi": 0,
                }
            ]
        )
    )
    storage.upsert_corporate_actions(
        pd.DataFrame(
            [
                {
                    "id": "a1",
                    "symbol": "RELIANCE",
                    "isin": "I",
                    "ex_date": pd.Timestamp("2026-02-01"),
                    "action_type": "split",
                    "subject": "split",
                    "factor": 2.0,
                    "needs_review": False,
                }
            ]
        )
    )

    apply_adjustments(storage)
    apply_adjustments(storage)  # must not compound into 4.0

    assert storage.read_sql("SELECT adj_factor FROM candles").iloc[0]["adj_factor"] == 2.0
