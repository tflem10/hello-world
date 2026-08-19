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
overlap window and compares closes; if they disagree in a way that looks like a
re-adjustment rather than rounding noise, that symbol's entire history is
refetched.

Four audit findings shaped the current version:

* **BUG-004** — the parquet and its sidecar are written separately, so a second
  process could leave a sidecar describing someone else's frame. The mutating
  half of :meth:`BarCache.get_bars` now runs under one advisory lock for the
  whole cache directory, and every read cross-checks the sidecar against the
  frame it claims to describe, rebuilding it when they disagree.
* **BUG-013** — coverage is a *range*, and a refetch must widen it at both
  ends. Asking for an earlier ``end`` used to delete every cached bar after it.
* **BUG-014** — the overlap check used to fail open twice: a re-adjustment
  smaller than the tolerance was merged (splicing two price bases into one
  series), and a response sharing no dates at all counted as agreement.
* **BUG-035** — "the vendor returned nothing" and "the vendor call failed" are
  now different outcomes; only the first advances coverage.

:class:`TtlJsonCache` is a much dumber thing: a JSON dict of
``key -> {fetched_at, value}`` used for earnings dates and fundamentals, which
change on the order of days, not minutes. It writes under the same lock, drops
long-dead keys, and can expire "we found nothing" answers faster than real ones
(audit LEAK-003, BUG-051).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time as _time
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.data.provider import as_date, as_utc, chunked, clean_symbols, normalize_bars
from swing.state import file_lock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "CACHE_LOCK_TIMEOUT",
    "DEFAULT_MEMO_LIMIT",
    "DEFAULT_OVERLAP_ROWS",
    "DEFAULT_TOLERANCE",
    "PRUNE_TTL_MULTIPLE",
    "REFETCH_AFTER_DAYS",
    "TMP_SWEEP_AFTER",
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
#: same series" — 0.02%. Lowered from 0.1% because a 0.05% dividend
#: re-adjustment reproducibly slipped underneath it and spliced two price bases
#: into one series (audit BUG-014). Differences *below* this only survive when
#: they also look like per-bar noise, never like a uniform rescale.
DEFAULT_TOLERANCE = 0.0002
#: A re-adjustment multiplies every bar by the same factor, so the spread of
#: the relative differences collapses to floating-point dust. Anything within
#: this fraction of the mean difference counts as "uniform".
UNIFORM_SPREAD_FRACTION = 0.05
#: Below this, a uniform difference is rounding in the vendor's own arithmetic
#: (1e-6 of a $100 stock is a hundredth of a cent), not a corporate action.
UNIFORM_MEAN_FLOOR = 1e-6
#: Cached history this old is refetched outright, however complete it looks:
#: small re-adjustments accumulate, and nothing else ever resets them.
REFETCH_AFTER_DAYS = 90
#: Leftover ``*.tmp`` files older than this are swept (audit LEAK-002). Each
#: orphan is a full symbol history left behind by a SIGKILL mid-write.
TMP_SWEEP_AFTER = timedelta(days=1)
#: How long a caller waits for another process's cache write before giving up
#: and serving what is already on disk. Generous, because the holder may be a
#: cold 1,500-symbol download.
CACHE_LOCK_TIMEOUT = 600.0
#: Frames memoised per :class:`BarCache` instance before the memo is dropped
#: wholesale — roughly twice the largest shipped universe (audit PERF-006).
DEFAULT_MEMO_LIMIT = 4_000
#: TTL entries older than this multiple of their TTL are dropped on write, so
#: symbols that left the universe do not live in the file forever (LEAK-003).
PRUNE_TTL_MULTIPLE = 10

_UNSAFE_FILENAME = re.compile(r"[^A-Z0-9._-]")
#: Base name of the whole-directory lock; ``file_lock`` appends ``.lock``.
_LOCK_BASENAME = ".bars"


def utcnow() -> datetime:
    """Current UTC time, timezone-aware. Only ever called at a public boundary."""
    return datetime.now(tz=UTC)


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


