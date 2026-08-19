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
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.data.cache import BarCache, TtlJsonCache, utcnow
from swing.data.provider import (
    PRICE_FIELD_LABELS,
    Fundamentals,
    Quote,
    as_date,
    as_utc,
    chunked,
    clean_symbols,
    coerce_float,
    normalize_bars,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = ["EARNINGS_TTL", "FUNDAMENTALS_TTL", "YFinanceProvider"]

log = logging.getLogger(__name__)

#: Yahoo's bulk endpoint is happiest a couple of hundred tickers at a time.
DOWNLOAD_BATCH = 200
#: Earnings dates get confirmed/moved on a scale of days, not hours.
EARNINGS_TTL = timedelta(days=3)
#: Fundamentals only change when a company reports.
FUNDAMENTALS_TTL = timedelta(days=7)

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


class YFinanceProvider:
    """A :class:`~swing.data.provider.DataProvider` backed by Yahoo Finance.

    Args:
        cfg: the loaded configuration; only ``data.cache_dir`` is read.
        download: injected replacement for ``yfinance.download`` (tests).
        ticker_factory: injected replacement for ``yfinance.Ticker`` (tests).
        cache: injected :class:`BarCache`; built from ``cfg`` when omitted.
        batch_size: tickers per bulk download call.
        retries: attempts per network call, including the first.
        retry_backoff: seconds multiplier for exponential backoff; ``0``
            disables waiting entirely, which is what tests want.
        earnings_ttl / fundamentals_ttl: how long those caches stay fresh.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        download: Callable[..., Any] | None = None,
        ticker_factory: Callable[[str], Any] | None = None,
        cache: BarCache | None = None,
        batch_size: int = DOWNLOAD_BATCH,
        retries: int = 3,
        retry_backoff: float = 0.5,
        earnings_ttl: timedelta = EARNINGS_TTL,
        fundamentals_ttl: timedelta = FUNDAMENTALS_TTL,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1.")
        if retries < 1:
            raise ValueError("retries must be at least 1 (one attempt, no retry).")
        self._cfg = cfg
        self._download = download
        self._ticker_factory = ticker_factory
        self._cache = cache if cache is not None else BarCache.from_config(cfg)
        self._batch_size = batch_size
        self._retries = retries
        self._retry_backoff = retry_backoff
        cache_dir = self._cache.root.parent
        self._earnings_cache = TtlJsonCache(cache_dir / "earnings.json", earnings_ttl)
        self._fundamentals_cache = TtlJsonCache(cache_dir / "fundamentals.json", fundamentals_ttl)

    @property
    def cache(self) -> BarCache:
        """The parquet cache these bars are stored in."""
        return self._cache

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

    def _retrying(self) -> Any:
        from tenacity import Retrying, stop_after_attempt, wait_exponential

        return Retrying(
            stop=stop_after_attempt(self._retries),
            wait=wait_exponential(multiplier=self._retry_backoff, min=0, max=8),
            reraise=True,
        )

    def _with_retry(self, what: str, call: Callable[[], Any]) -> Any:
        """Run ``call`` with bounded retries; return ``None`` if it never works."""
        try:
            for attempt in self._retrying():
                with attempt:
                    return call()
        except Exception as exc:  # noqa: BLE001 - the caller decides what to skip
            log.warning("Gave up on %s after %d attempts (%s).", what, self._retries, exc)
        return None

    # -- Contract 3 -------------------------------------------------------

    def daily_bars(self, symbols: Sequence[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Auto-adjusted daily bars, served from the parquet cache where possible."""
        return self._cache.get_bars(symbols, as_date(start), as_date(end), self._fetch_bars)

    def latest_quotes(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, Quote]:
        """Latest price per symbol; symbols Yahoo cannot price are omitted."""
        stamp = as_utc(now) if now is not None else utcnow()
        out: dict[str, Quote] = {}
        for symbol in clean_symbols(symbols):
            price = self._with_retry(
                f"the latest price for {symbol}", lambda s=symbol: self._price(s)
            )
            if price is None:
                log.warning("No usable price for %s, so it will be skipped.", symbol)
                continue
            out[symbol] = Quote(symbol=symbol, price=price, asof=stamp)
        return out

    def earnings_dates(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, date | None]:
        """Next upcoming earnings date per symbol, ``None`` when Yahoo has none.

        Cached for :data:`EARNINGS_TTL`. Yahoo mixes confirmed dates with
        estimated ones and does not always say which is which; we return the
        earliest upcoming date from either source, because for an earnings
        blackout an estimate that is a few days off is far better than nothing.
        """
        stamp = as_utc(now) if now is not None else utcnow()
        today = stamp.date()
        wanted = clean_symbols(symbols)
        return self._earnings_cache.get_or_fetch(
            wanted,
            lambda keys: {key: self._next_earnings(key, today) for key in keys},
            now=stamp,
            encode=lambda value: value.isoformat() if isinstance(value, date) else None,
            decode=lambda value: date.fromisoformat(value) if isinstance(value, str) else None,
        )

    def fundamentals(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, Fundamentals]:
        """Trailing EPS and revenue growth per symbol, cached for a week."""
        stamp = as_utc(now) if now is not None else utcnow()
        wanted = clean_symbols(symbols)
        return self._fundamentals_cache.get_or_fetch(
            wanted,
            lambda keys: {key: self._fundamentals(key) for key in keys},
            now=stamp,
            encode=lambda value: {
                "symbol": value.symbol,
                "eps_growth": value.eps_growth,
                "revenue_growth": value.revenue_growth,
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
        for batch in chunked(wanted, self._batch_size):
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
        if frame is None:
            return None
        if not isinstance(frame, pd.DataFrame):
            log.warning(
                "Yahoo returned something that is not a table for %s.", ", ".join(batch[:5])
            )
            return None
        return frame

    # -- quotes -----------------------------------------------------------

    def _price(self, symbol: str) -> float | None:
        ticker = self._ticker(symbol)
        fast = getattr(ticker, "fast_info", None)
        for attr in _PRICE_ATTRS:
            price = coerce_float(_lookup(fast, attr))
            if price is not None and price > 0:
                return price
        history = getattr(ticker, "history", None)
        if callable(history):
            frame = history(period="5d", auto_adjust=True)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                for column in ("Close", "close"):
                    if column in frame.columns:
                        closes = frame[column].dropna()
                        if not closes.empty:
                            price = coerce_float(closes.iloc[-1])
                            if price is not None and price > 0:
                                return price
        return None

    # -- earnings ---------------------------------------------------------

    def _next_earnings(self, symbol: str, today: date) -> date | None:
        candidates = self._with_retry(
            f"the earnings calendar for {symbol}", lambda: self._earnings_candidates(symbol)
        )
        upcoming = sorted(day for day in (candidates or []) if day >= today)
        return upcoming[0] if upcoming else None

    def _earnings_candidates(self, symbol: str) -> list[date]:
        ticker = self._ticker(symbol)
        days = _dates_from_frame(_safe_call(ticker, "get_earnings_dates", limit=16))
        if days:
            return days
        calendar = _safe_call(ticker, "get_calendar")
        if calendar is None:
            calendar = getattr(ticker, "calendar", None)
        return _dates_from_calendar(calendar)

    # -- fundamentals -----------------------------------------------------

    def _fundamentals(self, symbol: str) -> Fundamentals:
        """Best-effort growth figures; ``None`` beats a made-up number."""
        info = (
            self._with_retry(f"the company profile for {symbol}", lambda: self._info(symbol)) or {}
        )
        eps = _first_number(info, _EPS_INFO_KEYS)
        revenue = _first_number(info, _REVENUE_INFO_KEYS)
        if eps is None or revenue is None:
            statement = self._with_retry(
                f"the income statement for {symbol}", lambda: self._financials(symbol)
            )
            if eps is None:
                eps = _growth_from_statement(statement, _EPS_ROWS)
            if revenue is None:
                revenue = _growth_from_statement(statement, _REVENUE_ROWS)
        return Fundamentals(symbol=symbol, eps_growth=eps, revenue_growth=revenue)

    def _info(self, symbol: str) -> dict[str, Any]:
        ticker = self._ticker(symbol)
        info = _safe_call(ticker, "get_info")
        if info is None:
            info = getattr(ticker, "info", None)
        return info if isinstance(info, dict) else {}

    def _financials(self, symbol: str) -> pd.DataFrame | None:
        ticker = self._ticker(symbol)
        for name in ("get_income_stmt", "get_financials"):
            frame = _safe_call(ticker, name)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                return frame
        for attr in ("income_stmt", "financials"):
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
        return {symbols[0]: frame} if len(symbols) == 1 else {}

    wanted = {symbol.upper() for symbol in symbols}
    level = _ticker_level(columns, wanted)
    labels = {str(v).strip().upper(): v for v in columns.get_level_values(level).unique()}
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        label = labels.get(symbol.upper())
        if label is None:
            log.warning("Yahoo returned no data for %s in this batch, so it is skipped.", symbol)
            continue
        out[symbol] = frame.xs(label, axis=1, level=level)
    return out


def _ticker_level(columns: pd.MultiIndex, wanted: set[str]) -> int:
    """Pick the column level that holds tickers rather than OHLCV field names."""
    best_level, best_score = 0, float("-inf")
    for level in range(columns.nlevels):
        values = {str(v).strip().upper() for v in columns.get_level_values(level)}
        size = max(len(values), 1)
        score = len(values & wanted) / size - len(values & PRICE_FIELD_LABELS) / size
        if score > best_score:
            best_level, best_score = level, score
    return best_level


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
    """Call ``obj.name(**kwargs)`` if it exists, swallowing vendor exceptions."""
    method = getattr(obj, name, None)
    if not callable(method):
        return None
    try:
        return method(**kwargs)
    except Exception as exc:  # noqa: BLE001 - every yfinance accessor can throw
        log.debug("%s() failed (%s).", name, exc)
        return None


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
    """Period-over-period growth for the first matching row of an income statement.

    yfinance returns statements with one column per reporting period, newest
    first — but not reliably, so we sort by column date before comparing.
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
        usable = [value for value in numbers if value is not None]
        if len(usable) < 2:
            continue
        latest, prior = usable[-1], usable[-2]
        if prior == 0:
            continue
        return (latest - prior) / abs(prior)
    return None


def _decode_fundamentals(payload: Any) -> Fundamentals:
    if not isinstance(payload, dict):
        raise ValueError("A cached fundamentals record was not a record.")
    return Fundamentals(
        symbol=str(payload.get("symbol", "")),
        eps_growth=coerce_float(payload.get("eps_growth")),
        revenue_growth=coerce_float(payload.get("revenue_growth")),
    )
