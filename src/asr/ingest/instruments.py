"""Resolve the Nifty 500 universe to Upstox instrument keys.

Two inputs, both fetched automatically (neither needs auth):
  1) ``universe/nifty500.csv`` — NSE's index constituents (Company, Symbol, ISIN Code).
  2) The Upstox NSE instrument master — maps ISIN/symbol to an ``instrument_key`` like
     ``NSE_EQ|INE848E01016``.

We deliberately AVOID the instrument-search API (known Analytics-Token quirk, error
UDAPI100050) and use the downloadable master instead.

The join is on ISIN, which is stable across NSE symbol renames; symbol is only a
fallback for rows the ISIN join misses.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pandas as pd

from .upstox_client import download_nse_master, read_master

UNIVERSE_CSV = Path("universe/nifty500.csv")
MASTER_PATH = Path("data/NSE.json.gz")

NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

#: Columns every downstream consumer can rely on.
INSTRUMENT_COLUMNS = ["instrument_key", "symbol", "isin", "name", "industry"]


def download_universe(dest: Path = UNIVERSE_CSV) -> Path:
    """Fetch NSE's current Nifty 500 constituents CSV. NSE 403s a default client UA."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}
    with httpx.Client(timeout=60.0, follow_redirects=True, headers=headers) as c:
        r = c.get(NIFTY500_URL)
        r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def load_universe(path: Path = UNIVERSE_CSV, refresh: bool = False) -> pd.DataFrame:
    """Read the Nifty 500 CSV and normalise its columns to ``symbol`` / ``isin`` / ``name``."""
    path = Path(path)
    if refresh or not path.exists():
        try:
            download_universe(path)
        except (httpx.HTTPError, OSError) as exc:
            raise FileNotFoundError(
                f"{path} missing and the NSE download failed ({exc}). Download the Nifty 500 "
                "constituents CSV by hand and save it there — see universe/README.md."
            ) from exc
    df = pd.read_csv(path)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    rename = {"isin_code": "isin", "company_name": "name"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    missing = {"symbol", "isin"} - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing required column(s): {sorted(missing)}")
    for col in ("symbol", "isin"):
        df[col] = df[col].astype(str).str.strip().str.upper()
    if "name" not in df.columns:
        df["name"] = df["symbol"]
    # NSE's CSV carries the sector. It's what lets a research pack say whether a stock is
    # falling on its own or falling with everything around it.
    if "industry" not in df.columns:
        df["industry"] = None
    else:
        df["industry"] = df["industry"].astype(str).str.strip()

    return (
        df[["symbol", "isin", "name", "industry"]]
        .drop_duplicates(subset="isin")
        .reset_index(drop=True)
    )


def load_master(path: Path = MASTER_PATH, refresh: bool = False) -> pd.DataFrame:
    """Load the Upstox NSE master, downloading it if absent (or if ``refresh``)."""
    path = Path(path)
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        download_nse_master(str(path))
    master = read_master(str(path))
    # Cash equities only: the master also carries F&O, indices and ETFs.
    eq = master[
        (master.get("segment") == "NSE_EQ") & (master.get("instrument_type") == "EQ")
    ].copy()
    for col in ("isin", "trading_symbol"):
        if col in eq.columns:
            eq[col] = eq[col].astype(str).str.strip().str.upper()
    return eq


def resolve_universe(
    universe: pd.DataFrame | None = None,
    master: pd.DataFrame | None = None,
    refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Join the universe to the master.

    Returns ``(resolved, unresolved)`` — resolved has :data:`INSTRUMENT_COLUMNS`;
    unresolved holds the universe rows that matched neither ISIN nor symbol, so a
    caller can surface them instead of silently ingesting a short universe.
    """
    uni = universe if universe is not None else load_universe(refresh=refresh)
    mst = master if master is not None else load_master(refresh=refresh)

    by_isin = mst.drop_duplicates(subset="isin").set_index("isin")["instrument_key"]
    by_symbol = mst.drop_duplicates(subset="trading_symbol").set_index("trading_symbol")[
        "instrument_key"
    ]

    out = uni.copy()
    if "industry" not in out.columns:  # a universe from an older CSV, or a hand-built one
        out["industry"] = None
    out["instrument_key"] = out["isin"].map(by_isin)
    fallback = out["instrument_key"].isna()
    out.loc[fallback, "instrument_key"] = out.loc[fallback, "symbol"].map(by_symbol)

    unresolved = out[out["instrument_key"].isna()].drop(columns=["instrument_key"])
    resolved = out.dropna(subset=["instrument_key"])[INSTRUMENT_COLUMNS].reset_index(drop=True)
    return resolved, unresolved.reset_index(drop=True)


def sync_instruments(refresh: bool = False, storage=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Resolve the universe and persist it to the ``instruments`` table."""
    from ..storage.base import get_storage

    resolved, unresolved = resolve_universe(refresh=refresh)
    (storage or get_storage()).upsert_instruments(resolved)
    return resolved, unresolved


def instrument_keys(limit: int | None = None, storage=None) -> list[str]:
    """The stored universe, as instrument keys — the input to any ingest run."""
    from ..storage.base import get_storage

    df = (storage or get_storage()).read_sql("SELECT instrument_key FROM instruments ORDER BY 1")
    keys = df["instrument_key"].tolist()
    return keys[:limit] if limit else keys
