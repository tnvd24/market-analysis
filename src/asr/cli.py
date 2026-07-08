"""Typer CLI. Commands fill in per phase."""

from __future__ import annotations

from datetime import date, timedelta

import typer
from rich import print

app = typer.Typer(help="Agentic stock research (research-only).")
ingest = typer.Typer(help="Phase 2: data ingestion")
app.add_typer(ingest, name="ingest")


@ingest.command("smoke")
def ingest_smoke(instrument_key: str = "NSE_EQ|INE848E01016", days: int = 30):
    """Pull recent daily candles for one instrument and store them. Auth check."""
    from .ingest.upstox_client import UpstoxClient
    from .storage.base import get_storage

    client = UpstoxClient()
    to_d = date.today()
    from_d = to_d - timedelta(days=days)
    df = client.daily_candles(instrument_key, from_d, to_d)
    df["symbol"] = instrument_key.split("|")[-1]
    n = get_storage().upsert_candles(df)
    print(f"[green]Stored {n} candles for {instrument_key}[/green]")
    print(df.tail())


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
