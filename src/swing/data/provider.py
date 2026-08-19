"""FROZEN CONTRACT 3 — the shape every data provider must satisfy.

Nothing in here talks to a network. It defines the value objects the rest of
the system passes around (:class:`Quote`, :class:`Fundamentals`), the
:class:`DataProvider` protocol that ``yf_provider`` and ``schwab_provider``
implement, and the one function that guarantees both of them hand back bars in
*exactly* the same shape — :func:`normalize_bars`.

The bars format is deliberately strict, because every indicator, rule and
backtest downstream assumes it:

* index: tz-naive, ascending, unique ``DatetimeIndex`` at midnight, **no name**
* columns: exactly ``open, high, low, close, volume``, all ``float64``
* OHLC are auto-adjusted (splits *and* dividends), so a cached series can be
  compared against a freshly fetched one to detect a re-adjustment.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd

__all__ = [
    "BAR_COLUMNS",
    "PRICE_FIELD_LABELS",
    "DataProvider",
    "Fundamentals",
    "Quote",
    "as_date",
    "as_utc",
    "chunked",
    "clean_symbols",
    "coerce_float",
    "empty_bars",
    "normalize_bars",
]

log = logging.getLogger(__name__)

#: The only columns a bars frame may have, in this order.
BAR_COLUMNS: tuple[str, str, str, str, str] = ("open", "high", "low", "close", "volume")

#: Column labels a vendor may use for a price field — used to tell a "field"
#: column level apart from a "ticker" one, and to spot an unadjusted close.
PRICE_FIELD_LABELS = frozenset(
    {
        "OPEN",
        "HIGH",
        "LOW",
        "CLOSE",
        "ADJ CLOSE",
        "ADJCLOSE",
        "VOLUME",
        "DIVIDENDS",
        "STOCK SPLITS",
        "CAPITAL GAINS",
    }
)

_CLOSE_ALIASES = ("adj close", "adjclose", "adjusted close")


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """The most recent price we could get for one symbol.

    ``asof`` is timezone-aware UTC: a quote without a timestamp is worthless
    for the execution guardrails, and a naive one is a bug waiting to happen.
    """

    symbol: str
    price: float
    asof: datetime


@dataclass(frozen=True)
class Fundamentals:
    """Trailing growth for one symbol, as *fractions* — ``0.18`` means +18%.

    Both fields are ``None`` when the vendor has nothing usable, which is
    common: free fundamental data is spotty, and a wrong number is worse than
    a missing one. Consumers must treat ``None`` as "unknown", never as zero.
    """

    symbol: str
    eps_growth: float | None
    revenue_growth: float | None


@runtime_checkable
class DataProvider(Protocol):
    """Everything the strategy needs from the outside world.

    Implementations must tolerate individual symbol failures: a delisted or
    misspelled ticker is logged and skipped, never raised, so one bad symbol
    can never take down a nightly scan over 1,500 of them. That means callers
    must expect the returned dicts to be *missing keys*.
    """

    def daily_bars(self, symbols: Sequence[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        """Auto-adjusted daily bars per symbol, inclusive of ``start`` and ``end``."""
        ...

    def latest_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """The freshest price available per symbol."""
        ...

    def earnings_dates(self, symbols: Sequence[str]) -> dict[str, date | None]:
        """Next upcoming earnings date per symbol, or ``None`` when unknown."""
        ...

    def fundamentals(self, symbols: Sequence[str]) -> dict[str, Fundamentals]:
        """Trailing EPS and revenue growth per symbol."""
        ...


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------


def clean_symbols(symbols: Iterable[str]) -> list[str]:
    """Upper-case, strip and de-duplicate tickers, keeping the caller's order."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    """Yield ``items`` in lists of at most ``size`` (never yields an empty list)."""
    if size < 1:
        raise ValueError("Batch size must be at least 1.")
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime so every stamp we store or compare lines up.

    A caller who injects ``now=datetime(2026, 8, 18, 17, 30)`` means a real
    moment, not an ambiguous one, so we read a naive value as UTC rather than
    letting it leak into a :class:`Quote` (whose ``asof`` is promised to be
    timezone-aware) or into a TTL subtraction (which raises on mixed operands).
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def as_date(value: date | datetime | pd.Timestamp | str) -> date:
    """Coerce anything date-ish to a plain ``datetime.date``."""
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"Expected a date, but got {value!r} ({type(value).__name__}).")


