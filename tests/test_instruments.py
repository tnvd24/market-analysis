import httpx
import pandas as pd
import pytest

from asr.ingest import instruments as inst
from asr.ingest.instruments import load_universe, resolve_universe

MASTER = pd.DataFrame(
    [
        # (segment, instrument_type) gate out everything that isn't a cash equity
        {
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE002A01018",
            "trading_symbol": "RELIANCE",
            "isin": "INE002A01018",
        },
        {
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE467B01029",
            "trading_symbol": "TCS",
            "isin": "INE467B01029",
        },
        {
            "segment": "NSE_EQ",
            "instrument_type": "EQ",
            "instrument_key": "NSE_EQ|INE040A01034",
            "trading_symbol": "HDFCBANK",
            "isin": "INE040A01034",
        },
    ]
)


def _write_universe(tmp_path, rows):
    p = tmp_path / "nifty500.csv"
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_load_universe_normalises_nse_column_names(tmp_path):
    p = _write_universe(
        tmp_path,
        [
            {
                "Company Name": "Reliance Industries",
                "Symbol": " reliance ",
                "ISIN Code": "ine002a01018",
            }
        ],
    )
    uni = load_universe(p)

    assert list(uni.columns) == ["symbol", "isin", "name", "industry"]
    assert uni.iloc[0]["symbol"] == "RELIANCE"  # trimmed + upcased
    assert uni.iloc[0]["isin"] == "INE002A01018"


def test_load_universe_fetches_the_csv_when_it_is_absent(tmp_path, monkeypatch):
    target = tmp_path / "nifty500.csv"

    def fake_download(dest):
        pd.DataFrame(
            [{"Company Name": "TCS", "Symbol": "TCS", "ISIN Code": "INE467B01029"}]
        ).to_csv(dest, index=False)
        return dest

    monkeypatch.setattr(inst, "download_universe", fake_download)
    assert load_universe(target).iloc[0]["symbol"] == "TCS"


def test_load_universe_falls_back_to_a_clear_error_if_nse_is_unreachable(tmp_path, monkeypatch):
    def boom(dest):
        raise httpx.ConnectError("NSE unreachable")

    monkeypatch.setattr(inst, "download_universe", boom)
    with pytest.raises(FileNotFoundError, match="download failed"):
        load_universe(tmp_path / "nope.csv")


def test_resolve_joins_on_isin():
    uni = pd.DataFrame(
        [
            {"symbol": "RELIANCE", "isin": "INE002A01018", "name": "Reliance"},
            {"symbol": "TCS", "isin": "INE467B01029", "name": "TCS"},
        ]
    )
    resolved, unresolved = resolve_universe(uni, MASTER)

    assert unresolved.empty
    assert resolved.set_index("symbol").loc["RELIANCE", "instrument_key"] == "NSE_EQ|INE002A01018"


def test_resolve_falls_back_to_symbol_when_isin_is_stale():
    uni = pd.DataFrame([{"symbol": "HDFCBANK", "isin": "INE_STALE_0001", "name": "HDFC Bank"}])
    resolved, unresolved = resolve_universe(uni, MASTER)

    assert unresolved.empty
    assert resolved.iloc[0]["instrument_key"] == "NSE_EQ|INE040A01034"


def test_unresolved_rows_are_reported_not_dropped_silently():
    uni = pd.DataFrame(
        [
            {"symbol": "TCS", "isin": "INE467B01029", "name": "TCS"},
            {"symbol": "DELISTED", "isin": "INE999Z01011", "name": "Gone Ltd"},
        ]
    )
    resolved, unresolved = resolve_universe(uni, MASTER)

    assert len(resolved) == 1
    assert unresolved["symbol"].tolist() == ["DELISTED"]
