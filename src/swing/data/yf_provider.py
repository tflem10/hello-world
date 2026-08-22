"""The default data provider: free Yahoo data via ``yfinance``, parquet-cached.

Everything here is written on the assumption that Yahoo is a best-effort,
unsupported, occasionally-wrong data source, because it is:

* a bulk download of 200 tickers may silently omit one of them,
* fundamentals are present for one symbol and absent for the next,
* the same request can fail once and succeed a second later.

So: bulk downloads in batches with bounded retries, per-symbol failures logged
and skipped, every scalar run through a "is this actually a number" gate, and
anything slow-moving (earnings, fundamentals) cached with a TTL. The
:mod:`yfinance` import itself is lazy so that ``import swing.data`` stays cheap
and offline tests never touch it.

The per-symbol calls (quotes, earnings, fundamentals) run in a small thread
pool and persist in chunks rather than in one final write, because serially
walking 1,500 symbols took thousands of round trips and a Ctrl-C near the end
threw all of them away (audit PERF-002, PERF-003).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.data.cache import BarCache, TtlJsonCache, utcnow
from swing.data.provider import (
    DEFAULT_WORKERS,
    Fundamentals,
    Quote,
    RetryPolicy,
    as_date,
    as_utc,
    choose_header_level,
    chunked,
    clean_symbols,
    coerce_float,
    map_concurrent,
    normalize_bars,
    with_retry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "EARNINGS_CHUNK_PAUSE",
    "EARNINGS_EMPTY_LIMIT",
    "EARNINGS_HISTORY_TTL",
    "EARNINGS_MISS_TTL",
    "EARNINGS_SETTLE_LAG",
    "EARNINGS_THROTTLE_COOLDOWN",
    "EARNINGS_THROTTLE_RETRIES",
    "EARNINGS_TTL",
    "FUNDAMENTALS_TTL",
    "EarningsUnavailable",
    "YFinanceProvider",
    "settled_history_ttl",
]

log = logging.getLogger(__name__)


class EarningsUnavailable(RuntimeError):
    """Yahoo could not be asked about a symbol's earnings at all.

    Distinct from "Yahoo says there is no announcement", and deliberately an
    exception rather than a return value so it cannot be quietly treated as
    one. The retry policy turns a persistent one into ``None``, and ``None``
    is the one answer this module refuses to cache (audit COVER-1).
    """


#: Yahoo's bulk endpoint is happiest a couple of hundred tickers at a time.
DOWNLOAD_BATCH = 200
#: Earnings dates get confirmed/moved on a scale of days, not hours. This is
#: the TTL for :meth:`YFinanceProvider.earnings_dates` — the *next* upcoming
#: announcement, which is genuinely volatile — and the floor under the
#: history TTL below.
EARNINGS_TTL = timedelta(days=3)
#: But "Yahoo has no date for this symbol" is a *much* weaker statement, and
#: caching it for three days against a ten-day blackout lets a newly published
#: date slip through the window it exists to block (audit BUG-051).
EARNINGS_MISS_TTL = timedelta(hours=12)
#: How long after an announcement its date stops moving.
#:
#: ``get_earnings_dates`` returns one row per quarter, and the rows for
#: quarters that have not happened yet hold Yahoo's *estimate*. An estimate
#: slips: a company pencils in "late April", confirms a date two weeks out,
#: and occasionally moves it again. Once the company has actually reported,
#: the row is a matter of record and never changes again. A month is
#: comfortably past the usual estimate-to-actual slippage, so a date this far
#: behind the moment we captured it is treated as settled history.
EARNINGS_SETTLE_LAG = timedelta(days=30)
#: Ceiling on how long a *settled* earnings-history entry stays fresh.
#:
#: A company's 2014 earnings date is immutable, so the honest answer to "when
#: should this expire?" is "never" — and refetching is not merely pointless,
#: it is lossy: the vendor returns the most recent
#: :data:`EARNINGS_HISTORY_LIMIT` announcements, so the window walks *forward*
#: with the fetch date and a refetch in 2036 would drop the 2011 rows a 2026
#: fetch still holds. A finite ceiling exists only so the entry is not
#: literally immortal after a vendor backfill or a schema change; five years
#: is long enough that no backtest ever trips it.
#:
#: The ceiling doubles as the history cache's *base* TTL, which is what
#: :meth:`TtlJsonCache.write_all` prunes against. That is deliberate: at ten
#: times five years nothing is ever pruned, and a delisted symbol's earnings
#: history is exactly the record a point-in-time backtest of an earlier period
#: needs (audit LEAK-003 traded away knowingly, for ~250 bytes a symbol).
EARNINGS_HISTORY_TTL = timedelta(days=365 * 5)
#: Fundamentals only change when a company reports.
FUNDAMENTALS_TTL = timedelta(days=7)
#: Symbols per persisted chunk of a cold earnings/fundamentals walk.
FETCH_CHUNK = 50
#: Past *and* future announcements, enough for a decade of backtests.
EARNINGS_HISTORY_LIMIT = 60
#: Seconds to wait between chunks of a cold earnings-history walk.
#:
#: A cold walk is ~1,500 requests. Sent as fast as the thread pool can make
#: them, the endpoint answers a few hundred and then stops — which is how the
#: cache came to hold 1,507 nulls (audit COVER-1). Thirty seconds spread over a
#: sweep that takes minutes is a trade worth making every time.
EARNINGS_CHUNK_PAUSE = 2.0
#: Seconds to wait after a chunk that looked rate-limited; doubles per retry.
EARNINGS_THROTTLE_COOLDOWN = 15.0
#: Extra attempts for a chunk that looked rate-limited, after the first.
EARNINGS_THROTTLE_RETRIES = 2
#: Fraction of a chunk that may come back empty before we stop believing it.
#:
#: Measured, not guessed: a healthy burst of 30 symbols answered 28 (7% empty),
#: and the universe is roughly 8% ETFs, which genuinely have no earnings. A
#: throttled batch is empty at or near 100%. Anywhere between the two is safe,
#: and erring high only costs a re-ask.
EARNINGS_EMPTY_LIMIT = 0.6
#: Batches smaller than this say nothing about rate limiting either way.
EARNINGS_MIN_BATCH = 8

_EPS_INFO_KEYS = ("earningsQuarterlyGrowth", "earningsGrowth")
_REVENUE_INFO_KEYS = ("revenueGrowth", "revenueQuarterlyGrowth")
_EPS_ROWS = ("Diluted EPS", "Basic EPS")
_REVENUE_ROWS = ("Total Revenue", "OperatingRevenue", "Operating Revenue")
_PRICE_ATTRS = (
    "last_price",
    "lastPrice",
    "regularMarketPrice",
    "regular_market_price",
    "previous_close",
    "previousClose",
)
#: ``info``'s growth figures are quarter-over-quarter year-on-year.
_INFO_BASIS = "quarterly"


@dataclass
class _Sweep:
    """Running state for one walk over the earnings-history endpoint."""

    #: Chunks sent so far, so the first one need not wait.
    chunks: int = 0
    #: Symbols the vendor actually answered for.
    asked: int = 0
    #: ...of which came back with at least one date.
    dated: int = 0
    #: Symbols in batches that looked rate-limited, so nothing was cached.
    unanswered: int = 0


def _looks_throttled(keys: Sequence[str], answered: Mapping[str, Sequence[date]]) -> bool:
    """Is this batch's silence the vendor's answer, or the vendor refusing?

    One empty reply is ordinary: an ETF has no earnings, and a handful of real
    equities have no rows on any given day. A batch that is *almost entirely*
    empty is not fifty companies that never reported — it is one rate limiter,
    and believing it is what wrote Microsoft down as having no earnings history
    (audit COVER-1).

    Hard failures and empty answers are counted together on purpose. A
    throttled reply can arrive either way — as a raised ``YFRateLimitError`` or
    as an empty frame — and which one you get is not something a caller can
    depend on. Counting both means the guard does not have to know.

    Erring towards "throttled" is the safe direction: the cost of a false
    positive is asking again next run, and the cost of a false negative is a
    wrong fact cached against every backtest that reads it.
    """
    if len(keys) < EARNINGS_MIN_BATCH:
        return False  # too small to infer a rate limit from
    silent = len(keys) - sum(1 for days in answered.values() if days)
    return silent / len(keys) > EARNINGS_EMPTY_LIMIT


def settled_history_ttl(
    end: date,
    *,
    now: datetime,
    lag: timedelta = EARNINGS_SETTLE_LAG,
    floor: timedelta = EARNINGS_TTL,
    ceiling: timedelta = EARNINGS_HISTORY_TTL,
) -> timedelta:
    """How long a cached earnings history may serve a window ending on ``end``.

    One cached record holds both kinds of date at once — that is the boundary a
    naive "history is immutable, so cache it forever" split walks into. The
    record Yahoo returns for AAPL today lists announcements back to 2011 *and*
    an estimate for next quarter, under a single ``fetched_at``. Freezing the
    whole record for five years would freeze the estimate too.

    So the expiry is a property of the **window the caller asked for**, not of
    the record. A record captured at ``F`` has settled every date on or before
    ``F - lag``; a request for ``[start, end]`` only ever exposes dates up to
    ``end``; so the record answers that request immutably exactly when
    ``end <= F - lag``. Rearranged against the cache's own ``now - F <= ttl``
    test, that is a time-to-live of ``now - end - lag`` — long for a backtest
    that ended in 2024, negative for a scan that runs to today.

    The clamp keeps both ends sane. ``floor`` is the live behaviour, unchanged:
    a window reaching into the unsettled zone ages at :data:`EARNINGS_TTL`,
    because part of what it exposes really can still move. ``ceiling`` is
    :data:`EARNINGS_HISTORY_TTL`.

    Note the lag is measured against the *fetch* time, not the present. A date
    twelve days old when we captured it does not become trustworthy merely
    because a month has since passed on our clock: it was an estimate when we
    wrote it down, and it stays one until someone refetches.

    Args:
        end: last calendar day the caller will read out of the record.
        now: injected clock.
        lag: how far behind the fetch a date must be to count as settled.
        floor: shortest lifetime this may return — the volatile-data TTL.
        ceiling: longest lifetime this may return.

    Returns:
        The time-to-live to apply to real (non-``None``) history entries.
        Misses are not covered: an absence of data is never settled, and keeps
        :data:`EARNINGS_MISS_TTL` (audit BUG-051).
    """
    # Midnight *after* ``end``: the window includes the whole of that day, so
    # the day is only behind us once it has finished.
    finished = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    settled = as_utc(now) - finished - lag
    if settled <= floor:
        return floor
    return min(settled, ceiling)


class YFinanceProvider:
    """A :class:`~swing.data.provider.DataProvider` backed by Yahoo Finance.

    Args:
        cfg: the loaded configuration; ``data.cache_dir`` and the ``data``
            network knobs (``retries``, ``retry_backoff``, ``download_batch``)
            are read from it.
        download: injected replacement for ``yfinance.download`` (tests).
        ticker_factory: injected replacement for ``yfinance.Ticker`` (tests).
        cache: injected :class:`BarCache`; built from ``cfg`` when omitted.
        batch_size: tickers per bulk download call; defaults to
            ``cfg.data.download_batch``.
        retries: attempts per network call, including the first; defaults to
            ``cfg.data.retries``.
        retry_backoff: seconds multiplier for exponential backoff; ``0``
            disables waiting entirely, which is what tests want. Defaults to
            ``cfg.data.retry_backoff``.
        workers: upper bound on concurrent per-symbol calls.
        chunk_size: symbols per persisted chunk of a cold TTL-cache walk.
        earnings_ttl: how long the *next upcoming* announcement stays fresh,
            and the floor under the history TTL.
        earnings_history_ttl: ceiling on how long a settled historical window
            stays fresh. See :func:`settled_history_ttl`.
        earnings_settle_lag: how far behind its fetch a date must be before it
            counts as settled history rather than a live estimate.
        history_pause: seconds between chunks of a cold earnings-history walk.
        history_cooldown: seconds to wait after a chunk that looked rate
            limited, doubling on each further attempt.
        history_retries: extra attempts for such a chunk, after the first.
        sleep: injected replacement for :func:`time.sleep`, so tests can watch
            the pacing without waiting for it.
        fundamentals_ttl: how long the fundamentals cache stays fresh.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        download: Callable[..., Any] | None = None,
        ticker_factory: Callable[[str], Any] | None = None,
        cache: BarCache | None = None,
        batch_size: int | None = None,
        retries: int | None = None,
        retry_backoff: float | None = None,
        workers: int = DEFAULT_WORKERS,
        chunk_size: int = FETCH_CHUNK,
        earnings_ttl: timedelta = EARNINGS_TTL,
        earnings_history_ttl: timedelta = EARNINGS_HISTORY_TTL,
        earnings_settle_lag: timedelta = EARNINGS_SETTLE_LAG,
        history_pause: float = EARNINGS_CHUNK_PAUSE,
        history_cooldown: float = EARNINGS_THROTTLE_COOLDOWN,
        history_retries: int = EARNINGS_THROTTLE_RETRIES,
        sleep: Callable[[float], None] | None = None,
        fundamentals_ttl: timedelta = FUNDAMENTALS_TTL,
    ) -> None:
        if earnings_history_ttl < earnings_ttl:
            raise ValueError(
                "Historical earnings must not expire sooner than the next upcoming date "
                f"({earnings_history_ttl} against {earnings_ttl}): a past announcement is the "
                "more stable of the two, never the less stable one."
            )
        if earnings_settle_lag < timedelta(0):
            raise ValueError(
                "The earnings settle lag is how long after an announcement its date stops "
                "moving, so it cannot be negative."
            )
        self._policy = RetryPolicy.from_config(
            cfg, retries=retries, backoff=retry_backoff, batch=batch_size
        )
        self._cfg = cfg
        self._download = download
        self._ticker_factory = ticker_factory
        self._cache = cache if cache is not None else BarCache.from_config(cfg)
        self._workers = max(1, workers)
        self._chunk_size = max(1, chunk_size)
        self._earnings_ttl = earnings_ttl
        self._earnings_history_ttl = earnings_history_ttl
        self._earnings_settle_lag = earnings_settle_lag
        self._history_pause = max(0.0, history_pause)
        self._history_cooldown = max(0.0, history_cooldown)
        self._history_retries = max(0, history_retries)
        self._sleep = sleep if sleep is not None else time.sleep
        cache_dir = self._cache.root.parent
        self._earnings_cache = TtlJsonCache(
            cache_dir / "earnings.json", earnings_ttl, miss_ttl=EARNINGS_MISS_TTL
        )
        # The history cache's *base* TTL is the long one. It is what
        # ``write_all`` prunes against, and pruning settled history at ten
        # times three days would delete the very entries this scheme exists to
        # keep — including the delisted symbols a point-in-time backtest wants.
        # The short lifetime is applied per call instead, from the window.
        self._earnings_history_cache = TtlJsonCache(
            cache_dir / "earnings-history.json", earnings_history_ttl, miss_ttl=EARNINGS_MISS_TTL
        )
        self._fundamentals_cache = TtlJsonCache(cache_dir / "fundamentals.json", fundamentals_ttl)

    @property
    def cache(self) -> BarCache:
        """The parquet cache these bars are stored in."""
        return self._cache

    @property
    def batch_size(self) -> int:
        """Tickers per bulk download call."""
        return self._policy.batch

    # -- lazy yfinance handles -------------------------------------------

    def _downloader(self) -> Callable[..., Any]:
        if self._download is None:
            import yfinance  # imported here so the module stays import-cheap

            self._download = yfinance.download
        return self._download

    def _ticker(self, symbol: str) -> Any:
        if self._ticker_factory is None:
            import yfinance

            self._ticker_factory = yfinance.Ticker
        return self._ticker_factory(symbol)

    def _with_retry(self, what: str, call: Callable[[], Any]) -> Any:
        """Run ``call`` under the configured retry policy (audit DEBT-013)."""
        return with_retry(what, call, policy=self._policy)

    def _map(self, keys: Sequence[str], call: Callable[[str], Any]) -> dict[str, Any]:
        """Run a per-symbol call over ``keys`` in the bounded pool."""
        return map_concurrent(keys, call, workers=self._workers)

    # -- Contract 3 -------------------------------------------------------

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date, *, now: datetime | None = None
    ) -> dict[str, pd.DataFrame]:
        """Auto-adjusted daily bars, served from the parquet cache where possible.

        ``now`` is injected only to stamp and age the cache records; it exists
        so tests and backtests can pin the clock the way every other call in
        this contract already lets them (audit DEBT-014).
        """
        return self._cache.get_bars(
            symbols, as_date(start), as_date(end), self._fetch_bars, now=now
        )

    def latest_quotes(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, Quote]:
        """Latest price per symbol; symbols Yahoo cannot price are omitted.

        The intraday ``fast_info`` price is still the answer wherever Yahoo has
        one — a stale close would quietly weaken the execution drift guard —
        but the probes now run concurrently instead of one blocking round trip
        after another, and the symbols that come back unpriced are resolved by
        a *single* bulk download rather than one 5-day history call each
        (audit PERF-002).
        """
        stamp = as_utc(now) if now is not None else utcnow()
        wanted = clean_symbols(symbols)
        prices: dict[str, float | None] = dict(self._map(wanted, self._quote_price))

        missing = [symbol for symbol in wanted if prices.get(symbol) is None]
        if missing:
            prices.update(self._bulk_last_closes(missing))

        out: dict[str, Quote] = {}
        for symbol in wanted:
            price = prices.get(symbol)
            if price is None:
                log.warning("No usable price for %s, so it will be skipped.", symbol)
                continue
            out[symbol] = Quote(symbol=symbol, price=price, asof=stamp)
        return out

    def earnings_dates(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, date | None]:
        """Next upcoming earnings date per symbol, ``None`` when Yahoo has none.

        Cached for :data:`EARNINGS_TTL` — but a ``None`` only for
        :data:`EARNINGS_MISS_TTL`. Three days is right *here* and nowhere else
        in this module: the next announcement is the one earnings fact that
        genuinely moves. :meth:`earnings_history` keeps its own, far longer
        expiry. Yahoo mixes confirmed dates with estimated
        ones and does not always say which is which; we return the earliest
        upcoming date from either source, because for an earnings blackout an
        estimate that is a few days off is far better than nothing.
        """
        stamp = as_utc(now) if now is not None else utcnow()
        today = stamp.date()
        wanted = clean_symbols(symbols)
        sweep = _Sweep()
        out = self._earnings_cache.get_or_fetch(
            wanted,
            lambda keys: self._fetch_earnings_chunk(
                keys,
                sweep,
                lambda key: self._upcoming_earnings(key, today),
                lambda days: days[0] if days else None,
                what="upcoming earnings date",
            ),
            now=stamp,
            chunk_size=self._chunk_size,
            encode=lambda value: value.isoformat() if isinstance(value, date) else None,
            decode=lambda value: date.fromisoformat(value) if isinstance(value, str) else None,
        )
        if sweep.chunks:
            self._log_earnings_refetch(sweep, len(wanted), what="upcoming earnings dates")
        return out

    def earnings_history(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        *,
        now: datetime | None = None,
    ) -> dict[str, tuple[date, ...]]:
        """Announcement dates intersecting ``[start, end]``, past and future.

        Amendment A12. yfinance already returns past dates from
        ``get_earnings_dates``; the contract simply used to throw them away, so
        every historical blackout check in the backtest was fed a single future
        date and blocked nothing (audit BUG-036).

        The *unfiltered* list is what gets cached, keyed by symbol alone, so
        two callers asking for different windows share one download.

        **Expiry depends on the window, not the clock** (audit REPRO-1). This
        used to share :data:`EARNINGS_TTL` with the upcoming-date cache, so a
        backtest over 2014-2024 re-downloaded ~1,500 symbols of immutable
        history every three days — and a comparison run either side of that
        boundary silently changed its own answer, with an identical
        ``config_hash``, ``data_hash`` and ``code_ref`` because none of the
        three can see earnings. A window that ends in the settled past now has
        an effectively unbounded lifetime; a window that runs to today keeps
        the old three days. :func:`settled_history_ttl` derives both.

        A ``None`` — "Yahoo has no history for this symbol" — is exempt and
        still expires at :data:`EARNINGS_MISS_TTL` whatever the window, because
        an absence of data is not settled data: it is the one answer that can
        turn into a real one at any moment (audit BUG-051). That leaves a
        genuine, deliberate source of run-to-run drift, which is precisely what
        :func:`~swing.data.cache.earnings_fingerprint` exists to make visible.

        Returns:
            ``{symbol: (dates...)}`` for every requested symbol, sorted and
            unique. An empty tuple means "unknown", never "there were none".
        """
        stamp = as_utc(now) if now is not None else utcnow()
        first, last = as_date(start), as_date(end)
        wanted = clean_symbols(symbols)
        ttl = settled_history_ttl(
            last,
            now=stamp,
            lag=self._earnings_settle_lag,
            floor=self._earnings_ttl,
            ceiling=self._earnings_history_ttl,
        )
        sweep = _Sweep()
        known = self._earnings_history_cache.get_or_fetch(
            wanted,
            lambda keys: self._fetch_earnings_chunk(
                keys,
                sweep,
                self._earnings_history,
                lambda days: tuple(sorted(set(days))) if days else None,
                what="earnings history",
            ),
            now=stamp,
            ttl=ttl,
            chunk_size=self._chunk_size,
            encode=_encode_days,
            decode=_decode_days,
        )
        if sweep.chunks:
            self._log_history_refetch(sweep, len(wanted), last, ttl)
        return {
            symbol: tuple(day for day in (known.get(symbol) or ()) if first <= day <= last)
            for symbol in wanted
        }

    def _log_earnings_refetch(self, sweep: _Sweep, wanted: int, *, what: str) -> None:
        """Say how much came off the wire, and whether the vendor gave up.

        Three things a reader needs and used to have no way to learn: how much
        was downloaded, how much of it actually carried dates, and whether the
        vendor stopped answering partway through.
        """
        if sweep.unanswered:
            log.warning(
                "Yahoo stopped answering while downloading %s: %d symbols went unanswered and "
                "were deliberately not cached, so they will be asked again rather than treated as "
                "symbols with no earnings. Re-run to fill them in; the earnings blackout is "
                "weaker than usual until you do.",
                what,
                sweep.unanswered,
            )
        log.info(
            "Downloaded %s for %d of %d symbols; %d had dates.",
            what,
            sweep.asked,
            wanted,
            sweep.dated,
        )

    def _log_history_refetch(self, sweep: _Sweep, wanted: int, end: date, ttl: timedelta) -> None:
        """The shared report, plus the window caveat that only history has."""
        self._log_earnings_refetch(sweep, wanted, what="earnings history")
        if ttl > self._earnings_ttl:
            return
        log.info(
            "The requested window ends on %s, which is inside the %d-day period where an "
            "announcement date can still move, so the whole record expires on the same short "
            "schedule as a live date and comes back down with it. Two runs either side of that "
            "can read different earnings and so produce different trades, with nothing in the "
            "run's config, data or code hash to show it. Ending the window on an earlier, fixed "
            "day keeps repeated runs comparable.",
            end,
            self._earnings_settle_lag.days,
        )

    def fundamentals(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, Fundamentals]:
        """Trailing EPS and revenue growth per symbol, cached for a week."""
        stamp = as_utc(now) if now is not None else utcnow()
        wanted = clean_symbols(symbols)
        return self._fundamentals_cache.get_or_fetch(
            wanted,
            lambda keys: self._map(keys, self._fundamentals),
            now=stamp,
            chunk_size=self._chunk_size,
            encode=lambda value: {
                "symbol": value.symbol,
                "eps_growth": value.eps_growth,
                "revenue_growth": value.revenue_growth,
                "basis": value.basis,
            },
            decode=_decode_fundamentals,
        )

    # -- bars -------------------------------------------------------------

    def _fetch_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """The cache's fetch callback: bulk download, then normalise per symbol."""
        wanted = clean_symbols(symbols)
        out: dict[str, pd.DataFrame] = {}
        for batch in chunked(wanted, self._policy.batch):
            frame = self._download_batch(batch, start, end)
            if frame is None:
                continue
            for symbol, raw in _split_download(frame, batch).items():
                try:
                    bars = normalize_bars(raw)
                except ValueError as exc:
                    log.warning("Skipping %s: its price history was unreadable (%s).", symbol, exc)
                    continue
                out[symbol] = bars
        return out

    def _download_batch(self, batch: list[str], start: date, end: date) -> pd.DataFrame | None:
        download = self._downloader()

        def call() -> Any:
            # yfinance treats ``end`` as exclusive; Contract 3 is inclusive.
            return download(
                batch,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="ticker",
                actions=False,
            )

        frame = self._with_retry(f"the price history for {len(batch)} symbols", call)
        return self._usable_frame(frame, batch)

    def _usable_frame(self, frame: Any, batch: Sequence[str]) -> pd.DataFrame | None:
        if frame is None:
            return None
        if not isinstance(frame, pd.DataFrame):
            log.warning(
                "Yahoo returned something that is not a table for %s.", ", ".join(batch[:5])
            )
            return None
        return frame

    # -- quotes -----------------------------------------------------------

    def _quote_price(self, symbol: str) -> float | None:
        """The freshest price Yahoo will admit to for one symbol, or ``None``."""
        return self._with_retry(f"the latest price for {symbol}", lambda: self._fast_price(symbol))

    def _fast_price(self, symbol: str) -> float | None:
        ticker = self._ticker(symbol)
        fast = getattr(ticker, "fast_info", None)
        for attr in _PRICE_ATTRS:
            price = coerce_float(_lookup(fast, attr))
            if price is not None and price > 0:
                return price
        return None

    def _bulk_last_closes(self, symbols: Sequence[str]) -> dict[str, float]:
        """One download for every symbol ``fast_info`` could not price.

        Replaces a per-symbol 5-day history call, which was the slowest part of
        a quote sweep over an arbitrary list (audit PERF-002).
        """
        batch = list(symbols)
        download = self._downloader()

        def call() -> Any:
            return download(
                batch,
                period="5d",
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="ticker",
                actions=False,
            )

        frame = self._usable_frame(
            self._with_retry(f"recent closes for {len(batch)} symbols", call), batch
        )
        if frame is None:
            return {}
        out: dict[str, float] = {}
        for symbol, raw in _split_download(frame, batch).items():
            price = _last_close(raw)
            if price is not None:
                out[symbol] = price
        return out

    # -- earnings ---------------------------------------------------------

    def _upcoming_earnings(self, symbol: str, today: date) -> list[date] | None:
        """Announcement dates from ``today`` onwards, soonest first.

        Returns:
            The upcoming dates; an **empty list** when the vendor answered and
            has none; or ``None`` when the vendor could not be asked at all.

            That last distinction is the point (audit COVER-1 on the live
            path). A rate-limited reply used to arrive here as "no upcoming
            earnings date", get cached as one, and let the scanner enter a
            position the blackout existed to prevent — BUG-051's failure with
            real money behind it. "Unknown" now means genuinely unknown, which
            is what makes the pipeline's *earnings unknown* warning tag
            truthful rather than accidental.
        """
        candidates = self._with_retry(
            f"the earnings calendar for {symbol}", lambda: self._earnings_candidates(symbol)
        )
        if candidates is None:
            return None
        return sorted(day for day in candidates if day >= today)

    def _earnings_candidates(self, symbol: str) -> list[date]:
        """Every announcement date Yahoo will offer for one symbol.

        Two sources, because Yahoo populates them inconsistently. Either one
        *answering* is enough to make an empty result meaningful; only when
        neither could be read at all is the answer unknown, and then this
        raises so the retry policy above it — and ultimately the cache — can
        tell "there is no date" from "we never got to ask".

        Raises:
            EarningsUnavailable: neither the earnings table nor the calendar
                could be read.
        """
        ticker = self._ticker(symbol)
        frame, table_answered = _ask(ticker, "get_earnings_dates", limit=16)
        days = _dates_from_frame(frame)
        if days:
            return days

        calendar, calendar_answered = _ask(ticker, "get_calendar")
        if calendar is None:
            calendar, attr_answered = _read_attr(ticker, "calendar")
            calendar_answered |= attr_answered
        days = _dates_from_calendar(calendar)
        if days or table_answered or calendar_answered:
            return days
        raise EarningsUnavailable(
            f"Neither Yahoo's earnings table nor its calendar could be read for {symbol}, so "
            f"whether it has an upcoming announcement is unknown."
        )

    def _earnings_history(self, symbol: str) -> list[date] | None:
        """Every announcement date Yahoo remembers for one symbol.

        Returns:
            The dates; an **empty list** when the vendor answered and had none;
            or ``None`` when the vendor could not be asked at all. Those last
            two used to be the same value, which is how 1,369 S&P constituents
            — Microsoft, JPMorgan and Exxon among them — came to be recorded as
            companies that have never reported earnings (audit COVER-1).
        """

        def call() -> list[date]:
            # Deliberately *not* through ``_safe_call``. That helper turns
            # every exception into ``None``, so a rate-limit reply never
            # reached the retry policy above it and arrived here looking
            # exactly like "this company has no earnings".
            ticker = self._ticker(symbol)
            method = getattr(ticker, "get_earnings_dates", None)
            if not callable(method):
                raise AttributeError(f"{symbol} has no get_earnings_dates() to ask.")
            return _dates_from_frame(method(limit=EARNINGS_HISTORY_LIMIT))

        return self._with_retry(f"the earnings history for {symbol}", call)

    def _fetch_earnings_chunk(
        self,
        keys: list[str],
        sweep: _Sweep,
        ask: Callable[[str], Sequence[date] | None],
        store: Callable[[Sequence[date]], Any],
        *,
        what: str,
    ) -> dict[str, Any]:
        """Ask about one chunk of symbols, keeping only answers we can trust.

        One mechanism for both earnings endpoints, because they fail the same
        way and there is no version of this worth getting right twice.

        Args:
            keys: the symbols in this chunk.
            sweep: running state for the whole walk.
            ask: per-symbol call returning the dates found, an **empty**
                sequence when the vendor answered and had none, or ``None``
                when the vendor could not be asked at all.
            store: turns a trusted answer into the value the cache should hold.
            what: noun phrase for the log lines.

        Returns:
            Values for the symbols we trust. Keys missing from it are **not
            written to the cache** — :meth:`TtlJsonCache.get_or_fetch` stores
            only what it is handed — so a symbol we could not ask about stays
            stale and is asked again next run, instead of being written down as
            a fact about the company.
        """
        for attempt in range(1 + self._history_retries):
            self._pace(sweep, attempt)
            answers = self._map(keys, ask)
            answered = {key: days for key, days in answers.items() if days is not None}
            if not _looks_throttled(keys, answered):
                sweep.asked += len(answered)
                sweep.dated += sum(1 for days in answered.values() if days)
                return {key: store(days) for key, days in answered.items()}
            log.warning(
                "Yahoo answered %d of %d %s requests in this batch, which looks like rate "
                "limiting rather than %d symbols with nothing to report. Nothing from this batch "
                "will be cached%s.",
                sum(1 for days in answered.values() if days),
                len(keys),
                what,
                len(keys),
                "; waiting and trying again" if attempt < self._history_retries else "",
            )
        sweep.unanswered += len(keys)
        return {}

    def _pace(self, sweep: _Sweep, attempt: int) -> None:
        """Wait before a chunk — briefly between them, at length after a refusal.

        Resilience beats speed here. A cold walk over 1,500 symbols is a few
        thousand requests, and the endpoint stops answering partway through if
        they arrive as fast as the thread pool can make them.
        """
        if attempt:
            self._sleep(self._history_cooldown * (2 ** (attempt - 1)))
        elif sweep.chunks:
            self._sleep(self._history_pause)
        sweep.chunks += 1

    # -- fundamentals -----------------------------------------------------

    def _fundamentals(self, symbol: str) -> Fundamentals:
        """Best-effort growth figures; ``None`` beats a made-up number."""
        info = (
            self._with_retry(f"the company profile for {symbol}", lambda: self._info(symbol)) or {}
        )
        eps = _first_number(info, _EPS_INFO_KEYS)
        revenue = _first_number(info, _REVENUE_INFO_KEYS)
        bases = {_INFO_BASIS} if (eps is not None or revenue is not None) else set()

        if eps is None or revenue is None:
            for basis, statement in self._statements(symbol):
                if eps is None:
                    eps = _growth_from_statement(statement, _EPS_ROWS)
                    if eps is not None:
                        bases.add(basis)
                if revenue is None:
                    revenue = _growth_from_statement(statement, _REVENUE_ROWS)
                    if revenue is not None:
                        bases.add(basis)
                if eps is not None and revenue is not None:
                    break
        return Fundamentals(
            symbol=symbol, eps_growth=eps, revenue_growth=revenue, basis=_basis_label(bases)
        )

    def _info(self, symbol: str) -> dict[str, Any]:
        ticker = self._ticker(symbol)
        info = _safe_call(ticker, "get_info")
        if info is None:
            info = getattr(ticker, "info", None)
        return info if isinstance(info, dict) else {}

    def _statements(self, symbol: str) -> Iterator[tuple[str, pd.DataFrame]]:
        """Yield ``(basis, income statement)``, quarterly before annual.

        The fallback used to be whatever ``get_income_stmt()`` returned, which
        is the *annual* statement — silently mixing a year-over-year quarter
        from ``info`` with a year-over-year year from here (audit BUG-037).
        Asking for the quarterly frequency first keeps the two comparable, and
        whichever one answers is recorded in ``Fundamentals.basis``.
        """
        for basis, quarterly in (("quarterly", True), ("annual", False)):
            frame = self._with_retry(
                f"the {basis} income statement for {symbol}",
                lambda q=quarterly: self._financials(symbol, quarterly=q),
            )
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                yield basis, frame

    def _financials(self, symbol: str, *, quarterly: bool) -> pd.DataFrame | None:
        ticker = self._ticker(symbol)
        kwargs: dict[str, Any] = {"freq": "quarterly"} if quarterly else {}
        for name in ("get_income_stmt", "get_financials"):
            frame = _safe_call(ticker, name, **kwargs)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        attrs = (
            ("quarterly_income_stmt", "quarterly_financials")
            if quarterly
            else ("income_stmt", "financials")
        )
        for attr in attrs:
            frame = getattr(ticker, attr, None)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        return None


