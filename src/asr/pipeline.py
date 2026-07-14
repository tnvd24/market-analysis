"""The whole pipeline as one command — what a container or a scheduler actually runs.

The stages have a hard order, and getting it wrong is silent rather than loud:

    instruments -> prices -> actions -> adjust -> features -> news -> quality -> packs

**`adjust` must run before `features`**, or indicators are computed on prices where a split
still looks like a 50% crash. **`quality` must run before the packs are trusted**, and it is
the gate: if it finds an ERROR, the run stops non-zero rather than writing packs full of
numbers nobody should read.

Chaining this by hand in a cron line is how the order eventually gets broken. So it lives
here, in code, with the ordering constraint written down next to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from rich import print

from .storage.base import get_storage


@dataclass
class StageResult:
    name: str
    detail: str
    ok: bool = True


@dataclass
class PipelineReport:
    stages: list[StageResult] = field(default_factory=list)
    quality_errors: int = 0
    packs_written: int = 0

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.stages) and self.quality_errors == 0


def _stage(report: PipelineReport, name: str, fn: Callable[[], str]) -> None:
    print(f"[bold blue]▶ {name}[/bold blue]")
    try:
        detail = fn()
        report.stages.append(StageResult(name, detail))
        print(f"  [green]{detail}[/green]")
    except Exception as exc:  # one failed stage must not hide which stage failed
        report.stages.append(StageResult(name, str(exc), ok=False))
        print(f"  [red]FAILED: {exc}[/red]")
        raise


def run_pipeline(
    years: int = 3,
    incremental: bool = True,
    news_days: int = 30,
    pack_dir: str | None = None,
    strict: bool = True,
) -> PipelineReport:
    """Ingest → adjust → indicators → news → quality → packs.

    ``incremental`` pulls only the missing days (the daily case). Set it False for a first
    run, or to rebuild a window from scratch.
    """
    from .ingest.adjust import apply_adjustments
    from .ingest.corporate_actions import CorporateActions
    from .ingest.instruments import sync_instruments
    from .ingest.prices import backfill, daily
    from .news.fetch import fetch_news
    from .pack.build import build_many, default_out_dir, to_markdown
    from .quality.checks import run_checks

    report = PipelineReport()
    storage = get_storage()

    _stage(
        report,
        "instruments",
        lambda: f"{len(sync_instruments()[0])} resolved",
    )

    def _prices() -> str:
        r = daily(storage=storage) if incremental else backfill(years=years, storage=storage)
        return r.summary()

    _stage(report, "prices", _prices)

    def _actions() -> str:
        from datetime import timedelta

        client = CorporateActions()
        # Incremental runs only need the recent window; a full run needs the whole history,
        # because a split announced today changes the factor for every bar before it.
        span = 30 if incremental else 365 * years
        until = date.today()
        cursor = until - timedelta(days=span)
        import pandas as pd

        frames = []
        while cursor < until:
            end = min(cursor + timedelta(days=90), until)
            frames.append(client.fetch(cursor, end))
            cursor = end + timedelta(days=1)
        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="id")
        n = storage.upsert_corporate_actions(df) if not df.empty else 0
        review = int(df["needs_review"].sum()) if not df.empty else 0
        return f"{n} actions ({review} need review)"

    _stage(report, "actions", _actions)

    # MUST precede features: indicators on unadjusted prices are quietly wrong.
    _stage(report, "adjust", lambda: apply_adjustments(storage).summary())
    _stage(report, "features", lambda: _features(storage))
    _stage(report, "news", lambda: fetch_news(days=news_days, storage=storage).summary())

    # The gate.
    def _quality() -> str:
        q = run_checks(storage)
        report.quality_errors = len(q.errors)
        for f in q.errors[:10]:
            print(f"  [red]{f}[/red]")
        return q.summary()

    _stage(report, "quality", _quality)

    if strict and report.quality_errors:
        print(
            f"[red]Stopping: {report.quality_errors} data-quality errors. "
            "Packs built on this data would be confidently wrong.[/red]"
        )
        return report

    def _packs() -> str:
        from pathlib import Path

        symbols = storage.read_sql("SELECT DISTINCT symbol FROM features ORDER BY symbol")[
            "symbol"
        ].tolist()
        directory = Path(pack_dir or default_out_dir())
        directory.mkdir(parents=True, exist_ok=True)
        packs, _ = build_many(symbols, storage=storage, news_days=news_days)
        for p in packs:
            (directory / f"{p['meta']['symbol']}.md").write_text(to_markdown(p))
        report.packs_written = len(packs)
        return f"{len(packs)} packs -> {directory}/"

    _stage(report, "packs", _packs)
    return report


def _features(storage) -> str:
    from .features.build import build_features

    return build_features(storage=storage).summary()
