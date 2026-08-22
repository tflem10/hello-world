#!/usr/bin/env python3
"""Deepen the local price cache to the earliest history the vendor will serve.

The nightly scan only ever asks for the last couple of years, so the parquet cache under
``cfg.data.cache_dir/daily`` is exactly as deep as the shallowest thing that ever asked for it.
This script asks for everything instead: it walks the committed universe and calls
``provider.daily_bars(symbol, start, today)`` with ``start`` far enough back (1990-01-01 by
default) that the vendor's answer is bounded by the instrument's own inception rather than by our
request.

It goes through :func:`swing.data.get_provider` and the ordinary ``daily_bars`` call on purpose.
Hand-writing parquet into the cache directory would skip the sidecar bookkeeping, the
re-adjustment overlap check and the atomic-write lock, and would leave the cache in a state the
rest of the system does not believe. The one visible consequence of using the front door is that a
symbol whose cached ``covered_start`` is later than ``--start`` takes the "the head is missing"
branch in :class:`~swing.data.cache.BarCache` and refetches its whole history over the *union*
range (audit BUG-013). That is the correct behaviour and it is why the first deep run is slow: it
is a full re-download of every symbol, not an append.

Symbols are fetched one at a time rather than in one large call. It is slower in the best case but
much better behaved in the worst: a vendor failure costs one symbol instead of a 200-symbol batch,
and the cache is durable after every symbol, so an interrupted run keeps everything it had already
written.

Usage::

    uv run python scripts/fetch_history.py --dry-run
    uv run python scripts/fetch_history.py --etf-only
    uv run python scripts/fetch_history.py --stocks-only --start 1990-01-01
    uv run python scripts/fetch_history.py --symbols SPY,QQQ --start 1993-01-01

Exit codes: ``0`` when every requested symbol ended up with cached bars, ``1`` when at least one
did not, ``2`` for a bad invocation.

Survivorship warning: deepening the *stock* history does not make a pre-2010 stock backtest
honest. The S&P snapshots in ``src/swing/assets/universe`` are today's membership, so a 1996 run
over them is a run over the companies that survived to 2026. See ``docs/data-coverage.md``. ETF
series carry no such bias — an ETF's own price history is the price history of a thing that was
continuously tradable.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Default first calendar day requested. Comfortably before SPY (1993-01-29), which is the oldest
#: US-listed ETF, so the vendor's answer is always bounded by inception rather than by us.
DEFAULT_START = date(1990, 1, 1)

#: Seconds to pause between symbols. Yahoo is free, unsupported and rate-limited by vibes; a
#: sixteenth of a second over 1,700 symbols costs under two minutes and keeps the run polite.
DEFAULT_PAUSE = 0.0625

#: How often to print a progress line, in symbols.
PROGRESS_EVERY = 25

log = logging.getLogger("fetch_history")


# --------------------------------------------------------------------------------------------
# lazy project imports
# --------------------------------------------------------------------------------------------


def _ensure_src_on_path() -> None:
    """Allow ``python3 scripts/fetch_history.py`` to work without an editable install."""
    src = REPO_ROOT / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def load_base_config() -> Any:
    """Load the resolved ``Config`` via SPEC Contract 1's ``load_config``."""
    _ensure_src_on_path()
    from swing.config import load_config

    return load_config()


def load_universe(cfg: Any) -> list[Any]:
    """Every instrument the committed snapshots know about, whatever the config toggles say.

    ``swing.universe.load`` honours ``cfg.universe.sp500`` and friends. This script's job is to
    make the *cache* deep, and a toggle someone flipped off in ``config.toml`` for tonight's scan
    is no reason to leave a hole in it, so the toggles are forced on here.
    """
    _ensure_src_on_path()
    import dataclasses

    from swing.universe import load

    universe = dataclasses.replace(cfg.universe, sp500=True, sp400=True, sp600=True, etfs=True)
    return list(load(dataclasses.replace(cfg, universe=universe)))


# --------------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolResult:
    """What one symbol's fetch produced, read back from the cache sidecar."""

    symbol: str
    source: str
    ok: bool
    first_bar: date | None = None
    last_bar: date | None = None
    rows: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def years(self) -> float:
        if self.first_bar is None or self.last_bar is None:
            return 0.0
        return (self.last_bar - self.first_bar).days / 365.25