# ---------------------------------------------------------------------------
# module-level helpers (kept out of the class so tests can hit them directly)
# ---------------------------------------------------------------------------


def _split_download(frame: pd.DataFrame, symbols: Sequence[str]) -> dict[str, pd.DataFrame]:
    """Split a ``yfinance.download`` frame into one sub-frame per symbol.

    Handles both column layouts (``group_by="ticker"`` and ``group_by="column"``)
    by working out which header level holds tickers rather than price fields,
    which matters for real tickers like ``OPEN`` that collide with field names.
    """
    columns = frame.columns
    if not isinstance(columns, pd.MultiIndex):
        if len(symbols) == 1:
            return {symbols[0]: frame}
        # Yahoo flattens the header when all but one ticker in the batch fail.
        # A whole 200-symbol batch evaporating deserves more than a DEBUG line:
        # downstream it looks exactly like a bear market (audit BUG-015).
        log.warning(
            "Yahoo returned a single unlabelled table for a batch of %d symbols (%s...), so none "
            "of them could be read. This is usually a transient failure — the cached history is "
            "used instead.",
            len(symbols),
            ", ".join(symbols[:5]),
        )
        return {}

    wanted = {symbol.upper() for symbol in symbols}
    level = choose_header_level(columns, tickers=wanted)
    labels = {str(v).strip().upper(): v for v in columns.get_level_values(level).unique()}
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        label = labels.get(symbol.upper())
        if label is None:
            log.warning("Yahoo returned no data for %s in this batch, so it is skipped.", symbol)
            continue
        out[symbol] = frame.xs(label, axis=1, level=level)
    return out


