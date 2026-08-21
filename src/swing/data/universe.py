"""Tradable-universe construction.

The universe is the union of the enabled index seed files plus ``extra_symbols``,
minus ``exclude_symbols``, optionally narrowed by a liquidity filter computed
from cached bars (price floor and 20-day average dollar volume).

Honest caveat, repeated here because it matters: the shipped CSVs list *current*
members. Backtesting a current-membership list is survivorship-biased — the
companies that blew up between 2010 and today are simply absent. The ETF-only
backtest (``swing backtest --etf-only``) exists as the survivorship-free lower
bound. See docs/indicator-research.md.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from ..config import REPO_ROOT, Config
from ..logging_setup import get_logger

log = get_logger("swing.universe")

UNIVERSE_DIR = REPO_ROOT / "data" / "universe"

SOURCE_FILES = {
    "sp500": "sp500.csv",
    "sp400": "sp400.csv",
    "sp600": "sp600.csv",
    "etfs": "etfs.csv",
}


@dataclass(frozen=True)
class Symbol:
    symbol: str
    kind: str          # "stock" | "etf"
    name: str = ""
    source: str = ""

    @property
    def is_etf(self) -> bool:
        return self.kind == "etf"


def read_symbol_file(path: Path, source: str = "") -> list[Symbol]:
    """Read one universe CSV, skipping ``#`` comment lines and blanks."""
    if not path.exists():
        log.warning("universe file not found: %s", path)
        return []
    rows: list[Symbol] = []
    with path.open(newline="") as fh:
        lines = [ln for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        sym = (row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        rows.append(
            Symbol(
                symbol=sym,
                kind=(row.get("kind") or "stock").strip().lower(),
                name=(row.get("name") or "").strip(),
                source=source or path.stem,
            )
        )
    return rows


def build_universe(cfg: Config, apply_liquidity_filter: bool = False) -> list[Symbol]:
    """Assemble the configured universe, optionally screening on liquidity."""
    uni_cfg = cfg.universe
    seen: dict[str, Symbol] = {}

    for key, filename in SOURCE_FILES.items():
        if not uni_cfg.get(key, False):
            continue
        for sym in read_symbol_file(UNIVERSE_DIR / filename, source=key):
            seen.setdefault(sym.symbol, sym)

    for raw in uni_cfg.get("extra_symbols", []) or []:
        sym = str(raw).strip().upper()
        if sym:
            seen.setdefault(sym, Symbol(symbol=sym, kind="stock", source="extra"))

    for raw in uni_cfg.get("exclude_symbols", []) or []:
        seen.pop(str(raw).strip().upper(), None)

    symbols = sorted(seen.values(), key=lambda s: s.symbol)

    if apply_liquidity_filter:
        symbols = filter_by_liquidity(cfg, symbols)

    cap = int(uni_cfg.get("max_symbols", 0) or 0)
    if cap and len(symbols) > cap:
        log.info("capping universe at %d symbols (universe.max_symbols)", cap)
        symbols = symbols[:cap]

    log.info("universe: %d symbols", len(symbols))
    return symbols


def liquidity_table(cfg: Config, symbols: list[Symbol]) -> pd.DataFrame:
    """Latest price and 20-day average dollar volume per symbol, from the cache."""
    from .cache import BarCache

    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    rows = []
    for sym in symbols:
        bars = cache.read(sym.symbol)
        if len(bars) < 20:
            rows.append(
                {"symbol": sym.symbol, "price": float("nan"),
                 "dollar_volume": float("nan"), "bars": len(bars)}
            )
            continue
        tail = bars.tail(20)
        rows.append(
            {
                "symbol": sym.symbol,
                "price": float(bars["close"].iloc[-1]),
                "dollar_volume": float((tail["close"] * tail["volume"]).mean()),
                "bars": len(bars),
            }
        )
    return pd.DataFrame(rows).set_index("symbol")


def filter_by_liquidity(cfg: Config, symbols: list[Symbol]) -> list[Symbol]:
    """Drop symbols below the price / dollar-volume floors.

    Symbols with no cached data are *kept* — an empty cache should not silently
    shrink the universe to nothing. The screen is re-applied per bar inside the
    backtester and the scanner, where the data is guaranteed to be there.
    """
    table = liquidity_table(cfg, symbols)
    min_price = float(cfg.universe.min_price)
    min_dv = float(cfg.universe.min_dollar_volume)

    kept, dropped, unknown = [], 0, 0
    for sym in symbols:
        if sym.symbol not in table.index:
            kept.append(sym)
            continue
        row = table.loc[sym.symbol]
        if pd.isna(row["price"]):
            unknown += 1
            kept.append(sym)
            continue
        if row["price"] < min_price or row["dollar_volume"] < min_dv:
            dropped += 1
            continue
        kept.append(sym)

    log.info(
        "liquidity filter: kept %d, dropped %d, no-data %d (price>=%.2f, $vol>=%s)",
        len(kept), dropped, unknown, min_price, f"{min_dv:,.0f}",
    )
    return kept


def describe_universe(symbols: list[Symbol], limit: int = 20) -> str:
    by_source: dict[str, int] = {}
    for sym in symbols:
        by_source[sym.source] = by_source.get(sym.source, 0) + 1
    lines = [f"universe: {len(symbols)} symbols"]
    for source, count in sorted(by_source.items()):
        lines.append(f"  {source:<8} {count:>5}")
    if limit > 0 and symbols:
        shown = ", ".join(s.symbol for s in symbols[:limit])
        lines.append("")
        lines.append(f"first {min(limit, len(symbols))}: {shown}")
        if len(symbols) > limit:
            lines.append(f"... and {len(symbols) - limit} more")
    return "\n".join(lines)


def fetch_constituents(which: str = "all") -> dict[str, int]:
    """Refresh the index seed CSVs from Wikipedia's constituent tables.

    Requires network access, ``lxml`` and ``html5lib`` (both pulled in by
    pandas' ``read_html``). Failures are reported per index rather than
    aborting — a stale seed file is far better than a truncated one.
    """
    pages = {
        "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", 0),
        "sp400": ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", 0),
        "sp600": ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", 0),
    }
    targets = pages if which in ("all", "") else {which: pages[which]}
    written: dict[str, int] = {}

    for key, (url, _) in targets.items():
        try:
            tables = pd.read_html(url)
        except Exception as exc:
            log.error("could not fetch %s constituents (%s); leaving seed file alone", key, exc)
            continue
        frame = _pick_constituent_table(tables)
        if frame is None:
            log.error("no recognisable constituent table at %s", url)
            continue
        sym_col = _find_col(frame, ("Symbol", "Ticker", "Ticker symbol"))
        name_col = _find_col(frame, ("Security", "Company", "Name"))
        path = UNIVERSE_DIR / SOURCE_FILES[key]
        header = _preserve_header(path)
        count = 0
        with path.open("w", newline="") as fh:
            fh.write(header)
            writer = csv.writer(fh)
            writer.writerow(["symbol", "kind", "name"])
            for _, row in frame.iterrows():
                sym = str(row[sym_col]).strip().upper().replace(".", "-")
                if not sym or sym == "NAN":
                    continue
                name = str(row[name_col]).strip() if name_col else ""
                writer.writerow([sym, "stock", name])
                count += 1
        written[key] = count
        log.info("refreshed %s: %d symbols", path.name, count)
    return written


def _pick_constituent_table(tables: list[pd.DataFrame]) -> pd.DataFrame | None:
    for frame in tables:
        cols = {str(c) for c in frame.columns}
        if cols & {"Symbol", "Ticker", "Ticker symbol"} and len(frame) > 50:
            return frame
    return None


def _find_col(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    for cand in candidates:
        if cand in frame.columns:
            return cand
    return None


def _preserve_header(path: Path) -> str:
    """Keep the leading ``#`` comment block when rewriting a seed file."""
    if not path.exists():
        return ""
    out = []
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            out.append(line)
        else:
            break
    return ("\n".join(out) + "\n") if out else ""