def sweep_stale_tmp(root: Path) -> int:
    """Delete ``*.tmp`` files in ``root`` older than :data:`TMP_SWEEP_AFTER`.

    A temp file only outlives its write when the process was killed between
    ``mkstemp`` and ``os.replace``; the survivors are dead weight of exactly
    one symbol history each (audit LEAK-002).

    This deliberately reads the wall clock rather than an injected one: a file
    modification time is a real-world fact, not part of the simulated timeline
    a caller passes in through ``now=``.

    Returns:
        How many files were removed.
    """
    if not root.is_dir():
        return 0
    cutoff = _time.time() - TMP_SWEEP_AFTER.total_seconds()
    removed = 0
    for path in root.glob("*.tmp"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:  # pragma: no cover - another process got there first
            continue
    if removed:
        log.info("Removed %d abandoned temporary file(s) from %s.", removed, root)
    return removed


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
            "fetched_at": as_utc(self.fetched_at).isoformat(),
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
                fetched_at=as_utc(datetime.fromisoformat(raw["fetched_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def describes(self, frame: pd.DataFrame) -> bool:
        """True when this sidecar actually matches the frame beside it.

        The pair is written as two separate atomic writes, so a crash or a
        second process can leave a sidecar describing a *different* frame —
        and ``get_bars`` used to believe it, serving months-truncated history
        as "fully cached, zero network" (audit BUG-004).
        """
        if self.rows != len(frame):
            return False
        last = frame.index[-1].date() if len(frame) else None
        return self.last_bar == last


@dataclass
class _Work:
    """Per-call scratch state for :meth:`BarCache.get_bars`."""

    start: date
    end: date
    stamp: datetime
    cached: dict[str, pd.DataFrame] = field(default_factory=dict)
    #: symbol -> (covered_start, covered_end) as the sidecar records it
    covered: dict[str, tuple[date, date]] = field(default_factory=dict)
    #: symbol -> (from, to) for a whole-history refetch
    full: dict[str, tuple[date, date]] = field(default_factory=dict)
    #: overlap start -> symbols sharing that cache edge
    tails: dict[date, list[str]] = field(default_factory=dict)

    @property
    def needs_network(self) -> bool:
        return bool(self.full or self.tails)


class BarCache:
    """Per-symbol parquet cache of daily bars with an overlap-checked tail fetch."""

    def __init__(
        self,
        root: Path,
        *,
        overlap_rows: int = DEFAULT_OVERLAP_ROWS,
        tolerance: float = DEFAULT_TOLERANCE,
        refetch_after_days: int = REFETCH_AFTER_DAYS,
        memo_limit: int = DEFAULT_MEMO_LIMIT,
        lock_timeout: float = CACHE_LOCK_TIMEOUT,
    ) -> None:
        """
        Args:
            root: directory holding ``<SYMBOL>.parquet`` files (created lazily).
            overlap_rows: how many cached bars a tail fetch re-requests.
            tolerance: relative close difference tolerated over the overlap.
            refetch_after_days: cached history older than this is refetched
                outright, whatever the sidecar claims to cover.
            memo_limit: in-memory frames kept before the memo is dropped.
            lock_timeout: seconds to wait for another process's cache write.
        """
        if overlap_rows < 1:
            raise ValueError("overlap_rows must be at least 1 to detect re-adjusted history.")
        if tolerance < 0:
            raise ValueError("tolerance must not be negative.")
        if refetch_after_days < 1:
            raise ValueError("refetch_after_days must be at least 1.")
        self.root = Path(root)
        self.overlap_rows = overlap_rows
        self.tolerance = tolerance
        self.refetch_after = timedelta(days=refetch_after_days)
        self.memo_limit = max(1, memo_limit)
        self.lock_timeout = lock_timeout
        # symbol -> ((mtime_ns, size), frame). Copy-on-write (pandas 3) makes
        # handing the same frame to several callers safe: a caller that writes
        # to its slice gets its own copy, and the memo keeps the original.
        self._memo: dict[str, tuple[tuple[int, int], pd.DataFrame]] = {}

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

    @property
    def lock_path(self) -> Path:
        """The file whose advisory lock serialises writers to this directory.

        One lock for the whole directory, not one per symbol: a scan touches
        1,500 symbols in a handful of batched calls, so per-symbol locking
        would buy nothing but 1,500 file descriptors. The cost is that two
        processes cannot write *different* symbols at the same time — which
        only matters when both are cold, and the loser still serves whatever
        is already on disk rather than failing.
        """
        return self.root / _LOCK_BASENAME

    # -- single-symbol IO -------------------------------------------------

    def read(self, symbol: str) -> pd.DataFrame | None:
        """Read one symbol's cached bars, or ``None`` if absent or corrupt.

        Repeat reads of an unchanged file come from an in-memory memo keyed by
        the file's modification time and size (audit PERF-006), so a scan that
        asks for the same symbol from several code paths pays one parquet read.

        A corrupt file is a logged warning, not an error: we delete it and let
        the caller refetch, because a half-written parquet is a normal outcome
        of a laptop lid closing mid-download.
        """
        path = self.path_for(symbol)
        if not path.is_file():
            self._memo.pop(symbol, None)
            return None
        try:
            info = path.stat()
        except OSError:  # pragma: no cover - vanished between the two calls
            self._memo.pop(symbol, None)
            return None
        key = (info.st_mtime_ns, info.st_size)
        remembered = self._memo.get(symbol)
        if remembered is not None and remembered[0] == key:
            return remembered[1]
        try:
            frame = normalize_bars(pd.read_parquet(path))
        except Exception as exc:  # noqa: BLE001 - any read failure means "refetch"
            # A log line, not warnings.warn: this runs once per symbol in a
            # 1,500-iteration loop, where every sibling failure logs (DEBT-014).
            log.warning(
                "The cached price history for %s at %s could not be read (%s), so it will be "
                "downloaded again. The damaged file has been removed.",
                symbol,
                path,
                exc,
            )
            path.unlink(missing_ok=True)
            self.meta_path_for(symbol).unlink(missing_ok=True)
            self._memo.pop(symbol, None)
            return None
        self._remember(symbol, key, frame)
        return frame

    def _remember(self, symbol: str, key: tuple[int, int], frame: pd.DataFrame) -> None:
        if len(self._memo) >= self.memo_limit and symbol not in self._memo:
            self._memo.clear()
        self._memo[symbol] = (key, frame)

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
        path = self.path_for(symbol)
        _write_atomic(path, lambda tmp: frame.to_parquet(tmp, engine="pyarrow"))
        try:
            info = path.stat()
        except OSError:  # pragma: no cover - only if the file vanished instantly
            self._memo.pop(symbol, None)
        else:
            self._remember(symbol, (info.st_mtime_ns, info.st_size), frame)
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
        all, and without taking any lock. Partly cached ones fetch only the
        missing tail (plus an overlap window); if the overlap disagrees with
        what we stored, that symbol's whole history is refetched — over the
        *union* of what was cached and what was asked for, so a narrower
        request can never delete bars (audit BUG-013).

        Everything that writes runs inside one advisory lock over the cache
        directory (audit BUG-004). If another process holds it for longer than
        ``lock_timeout``, this call degrades exactly as a vendor outage does:
        a warning, and whatever is already cached.

        Args:
            symbols: tickers, in any case; duplicates are ignored.
            start: first calendar day wanted, inclusive.
            end: last calendar day wanted, inclusive.
            fetch: callback that actually talks to the vendor.
            now: injected clock, used to stamp the sidecar and to decide when
                cached history is too old to trust.

        Returns:
            ``{symbol: bars}``, omitting symbols with no usable data.
        """
        stamp = as_utc(now) if now is not None else utcnow()
        start, end = as_date(start), as_date(end)
        if start > end:
            raise ValueError(
                f"The requested price history starts on {start}, which is after it ends ({end})."
            )

        sweep_stale_tmp(self.root)
        work = self._plan(clean_symbols(symbols), start, end, stamp)
        if work.needs_network:
            try:
                with file_lock(self.lock_path, timeout=self.lock_timeout):
                    self._fetch_tails(work, fetch)
                    self._fetch_full(work, fetch)
            except TimeoutError as exc:
                log.warning(
                    "Another process is still writing the price cache (%s), so this run will use "
                    "the history already on disk.",
                    exc,
                )
        return self._window(work.cached, start, end)

    # -- internals --------------------------------------------------------

    def _plan(self, wanted: list[str], start: date, end: date, stamp: datetime) -> _Work:
        """Decide, per symbol, whether we need the network and for what range."""
        work = _Work(start=start, end=end, stamp=stamp)
        for symbol in wanted:
            frame = self.read(symbol)
            if frame is None or frame.empty:
                work.full[symbol] = (start, end)
                continue
            meta = self._trusted_meta(symbol, frame, start, stamp)
            work.cached[symbol] = frame
            span = (meta.covered_start, meta.covered_end)
            work.covered[symbol] = span
            if stamp - as_utc(meta.fetched_at) > self.refetch_after:
                log.info(
                    "%s was last downloaded on %s, so its history is being refreshed in full.",
                    symbol,
                    as_utc(meta.fetched_at).date(),
                )
                work.full[symbol] = (min(start, span[0]), max(end, span[1]))
                continue
            if span[0] <= start and span[1] >= end:
                continue  # fully cached — no network, which is the whole point
            if start < span[0]:
                # The head is missing, so the tail trick cannot help. Fetch the
                # union so the refetch cannot truncate the live tail (BUG-013).
                work.full[symbol] = (start, max(end, span[1]))
                continue
            work.tails.setdefault(self._overlap_start(frame), []).append(symbol)
        return work

    def _trusted_meta(
        self, symbol: str, frame: pd.DataFrame, start: date, stamp: datetime
    ) -> CacheMeta:
        """The sidecar, or a rebuilt one when it does not match the frame.

        Self-healing for BUG-004: a sidecar that claims a different row count
        or a different last bar than the parquet beside it is describing some
        other write, and believing it is how a truncated series gets served as
        "fully cached".
        """
        meta = self.read_meta(symbol)
        if meta is None:
            return self._meta_from_frame(symbol, frame, stamp, requested_start=start)
        if not meta.describes(frame):
            log.warning(
                "The cache record for %s says %d rows ending %s but the file holds %d rows ending "
                "%s, so the record is being rebuilt from the file.",
                symbol,
                meta.rows,
                meta.last_bar,
                len(frame),
                frame.index[-1].date(),
            )
            return self._meta_from_frame(symbol, frame, stamp, requested_start=start)
        return meta

    def _fetch_tails(self, work: _Work, fetch: FetchBars) -> None:
        """Extend each partly-cached symbol, or fall back to a full refetch."""
        for tail_start in sorted(work.tails):
            group = work.tails[tail_start]
            fetched = self._call(fetch, group, tail_start, work.end)
            if fetched is None:
                continue  # the call failed: coverage stands still (audit BUG-035)
            for symbol in group:
                fresh = fetched.get(symbol)
                if fresh is None:
                    continue  # vendor dropped this symbol: keep serving what we have
                base = work.cached[symbol]
                reason = self._tail_reason(base, fresh)
                if reason is not None:
                    log.warning(
                        "%s %s, so its full history is being downloaded again.", symbol, reason
                    )
                    span = work.covered[symbol]
                    work.full[symbol] = (min(work.start, span[0]), max(work.end, span[1]))
                    continue
                span = work.covered[symbol]
                if fresh.empty:
                    # "Nothing traded" and "the vendor blinked" arrive as the
                    # same empty frame, so coverage may only advance across days
                    # that could not have traded anyway (audit BUG-035).
                    reached = _coverage_after_empty_tail(base.index[-1].date(), work.end)
                    if reached <= span[1]:
                        continue  # a trading day with no bar: ask again next run
                    self.write_meta(symbol, self._meta(symbol, base, span[0], reached, work.stamp))
                    continue
                merged = self._merge(base, fresh)
                work.cached[symbol] = merged
                self.write(
                    symbol,
                    merged,
                    self._meta(symbol, merged, span[0], max(work.end, span[1]), work.stamp),
                )

    def _fetch_full(self, work: _Work, fetch: FetchBars) -> None:
        """Download whole histories, batching symbols that share a range."""
        by_span: dict[tuple[date, date], list[str]] = {}
        for symbol, span in work.full.items():
            by_span.setdefault(span, []).append(symbol)
        for span in sorted(by_span):
            group = by_span[span]
            fetched = self._call(fetch, group, span[0], span[1])
            if fetched is None:
                continue
            for symbol in group:
                fresh = fetched.get(symbol)
                if fresh is None or fresh.empty:
                    continue  # nothing usable — do not poison the cache
                work.cached[symbol] = fresh
                self.write(symbol, fresh, self._meta(symbol, fresh, span[0], span[1], work.stamp))

    @staticmethod
    def _window(cached: dict[str, pd.DataFrame], start: date, end: date) -> dict[str, pd.DataFrame]:
        left, right = pd.Timestamp(start), pd.Timestamp(end)
        out: dict[str, pd.DataFrame] = {}
        for symbol, frame in cached.items():
            window = frame.loc[left:right]
            if not window.empty:
                out[symbol] = window
        return out

    def _overlap_start(self, frame: pd.DataFrame) -> date:
        """The date of the Nth-from-last cached bar — where a tail fetch begins."""
        rows = min(self.overlap_rows, len(frame))
        return frame.index[-rows].date()

    def _tail_reason(self, base: pd.DataFrame, fresh: pd.DataFrame) -> str | None:
        """Why this tail response cannot be appended, or ``None`` when it can.

        A tail fetch starts *at a cached bar* by construction, so a non-empty
        response that shares no dates with the cache means the vendor ignored
        the range we asked for — precisely when its price basis is least
        trustworthy. That used to read as "no conflict" (audit BUG-014b).
        """
        if fresh.empty:
            return None
        if len(base.index.intersection(fresh.index)) == 0:
            return "came back covering none of the dates we already hold"
        if self._overlap_conflicts(base, fresh):
            return "looks re-adjusted (its cached closes no longer match the vendor's)"
        return None

    def _overlap_conflicts(self, base: pd.DataFrame, fresh: pd.DataFrame) -> bool:
        """True when the shared closes cannot be two views of the same series.

        Two separate questions, because they fail differently:

        1. *Is this a re-adjustment?* A dividend or split rescales every
           historical bar by one factor, so the relative differences are all
           but identical. Any such uniform shift above the rounding floor is a
           conflict **however small it is** — a 0.05% dividend used to sail
           under the tolerance and splice two price bases together, which is
           the exact discontinuity this check exists to prevent (BUG-014a).
        2. *Is this just noise?* Scattered, non-uniform differences are the
           vendor's own rounding; they are tolerated up to ``tolerance``.
        """
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
        relative = (new[usable] - old[usable]) / old[usable].abs()
        mean = float(relative.mean())
        if len(relative) > 1 and abs(mean) > UNIFORM_MEAN_FLOOR:
            spread = float(relative.max() - relative.min())
            if spread <= UNIFORM_SPREAD_FRACTION * abs(mean):
                return True
        return bool((relative.abs() > self.tolerance).any())

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

    def _meta_from_frame(
        self,
        symbol: str,
        frame: pd.DataFrame,
        stamp: datetime,
        *,
        requested_start: date | None = None,
    ) -> CacheMeta:
        """Rebuild a lost or wrong sidecar from the parquet itself.

        ``covered_start`` is the earlier of the caller's start and the frame's
        own first bar: a symbol that listed in 2021 has no 2015 bars to fetch,
        and treating its own first bar as "the head is missing" turned a warm
        cache into a full refetch on every run (audit BUG-050).
        """
        first = frame.index[0].date()
        start = min(requested_start, first) if requested_start is not None else first
        return self._meta(symbol, frame, start, frame.index[-1].date(), stamp)

    @staticmethod
    def _call(
        fetch: FetchBars, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, pd.DataFrame] | None:
        """Run the fetch callback.

        Returns:
            The vendor's answer, or ``None`` when the call itself failed.
            "Nothing traded" and "the download blew up" must not look the same
            to the caller: recording a failure as coverage marked symbols
            up-to-date until tomorrow, so the natural rerun-an-hour-later
            stayed offline on stale bars (audit BUG-035).
        """
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
            return None
        return result or {}


# ---------------------------------------------------------------------------
# TTL cache for slow-moving facts (earnings dates, fundamentals)
# ---------------------------------------------------------------------------


class TtlJsonCache:
    """A JSON dict of ``key -> {fetched_at, value}`` with a time-to-live.

    Misses are cached too: if Yahoo has no earnings date for a symbol today it
    will not have one in five minutes either, and a nightly scan over 1,500
    symbols cannot afford to re-ask every time. They can be cached for *less*
    long than real answers though — see ``miss_ttl``.
    """

    def __init__(self, path: Path, ttl: timedelta, *, miss_ttl: timedelta | None = None) -> None:
        """
        Args:
            path: the JSON file.
            ttl: how long a real answer stays fresh.
            miss_ttl: how long a ``None`` answer stays fresh. Caching "no
                earnings date" for three days against a ten-day blackout means
                a date that appears inside the window can admit exactly the
                entry the blackout exists to block (audit BUG-051), so the
                providers set this much shorter. ``None`` means "same as
                ``ttl``".
        """
        if ttl <= timedelta(0):
            raise ValueError("The cache time-to-live must be a positive amount of time.")
        if miss_ttl is not None and miss_ttl <= timedelta(0):
            raise ValueError("The cache time-to-live must be a positive amount of time.")
        self.path = Path(path)
        self.ttl = ttl
        self.miss_ttl = miss_ttl

    def ttl_for(self, value: Any) -> timedelta:
        """The lifetime that applies to one stored value."""
        if value is None and self.miss_ttl is not None:
            return self.miss_ttl
        return self.ttl

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

    def write_all(self, entries: dict[str, dict[str, Any]], *, now: datetime | None = None) -> None:
        """Write every entry, dropping the ones nothing will ever read again.

        Symbols leave the universe and never come back, but their entries used
        to sit in the file forever, and every mutation rewrites the whole
        document (audit LEAK-003). Anything older than
        :data:`PRUNE_TTL_MULTIPLE` times its own TTL is far past refetching.
        """
        moment = as_utc(now) if now is not None else utcnow()
        keep: dict[str, dict[str, Any]] = {}
        for key, entry in entries.items():
            fetched_at = _parse_stamp(entry.get("fetched_at")) if isinstance(entry, dict) else None
            if fetched_at is None:
                continue
            if moment - fetched_at > PRUNE_TTL_MULTIPLE * self.ttl_for(entry.get("value")):
                continue
            keep[key] = entry
        payload = {"version": 1, "entries": keep}
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
        chunk_size: int = 0,
    ) -> dict[str, Any]:
        """Return a value per key, fetching only the keys that are stale or new.

        Args:
            keys: the keys wanted.
            fetch: called with the stale/new keys — once, or once per chunk.
            now: injected clock — TTL is measured against this.
            encode: value -> JSON-safe payload.
            decode: JSON payload -> value.
            chunk_size: when positive, split the stale keys into chunks of this
                size and **persist after each one**. A cold run over 1,500
                symbols used to write nothing until the last one answered, so
                a Ctrl-C at symbol 1,400 discarded the lot (audit PERF-003).

        Returns:
            ``{key: value}`` for every key that was cached or successfully
            fetched; keys the fetch could not resolve are simply absent.
        """
        moment = as_utc(now)
        entries = self.read_all()
        out: dict[str, Any] = {}
        stale: list[str] = []
        for key in keys:
            entry = entries.get(key)
            fetched_at = _parse_stamp(entry.get("fetched_at")) if isinstance(entry, dict) else None
            if entry is None or fetched_at is None:
                stale.append(key)
                continue
            if moment - fetched_at > self.ttl_for(entry.get("value")):
                stale.append(key)
                continue
            try:
                out[key] = decode(entry.get("value"))
            except (TypeError, ValueError):
                stale.append(key)

        if not stale:
            return out

        batches = chunked(stale, chunk_size) if chunk_size > 0 else iter([list(stale)])
        for batch in batches:
            fresh = self._call(fetch, batch)
            if not fresh:
                continue
            out.update(fresh)
            self._store(fresh, encode, moment)
        return out

    def _store(self, fresh: dict[str, Any], encode: Callable[[Any], Any], moment: datetime) -> None:
        """Merge new values into the file under the shared lock.

        Read-modify-write on a whole document is a lost update waiting to
        happen — the scan and the morning confirm overlap by design — so the
        file is re-read *inside* the lock and only the new keys are applied
        (audit LEAK-003, contract A1).
        """
        try:
            with file_lock(self.path, timeout=CACHE_LOCK_TIMEOUT):
                entries = self.read_all()
                for key, value in fresh.items():
                    entries[key] = {"fetched_at": moment.isoformat(), "value": encode(value)}
                self.write_all(entries, now=moment)
        except TimeoutError as exc:
            log.warning(
                "Could not save %d freshly downloaded values to %s (%s); they will be fetched "
                "again next time.",
                len(fresh),
                self.path,
                exc,
            )

    @staticmethod
    def _call(fetch: Callable[[list[str]], dict[str, Any]], keys: list[str]) -> dict[str, Any]:
        try:
            return fetch(keys) or {}
        except Exception as exc:  # noqa: BLE001 - a stale value beats a crash
            log.error("Could not refresh %d cached values (%s).", len(keys), exc)
            return {}


def _coverage_after_empty_tail(last_bar: date, end: date) -> date:
    """How far an empty tail answer may push ``covered_end``.

    An empty answer is *expected* over a weekend and *suspicious* on a trading
    day, and the vendor sends the same empty frame either way. Recording the
    suspicious one as coverage is what kept the natural rerun-an-hour-later
    offline on stale bars for the rest of the evening (audit BUG-035).

    So coverage stops at the day before the first weekday we have no bar for.
    Weekday, not exchange calendar: on the nine market holidays a year this
    costs one extra tail request per symbol, which is the cheap side of the
    trade — the other side is a whole scan running on yesterday's prices.
    """
    day = last_bar + timedelta(days=1)
    while day <= end:
        if day.weekday() < 5:
            return day - timedelta(days=1)
        day += timedelta(days=1)
    return end


def _parse_stamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None
