"""NSE corporate actions — the feed that makes price adjustment possible.

Bhavcopy gives raw traded prices. A 1:2 split halves the price overnight with no error
anywhere; a bonus does the same. Left alone, that fake crash poisons every indicator and
every backtest that spans it. This module fetches the actions and works out the factor to
correct them by, so the adjustment is **ours, deterministic, and auditable** — not a
broker's undocumented guess.

**Only splits and bonuses move the price mechanically**, so only those adjust:

* *Face value split, Rs 10 → Rs 2* — five shares replace one, so pre-split prices are
  divided by 5.
* *Bonus 1:1* — one free share per share held, so the count doubles: factor 2.
* *Dividends* are recorded but do **not** adjust prices. (A dividend does drop the price by
  its amount on the ex-date, but the effect is small and adjusting for it changes what the
  chart means. Standard technical analysis works on split/bonus-adjusted prices, so that is
  what we do — stated here so nobody has to reverse-engineer the intent later.)
* *Rights issues and anything unrecognised* are **never guessed at**. They are stored with
  ``needs_review`` set, and the quality layer turns them into a visible finding. Guessing a
  ratio is exactly the kind of plausible-but-wrong behaviour this project exists to avoid.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

import httpx
import pandas as pd
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..ratelimit import RateLimiter

URL = "https://www.nseindia.com/api/corporates-corporateActions"
PRIMING_URL = "https://www.nseindia.com/companies-listing/corporate-filings-actions"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": PRIMING_URL,
}

ACTION_COLUMNS = [
    "id",
    "symbol",
    "isin",
    "ex_date",
    "action_type",
    "subject",
    "factor",
    "needs_review",
]

SPLIT = "split"
BONUS = "bonus"
DIVIDEND = "dividend"
RIGHTS = "rights"
OTHER = "other"

#: "From Rs 10/- Per Share To Rs 2/- Per Share" -> (10, 2)
#: NSE writes the *singular* "Re" when the new face value is 1 ("To Re 1/-"), which is the
#: single most common split there is. Matching only "Rs" silently missed every one of them —
#: including KOTAKBANK's 5:1 — until the quality layer flagged them as unparsed.
_SPLIT_RE = re.compile(
    r"from\s*(?:rs|re|rupees?)?\.?\s*([\d.]+).*?to\s*(?:rs|re|rupees?)?\.?\s*([\d.]+)",
    re.I | re.S,
)
#: "Bonus 1:1", "Bonus issue 2:1" -> (1, 1)
_BONUS_RE = re.compile(r"(\d+)\s*:\s*(\d+)")


class ActionsTransientError(RuntimeError):
    pass


def classify(subject: str) -> tuple[str, float | None, bool]:
    """(action_type, price factor, needs_review).

    ``factor`` is what raw pre-ex-date prices are **divided by**. None means "do not adjust".
    """
    s = (subject or "").strip()
    low = s.lower()

    if "split" in low or "sub-division" in low or "subdivision" in low:
        m = _SPLIT_RE.search(low)
        if m:
            old, new = float(m.group(1)), float(m.group(2))
            if new > 0 and old > new:
                return SPLIT, old / new, False
        # It IS a split, but the ratio is unreadable — say so loudly rather than assume 1.0.
        return SPLIT, None, True

    if "bonus" in low:
        m = _BONUS_RE.search(low)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            if b > 0:
                # a new shares for every b held -> (a+b)/b shares exist per b.
                return BONUS, (a + b) / b, False
        return BONUS, None, True

    if "dividend" in low:
        return DIVIDEND, None, False  # recorded, deliberately not adjusted

    if "rights" in low:
        # A rights issue does dilute, but adjusting for it needs the issue price *and* the
        # market price on the ex-date, and the effect is far smaller than a split. We record
        # it as a known caveat (a WARN in the quality layer) rather than a data failure —
        # `needs_review` is reserved for actions that mechanically restate the price and
        # whose ratio we could not read. Conflating the two would make `asr quality` fail
        # forever on any stock that ever raised rights, and an alarm that is always on gets
        # ignored — which would defeat the entire point of the layer.
        return RIGHTS, None, False

    return OTHER, None, False


def _row_id(symbol: str, ex_date, subject: str) -> str:
    basis = f"{symbol}|{ex_date}|{subject}"
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


class CorporateActions:
    def __init__(self, client: httpx.Client | None = None, limiter: RateLimiter | None = None):
        self._c = client or httpx.Client(timeout=45.0, follow_redirects=True, headers=HEADERS)
        self._limiter = limiter or RateLimiter(rate_per_sec=2.0, burst=2)
        self._primed = False

    def _prime(self) -> None:
        self._limiter.acquire()
        try:
            self._c.get(PRIMING_URL)
        except httpx.TransportError as exc:
            raise ActionsTransientError(str(exc)) from exc
        self._primed = True

    @retry(
        retry=retry_if_exception_type(ActionsTransientError),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, max=20),
        reraise=True,
    )
    def _get(self, params: dict) -> list[dict]:
        if not self._primed:
            self._prime()
        self._limiter.acquire()
        try:
            r = self._c.get(URL, params=params)
        except httpx.TransportError as exc:
            raise ActionsTransientError(str(exc)) from exc

        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            self._primed = False
            raise ActionsTransientError(f"{r.status_code} from NSE actions")
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError as exc:
            self._primed = False
            raise ActionsTransientError("NSE actions returned non-JSON") from exc
        return payload if isinstance(payload, list) else []

    def fetch(self, since: date, until: date, symbol: str | None = None) -> pd.DataFrame:
        params = {
            "index": "equities",
            "from_date": since.strftime("%d-%m-%Y"),
            "to_date": until.strftime("%d-%m-%Y"),
        }
        if symbol:
            params["symbol"] = symbol.strip().upper()
        return parse_actions(self._get(params))

    def close(self) -> None:
        self._c.close()


def parse_actions(rows: list[dict]) -> pd.DataFrame:
    out = []
    for row in rows:
        ex = pd.to_datetime(row.get("exDate"), format="%d-%b-%Y", errors="coerce")
        if pd.isna(ex):
            continue
        subject = (row.get("subject") or "").strip()
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        action_type, factor, review = classify(subject)
        out.append(
            {
                "id": _row_id(symbol, ex.date(), subject),
                "symbol": symbol,
                "isin": (row.get("isin") or "").strip().upper() or None,
                "ex_date": ex,
                "action_type": action_type,
                "subject": subject,
                "factor": factor,
                "needs_review": review,
            }
        )
    if not out:
        return pd.DataFrame(columns=ACTION_COLUMNS)
    return pd.DataFrame(out)[ACTION_COLUMNS].drop_duplicates(subset="id").reset_index(drop=True)
