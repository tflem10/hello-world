"""FROZEN CONTRACT 4 — the tradable universe.

The universe is read from CSV snapshots committed under
``src/swing/assets/universe/``. Snapshots rather than a live Wikipedia scrape,
for three reasons: scans must work offline, tests must never touch the network,
and a backtest that silently changes its universe between runs is not a
backtest.

Symbols are stored Yahoo-style (``BRK-B``, not ``BRK.B``) because yfinance is
the default data provider.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "INDEX_SOURCES",
    "Instrument",
    "asset_dir",
    "load",
    "load_csv",
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


def asset_dir() -> Path:
    """Directory holding the committed universe CSVs."""
    return Path(str(resources.files("swing"))) / "assets" / "universe"


def load_csv(stem: str) -> list[tuple[str, str]]:
    """Read one universe CSV and return ``(symbol, name)`` pairs in file order."""
    path = asset_dir() / f"{stem}.csv"
    if not path.is_file():
        raise UniverseError(
            f"The universe snapshot {path.name} is missing (looked in {path.parent}). "
            f"Reinstall the package, or restore the file from git."
        )
    rows: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames:
            raise UniverseError(
                f"{path} does not have the expected 'symbol,name' header row "
                f"(found {reader.fieldnames})."
            )
        for row in reader:
            symbol = to_yahoo_symbol(row.get("symbol") or "")
            name = (row.get("name") or "").strip()
            if not symbol:
                continue
            rows.append((symbol, name or symbol))
    return rows


def _etf_symbols() -> set[str]:
    try:
        return {sym for sym, _ in load_csv("etfs")}
    except UniverseError:  # pragma: no cover - only if the snapshot is missing
        return set()


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
        for symbol, name in load_csv(stem):
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
