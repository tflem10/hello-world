"""FROZEN CONTRACT 4 — the tradable universe.

The universe is read from CSV snapshots committed under
``src/swing/assets/universe/``. Snapshots rather than a live Wikipedia scrape,
for three reasons: scans must work offline, tests must never touch the network,
and a backtest that silently changes its universe between runs is not a
backtest.

Symbols are stored Yahoo-style (``BRK-B``, not ``BRK.B``) because yfinance is
the default data provider; :func:`to_schwab_symbol` translates on the way out
to Schwab.

The snapshots ship inside the package, so they are read through
``importlib.resources`` — ``files()`` for structure, ``as_file()`` for the
actual open, which is what keeps a zipped install working (audit DEBT-014).
They are also immutable for the life of the process, so each file is parsed
once and cached (audit PERF-011).
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib import resources
from importlib.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "INDEX_SOURCES",
    "Instrument",
    "UniverseError",
    "asset_dir",
    "load",
    "load_csv",
    "symbols",
    "to_schwab_symbol",
    "to_yahoo_symbol",
]

log = logging.getLogger(__name__)

#: CSV stem -> (source label, instrument kind)
INDEX_SOURCES: dict[str, tuple[str, str]] = {
    "sp500": ("sp500", "stock"),
    "sp400": ("sp400", "stock"),
    "sp600": ("sp600", "stock"),
    "etfs": ("etf", "etf"),
}

#: A Yahoo-style share class: a root, a dash, and a single class letter.
_CLASS_SHARE_RE = re.compile(r"^([A-Z0-9]+)-([A-Z])$")


@dataclass(frozen=True)
class Instrument:
    """One tradable thing: a listed stock or an ETF."""

    symbol: str
    name: str
    kind: str  # "stock" | "etf"
    source: str  # "sp500" | "sp400" | "sp600" | "etf" | "extra"


class UniverseError(RuntimeError):
    """Raised when the committed universe snapshots are missing or unreadable."""


def to_yahoo_symbol(symbol: str) -> str:
    """Normalise a ticker to the form yfinance expects.

    Share classes are written with a dot by the exchanges and by Wikipedia
    (``BRK.B``) but with a dash by Yahoo (``BRK-B``).
    """
    return symbol.strip().upper().replace(".", "-").replace(" ", "")


def to_schwab_symbol(symbol: str) -> str:
    """Translate a Yahoo-style ticker to the form Schwab expects.

    Schwab writes share classes with a forward slash (``BRK/B``) where Yahoo
    uses a dash (``BRK-B``). Sent verbatim, a dual-class name simply returns no
    history — one warning per symbol, no aggregate, so it looks like the stock
    stopped trading (audit BUG-039).

    Only the exact single-letter class form is rewritten; everything else is
    passed through untouched, because a wrong guess here is indistinguishable
    from a delisting.

    Note:
        The Schwab symbology was verified against the schwab-py client and the
        published docs, not against a live quote — no credentials exist in this
        checkout. Confirm it with a real quote the first time Schwab is used;
        ``docs/schwab-setup.md`` carries the check.
    """
    normalised = to_yahoo_symbol(symbol)
    match = _CLASS_SHARE_RE.match(normalised)
    if match is None:
        return normalised
    return f"{match.group(1)}/{match.group(2)}"


def _universe_root() -> Traversable:
    """The packaged universe directory, as a resource rather than a filesystem path."""
    return resources.files("swing").joinpath("assets").joinpath("universe")


@contextmanager
def _snapshot_path(stem: str) -> Iterator[Path]:
    """Yield a real filesystem path for one snapshot, extracting it if need be.

    ``as_file`` is the whole point: in a normal checkout it hands back the file
    where it already is, and in a zipped install it materialises a temporary
    copy for the duration of the block. Stringifying the ``Traversable``
    instead — what this module used to do — produces a path that does not exist
    inside a zip.
    """
    resource = _universe_root().joinpath(f"{stem}.csv")
    if not resource.is_file():
        raise UniverseError(
            f"The universe snapshot {stem}.csv is missing (looked in {_universe_root()}). "
            f"Reinstall the package, or restore the file from git."
        )
    with resources.as_file(resource) as path:
        yield Path(path)


def asset_dir() -> Path:
    """Directory holding the committed universe CSVs.

    Convenience for diagnostics and tests. Reading a snapshot goes through
    :func:`load_csv`, which opens each file inside its own ``as_file`` block —
    under a zipped install the directory materialised here would be gone by the
    time this function returned.
    """
    with resources.as_file(_universe_root()) as path:
        return Path(path)


@cache
def _read_snapshot(stem: str) -> tuple[tuple[str, str], ...]:
    """Parse one snapshot once per process; the files never change under us."""
    # utf-8-sig, not utf-8: a byte-order mark on a file someone re-saved in
    # Excel would otherwise land inside the first header name and be reported
    # as a missing 'symbol' column (audit DEBT-014).
    with _snapshot_path(stem) as path, path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames:
            raise UniverseError(
                f"{path} does not have the expected 'symbol,name' header row "
                f"(found {reader.fieldnames})."
            )
        rows: list[tuple[str, str]] = []
        for row in reader:
            symbol = to_yahoo_symbol(row.get("symbol") or "")
            name = (row.get("name") or "").strip()
            if not symbol:
                continue
            rows.append((symbol, name or symbol))
    return tuple(rows)


def load_csv(stem: str) -> list[tuple[str, str]]:
    """Read one universe CSV and return ``(symbol, name)`` pairs in file order.

    Raises:
        UniverseError: if the snapshot is missing or has the wrong header.
    """
    return list(_read_snapshot(stem))


@cache
def _etf_symbols() -> frozenset[str]:
    try:
        return frozenset(sym for sym, _ in _read_snapshot("etfs"))
    except UniverseError:  # pragma: no cover - only if the snapshot is missing
        return frozenset()


def load(cfg: Config) -> list[Instrument]:
    """Return the de-duplicated universe implied by ``cfg.universe``.

    Order is stable: S&P 500, then 400, then 600, then ETFs, then any
    ``extra_symbols``. The first appearance of a symbol wins, so a symbol that
    is in two indices is reported under the larger one.
    """
    wanted: list[str] = [
        stem
        for stem, enabled in (
            ("sp500", cfg.universe.sp500),
            ("sp400", cfg.universe.sp400),
            ("sp600", cfg.universe.sp600),
            ("etfs", cfg.universe.etfs),
        )
        if enabled
    ]

    instruments: list[Instrument] = []
    seen: set[str] = set()

    for stem in wanted:
        source, kind = INDEX_SOURCES[stem]
        for symbol, name in _read_snapshot(stem):
            if symbol in seen:
                continue
            seen.add(symbol)
            instruments.append(Instrument(symbol=symbol, name=name, kind=kind, source=source))

    if cfg.universe.extra_symbols:
        etfs = _etf_symbols()
        for raw in cfg.universe.extra_symbols:
            symbol = to_yahoo_symbol(raw)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            instruments.append(
                Instrument(
                    symbol=symbol,
                    name=symbol,
                    kind="etf" if symbol in etfs else "stock",
                    source="extra",
                )
            )

    log.info(
        "Universe: %d instruments from %s", len(instruments), ", ".join(wanted) or "extras only"
    )
    return instruments


def symbols(cfg: Config) -> list[str]:
    """Convenience: just the ticker strings of :func:`load`."""
    return [i.symbol for i in load(cfg)]