def empty_bars() -> pd.DataFrame:
    """An empty frame in the Contract 3 bars format — safe to concat or slice."""
    return pd.DataFrame(
        {name: pd.Series(dtype="float64") for name in BAR_COLUMNS},
        index=pd.DatetimeIndex([], name=None),
    )


# ---------------------------------------------------------------------------
# normalisation — the single definition of "a bars frame"
# ---------------------------------------------------------------------------


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse a MultiIndex column header down to the price-field level."""
    columns = frame.columns
    if not isinstance(columns, pd.MultiIndex):
        return frame
    best_level = columns.nlevels - 1
    best_score = -1.0
    for level in range(columns.nlevels):
        values = {str(v).strip().upper() for v in columns.get_level_values(level)}
        score = len(values & PRICE_FIELD_LABELS) / max(len(values), 1)
        if score > best_score:
            best_score, best_level = score, level
    frame = frame.copy()
    frame.columns = pd.Index(columns.get_level_values(best_level))
    return frame


def _normalized_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    """Return a tz-naive, midnight-stamped, unnamed ``DatetimeIndex``."""
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        try:
            index = pd.DatetimeIndex(pd.to_datetime(index))
        except Exception as exc:  # pragma: no cover - vendor-specific junk
            raise ValueError(
                f"The index of this price history is not a set of dates ({exc}). "
                f"Expected daily timestamps."
            ) from exc
    if index.tz is not None:
        # tz_localize(None) keeps the *local* wall clock, so a bar stamped
        # 00:00-05:00 stays on its own trading date instead of shifting a day.
        index = index.tz_localize(None)
    # ``name=None`` in the constructor means "unspecified", so rename explicitly:
    # a stray "Date" index name is exactly the kind of thing that breaks frame
    # comparisons against test fixtures.
    return pd.DatetimeIndex(index.normalize()).rename(None)


def normalize_bars(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Coerce a vendor frame into the Contract 3 bars format.

    Handles everything the wild throws at us: MultiIndex column headers,
    ``Close`` vs ``close``, tz-aware stamps, duplicated dates, out-of-order
    rows, integer volumes and all-NaN padding rows.

    Args:
        frame: any vendor-shaped OHLCV frame, or ``None``.

    Returns:
        A frame with a tz-naive ascending unique ``DatetimeIndex`` (no name)
        and float columns exactly ``open, high, low, close, volume``.

    Raises:
        ValueError: if the frame has no recognisable price columns or its
            values cannot be read as numbers. Callers are expected to catch
            this per symbol, log it and move on.
    """
    if frame is None:
        return empty_bars()
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"Expected a table of price history, but got a {type(frame).__name__}.")
    if frame.empty and len(frame.columns) == 0:
        return empty_bars()

    out = _flatten_columns(frame)
    out = out.copy()
    out.columns = pd.Index([str(c).strip().lower() for c in out.columns])
    out = out.loc[:, ~out.columns.duplicated(keep="first")]

    if "close" not in out.columns:
        for alias in _CLOSE_ALIASES:
            if alias in out.columns:
                out = out.rename(columns={alias: "close"})
                break

    missing = [name for name in ("open", "high", "low", "close") if name not in out.columns]
    if missing:
        found = ", ".join(map(str, out.columns)) or "nothing"
        raise ValueError(
            f"This price history is missing the {', '.join(missing)} column"
            f"{'s' if len(missing) > 1 else ''}. Got: {found}."
        )
    if "volume" not in out.columns:
        out["volume"] = 0.0

    out = out.loc[:, list(BAR_COLUMNS)]
    out.index = _normalized_index(out)

    try:
        out = out.astype("float64")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"This price history holds values that are not numbers ({exc}).") from exc

    out = out.sort_index(kind="stable")
    out = out.loc[~out.index.duplicated(keep="last")]
    out = out.dropna(how="all")
    return out


def coerce_float(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` if it is not usable.

    Vendors return ``None``, ``"N/A"``, ``NaN`` and ``0`` interchangeably for
    "no data", so every scalar coming off an API goes through here.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(number) or number in (float("inf"), float("-inf")):
        return None
    return number
