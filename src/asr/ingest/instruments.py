"""Resolve the Nifty 500 universe to Upstox instrument keys.

Two inputs:
  1) universe/nifty500.csv  -> NSE index constituents (Symbol, ISIN, Company).
     Source: NSE index constituents CSV (you'll drop this file in — see README).
  2) Upstox instrument master -> maps ISIN/symbol to instrument_key like
     "NSE_EQ|INE848E01016". Download the NSE master (JSON.gz) from Upstox docs.

We deliberately AVOID the instrument-search API (known Analytics-Token quirk,
error UDAPI100050) and use the downloadable master instead.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

UNIVERSE_CSV = Path("universe/nifty500.csv")


def load_universe() -> pd.DataFrame:
    if not UNIVERSE_CSV.exists():
        raise FileNotFoundError(
            "universe/nifty500.csv missing. Add the NSE Nifty 500 constituents CSV "
            "(columns include Symbol and ISIN Code)."
        )
    df = pd.read_csv(UNIVERSE_CSV)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


def resolve_instrument_keys(master_path: str) -> pd.DataFrame:
    """Join universe ISINs against the Upstox NSE instrument master.
    master_path: path to the downloaded Upstox NSE master (json/json.gz)."""
    uni = load_universe()
    master = pd.read_json(master_path)
    # Upstox master exposes instrument_key + isin (+ trading_symbol). Join on ISIN.
    isin_col = "isin_code" if "isin_code" in uni.columns else "isin"
    merged = uni.merge(
        master[["instrument_key", "isin", "trading_symbol"]],
        left_on=isin_col,
        right_on="isin",
        how="left",
    )
    missing = merged["instrument_key"].isna().sum()
    if missing:
        print(f"[warn] {missing} symbols did not resolve to an instrument_key")
    return merged