def _last_close(frame: Any) -> float | None:
    """The most recent usable close in a vendor sub-frame."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    for column in frame.columns:
        if str(column).strip().lower() not in ("close", "adj close", "adjclose"):
            continue
        closes = frame[column].dropna()
        if closes.empty:
            continue
        price = coerce_float(closes.iloc[-1])
        if price is not None and price > 0:
            return price
    return None


def _lookup(container: Any, key: str) -> Any:
    """Read ``key`` off an object that may be an attribute bag or a mapping."""
    if container is None:
        return None
    try:
        value = getattr(container, key)
    except Exception:  # noqa: BLE001 - yfinance's FastInfo raises for unknown keys
        value = None
    if value is not None:
        return value
    try:
        return container[key]
    except Exception:  # noqa: BLE001 - not a mapping, or no such key
        return None


def _safe_call(obj: Any, name: str, **kwargs: Any) -> Any:
    """Call ``obj.name(**kwargs)`` if it exists, swallowing vendor exceptions.

    Only for callers where "the vendor refused" and "the vendor has nothing"
    genuinely lead to the same decision — the fundamentals screen, which is
    documented to pass on missing data either way. Anything that *caches* the
    answer wants :func:`_ask` instead, because collapsing the two writes a
    refusal down as a fact (audit COVER-1).
    """
    return _ask(obj, name, **kwargs)[0]


def _ask(obj: Any, name: str, **kwargs: Any) -> tuple[Any, bool]:
    """Call ``obj.name(**kwargs)``, reporting whether the vendor actually answered.

    Returns:
        ``(result, answered)``. ``answered`` is ``False`` when the accessor is
        missing or threw — which is *not* the same as the vendor telling us
        there is nothing, however identical the two look from here. Conflating
        them is what let a rate-limited reply be cached as "this company has no
        earnings" (audit COVER-1).
    """
    method = getattr(obj, name, None)
    if not callable(method):
        return None, False
    try:
        return method(**kwargs), True
    except Exception as exc:  # noqa: BLE001 - every yfinance accessor can throw
        log.debug("%s() failed (%s).", name, exc)
        return None, False


def _read_attr(obj: Any, name: str) -> tuple[Any, bool]:
    """Read ``obj.name``, reporting whether it could be read at all.

    yfinance exposes some of the same data as a lazily-computed property, which
    means a plain attribute read can perform a request and raise like any other.

    Deliberately no ``getattr`` default: a default turns "there is no such
    attribute" into a successful read of ``None``, which is exactly the
    conflation this whole change exists to remove.
    """
    try:
        return getattr(obj, name), True
    except AttributeError:
        return None, False  # nothing here to read, so nothing was asked
    except Exception as exc:  # noqa: BLE001 - a yfinance property can throw
        log.debug("Reading .%s failed (%s).", name, exc)
        return None, False


def _dates_from_frame(frame: Any) -> list[date]:
    """Pull calendar dates out of a ``get_earnings_dates`` style frame."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        try:
            index = pd.DatetimeIndex(pd.to_datetime(index))
        except Exception:  # noqa: BLE001 - not a date index after all
            return []
    if index.tz is not None:
        index = index.tz_localize(None)
    return [stamp.date() for stamp in index if pd.notna(stamp)]


