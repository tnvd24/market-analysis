"""Upstox: instrument master + shared error types.

**The Upstox market-data client is gone.** Prices now come from NSE bhavcopy
(`ingest/bhavcopy.py`) — the exchange's own end-of-day file. That needs no token, no
account and no yearly renewal, gives the whole market in one request per day, and pairs
with NSE's corporate-actions feed so we adjust for splits *ourselves*, deterministically,
instead of trusting a broker's undocumented adjustment. See docs/decisions.md.

What remains here is what is still genuinely useful and needs no auth:

* the **instrument master** — the only source of Upstox ``instrument_key``s, which the
  optional Upstox news feed is keyed by;
* the **error types**, shared with that news client.
"""

from __future__ import annotations

import gzip
import json

import httpx
import pandas as pd

#: Instrument master (no auth). We join the universe against this rather than calling the
#: instrument-search API, which rejects Analytics Tokens (error UDAPI100050).
NSE_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"


class UpstoxError(RuntimeError):
    """Non-retryable API failure (bad token, unknown instrument, malformed request)."""


class UpstoxTransientError(RuntimeError):
    """Rate limit / server hiccup / network blip — worth retrying."""


def download_nse_master(dest: str) -> str:
    """Fetch the Upstox NSE instrument master (gzipped JSON). No auth required."""
    with httpx.Client(timeout=120.0, follow_redirects=True) as c:
        r = c.get(NSE_MASTER_URL)
        r.raise_for_status()
        with open(dest, "wb") as fh:
            fh.write(r.content)
    return dest


def read_master(path: str) -> pd.DataFrame:
    """Read a master file written by :func:`download_nse_master` (or a plain .json)."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return pd.DataFrame(json.load(fh))
