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
* **every OHLC value is finite** (amendment A3, audit BUG-005): a row whose
  open, high, low or close is NaN or infinite is not a bar, it is a vendor
  padding artefact, and it is dropped here rather than left for the engine to
  turn into a NaN equity curve. A missing *volume* is not fatal — it becomes
  ``0.0``, which every consumer already reads as "no liquidity recorded".

The retry policy and the bounded thread pool live here too, so both providers
obey the same configured limits instead of two copies of the same literals
(audit DEBT-013).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "BAR_COLUMNS",
    "DEFAULT_WORKERS",
    "PRICE_FIELD_LABELS",
    "DataProvider",
    "Fundamentals",
    "Quote",
    "RetryPolicy",
    "as_date",
    "as_utc",
    "choose_header_level",
    "chunked",
    "clean_symbols",
    "coerce_float",
    "empty_bars",
    "is_contract_shaped",
    "map_concurrent",
    "normalize_bars",
    "with_retry",
]

log = logging.getLogger(__name__)

T = TypeVar("T")

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

#: The four columns that make a row an actual bar. Volume is not one of them.
_CORE_PRICE_FIELDS = frozenset({"OPEN", "HIGH", "LOW", "CLOSE"})

_CLOSE_ALIASES = ("adj close", "adjclose", "adjusted close")

#: Defaults mirrored from :class:`swing.config.DataCfg`, used when a provider
#: is built without a config (tests) or from an older config object.
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.5
DEFAULT_BATCH = 200
#: Upper bound on concurrent per-symbol vendor calls. Deliberately small: the
#: point is to stop waiting serially on 1,500 round trips, not to hammer a free
#: endpoint into rate-limiting us (audit PERF-002/003/007).
DEFAULT_WORKERS = 8


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

    Both growth fields are ``None`` when the vendor has nothing usable, which
    is common: free fundamental data is spotty, and a wrong number is worse
    than a missing one. Consumers must treat ``None`` as "unknown", never as
    zero.

    ``basis`` says *what period* the growth compares, because the sources do
    not agree and used to be mixed silently (audit BUG-037): ``"quarterly"``
    for a year-over-year quarter, ``"annual"`` for a year-over-year full year,
    ``"mixed"`` when the two fields came from different sources, and ``None``
    when nothing is known. It is informational — the screen only reads the
    sign — but an unlabelled number is a number nobody can check.
    """

    symbol: str
    eps_growth: float | None
    revenue_growth: float | None
    basis: str | None = None


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

    def earnings_history(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, tuple[date, ...]]:
        """Announcement dates per symbol intersecting ``[start, end]``, past and future.

        Amendment A12. :meth:`earnings_dates` answers "when do they report
        next", which is the only question a live scan asks — and exactly the
        wrong one for a backtest, which needs to know where the blackouts
        *were* (audit BUG-036). An empty tuple means "we do not know", never
        "there were none": a vendor with no calendar and a company that never
        reported look identical from here.
        """
        ...

    def fundamentals(self, symbols: Sequence[str]) -> dict[str, Fundamentals]:
        """Trailing EPS and revenue growth per symbol."""
        ...


# ---------------------------------------------------------------------------
# retries and concurrency, shared by both providers (audit DEBT-013)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """How hard a provider tries, and how many symbols it asks for at once.

    Read from ``[data]`` in the config so a rate-limited user can back off
    without editing source, which used to be the only remedy (audit DEBT-013).
    """

    retries: int = DEFAULT_RETRIES
    backoff: float = DEFAULT_RETRY_BACKOFF
    batch: int = DEFAULT_BATCH

    def __post_init__(self) -> None:
        if self.retries < 1:
            raise ValueError("retries must be at least 1 (one attempt, no retry).")
        if self.backoff < 0:
            raise ValueError("retry_backoff must not be negative.")
        if self.batch < 1:
            raise ValueError("batch_size must be at least 1.")

    @classmethod
    def from_config(
        cls,
        cfg: Config | None,
        *,
        retries: int | None = None,
        backoff: float | None = None,
        batch: int | None = None,
    ) -> RetryPolicy:
        """Build the policy from ``cfg.data``; explicit arguments win over it."""
        data = getattr(cfg, "data", None)
        return cls(
            retries=int(
                retries if retries is not None else getattr(data, "retries", DEFAULT_RETRIES)
            ),
            backoff=float(
                backoff
                if backoff is not None
                else getattr(data, "retry_backoff", DEFAULT_RETRY_BACKOFF)
            ),
            batch=int(
                batch if batch is not None else getattr(data, "download_batch", DEFAULT_BATCH)
            ),
        )


def with_retry(what: str, call: Callable[[], T], *, policy: RetryPolicy) -> T | None:
    """Run ``call`` with bounded exponential-backoff retries.

    Args:
        what: a noun phrase naming the thing being fetched, for the log line.
        call: the zero-argument vendor call.
        policy: attempt count and backoff.

    Returns:
        Whatever ``call`` returned, or ``None`` if every attempt raised. A
        vendor hiccup on one symbol must never end a scan over 1,500 of them,
        so the exception is logged and swallowed here.
    """
    from tenacity import Retrying, stop_after_attempt, wait_exponential

    try:
        for attempt in Retrying(
            stop=stop_after_attempt(policy.retries),
            wait=wait_exponential(multiplier=policy.backoff, min=0, max=8),
            reraise=True,
        ):
            with attempt:
                return call()
    except Exception as exc:  # noqa: BLE001 - the caller decides what to skip
        log.warning("Gave up on %s after %d attempts (%s).", what, policy.retries, exc)
    return None


def map_concurrent(
    items: Sequence[str], call: Callable[[str], T], *, workers: int = DEFAULT_WORKERS
) -> dict[str, T | None]:
    """Apply ``call`` to every item in a bounded thread pool.

    The result is keyed by item in the *input* order, so a caller that iterates
    it stays deterministic even though the calls did not finish in that order.
    A single item, or ``workers <= 1``, runs inline — cheaper, and it keeps
    tracebacks readable in tests.

    An exception from one item is logged and becomes ``None`` for that item
    only; the rest of the batch still comes back.
    """
    if not items:
        return {}
    unique = list(dict.fromkeys(items))
    if workers <= 1 or len(unique) == 1:
        return {item: _guarded(call, item) for item in unique}
    with ThreadPoolExecutor(max_workers=min(workers, len(unique))) as pool:
        return dict(zip(unique, pool.map(lambda item: _guarded(call, item), unique), strict=True))


def _guarded(call: Callable[[str], T], item: str) -> T | None:
    try:
        return call(item)
    except Exception as exc:  # noqa: BLE001 - one symbol must not sink the batch
        log.warning("Could not fetch %s (%s), so it is skipped.", item, exc)
        return None


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


def choose_header_level(columns: pd.MultiIndex, *, tickers: Collection[str] | None = None) -> int:
    """Pick the header level holding price fields, or tickers when given.

    One heuristic answers both questions, and it is *subtractive*: a level
    scores how much it looks like what we want **minus** how much it looks like
    the other thing. Purely additive scoring is what a ticker literally named
    ``OPEN`` defeats (audit BUG-048) — in a single-symbol frame the ticker
    level and the field level both scored a perfect 1.0 for "field-ness", the
    ticker level was reached first, and the whole frame collapsed to one
    column and a "missing the high, low, close column" error.

    Args:
        columns: the MultiIndex header.
        tickers: when given, find the level holding *these* symbols; when
            ``None``, find the level holding OHLCV field names.

    Returns:
        The winning level index.
    """
    wanted = {str(symbol).strip().upper() for symbol in (tickers or ())}
    best_level, best_score = 0, float("-inf")
    for level in range(columns.nlevels):
        values = {str(value).strip().upper() for value in columns.get_level_values(level)}
        size = max(len(values), 1)
        if tickers is None:
            # Reward covering the OHLC core; penalise every label on the level
            # that is not a price field at all (i.e. that looks like a ticker).
            score = len(values & _CORE_PRICE_FIELDS) / len(_CORE_PRICE_FIELDS) - (
                len(values - PRICE_FIELD_LABELS) / size
            )
        else:
            score = (len(values & wanted) - len(values & PRICE_FIELD_LABELS)) / size
        if score > best_score:
            best_level, best_score = level, score
    return best_level


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse a MultiIndex column header down to the price-field level."""
    columns = frame.columns
    if not isinstance(columns, pd.MultiIndex):
        return frame
    frame = frame.copy()
    frame.columns = pd.Index(columns.get_level_values(choose_header_level(columns)))
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


