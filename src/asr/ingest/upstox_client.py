"""Upstox market-data client (research-only).

Auth: uses the Analytics Token (1-year, read-only) from settings.upstox_access_token.
No order-placement methods live here on purpose — this system never trades.

NOTE (Phase 2): v2 historical-candle is documented and working today but is being
deprecated toward v3. The `day` endpoint below is the known-good v2 path. When we
build real ingestion we'll confirm the exact v3 path / switch to the official SDK's
HistoryV3Api. Response candle shape: [ts, open, high, low, close, volume, oi].
"""

from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings

BASE = "https://api.upstox.com"


class UpstoxClient:
    def __init__(self, token: str | None = None):
        self.token = token or settings.upstox_access_token
        if not self.token:
            raise RuntimeError("No UPSTOX_ACCESS_TOKEN set. Generate an Analytics Token.")
        self._c = httpx.Client(
            base_url=BASE,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=30.0,
        )

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, max=20))
    def daily_candles(self, instrument_key: str, from_date: date, to_date: date) -> pd.DataFrame:
        # v2: /v2/historical-candle/{instrument_key}/day/{to}/{from}
        url = f"/v2/historical-candle/{instrument_key}/day/{to_date}/{from_date}"
        r = self._c.get(url)
        r.raise_for_status()
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "oi"])
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume", "oi"])
        df["ts"] = pd.to_datetime(df["ts"])
        df.insert(0, "instrument_key", instrument_key)
        return df.sort_values("ts").reset_index(drop=True)

    def close(self):
        self._c.close()
