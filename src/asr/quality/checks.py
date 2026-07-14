"""Data-quality assertions: turn silent corruption into loud errors.

The premise: **in market data, the dangerous failures don't throw.** A crash is easy — you
see it, you fix it. What wrecks a research system is plausible-but-wrong data that runs
clean: an unadjusted split halving a price overnight, a stale candle from a holiday, a
timezone bug shifting every bar by a day, a symbol quietly dropping out of the universe.
None of those raise an exception. They just produce a confident, wrong RSI.

So every one of those states gets an explicit assertion here. "We look into errors" is only
a safe policy once the silent failures *are* errors.

Severity is about trust, not tidiness:
  ERROR — do not trust downstream numbers computed from this. Exit non-zero.
  WARN  — suspicious; a human should look, but it may be legitimate.
  INFO  — worth knowing, no action implied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from ..storage.base import StorageAdapter, get_storage

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"

#: A close-to-close move this large is far more likely a corporate action the data did not
#: adjust for than a real day's trading. 20% is above almost any single-session move in a
#: large/mid cap, and well below a 1:2 split (-50%) or a 1:1 bonus (-50%).
SPLIT_JUMP_PCT = 0.20

#: Calendar days without a candle before we call the series broken. NSE closes for weekends
#: plus the odd multi-day festival cluster, so a legitimate gap can reach ~4-5 days.
MAX_GAP_DAYS = 6

#: How stale the newest candle may be before the warehouse counts as out of date.
MAX_STALENESS_DAYS = 5


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    symbol: str | None
    detail: str

    def __str__(self) -> str:
        who = f" [{self.symbol}]" if self.symbol else ""
        return f"{self.severity:5s} {self.check}{who}: {self.detail}"


@dataclass
class QualityReport:
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    def for_symbol(self, symbol: str) -> list[Finding]:
        return [f for f in self.findings if f.symbol == symbol]

    def summary(self) -> str:
        if not self.findings:
            return "all checks passed"
        return (
            f"{len(self.errors)} errors, {len(self.warnings)} warnings, "
            f"{len(self.findings)} findings total"
        )


def _q(storage: StorageAdapter, sql: str) -> pd.DataFrame:
    return storage.read_sql(sql)


# --- individual checks --------------------------------------------------------


def check_universe_resolved(
    storage: StorageAdapter, universe: pd.DataFrame | None = None
) -> list[Finding]:
    """Every universe symbol must map to an instrument key.

    A symbol that silently fails to resolve doesn't error — it just quietly isn't researched.
    """
    from ..ingest.instruments import load_universe

    if universe is None:
        try:
            universe = load_universe()
        except FileNotFoundError as exc:
            return [Finding(WARN, "universe_resolved", None, str(exc))]

    stored = _q(storage, "SELECT symbol FROM instruments")
    missing = set(universe["symbol"]) - set(stored["symbol"])
    if not missing:
        return []
    sample = ", ".join(sorted(missing)[:8])
    return [
        Finding(
            ERROR,
            "universe_resolved",
            None,
            f"{len(missing)} of {len(universe)} universe symbols are not in `instruments` "
            f"({sample}{'...' if len(missing) > 8 else ''}). They will be silently unresearched.",
        )
    ]


def check_ohlc_sanity(storage: StorageAdapter) -> list[Finding]:
    """high >= max(open, close), low <= min(open, close), prices positive.

    Violations mean the row is not a candle, whatever it claims to be.
    """
    bad = _q(
        storage,
        """
        SELECT symbol, COUNT(*) AS n FROM candles
        WHERE high < low
           OR high < open OR high < close
           OR low  > open OR low  > close
           OR open <= 0 OR close <= 0 OR high <= 0 OR low <= 0
           OR volume < 0
        GROUP BY 1 ORDER BY n DESC
        """,
    )
    return [
        Finding(
            ERROR,
            "ohlc_sanity",
            r.symbol,
            f"{int(r.n)} impossible candles (OHLC violates its own bounds)",
        )
        for r in bad.itertuples()
    ]


def check_price_jumps(storage: StorageAdapter, threshold: float = SPLIT_JUMP_PCT) -> list[Finding]:
    """Overnight moves too large to be trading — almost certainly an unadjusted split/bonus.

    **This is the check that answers the open question** of whether Upstox candles are
    corporate-action adjusted. If they are, this stays quiet. If they aren't, every affected
    stock lights up here instead of silently poisoning its indicators and backtests.
    """
    jumps = _q(
        storage,
        f"""
        WITH stepped AS (
            SELECT symbol, ts, close,
                   LAG(close) OVER (PARTITION BY instrument_key ORDER BY ts) AS prev_close
            FROM candles
        )
        SELECT symbol, ts, prev_close, close,
               (close - prev_close) / prev_close AS move
        FROM stepped
        WHERE prev_close IS NOT NULL AND prev_close > 0
          AND ABS((close - prev_close) / prev_close) > {threshold}
        ORDER BY ABS((close - prev_close) / prev_close) DESC
        """,  # noqa: S608
    )
    out = []
    for r in jumps.itertuples():
        pct = r.move * 100
        # A move that lands near a clean split ratio is the giveaway.
        hint = " — close to a 1:2 split/bonus ratio" if -60 < pct < -40 else ""
        out.append(
            Finding(
                WARN,
                "price_jump",
                r.symbol,
                f"{pct:+.1f}% overnight on {pd.Timestamp(r.ts).date()} "
                f"({r.prev_close:.2f} -> {r.close:.2f}){hint}. "
                "Verify against a corporate action before trusting this stock's indicators.",
            )
        )
    return out


def check_gaps(storage: StorageAdapter, max_gap_days: int = MAX_GAP_DAYS) -> list[Finding]:
    """Missing sessions. A hole in the series shifts every window that spans it."""
    gaps = _q(
        storage,
        f"""
        WITH stepped AS (
            SELECT symbol, ts,
                   LAG(ts) OVER (PARTITION BY instrument_key ORDER BY ts) AS prev_ts
            FROM candles
        )
        SELECT symbol, prev_ts, ts, DATE_DIFF('day', prev_ts, ts) AS gap_days
        FROM stepped
        WHERE prev_ts IS NOT NULL AND DATE_DIFF('day', prev_ts, ts) > {int(max_gap_days)}
        ORDER BY gap_days DESC
        """,  # noqa: S608
    )
    return [
        Finding(
            WARN,
            "candle_gap",
            r.symbol,
            f"{int(r.gap_days)}-day hole between {pd.Timestamp(r.prev_ts).date()} and "
            f"{pd.Timestamp(r.ts).date()} (holidays explain ~4-5; longer suggests missing data)",
        )
        for r in gaps.itertuples()
    ]


def check_staleness(
    storage: StorageAdapter, as_of: date | None = None, max_days: int = MAX_STALENESS_DAYS
) -> list[Finding]:
    """Is the warehouse current? A stale candle looks exactly like a fresh one."""
    as_of = as_of or date.today()
    row = _q(storage, "SELECT MAX(ts) AS last_ts FROM candles").iloc[0]
    if pd.isna(row["last_ts"]):
        return [Finding(ERROR, "staleness", None, "no candles stored at all")]

    last = pd.Timestamp(row["last_ts"]).date()
    age = (as_of - last).days
    if age <= max_days:
        return []
    return [
        Finding(
            ERROR if age > max_days * 3 else WARN,
            "staleness",
            None,
            f"newest candle is {age} days old ({last}). Run `asr ingest daily`.",
        )
    ]


def check_feature_coverage(storage: StorageAdapter) -> list[Finding]:
    """Every candle should have a feature row. A shortfall means indicators are out of date."""
    rows = _q(
        storage,
        """
        SELECT c.symbol,
               COUNT(DISTINCT c.ts) AS candles,
               COUNT(DISTINCT f.ts) AS feats
        FROM candles c
        LEFT JOIN features f
          ON f.instrument_key = c.instrument_key AND f.ts = c.ts
        GROUP BY 1
        HAVING COUNT(DISTINCT f.ts) < COUNT(DISTINCT c.ts)
        """,
    )
    return [
        Finding(
            WARN,
            "feature_coverage",
            r.symbol,
            f"{int(r.candles - r.feats)} candles have no features. Run `asr features build`.",
        )
        for r in rows.itertuples()
    ]


def check_duplicate_candles(storage: StorageAdapter) -> list[Finding]:
    """Two rows for one (instrument, ts). The primary key should make this impossible —
    which is exactly why it is worth asserting: if it ever fires, an adapter is broken."""
    dupes = _q(
        storage,
        """
        SELECT symbol, COUNT(*) AS n FROM (
            SELECT instrument_key, symbol, ts, COUNT(*) AS c
            FROM candles GROUP BY 1,2,3 HAVING COUNT(*) > 1
        ) GROUP BY 1
        """,
    )
    return [
        Finding(ERROR, "duplicate_candles", r.symbol, f"{int(r.n)} duplicated timestamps")
        for r in dupes.itertuples()
    ]


def check_future_timestamps(storage: StorageAdapter, as_of: date | None = None) -> list[Finding]:
    """Candles dated in the future — the signature of a timezone bug.

    IST is UTC+5:30, so a UTC/IST mix-up can push an evening bar onto tomorrow's date.
    """
    as_of = as_of or date.today()
    cutoff = as_of + timedelta(days=1)
    rows = _q(
        storage,
        f"SELECT symbol, COUNT(*) AS n FROM candles WHERE ts >= '{cutoff.isoformat()}' GROUP BY 1",  # noqa: S608
    )
    return [
        Finding(
            ERROR,
            "future_timestamp",
            r.symbol,
            f"{int(r.n)} candles dated in the future (timezone bug? every bar may be shifted)",
        )
        for r in rows.itertuples()
    ]


def run_checks(
    storage: StorageAdapter | None = None,
    as_of: date | None = None,
    universe: pd.DataFrame | None = None,
) -> QualityReport:
    """Run every assertion. Order of the list is not significant; findings are sorted."""
    storage = storage or get_storage()
    findings: list[Finding] = [
        *check_universe_resolved(storage, universe),
        *check_ohlc_sanity(storage),
        *check_duplicate_candles(storage),
        *check_future_timestamps(storage, as_of),
        *check_price_jumps(storage),
        *check_gaps(storage),
        *check_staleness(storage, as_of),
        *check_feature_coverage(storage),
    ]
    order = {ERROR: 0, WARN: 1, INFO: 2}
    findings.sort(key=lambda f: (order[f.severity], f.check, f.symbol or ""))
    return QualityReport(findings)
