"""On-disk caches for the data layer: parquet bars plus small TTL sidecars.

Two things live here.

:class:`BarCache` keeps one parquet file per symbol under
``cfg.data.cache_dir/daily/<SYMBOL>.parquet`` with a JSON sidecar recording the
date range that has actually been asked for. It exists to make the common case
free: re-running a scan on the same evening must touch the network **zero**
times.

The interesting part is what happens when only the tail is missing. We do not
simply append, because ``auto_adjust=True`` prices are *retroactively* rewritten
by every dividend and split: yesterday's cached close of 100.00 becomes 99.83
the morning a dividend goes ex. Appending would leave a step discontinuity in
the middle of the series, which quietly corrupts every ATR, SMA and backtest
that reads it. So every tail fetch re-requests the last few cached bars as an
overlap window and compares closes; if they disagree by more than a whisker,
that symbol's entire history is refetched.

:class:`TtlJsonCache` is a much dumber thing: a JSON dict of
``key -> {fetched_at, value}`` used for earnings dates and fundamentals, which
change on the order of days, not minutes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.data.provider import as_date, clean_symbols, normalize_bars

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "DEFAULT_OVERLAP_ROWS",
    "DEFAULT_TOLERANCE",
    "BarCache",
    "CacheMeta",
    "FetchBars",
    "TtlJsonCache",
    "utcnow",
]

log = logging.getLogger(__name__)

#: ``fetch(symbols, start, end) -> {symbol: bars}``. A symbol missing from the
#: result means "could not fetch"; an empty frame means "nothing traded".
FetchBars = Callable[[Sequence[str], date, date], dict[str, pd.DataFrame]]

DAILY_SUBDIR = "daily"
META_SUFFIX = ".meta.json"
PARQUET_SUFFIX = ".parquet"

#: How many already-cached bars to re-request on a tail fetch.
DEFAULT_OVERLAP_ROWS = 5
#: Relative close difference over the overlap window that still counts as "the
#: same series" — 0.1%. Anything larger means the vendor re-adjusted history.
DEFAULT_TOLERANCE = 0.001

_UNSAFE_FILENAME = re.compile(r"[^A-Z0-9._-]")


def utcnow() -> datetime:
    """Current UTC time, timezone-aware. Only ever called at a public boundary."""
    return datetime.now(tz=UTC)


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime so comparisons never explode."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _write_atomic(path: Path, write: Callable[[Path], None]) -> None:
    """Write via a temp file in the same directory, then rename into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# bars
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheMeta:
    """What we know about one cached symbol without opening its parquet."""

    symbol: str
    covered_start: date
    covered_end: date
    first_bar: date | None
    last_bar: date | None
    rows: int
    fetched_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "covered_start": self.covered_start.isoformat(),
            "covered_end": self.covered_end.isoformat(),
            "first_bar": self.first_bar.isoformat() if self.first_bar else None,
            "last_bar": self.last_bar.isoformat() if self.last_bar else None,
            "rows": self.rows,
            "fetched_at": _as_utc(self.fetched_at).isoformat(),
        }

    @classmethod
    def from_json(cls, raw: Any) -> CacheMeta | None:
        """Rebuild from JSON, or return ``None`` if the sidecar is unusable."""
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                symbol=str(raw["symbol"]),
                covered_start=date.fromisoformat(raw["covered_start"]),
                covered_end=date.fromisoformat(raw["covered_end"]),
                first_bar=date.fromisoformat(raw["first_bar"]) if raw.get("first_bar") else None,
                last_bar=date.fromisoformat(raw["last_bar"]) if raw.get("last_bar") else None,
                rows=int(raw.get("rows", 0)),
                fetched_at=_as_utc(datetime.fromisoformat(raw["fetched_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None


class BarCache:
    """Per-symbol parquet cache of daily bars with an overlap-checked tail fetch."""

    def __init__(
        self,
        root: Path,
        *,
        overlap_rows: int = DEFAULT_OVERLAP_ROWS,
        tolerance: float = DEFAULT_TOLERANCE,
    ) -> None:
        """
        Args:
            root: directory holding ``<SYMBOL>.parquet`` files (created lazily).
            overlap_rows: how many cached bars a tail fetch re-requests.
            tolerance: relative close difference tolerated over the overlap.
        """
        if overlap_rows < 1:
            raise ValueError("overlap_rows must be at least 1 to detect re-adjusted history.")
        if tolerance < 0:
            raise ValueError("tolerance must not be negative.")
        self.root = Path(root)
        self.overlap_rows = overlap_rows
        self.tolerance = tolerance

    @classmethod
    def from_config(cls, cfg: Config, *, subdir: str = DAILY_SUBDIR, **kwargs: Any) -> BarCache:
        """Build the cache rooted at ``cfg.data.cache_dir/<subdir>``."""
        return cls(Path(cfg.data.cache_dir) / subdir, **kwargs)

    # -- paths ------------------------------------------------------------

    @staticmethod
    def _stem(symbol: str) -> str:
        return _UNSAFE_FILENAME.sub("_", symbol.strip().upper()) or "_"

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{self._stem(symbol)}{PARQUET_SUFFIX}"

    def meta_path_for(self, symbol: str) -> Path:
        return self.root / f"{self._stem(symbol)}{META_SUFFIX}"

    # -- single-symbol IO -------------------------------------------------

    def read(self, symbol: str) -> pd.DataFrame | None:
        """Read one symbol's cached bars, or ``None`` if absent or corrupt.

        A corrupt file is a warning, not an error: we delete it and let the
        caller refetch, because a half-written parquet is a normal outcome of
        a laptop lid closing mid-download.
        """
        path = self.path_for(symbol)
        if not path.is_file():
            return None
        try:
            return normalize_bars(pd.read_parquet(path))
        except Exception as exc:  # noqa: BLE001 - any read failure means "refetch"
            warnings.warn(
                f"The cached price history for {symbol} at {path} could not be read ({exc}), so "
                f"it will be downloaded again. The damaged file has been removed.",
                UserWarning,
                stacklevel=2,
            )
            path.unlink(missing_ok=True)
            self.meta_path_for(symbol).unlink(missing_ok=True)
            return None

    def read_meta(self, symbol: str) -> CacheMeta | None:
        """Read the JSON sidecar, or ``None`` if absent or unreadable."""
        path = self.meta_path_for(symbol)
        if not path.is_file():
            return None
        try:
            return CacheMeta.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            log.warning("Ignoring the unreadable cache record at %s.", path)
            return None

    def write(self, symbol: str, bars: pd.DataFrame, meta: CacheMeta) -> None:
        """Persist bars and their sidecar atomically."""
        frame = normalize_bars(bars)
        _write_atomic(self.path_for(symbol), lambda tmp: frame.to_parquet(tmp, engine="pyarrow"))
        self.write_meta(symbol, meta)

    def write_meta(self, symbol: str, meta: CacheMeta) -> None:
        _write_atomic(
            self.meta_path_for(symbol),
            lambda tmp: tmp.write_text(json.dumps(meta.to_json(), indent=2), encoding="utf-8"),
        )

    # -- the incremental read --------------------------------------------

    def get_bars(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        fetch: FetchBars,
        *,
        now: datetime | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Return bars for ``symbols`` between ``start`` and ``end`` inclusive.

        Fully cached symbols are served from disk with no call to ``fetch`` at
        all. Partly cached ones fetch only the missing tail (plus an overlap
        window); if the overlap disagrees with what we stored, that symbol's
        whole history is refetched.

        Args:
            symbols: tickers, in any case; duplicates are ignored.
            start: first calendar day wanted, inclusive.
            end: last calendar day wanted, inclusive.
            fetch: callback that actually talks to the vendor.
            now: injected clock, only used to stamp the sidecar.

        Returns:
            ``{symbol: bars}``, omitting symbols with no usable data.
        """
        stamp = _as_utc(now) if now is not None else utcnow()
        start, end = as_date(start), as_date(end)
        if start > end:
            raise ValueError(
                f"The requested price history starts on {start}, which is after it ends ({end})."
            )

        wanted = clean_symbols(symbols)
        cached: dict[str, pd.DataFrame] = {}
        covered_start: dict[str, date] = {}
        full_from: dict[str, date] = {}
        tails: dict[date, list[str]] = {}

        for symbol in wanted:
            frame = self.read(symbol)
            if frame is None or frame.empty:
                full_from[symbol] = start
                continue
            meta = self.read_meta(symbol) or self._meta_from_frame(symbol, frame, stamp)
            cached[symbol] = frame
            covered_start[symbol] = meta.covered_start
            if meta.covered_start <= start and meta.covered_end >= end:
                continue  # fully cached — no network, which is the whole point
            if start < meta.covered_start:
                full_from[symbol] = start
                continue
            tails.setdefault(self._overlap_start(frame), []).append(symbol)

        for tail_start in sorted(tails):
            group = tails[tail_start]
            fetched = self._call(fetch, group, tail_start, end)
            for symbol in group:
                fresh = fetched.get(symbol)
                if fresh is None:
                    continue  # vendor failure: keep serving what we have
                base = cached[symbol]
                if self._overlap_conflicts(base, fresh):
                    log.info(
                        "%s looks re-adjusted (its cached closes no longer match the vendor's), "
                        "so its full history is being downloaded again.",
                        symbol,
                    )
                    full_from[symbol] = min(start, covered_start[symbol])
                    continue
                merged = base if fresh.empty else self._merge(base, fresh)
                cached[symbol] = merged
                meta = self._meta(symbol, merged, covered_start[symbol], end, stamp)
                if fresh.empty:
                    self.write_meta(symbol, meta)
                else:
                    self.write(symbol, merged, meta)

        by_start: dict[date, list[str]] = {}
        for symbol, from_date in full_from.items():
            by_start.setdefault(from_date, []).append(symbol)
        for from_date in sorted(by_start):
            group = by_start[from_date]
            fetched = self._call(fetch, group, from_date, end)
            for symbol in group:
                fresh = fetched.get(symbol)
                if fresh is None or fresh.empty:
                    continue  # nothing usable — do not poison the cache
                cached[symbol] = fresh
                self.write(symbol, fresh, self._meta(symbol, fresh, from_date, end, stamp))

        left, right = pd.Timestamp(start), pd.Timestamp(end)
        out: dict[str, pd.DataFrame] = {}
        for symbol, frame in cached.items():
            window = frame.loc[left:right]
            if not window.empty:
                out[symbol] = window
        return out

    # -- internals --------------------------------------------------------

    def _overlap_start(self, frame: pd.DataFrame) -> date:
        """The date of the Nth-from-last cached bar — where a tail fetch begins."""
        rows = min(self.overlap_rows, len(frame))
        return frame.index[-rows].date()

    def _overlap_conflicts(self, base: pd.DataFrame, fresh: pd.DataFrame) -> bool:
        """True when shared closes differ by more than ``tolerance`` (relative)."""
        if fresh.empty or base.empty:
            return False
        shared = base.index.intersection(fresh.index)
        if len(shared) == 0:
            return False
        old = base.loc[shared, "close"]
        new = fresh.loc[shared, "close"]
        usable = old.notna() & new.notna() & (old != 0.0)
        if not bool(usable.any()):
            return False
        relative = (new[usable] - old[usable]).abs() / old[usable].abs()
        return bool((relative > self.tolerance).any())

    @staticmethod
    def _merge(base: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
        """Fresh rows win; older rows outside the overlap are kept."""
        keep = base.loc[~base.index.isin(fresh.index)]
        if keep.empty:
            return fresh.sort_index(kind="stable")
        return pd.concat([keep, fresh]).sort_index(kind="stable")

    @staticmethod
    def _meta(
        symbol: str, frame: pd.DataFrame, covered_start: date, covered_end: date, stamp: datetime
    ) -> CacheMeta:
        first = frame.index[0].date() if len(frame) else None
        last = frame.index[-1].date() if len(frame) else None
        return CacheMeta(
            symbol=symbol,
            covered_start=covered_start,
            covered_end=covered_end,
            first_bar=first,
            last_bar=last,
            rows=int(len(frame)),
            fetched_at=stamp,
        )

    def _meta_from_frame(self, symbol: str, frame: pd.DataFrame, stamp: datetime) -> CacheMeta:
        """Rebuild a lost sidecar from the parquet itself rather than refetching."""
        return self._meta(symbol, frame, frame.index[0].date(), frame.index[-1].date(), stamp)

    @staticmethod
    def _call(
        fetch: FetchBars, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """Run the fetch callback, turning any failure into "no data"."""
        try:
            result = fetch(symbols, start, end)
        except Exception as exc:  # noqa: BLE001 - one vendor hiccup must not stop a scan
            log.error(
                "Could not download %s between %s and %s (%s). Cached data will be used instead.",
                ", ".join(symbols[:5]) + ("..." if len(symbols) > 5 else ""),
                start,
                end,
                exc,
            )
            return {}
        return result or {}


# ---------------------------------------------------------------------------
# TTL cache for slow-moving facts (earnings dates, fundamentals)
# ---------------------------------------------------------------------------


class TtlJsonCache:
    """A JSON dict of ``key -> {fetched_at, value}`` with a time-to-live.

    Misses are cached too: if Yahoo has no earnings date for a symbol today it
    will not have one in five minutes either, and a nightly scan over 1,500
    symbols cannot afford to re-ask every time.
    """

    def __init__(self, path: Path, ttl: timedelta) -> None:
        if ttl <= timedelta(0):
            raise ValueError("The cache time-to-live must be a positive amount of time.")
        self.path = Path(path)
        self.ttl = ttl

    def read_all(self) -> dict[str, dict[str, Any]]:
        """Return the raw entries, recovering silently from a damaged file."""
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.warn(
                f"The cache file {self.path} could not be read ({exc}), so its contents will be "
                f"downloaded again.",
                UserWarning,
                stacklevel=2,
            )
            return {}
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {str(k): v for k, v in entries.items() if isinstance(v, dict)}

    def write_all(self, entries: dict[str, dict[str, Any]]) -> None:
        payload = {"version": 1, "entries": entries}
        _write_atomic(
            self.path,
            lambda tmp: tmp.write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            ),
        )

    def get_or_fetch(
        self,
        keys: Sequence[str],
        fetch: Callable[[list[str]], dict[str, Any]],
        *,
        now: datetime,
        encode: Callable[[Any], Any] = lambda value: value,
        decode: Callable[[Any], Any] = lambda value: value,
    ) -> dict[str, Any]:
        """Return a value per key, fetching only the keys that are stale or new.

        Args:
            keys: the keys wanted.
            fetch: called once with the list of stale/new keys.
            now: injected clock — TTL is measured against this.
            encode: value -> JSON-safe payload.
            decode: JSON payload -> value.

        Returns:
            ``{key: value}`` for every key that was cached or successfully
            fetched; keys the fetch could not resolve are simply absent.
        """
        moment = _as_utc(now)
        entries = self.read_all()
        out: dict[str, Any] = {}
        stale: list[str] = []
        for key in keys:
            entry = entries.get(key)
            fetched_at = _parse_stamp(entry.get("fetched_at")) if isinstance(entry, dict) else None
            if entry is None or fetched_at is None or moment - fetched_at > self.ttl:
                stale.append(key)
                continue
            try:
                out[key] = decode(entry.get("value"))
            except (TypeError, ValueError):
                stale.append(key)

        if not stale:
            return out

        fresh = self._call(fetch, stale)
        for key, value in fresh.items():
            out[key] = value
            entries[key] = {"fetched_at": moment.isoformat(), "value": encode(value)}
        if fresh:
            self.write_all(entries)
        return out

    @staticmethod
    def _call(fetch: Callable[[list[str]], dict[str, Any]], keys: list[str]) -> dict[str, Any]:
        try:
            return fetch(keys) or {}
        except Exception as exc:  # noqa: BLE001 - a stale value beats a crash
            log.error("Could not refresh %d cached values (%s).", len(keys), exc)
            return {}


def _parse_stamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None