def _dates_from_calendar(calendar: Any) -> list[date]:
    """Pull dates out of ``Ticker.calendar``, which has had three shapes."""
    if calendar is None:
        return []
    values: Any = None
    if isinstance(calendar, dict):
        values = calendar.get("Earnings Date") or calendar.get("earnings_date")
    elif isinstance(calendar, pd.DataFrame):
        for label in ("Earnings Date", "earningsDate"):
            if label in calendar.index:
                values = list(calendar.loc[label])
                break
        if values is None and not calendar.empty:
            values = list(calendar.iloc[0])
    if values is None:
        return []
    if not isinstance(values, list | tuple | set | pd.Series | pd.Index):
        values = [values]
    out: list[date] = []
    for value in values:
        if isinstance(value, datetime):  # pd.Timestamp subclasses datetime
            out.append(value.date())
        elif isinstance(value, date):
            out.append(value)
        elif isinstance(value, str):
            try:
                out.append(date.fromisoformat(value[:10]))
            except ValueError:
                continue
    return out


def _first_number(info: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = coerce_float(info.get(key))
        if value is not None:
            return value
    return None


def _growth_from_statement(frame: Any, rows: Sequence[str]) -> float | None:
    """Growth between two *adjacent* reporting periods of an income statement.

    yfinance returns statements with one column per reporting period, newest
    first — but not reliably, so we sort by column date before comparing.

    Adjacency matters: dropping the unreadable periods and comparing whatever
    survived turned a two-year gap into "one period of growth" with no way to
    tell (audit BUG-037). We now walk back from the newest period and take the
    first *neighbouring* pair that is usable, or give up.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty or len(frame.columns) < 2:
        return None
    for row in rows:
        if row not in frame.index:
            continue
        try:
            series = frame.loc[row]
        except Exception:  # noqa: BLE001 - duplicate labels and other oddities
            continue
        if isinstance(series, pd.DataFrame):
            series = series.iloc[0]
        with contextlib.suppress(TypeError):  # an unsortable header stays as-is
            series = series.sort_index(ascending=True)
        numbers = [coerce_float(value) for value in series.tolist()]
        for index in range(len(numbers) - 1, 0, -1):
            latest, prior = numbers[index], numbers[index - 1]
            if latest is None or prior is None or prior == 0:
                continue
            return (latest - prior) / abs(prior)
    return None


def _basis_label(bases: set[str]) -> str | None:
    """One word for where the growth figures came from."""
    if not bases:
        return None
    if len(bases) == 1:
        return next(iter(bases))
    return "mixed"


def _encode_days(value: Any) -> Any:
    if not value:
        return None
    return [day.isoformat() for day in value]


def _decode_days(payload: Any) -> tuple[date, ...] | None:
    if payload is None:
        return None
    if not isinstance(payload, list):
        raise ValueError("A cached earnings history was not a list of dates.")
    return tuple(date.fromisoformat(str(item)) for item in payload)


def _decode_fundamentals(payload: Any) -> Fundamentals:
    if not isinstance(payload, dict):
        raise ValueError("A cached fundamentals record was not a record.")
    basis = payload.get("basis")
    return Fundamentals(
        symbol=str(payload.get("symbol", "")),
        eps_growth=coerce_float(payload.get("eps_growth")),
        revenue_growth=coerce_float(payload.get("revenue_growth")),
        basis=str(basis) if isinstance(basis, str) and basis else None,
    )
