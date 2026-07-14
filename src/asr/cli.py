"""Typer CLI. Commands fill in per phase."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta

import pandas as pd
import typer
from rich import print
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

app = typer.Typer(help="Agentic stock research (research-only).")
ingest = typer.Typer(help="Phase 2: data ingestion")
features = typer.Typer(help="Phase 3: deterministic indicators")
news = typer.Typer(help="Phase 4: news & filings")
pack = typer.Typer(help="Research packs (the handoff to a human/chat read)")
app.add_typer(ingest, name="ingest")
app.add_typer(features, name="features")
app.add_typer(news, name="news")
app.add_typer(pack, name="pack")


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
    """Setup problems (no universe, no instruments) are operator errors, not crashes."""
    from .ingest.upstox_client import UpstoxError

    try:
        yield
    except (UpstoxError, FileNotFoundError, LookupError) as exc:
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


@ingest.command("prices")
def ingest_prices_cmd(
    years: int = typer.Option(3, help="Years of history to backfill."),
    since: str | None = typer.Option(None, help="Start date (YYYY-MM-DD). Overrides --years."),
    incremental: bool = typer.Option(False, help="Only the days missing since the last candle."),
):
    """Daily OHLCV from NSE bhavcopy -> candles. No token needed."""
    from .ingest.prices import backfill, daily, ingest_prices
    from .storage.base import get_storage

    storage = get_storage()
    with _handled():
        if incremental:
            report = daily(storage=storage)
        elif since:
            start = date.fromisoformat(since)
            print(f"Pulling bhavcopy from {start} ({(date.today() - start).days} calendar days)...")
            report = ingest_prices(start, storage=storage)
        else:
            print(f"Backfilling {years}y of bhavcopy (one request per trading day)...")
            report = backfill(years=years, storage=storage)
    _report(report)
    print("[dim]Next: `asr ingest actions` then `asr ingest adjust`.[/dim]")


@ingest.command("actions")
def ingest_actions(years: int = typer.Option(3, help="Years of corporate actions to pull.")):
    """Splits, bonuses and dividends from NSE -> corporate_actions."""
    from .ingest.corporate_actions import CorporateActions
    from .storage.base import get_storage

    storage = get_storage()
    until = date.today()
    start = until - timedelta(days=365 * years)
    client = CorporateActions()

    # NSE caps how wide a single query may be, so walk the range in quarters.
    frames, cursor = [], start
    with _progress() as bar:
        total = max(1, (until - start).days // 90 + 1)
        task = bar.add_task("actions", total=total)
        while cursor < until:
            end = min(cursor + timedelta(days=90), until)
            frames.append(client.fetch(cursor, end))
            bar.advance(task)
            cursor = end + timedelta(days=1)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        print("[yellow]No corporate actions returned.[/yellow]")
        return
    df = df.drop_duplicates(subset="id")
    n = storage.upsert_corporate_actions(df)

    kinds = df["action_type"].value_counts().to_dict()
    print(f"[green]Stored {n} corporate actions[/green] {kinds}")
    review = df[df["needs_review"]]
    if len(review):
        print(f"[red]{len(review)} could not be parsed into a ratio (prices NOT adjusted):[/red]")
        for r in review.head(10).itertuples():
            print(f"[red]  {r.symbol} {pd.Timestamp(r.ex_date).date()}: {r.subject}[/red]")


@ingest.command("adjust")
def ingest_adjust():
    """Recompute split/bonus adjustment factors over the stored candles."""
    from .ingest.adjust import apply_adjustments
    from .storage.base import get_storage

    report = apply_adjustments(get_storage())
    print(f"[green]{report.summary()}[/green]")
    for item in report.needs_review[:10]:
        print(f"[red]  needs review: {item}[/red]")
    print("[dim]Rebuild indicators on the corrected prices: `asr features build`.[/dim]")


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
    acts = storage.read_sql(
        "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE needs_review) AS review "
        "FROM corporate_actions"
    ).iloc[0]
    adj = storage.read_sql(
        "SELECT COUNT(*) AS n FROM candles WHERE adj_factor IS NOT NULL AND adj_factor <> 1.0"
    ).iloc[0]["n"]

    t = Table(title="Warehouse status", show_header=False)
    t.add_row("instruments (universe)", str(int(n_inst)))
    t.add_row("instruments with candles", str(int(stats["covered"])))
    t.add_row("candle rows", f"{int(stats['n_rows']):,}")
    t.add_row("date range", f"{stats['first_ts']} → {stats['last_ts']}")
    t.add_row("corporate actions", f"{int(acts['n'])} ({int(acts['review'])} need review)")
    t.add_row("adjusted candles", f"{int(adj):,}")
    print(t)


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


@news.command("fetch")
def news_fetch(
    days: int = typer.Option(30, help="Lookback window."),
    limit: int | None = typer.Option(None, help="Only the first N instruments."),
    symbol: list[str] = typer.Option(None, help="Restrict to these NSE symbols."),
    source: str = typer.Option("all", help="all | nse | upstox"),
):
    """Pull NSE filings and Upstox news for the universe -> news."""
    from .news.fetch import fetch_news
    from .news.schema import SOURCE_NSE, SOURCE_UPSTOX
    from .storage.base import get_storage

    chosen = {
        "all": (SOURCE_NSE, SOURCE_UPSTOX),
        "nse": (SOURCE_NSE,),
        "upstox": (SOURCE_UPSTOX,),
    }.get(source)
    if chosen is None:
        raise typer.BadParameter("source must be one of: all, nse, upstox")

    storage = get_storage()
    total = _count(storage, "SELECT COUNT(*) AS n FROM instruments")
    if not total:
        print("[yellow]No instruments stored. Run `asr ingest instruments` first.[/yellow]")
        raise typer.Exit(1)

    with _progress() as bar:
        task = bar.add_task("news", total=limit or len(symbol or []) or total)
        report = fetch_news(
            symbols=symbol or None,
            limit=limit,
            days=days,
            sources=chosen,
            storage=storage,
            on_progress=lambda *_: bar.advance(task),
        )
    print(f"[green]{report.summary()}[/green]")
    for what, err in list(report.failures.items())[:5]:
        print(f"[red]  {what}: {err}[/red]")


@news.command("show")
def news_show(symbol: str, limit: int = typer.Option(10, help="Rows to show.")):
    """Latest filings and headlines for one ticker."""
    from .storage.base import get_storage

    df = get_storage().read_sql(
        "SELECT published_at, source, category, headline FROM news "
        f"WHERE symbol = '{symbol.strip().upper()}' "  # noqa: S608
        f"ORDER BY published_at DESC LIMIT {int(limit)}"
    )
    if df.empty:
        print(f"[yellow]No news for {symbol}. Run `asr news fetch`.[/yellow]")
        raise typer.Exit(1)
    print(df.to_string(index=False))


@app.command("quality")
def quality_check(
    strict: bool = typer.Option(
        True, help="Exit non-zero on ERROR findings (the point of the check)."
    ),
):
    """Data-quality assertions: make the silent failures loud."""
    from .quality.checks import ERROR, WARN, run_checks
    from .storage.base import get_storage

    report = run_checks(get_storage())
    colour = {ERROR: "red", WARN: "yellow"}
    for f in report.findings:
        print(f"[{colour.get(f.severity, 'dim')}]{f}[/{colour.get(f.severity, 'dim')}]")

    if report.ok:
        print(f"[green]{report.summary()}[/green]")
    else:
        print(f"[red]{report.summary()}[/red]")
        if strict:
            raise typer.Exit(1)


@pack.command("build")
def pack_build(
    symbol: list[str] = typer.Argument(None, help="Tickers. Omit for the whole universe."),
    fmt: str = typer.Option("md", "--format", help="md | json"),
    out: str | None = typer.Option(None, help="Directory to write to. Omit to print."),
    news_days: int = typer.Option(30, help="News lookback window."),
):
    """Export a research pack: computed facts only, ready to paste into Claude."""
    from pathlib import Path

    from .pack.build import build_many, default_out_dir, to_json, to_markdown
    from .storage.base import get_storage

    if fmt not in ("md", "json"):
        raise typer.BadParameter("format must be md or json")

    storage = get_storage()
    symbols = [s.strip().upper() for s in symbol] if symbol else None
    if symbols is None:
        df = storage.read_sql("SELECT DISTINCT symbol FROM candles ORDER BY symbol")
        symbols = df["symbol"].tolist()
        if not symbols:
            print("[yellow]No candles stored. Run `asr ingest backfill` first.[/yellow]")
            raise typer.Exit(1)

    packs, failures = build_many(symbols, storage=storage, news_days=news_days)
    render = to_markdown if fmt == "md" else to_json

    if out is None and len(packs) == 1:
        print(render(packs[0]))
    elif packs:
        directory = Path(out or default_out_dir())
        directory.mkdir(parents=True, exist_ok=True)
        for p in packs:
            path = directory / f"{p['meta']['symbol']}.{fmt}"
            path.write_text(render(p))
        print(f"[green]Wrote {len(packs)} packs to {directory}/[/green]")
        print("[dim]Paste one into Claude with prompts/analysis.md[/dim]")

    for sym, err in list(failures.items())[:10]:
        print(f"[red]  {sym}: {err}[/red]")


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