@dataclass
class Summary:
    """Aggregate outcome of a run, per source and overall."""

    start: date
    end: date
    results: list[SymbolResult] = field(default_factory=list)

    @property
    def ok(self) -> list[SymbolResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[SymbolResult]:
        return [r for r in self.results if not r.ok]

    def by_source(self) -> dict[str, list[SymbolResult]]:
        out: dict[str, list[SymbolResult]] = {}
        for result in self.results:
            out.setdefault(result.source, []).append(result)
        return out

    def render(self) -> str:
        """A human-readable coverage summary, one block per source plus the failures."""
        lines: list[str] = []
        lines.append("")
        lines.append("=" * 78)
        lines.append(f"Coverage after fetching {self.start} -> {self.end}")
        lines.append("=" * 78)
        header = (
            f"{'source':<10}{'symbols':>9}{'rows':>12}"
            f"{'earliest':>13}{'latest':>13}{'pre-2010':>10}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for source in sorted(self.by_source()):
            rows = [r for r in self.by_source()[source] if r.ok and r.rows]
            if not rows:
                lines.append(f"{source:<10}{0:>9}{0:>12}{'-':>13}{'-':>13}{0:>10}")
                continue
            earliest = min(r.first_bar for r in rows if r.first_bar)
            latest = max(r.last_bar for r in rows if r.last_bar)
            deep = sum(1 for r in rows if r.first_bar and r.first_bar < date(2010, 1, 1))
            total = sum(r.rows for r in rows)
            lines.append(
                f"{source:<10}{len(rows):>9}{total:>12,}"
                f"{earliest.isoformat():>13}{latest.isoformat():>13}{deep:>10}"
            )
        lines.append("-" * len(header))
        lines.append(f"{len(self.ok)} symbol(s) cached, {len(self.failed)} without usable bars.")
        if self.failed:
            lines.append("")
            lines.append("No usable bars for:")
            for result in sorted(self.failed, key=lambda r: r.symbol):
                lines.append(f"  {result.symbol:<8} {result.error or 'vendor returned nothing'}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# the fetch
# --------------------------------------------------------------------------------------------


def _frame_bounds(frame: pd.DataFrame | None) -> tuple[date | None, date | None, int]:
    """First bar, last bar and row count of a returned frame."""
    if frame is None or len(frame) == 0:
        return None, None, 0
    return frame.index[0].date(), frame.index[-1].date(), int(len(frame))


def fetch_symbol(provider: Any, symbol: str, source: str, start: date, end: date) -> SymbolResult:
    """Fetch one symbol's full history through the provider, never raising.

    A vendor failure here must cost one symbol, not the run: this walk is long enough that a
    single transient 429 two thirds of the way through would otherwise throw away an hour of
    downloads that are already safely on disk.
    """
    began = time.monotonic()
    try:
        bars = provider.daily_bars([symbol], start, end)
    except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the walk
        return SymbolResult(
            symbol=symbol,
            source=source,
            ok=False,
            seconds=time.monotonic() - began,
            error=f"{type(exc).__name__}: {exc}",
        )
    first, last, rows = _frame_bounds(bars.get(symbol))
    return SymbolResult(
        symbol=symbol,
        source=source,
        ok=rows > 0,
        first_bar=first,
        last_bar=last,
        rows=rows,
        seconds=time.monotonic() - began,
        error=None if rows else "vendor returned no bars",
    )


def fetch_all(
    provider: Any,
    instruments: Sequence[Any],
    start: date,
    end: date,
    *,
    pause: float = DEFAULT_PAUSE,
    progress_every: int = PROGRESS_EVERY,
) -> Summary:
    """Walk every instrument, one call each, collecting a :class:`Summary`."""
    summary = Summary(start=start, end=end)
    total = len(instruments)
    began = time.monotonic()
    for index, instrument in enumerate(instruments, start=1):
        result = fetch_symbol(provider, instrument.symbol, instrument.source, start, end)
        summary.results.append(result)
        if not result.ok:
            log.warning("%s: %s", result.symbol, result.error)
        if index % progress_every == 0 or index == total:
            elapsed = time.monotonic() - began
            rate = index / elapsed if elapsed > 0 else 0.0
            remaining = (total - index) / rate if rate > 0 else 0.0
            log.info(
                "%d/%d symbols (%d cached, %d empty) — %.1f/s, ~%.0f min left",
                index,
                total,
                len(summary.ok),
                len(summary.failed),
                rate,
                remaining / 60,
            )
        if pause > 0 and index < total:
            time.sleep(pause)
    return summary


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def select(instruments: Iterable[Any], *, kinds: set[str], only: set[str] | None) -> list[Any]:
    """Filter the universe down to what this invocation asked for."""
    chosen = [i for i in instruments if i.kind in kinds]
    if only is not None:
        chosen = [i for i in chosen if i.symbol in only]
    return chosen


def parse_symbols(raw: str | None) -> set[str] | None:
    """``"spy, qqq"`` -> ``{"SPY", "QQQ"}``; ``None`` means "no restriction"."""
    if raw is None:
        return None
    _ensure_src_on_path()
    from swing.universe import to_yahoo_symbol

    wanted = {to_yahoo_symbol(part) for part in raw.split(",") if part.strip()}
    return wanted or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch_history.py",
        description="Deepen the parquet price cache to each instrument's earliest available bar.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--etf-only", action="store_true", help="fetch only the ETF universe (clean deep history)."
    )
    scope.add_argument(
        "--stocks-only",
        action="store_true",
        help="fetch only the S&P 500/400/600 snapshots (survivorship-biased before 2010).",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START.isoformat(),
        help=f"first calendar day to request (default {DEFAULT_START.isoformat()}).",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="last calendar day to request (default: today).",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma-separated subset of tickers to fetch, for spot repairs.",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=DEFAULT_PAUSE,
        help=f"seconds to wait between symbols (default {DEFAULT_PAUSE}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be fetched, and what the cache already holds, without any network.",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="only print the final summary and any failures."
    )
    return parser


def _kinds(args: argparse.Namespace) -> set[str]:
    if args.etf_only:
        return {"etf"}
    if args.stocks_only:
        return {"stock"}
    return {"etf", "stock"}


def _bail(message: str) -> SystemExit:
    """Exit code 2 for a bad invocation, matching argparse's own usage errors."""
    print(f"fetch_history.py: {message}", file=sys.stderr)
    return SystemExit(2)


def _parse_day(raw: str, what: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _bail(f"--{what} must be an ISO date like 1990-01-01, not {raw!r} ({exc})") from exc


def _report_dry_run(cfg: Any, instruments: Sequence[Any], start: date, end: date) -> int:
    """Print what a real run would do, reading the cache but never the network."""
    _ensure_src_on_path()
    from swing.data.cache import BarCache

    cache = BarCache.from_config(cfg)
    summary = Summary(start=start, end=end)
    would_fetch = 0
    for instrument in instruments:
        meta = cache.read_meta(instrument.symbol)
        if meta is None:
            would_fetch += 1
            summary.results.append(
                SymbolResult(instrument.symbol, instrument.source, ok=False, error="not cached")
            )
            continue
        if meta.covered_start > start or meta.covered_end < end:
            would_fetch += 1
        summary.results.append(
            SymbolResult(
                symbol=instrument.symbol,
                source=instrument.source,
                ok=meta.rows > 0,
                first_bar=meta.first_bar,
                last_bar=meta.last_bar,
                rows=meta.rows,
            )
        )
    print(summary.render())
    print()
    print(
        f"Dry run: {len(instruments)} symbol(s) selected, {would_fetch} would hit the network "
        f"for {start} -> {end}. Nothing was downloaded."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # The cache narrates its own union-range refetches at INFO, one line per symbol over a
    # 1,700-symbol walk. Expected here, and it drowns out this script's progress.
    logging.getLogger("swing.data.cache").setLevel(logging.WARNING)

    start = _parse_day(args.start, "start")
    end = _parse_day(args.end, "end") if args.end else datetime.now().date()
    if start > end:
        raise _bail(f"--start ({start}) is after --end ({end}).")

    cfg = load_base_config()
    instruments = select(load_universe(cfg), kinds=_kinds(args), only=parse_symbols(args.symbols))
    if not instruments:
        raise _bail("nothing to fetch: the selection matched no instruments.")

    if args.dry_run:
        return _report_dry_run(cfg, instruments, start, end)

    _ensure_src_on_path()
    from swing.data import get_provider

    provider = get_provider(cfg)
    log.info(
        "Fetching %s -> %s for %d symbol(s) into %s",
        start,
        end,
        len(instruments),
        Path(cfg.data.cache_dir) / "daily",
    )
    summary = fetch_all(provider, instruments, start, end, pause=max(0.0, args.pause))
    print(summary.render())
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