def is_contract_shaped(frame: pd.DataFrame) -> bool:
    """True when ``frame`` already satisfies Contract 3 exactly.

    The fast path for :func:`normalize_bars` (audit PERF-006). Every warm scan
    re-reads ~1,500 parquet files that *this module wrote*, so they are already
    in the contract format; normalising them again cost roughly five full frame
    copies each. These checks are all O(rows) at worst and none of them copies.
    """
    if list(frame.columns) != list(BAR_COLUMNS):
        return False
    if any(dtype != "float64" for dtype in frame.dtypes):
        return False
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is not None or index.name is not None:
        return False
    if len(index) == 0:
        return True
    if not index.is_monotonic_increasing or not index.is_unique:
        return False
    if bool((index.normalize() != index).any()):
        return False
    return bool(np.isfinite(frame.to_numpy(dtype="float64", copy=False)).all())


def _drop_unusable_rows(out: pd.DataFrame) -> pd.DataFrame:
    """Apply amendment A3: complete bars only, and a finite volume.

    A row with a NaN open but a real close is not a partially-known bar, it is
    a vendor artefact that turns into NaN P&L, NaN equity and a fabricated
    drawdown three layers downstream (audit BUG-005). Volume is different: it
    is not a price, nothing prices a fill off it, and "unknown volume" already
    reads as "no liquidity" everywhere, so it becomes ``0.0``.
    """
    prices = out.loc[:, ["open", "high", "low", "close"]].to_numpy(dtype="float64", copy=False)
    complete = np.isfinite(prices).all(axis=1)
    if not complete.all():
        out = out.loc[complete]
    volume = out["volume"].to_numpy(dtype="float64", copy=False)
    if not np.isfinite(volume).all():
        out = out.copy()
        out["volume"] = np.where(np.isfinite(volume), volume, 0.0)
    return out


def normalize_bars(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Coerce a vendor frame into the Contract 3 bars format.

    Handles everything the wild throws at us: MultiIndex column headers,
    ``Close`` vs ``close``, tz-aware stamps, duplicated dates, out-of-order
    rows, integer volumes and NaN padding rows.

    Args:
        frame: any vendor-shaped OHLCV frame, or ``None``.

    Returns:
        A frame with a tz-naive ascending unique ``DatetimeIndex`` (no name),
        float columns exactly ``open, high, low, close, volume``, finite OHLC
        on every row and a finite volume (amendment A3). A frame that already
        satisfies all of that is returned unchanged, not copied — treat the
        result as read-only, as every consumer already does.

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
    if is_contract_shaped(frame):
        return frame

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
    return _drop_unusable_rows(out)


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
