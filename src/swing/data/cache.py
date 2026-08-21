"""On-disk parquet cache for daily bars, earnings dates and fundamentals.

Design notes
------------
* One parquet file per symbol (``bars/AAPL.parquet``). Per-symbol files make
  incremental updates cheap and let a corrupted symbol be deleted in isolation.
* Writes are atomic (temp file + ``os.replace``) so an interrupted nightly
  refresh cannot leave a half-written frame that silently truncates history.
* ``merge_bars`` prefers freshly downloaded rows over cached ones for the same
  date — vendors revise the last bar, and adjusted prices shift after splits.
* Everything is content-addressable enough to answer "did the data change?":
  :func:`data_fingerprint` hashes the exact bars a backtest consumed so a
  report can prove which data produced it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..logging_setup import get_logger
from .provider import BARS_COLUMNS, Fundamentals, empty_bars, normalize_bars

log = get_logger("swing.cache")


class ProviderMismatch(RuntimeError):
    """The cache holds bars written by a different provider than the one configured.

    This is a hard error rather than a warning because the failure is silent
    and total. Providers do not merely disagree at the margin: yfinance serves
    split- *and* dividend-adjusted closes, Stooq serves split-adjusted only.
    Appending one to the other splices two different adjustment bases into a
    single series, so every indicator, every backtested trade and every stop
    downstream is computed from prices that never existed. Nothing about the
    resulting numbers looks wrong, which is exactly why it has to stop the run.
    """

_SAFE = str.maketrans({"/": "-", "\\": "-", ":": "-", "*": "-", "?": "-", " ": "_"})


class BarCache:
    """Filesystem-backed store for the data layer."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.bars_dir = self.root / "bars"
        self.meta_path = self.root / "meta.json"
        self.earnings_path = self.root / "earnings.json"
        self.fundamentals_path = self.root / "fundamentals.json"
        self.absent_path = self.root / "absent.json"

    # -- provider identity -------------------------------------------------
    # The cache used to record nothing about who wrote it, so "delete
    # data/cache/ when you switch providers" was advice you had to remember.
    # Now it is enforced.
    def stamped_provider(self) -> str | None:
        """Name of the provider that wrote these bars, or None if unstamped."""
        value = self._read_json(self.meta_path).get("provider")
        return str(value).lower() if value else None

    def stamp_provider(self, name: str) -> None:
        meta = self._read_json(self.meta_path)
        meta["provider"] = str(name).lower()
        meta["provider_stamped_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_json(self.meta_path, meta)

    # -- paths -------------------------------------------------------------
    def _ensure(self) -> None:
        self.bars_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        return self.bars_dir / f"{symbol.upper().translate(_SAFE)}.parquet"

    def has(self, symbol: str) -> bool:
        return self.path_for(symbol).exists()

    def symbols(self) -> list[str]:
        if not self.bars_dir.exists():
            return []
        return sorted(p.stem for p in self.bars_dir.glob("*.parquet"))

    # -- bars --------------------------------------------------------------
    def read(self, symbol: str) -> pd.DataFrame:
        path = self.path_for(symbol)
        if not path.exists():
            return empty_bars()
        try:
            df = pd.read_parquet(path)
        except Exception as exc:  # corrupted file: treat as a cache miss
            log.warning("cache read failed for %s (%s); ignoring file", symbol, exc)
            return empty_bars()
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df.index.name = "date"
        return df[BARS_COLUMNS].astype("float64").sort_index()

    def read_many(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            df = self.read(sym)
            if len(df):
                out[sym] = df
        return out

    def write(self, symbol: str, bars: pd.DataFrame) -> None:
        """Atomically replace a symbol's bars."""
        self._ensure()
        bars = normalize_bars(bars)
        if not len(bars):
            return
        path = self.path_for(symbol)
        fd, tmp = tempfile.mkstemp(dir=str(self.bars_dir), suffix=".tmp")
        os.close(fd)
        try:
            bars.to_parquet(tmp, index=True)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def upsert(self, symbol: str, fresh: pd.DataFrame) -> pd.DataFrame:
        """Merge fresh bars into the cache and return the merged frame."""
        merged = merge_bars(self.read(symbol), fresh)
        self.write(symbol, merged)
        return merged

    def last_date(self, symbol: str) -> date | None:
        df = self.read(symbol)
        if not len(df):
            return None
        return df.index[-1].date()

    def coverage(self) -> pd.DataFrame:
        """One row per cached symbol: first bar, last bar, row count."""
        rows = []
        for sym in self.symbols():
            df = self.read(sym)
            if not len(df):
                continue
            rows.append(
                {
                    "symbol": sym,
                    "start": df.index[0].date(),
                    "end": df.index[-1].date(),
                    "bars": len(df),
                }
            )
        if not rows:
            return pd.DataFrame(columns=["symbol", "start", "end", "bars"])
        return pd.DataFrame(rows).set_index("symbol").sort_index()

    # -- sidecar json ------------------------------------------------------
    def _read_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("ignoring unreadable %s (%s)", path.name, exc)
            return {}

    def _write_json(self, path: Path, payload: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
        os.replace(tmp, path)

    # -- earnings ----------------------------------------------------------
    def read_earnings(self) -> dict[str, date | None]:
        raw = self._read_json(self.earnings_path).get("dates", {})
        out: dict[str, date | None] = {}
        for sym, value in raw.items():
            out[sym] = date.fromisoformat(value) if value else None
        return out

    def write_earnings(self, dates: dict[str, date | None]) -> None:
        payload = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "dates": {k: (v.isoformat() if v else None) for k, v in dates.items()},
        }
        self._write_json(self.earnings_path, payload)

    def earnings_age_days(self) -> float | None:
        raw = self._read_json(self.earnings_path).get("fetched_at")
        if not raw:
            return None
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() / 86400.0

    # -- fundamentals ------------------------------------------------------
    def read_fundamentals(self) -> dict[str, Fundamentals]:
        raw = self._read_json(self.fundamentals_path).get("symbols", {})
        return {
            sym: Fundamentals(symbol=sym, **{k: v for k, v in vals.items() if k != "symbol"})
            for sym, vals in raw.items()
        }

    def write_fundamentals(self, funds: dict[str, Fundamentals]) -> None:
        payload = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "symbols": {sym: asdict(f) for sym, f in funds.items()},
        }
        self._write_json(self.fundamentals_path, payload)

    def fundamentals_age_days(self) -> float | None:
        raw = self._read_json(self.fundamentals_path).get("fetched_at")
        if not raw:
            return None
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() / 86400.0

    # -- negative cache ----------------------------------------------------
    # Delisted, renamed and simply-wrong tickers otherwise get re-requested
    # every single night forever. Remember which symbols came back empty and
    # leave them alone for a while — but do retry eventually, because "empty"
    # is also what a provider outage looks like.
    def read_absent(self) -> dict[str, date]:
        raw = self._read_json(self.absent_path)
        out: dict[str, date] = {}
        for sym, value in raw.items():
            try:
                out[sym] = date.fromisoformat(value)
            except (TypeError, ValueError):
                continue
        return out

    def mark_absent(self, symbols: list[str], when: date | None = None) -> None:
        if not symbols:
            return
        when = when or date.today()
        current = {k: v.isoformat() for k, v in self.read_absent().items()}
        for sym in symbols:
            current[sym.upper()] = when.isoformat()
        self._write_json(self.absent_path, current)

    def clear_absent(self, symbols: list[str]) -> None:
        current = {k: v.isoformat() for k, v in self.read_absent().items()}
        changed = False
        for sym in symbols:
            if current.pop(sym.upper(), None) is not None:
                changed = True
        if changed:
            self._write_json(self.absent_path, current)

    def absent_symbols(self, retry_after_days: int, as_of: date | None = None) -> set[str]:
        """Symbols known to return no data and not yet due for a retry."""
        as_of = as_of or date.today()
        return {
            sym
            for sym, when in self.read_absent().items()
            if (as_of - when).days < retry_after_days
        }


def merge_bars(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Combine cached and freshly fetched bars; fresh rows win on collision."""
    old = normalize_bars(old) if len(old) else empty_bars()
    new = normalize_bars(new) if new is not None and len(new) else empty_bars()
    if not len(new):
        return old
    if not len(old):
        return new
    combined = pd.concat([old, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def data_fingerprint(bars: dict[str, pd.DataFrame]) -> str:
    """Hash the exact bars a run consumed, so reports are reproducible."""
    h = hashlib.sha256()
    for sym in sorted(bars):
        df = bars[sym]
        h.update(sym.encode())
        if not len(df):
            continue
        h.update(str(df.index[0].date()).encode())
        h.update(str(df.index[-1].date()).encode())
        h.update(str(len(df)).encode())
        # Rounded closes: enough to detect a re-adjustment, cheap to compute.
        h.update(df["close"].round(4).to_numpy().tobytes())
    return h.hexdigest()[:16]
