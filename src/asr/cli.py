"""Typer CLI. Commands fill in per phase."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta

import typer
from rich import print
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

app = typer.Typer(help="Agentic stock research (research-only).")
ingest = typer.Typer(help="Phase 2: data ingestion")
features = typer.Typer(help="Phase 3: deterministic indicators")
app.add_typer(ingest, name="ingest")
app.add_typer(features, name="features")


def _progress() -> Progress:
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
    )


def _select_keys(storage, symbols: list[str] | None, limit: int | None) -> list[str]:
    df = storage.read_sql("SELECT instrument_key, symbol FROM instruments ORDER BY symbol")
    if df.empty:
        raise typer.BadParameter(
            "No instruments stored. Run `asr ingest instruments` first "
            "(needs universe/nifty500.csv)."
        )
    if symbols:
        wanted = {s.strip().upper() for s in symbols}
        df = df[df["symbol"].isin(wanted)]
        missing = wanted - set(df["symbol"])
        if missing:
            print(f"[yellow]Not in the stored universe: {', '.join(sorted(missing))}[/yellow]")
    keys = df["instrument_key"].tolist()
    return keys[:limit] if limit else keys


def _count(storage, sql: str) -> int:
    return int(storage.read_sql(sql).iloc[0]["n"])


def _report(report) -> None:
    print(f"[green]{report.summary()}[/green]")
    for key, err in list(report.failures.items())[:10]:
        print(f"[red]  {key}: {err}[/red]")


@contextmanager
def _handled():
    """Config problems (no token, no universe) are operator errors, not crashes."""
    from .ingest.upstox_client import UpstoxError

    try:
        yield
    except (UpstoxError, FileNotFoundError) as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


@ingest.command("instruments")
def ingest_instruments(
    refresh: bool = typer.Option(False, help="Re-download the NSE universe + Upstox master."),
):
    """Resolve the Nifty 500 -> Upstox instrument keys and store them."""
    from .ingest.instruments import sync_instruments

    with _handled():
        resolved, unresolved = sync_instruments(refresh=refresh)
    print(f"[green]Resolved {len(resolved)} instruments[/green]")
    if len(unresolved):
        print(f"[yellow]{len(unresolved)} unresolved (no ISIN or symbol match):[/yellow]")
        print(unresolved[["symbol", "isin"]].head(20).to_string(index=False))


@ingest.command("backfill")
def ingest_backfill(
    years: int = typer.Option(3, help="Years of daily history to pull."),
    limit: int | None = typer.Option(None, help="Only the first N instruments (smoke runs)."),
    symbol: list[str] = typer.Option(None, help="Restrict to these NSE symbols."),
):
    """Historical daily OHLCV for the stored universe -> candles."""
    from .ingest.ohlcv import backfill
    from .storage.base import get_storage

    storage = get_storage()
    keys = _select_keys(storage, symbol, limit)
    print(f"Backfilling {len(keys)} instruments, {years}y of daily candles...")

    with _handled(), _progress() as bar:
        task = bar.add_task("backfill", total=len(keys))
        report = backfill(
            keys, years=years, storage=storage, on_progress=lambda *_: bar.advance(task)
        )
        bar.update(task, completed=len(keys))
    _report(report)


@ingest.command("daily")
def ingest_daily(
    limit: int | None = typer.Option(None, help="Only the first N instruments."),
    symbol: list[str] = typer.Option(None, help="Restrict to these NSE symbols."),
):
    """Incremental pull: only the candles newer than what's already stored."""
    from .ingest.ohlcv import daily_incremental
    from .storage.base import get_storage

    storage = get_storage()
    keys = _select_keys(storage, symbol, limit)

    with _handled(), _progress() as bar:
        task = bar.add_task("daily", total=len(keys))
        report = daily_incremental(keys, storage=storage, on_progress=lambda *_: bar.advance(task))
        bar.update(task, completed=len(keys))
    _report(report)


@ingest.command("status")
def ingest_status():
    """What's in the warehouse: universe size, candle coverage, date range."""
    from .storage.base import get_storage

    storage = get_storage()
    n_inst = storage.read_sql("SELECT COUNT(*) AS n FROM instruments").iloc[0]["n"]
    stats = storage.read_sql(
        "SELECT COUNT(*) AS n_rows, COUNT(DISTINCT instrument_key) AS covered, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM candles"
    ).iloc[0]

    t = Table(title="Ingest status", show_header=False)
    t.add_row("instruments (universe)", str(int(n_inst)))
    t.add_row("instruments with candles", str(int(stats["covered"])))
    t.add_row("candle rows", f"{int(stats['n_rows']):,}")
    t.add_row("date range", f"{stats['first_ts']} → {stats['last_ts']}")
    print(t)


@ingest.command("smoke")
def ingest_smoke(instrument_key: str = "NSE_EQ|INE848E01016", days: int = 30):
    """Pull recent daily candles for one instrument and store them. Auth check."""
    from .ingest.ohlcv import ingest_range
    from .storage.base import get_storage

    storage = get_storage()
    to_d = date.today()
    with _handled():
        report = ingest_range([instrument_key], to_d - timedelta(days=days), to_d, storage=storage)
    _report(report)
    print(
        storage.read_sql(
            "SELECT * FROM candles WHERE instrument_key = "
            f"'{instrument_key}' ORDER BY ts DESC LIMIT 5"  # noqa: S608
        )
    )


@features.command("build")
def features_build(
    limit: int | None = typer.Option(None, help="Only the first N instruments."),
    symbol: list[str] = typer.Option(None, help="Restrict to these NSE symbols."),
):
    """Compute indicators over stored candles -> features."""
    from .features.build import build_features
    from .storage.base import get_storage

    storage = get_storage()
    keys = _select_keys(storage, symbol, limit) if (symbol or limit) else None
    if keys is None:
        total = _count(storage, "SELECT COUNT(DISTINCT instrument_key) AS n FROM candles")
    else:
        total = len(keys)

    if not total:
        print("[yellow]No candles stored yet. Run `asr ingest backfill` first.[/yellow]")
        raise typer.Exit(1)

    with _progress() as bar:
        task = bar.add_task("features", total=total)
        report = build_features(keys, storage=storage, on_progress=lambda *_: bar.advance(task))
        bar.update(task, completed=total)

    print(f"[green]{report.summary()}[/green]")
    if report.thin:
        print(f"[yellow]Thin history (slow MAs NULL): {', '.join(report.thin[:10])}[/yellow]")


@features.command("show")
def features_show(symbol: str, days: int = typer.Option(10, help="Rows to show.")):
    """Spot-check one ticker's latest indicators against its candles."""
    from .storage.base import get_storage

    df = get_storage().read_sql(
        "SELECT ts, rsi_14, macd, macd_signal, sma_20, sma_50, sma_200, atr_14, rel_volume "
        f"FROM features WHERE symbol = '{symbol.strip().upper()}' "  # noqa: S608
        f"ORDER BY ts DESC LIMIT {int(days)}"
    )
    if df.empty:
        print(f"[yellow]No features for {symbol}. Run `asr features build`.[/yellow]")
        raise typer.Exit(1)
    print(df.to_string(index=False))


@app.command("info")
def info():
    """Show effective config (no secrets)."""
    from .config import settings

    print(
        {
            "storage_backend": settings.storage_backend,
            "duckdb_path": settings.duckdb_path,
            "model": settings.anthropic_model,
            "upstox_token_set": bool(settings.upstox_access_token),
            "anthropic_key_set": bool(settings.anthropic_api_key),
        }
    )


if __name__ == "__main__":
    app()
