"""FROZEN CONTRACT 11 — the cross-sectional daily-bar portfolio simulator.

This module is the audit surface of the whole project: if the engine cheats,
every number downstream is a lie. So the semantics are spelled out here in
prose, and the code below is commented against this list rather than against
itself.

THE DAY, IN ORDER
-----------------
For each date ``t`` on the master calendar the engine does exactly four things,
in this order:

1. **Exits, at the open or intraday.** Evaluated against the effective stop as
   it stood at the *close of t-1* — never a stop that knows today's prices.
2. **Entries, at the open.** These fill the orders that were *decided at the
   close of t-1*. Exits run first, so cash and slots freed this morning are
   usable by this morning's entries (Contract 11: "process exits BEFORE
   entries").
3. **Close-of-day bookkeeping.** Ratchet every open position's stop, mark the
   portfolio to the close, append one equity row.
4. **Signal generation for tomorrow.** Read the WP-D rule Series at ``t``,
   rank the candidates, and queue the top ones for a fill at ``open(t+1)``.

The load-bearing consequence: a signal is computed from data ``<= t`` and
filled at ``open(t+1)``. There is no path in this file by which a decision made
on bar ``t`` can see bar ``t``'s high, low, or any later bar.

ENTRY GATE
----------
A symbol is a candidate on bar ``t`` when ALL of these are true at ``t``::

    trend_template & entry_signal & liquidity_ok & entries_allowed & ~earnings_blackout

``entries_allowed`` (the SPY regime filter) gates **entries only**. A regime
that turns off does not close existing positions; they keep trailing their
stops until a stop, the time stop, or the end of data takes them out.

EXIT LADDER (evaluated in this order, first match wins)
-------------------------------------------------------
a. **Gap-through at the open.** ``open(t) <= effective_stop(t-1)`` — the market
   gapped past the stop overnight, so the fill is at ``open(t)``, not at the
   stop. This is the single most commonly faked-away cost in retail backtests.
b. **Time stop.** A time stop signalled at the close of ``t-1`` fills at
   ``open(t)``. It is checked after (a) because a gap through the stop is the
   worse fill and happens at the same instant.
c. **Intraday breach.** ``low(t) <= effective_stop(t-1)`` with an open above
   it — the stop order is resting in the book, so it fills AT the stop price.
d. **End of data.** Anything still open on the last bar is marked out at that
   bar's close with reason ``end_of_data``.

Reasons (a) and (c) are reported as ``chandelier`` when the effective stop has
ratcheted above where it started, and ``stop`` when it is still the initial
stop from the signal bar.

THE CHANDELIER RATCHET (per position, not per symbol)
------------------------------------------------------
::

    effective_stop(t) = max(effective_stop(t-1),
                            rules.chandelier_stop(t),
                            initial_stop_at_entry)

``rules.chandelier_stop`` is a plain Series with no memory (Contract 7 says
"NO ratchet; consumers ratchet per-position") — it can and does fall when ATR
expands. The ``max`` is what makes the stop monotonic **for this position**,
and ``initial_stop_at_entry`` is the floor, so a position never loosens below
the risk it was sized against. Two positions in the same symbol entered on
different days would carry different effective stops; the engine never opens
two, but the ratchet lives on the position object for exactly this reason.

MONEY
-----
Whole shares, cash accounting, no margin, no fractional anything, one position
per symbol at a time, at most ``cfg.account.max_positions`` open at once.
Sizing is delegated to :func:`swing.strategy.sizing.size_position`, which is
handed the *cost-inclusive* entry price (so the risk per share is the real one)
and the cash actually available at the moment of the fill. A pick that sizes to
zero shares is skipped and does **not** consume a slot — the next-ranked
candidate gets it.

DETERMINISM
-----------
Symbols are iterated in sorted order, candidates are ranked with a total order
(score desc, then proximity-to-high desc, then symbol asc), and nothing reads
the clock. Same inputs in, same trades out, byte for byte.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, TypeVar

import numpy as np
import pandas as pd

from swing.backtest.costs import CostModel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "EQUITY_COLUMNS",
    "EXIT_CHANDELIER",
    "EXIT_END_OF_DATA",
    "EXIT_REASONS",
    "EXIT_STOP",
    "EXIT_TIME",
    "TRADE_COLUMNS",
    "EngineResult",
    "Position",
    "SignalCache",
    "empty_equity",
    "empty_trades",
    "run_engine",
]

T = TypeVar("T")

#: Exit reasons, exactly the four Contract 11 allows.
EXIT_STOP = "stop"
EXIT_CHANDELIER = "chandelier"
EXIT_TIME = "time"
EXIT_END_OF_DATA = "end_of_data"
EXIT_REASONS: tuple[str, ...] = (EXIT_STOP, EXIT_CHANDELIER, EXIT_TIME, EXIT_END_OF_DATA)

#: Column order of the trades frame (Contract 11).
TRADE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "entry_date",
    "entry_price",
    "exit_date",
    "exit_price",
    "shares",
    "pnl",
    "pnl_pct",
    "hold_days",
    "exit_reason",
    "entry_cost",
    "exit_cost",
)

#: Column order of the equity frame (Contract 11).
EQUITY_COLUMNS: tuple[str, ...] = ("equity", "cash", "n_positions", "drawdown")


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """A position that was still open when the data ran out.

    This is a *snapshot* for reporting. The simulator's own mutable position
    record is private; by the time a caller sees an :class:`EngineResult` every
    position has also been marked out into ``trades`` with reason
    ``end_of_data``, so the two views agree.
    """

    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    entry_cost: float
    initial_stop: float
    stop: float
    last_close: float
    hold_days: int
    unrealized_pnl: float


@dataclass(frozen=True)
class EngineResult:
    """Everything one simulation produced.

    Attributes:
        trades: one row per closed trade, columns exactly :data:`TRADE_COLUMNS`.
        equity: one row per simulated date, columns exactly
            :data:`EQUITY_COLUMNS`, indexed by a ``date``-named DatetimeIndex.
            ``drawdown`` is a non-positive *fraction* (``-0.12`` is -12%).
        positions: positions still open on the final bar, as snapshots.
        initial_equity: the starting cash, so P&L can be reconciled without
            reaching back into the config.
    """

    trades: pd.DataFrame
    equity: pd.DataFrame
    positions: list[Position]
    initial_equity: float


@dataclass
class _OpenPosition:
    """The simulator's private, mutable position record."""

    symbol: str
    symbol_index: int
    entry_day: int  # index into the master calendar
    entry_date: pd.Timestamp
    entry_price: float  # raw market price, cost excluded
    shares: int
    entry_cost: float  # total dollars, both share-count and side included
    initial_stop: float  # rules.initial_stop at the SIGNAL bar — the ratchet floor
    stop: float  # effective stop as of the last close
    time_exit_pending: bool = False
    last_row: int = -1  # last row of this symbol's own frame that we marked at
    last_close: float = float("nan")
    last_day: int = -1  # master-calendar index of that mark


@dataclass(frozen=True)
class _PendingEntry:
    """An order decided at the close of ``signal_day``, to fill at the next open."""

    symbol_index: int
    signal_row: int  # row in the symbol's own frame
    rank_key: tuple[int, float, float, str]


@dataclass(frozen=True)
class _SymbolPlan:
    """Everything the daily loop needs about one symbol, as flat numpy arrays.

    Building these up front is what keeps the loop cheap: the per-day work is
    then proportional to (open positions + today's candidates), not to the size
    of the universe.
    """

    symbol: str
    open_: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    initial_stop: np.ndarray
    chandelier: np.ndarray
    score: np.ndarray
    high_prox: np.ndarray
    signal: np.ndarray  # bool, per row of this symbol's frame
    row_of_day: np.ndarray  # len(calendar) ints; -1 where the symbol has no bar


# ---------------------------------------------------------------------------
# memoisation
# ---------------------------------------------------------------------------


class SignalCache:
    """Memoises the WP-C/WP-D Series that a backtest asks for over and over.

    The walk-forward search runs the same universe through 81 parameter
    combinations per window. Most of the expensive Series do not depend on the
    tuned parameters at all (the trend template, liquidity, ATR, the momentum
    score), and the ones that do depend on only one or two of them. Keying the
    cache on ``(what, symbol-identity, the params that actually matter)`` turns
    81 full recomputations into a handful.

    Scope it to one walk-forward window and throw it away afterwards; it holds
    references to every Series it has built. It is an ordinary object, never a
    module-level singleton, so two simulations can never contaminate each other.
    """

    __slots__ = ("_values", "hits", "misses")

    def __init__(self) -> None:
        self._values: dict[tuple[Any, ...], Any] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[Any, ...], build: Callable[[], T]) -> T:
        """Return the cached value for ``key``, building it on first sight."""
        if key in self._values:
            self.hits += 1
            return self._values[key]  # type: ignore[return-value]
        self.misses += 1
        value = build()
        self._values[key] = value
        return value

    def clear(self) -> None:
        """Drop everything (and the hit/miss counters)."""
        self._values.clear()
        self.hits = 0
        self.misses = 0


def _frame_id(symbol: str, frame: pd.DataFrame) -> tuple[Any, ...]:
    """A cheap identity for a bars frame that is stable across calls.

    Symbol name alone is not enough (two windows may pass different slices),
    and ``id()`` is recycled by the allocator, so we fingerprint the index.
    """
    index = frame.index
    if len(index) == 0:
        return (symbol, 0, 0, 0)
    return (symbol, len(index), int(index[0].value), int(index[-1].value))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _finite(value: float) -> bool:
    return isinstance(value, float | int | np.floating | np.integer) and math.isfinite(float(value))


def _as_bool_array(series: pd.Series, index: pd.Index) -> np.ndarray:
    """Align a WP-D boolean Series to ``index``, treating missing/NaN as False."""
    aligned = series.reindex(index)
    filled = aligned.astype("float64").fillna(0.0).to_numpy()
    return filled > 0.5


def _as_float_array(series: pd.Series, index: pd.Index) -> np.ndarray:
    return series.reindex(index).astype("float64").to_numpy()


def empty_trades() -> pd.DataFrame:
    """An empty trades frame with the right columns and dtypes."""
    return pd.DataFrame(
        {
            "symbol": pd.Series(dtype="object"),
            "entry_date": pd.Series(dtype="datetime64[ns]"),
            "entry_price": pd.Series(dtype="float64"),
            "exit_date": pd.Series(dtype="datetime64[ns]"),
            "exit_price": pd.Series(dtype="float64"),
            "shares": pd.Series(dtype="int64"),
            "pnl": pd.Series(dtype="float64"),
            "pnl_pct": pd.Series(dtype="float64"),
            "hold_days": pd.Series(dtype="int64"),
            "exit_reason": pd.Series(dtype="object"),
            "entry_cost": pd.Series(dtype="float64"),
            "exit_cost": pd.Series(dtype="float64"),
        }
    )[list(TRADE_COLUMNS)]


def empty_equity() -> pd.DataFrame:
    """An empty equity frame with the right columns and dtypes."""
    frame = pd.DataFrame(
        {
            "equity": pd.Series(dtype="float64"),
            "cash": pd.Series(dtype="float64"),
            "n_positions": pd.Series(dtype="int64"),
            "drawdown": pd.Series(dtype="float64"),
        },
        index=pd.DatetimeIndex([], name="date"),
    )
    return frame[list(EQUITY_COLUMNS)]


# ---------------------------------------------------------------------------
# precomputation
# ---------------------------------------------------------------------------


def _high_prox(frame: pd.DataFrame) -> pd.Series:
    """Where the close sits inside its 52-week closing high, 1.0 = at the high.

    This is the ranking tiebreak, and it is deliberately the *same* arithmetic
    as the ``high_prox`` column of
    :func:`swing.strategy.scoring.rank_candidates`: close divided by
    :func:`swing.strategy.rules.rolling_high_52w` of the close. Computing it
    here as a Series (rather than calling ``rank_candidates`` once per day) is
    what keeps the daily loop cheap; ``test_backtest_engine`` asserts the two
    orderings agree.
    """
    from swing.strategy.rules import rolling_high_52w

    yearly_high = rolling_high_52w(frame["close"])
    return frame["close"] / yearly_high.replace(0.0, np.nan)


def _build_plan(
    symbol: str,
    frame: pd.DataFrame,
    cfg: Config,
    calendar: pd.DatetimeIndex,
    *,
    is_etf: bool,
    earnings_date: date | None,
    cache: SignalCache,
) -> _SymbolPlan | None:
    """Compute every Series this symbol needs and flatten them to numpy.

    All Series are computed on the symbol's FULL history — the ``start``/``end``
    arguments of :func:`run_engine` bound the *simulation*, not the data, so
    indicators warm up on bars that precede the window. Restricting the data
    instead would silently change every signal near a window boundary, which is
    exactly the bug walk-forward exists to avoid.
    """
    from swing.strategy import rules, scoring

    if frame.empty:
        return None

    ident = _frame_id(symbol, frame)
    strat = cfg.strategy

    # --- invariant across the whole tuning grid: computed once per symbol ----
    # Each key names only the settings the callee actually reads, so a grid
    # that moves atr_stop_mult does not invalidate the trend template.
    trend = cache.get(
        (
            "trend",
            ident,
            is_etf,
            strat.sma_fast,
            strat.sma_mid,
            strat.sma_slow,
            strat.sma_slow_rising_days,
            strat.min_above_low_mult,
            strat.max_below_high_pct,
            strat.adx_min,
        ),
        lambda: rules.trend_template(frame, cfg, is_etf=is_etf),
    )
    liquid = cache.get(
        ("liquid", ident, is_etf, strat.min_price, strat.min_dollar_volume),
        lambda: rules.liquidity_ok(frame, cfg, is_etf=is_etf),
    )
    atr_series = cache.get(
        ("atr", ident, strat.atr_window),
        lambda: _atr_series(frame, strat.atr_window),
    )
    score = cache.get(
        ("score", ident, strat.mom_weight_126, strat.mom_weight_63, strat.mom_skip_days),
        lambda: scoring.momentum_score(frame, cfg),
    )
    prox = cache.get(("prox", ident), lambda: _high_prox(frame))
    blackout = cache.get(
        ("blackout", ident, earnings_date, strat.earnings_blackout_days),
        lambda: rules.earnings_blackout(frame.index, earnings_date, cfg),
    )

    # --- depends on the tuned parameters: a handful of variants per symbol ---
    entry = cache.get(
        (
            "entry",
            ident,
            strat.donchian_window,
            strat.volume_mult,
            strat.breakout_proximity_pct,
            strat.volume_avg_window,
            strat.rsi2_enabled,
            strat.sma_fast,
        ),
        lambda: rules.entry_signal(frame, cfg),
    )
    init_stop = cache.get(
        ("init_stop", ident, strat.atr_stop_mult, strat.atr_window),
        lambda: rules.initial_stop(frame, cfg),
    )
    chandelier = cache.get(
        ("chandelier", ident, strat.chandelier_mult, strat.atr_window),
        lambda: rules.chandelier_stop(frame, cfg),
    )

    index = frame.index
    signal = (
        _as_bool_array(trend, index)
        & _as_bool_array(entry, index)
        & _as_bool_array(liquid, index)
        & ~_as_bool_array(blackout, index)
    )

    return _SymbolPlan(
        symbol=symbol,
        open_=frame["open"].astype("float64").to_numpy(),
        high=frame["high"].astype("float64").to_numpy(),
        low=frame["low"].astype("float64").to_numpy(),
        close=frame["close"].astype("float64").to_numpy(),
        atr=_as_float_array(atr_series, index),
        initial_stop=_as_float_array(init_stop, index),
        chandelier=_as_float_array(chandelier, index),
        score=_as_float_array(score, index),
        high_prox=_as_float_array(prox, index),
        signal=signal,
        row_of_day=np.asarray(index.get_indexer(calendar), dtype=np.int64),
    )


def _atr_series(frame: pd.DataFrame, window: int) -> pd.Series:
    from swing.indicators import atr as atr_fn

    return atr_fn(frame, window)


def _regime_array(spy_bars: pd.DataFrame, cfg: Config, calendar: pd.DatetimeIndex) -> np.ndarray:
    """The SPY regime filter, aligned to the master calendar.

    Forward-filled: a holiday in SPY's series that is a trading day for some
    other symbol inherits the last known regime rather than silently blocking
    entries. A calendar day with no prior SPY bar at all is treated as
    "entries blocked", which is the conservative direction.
    """
    from swing.strategy import regime

    if spy_bars is None or spy_bars.empty:
        # No regime data at all: fall back to the config switch. With the
        # filter disabled that means "always allowed"; with it enabled we
        # refuse to invent a regime we cannot see.
        allowed = bool(not cfg.regime.enabled)
        return np.full(len(calendar), allowed, dtype=bool)

    series = regime.entries_allowed(spy_bars, cfg)
    numeric = series.astype("float64").reindex(calendar.union(series.index)).ffill()
    return numeric.reindex(calendar).fillna(0.0).to_numpy() > 0.5


# ---------------------------------------------------------------------------
# the simulator
# ---------------------------------------------------------------------------


def run_engine(
    bars_by_symbol: dict[str, pd.DataFrame],
    spy_bars: pd.DataFrame,
    cfg: Config,
    *,
    earnings: dict[str, date | None] | None = None,
    is_etf: dict[str, bool] | None = None,
    start: date | None = None,
    end: date | None = None,
    cache: SignalCache | None = None,
) -> EngineResult:
    """Simulate the strategy over ``bars_by_symbol`` and return the result.

    Args:
        bars_by_symbol: Contract 3 frames, keyed by symbol. Pass the FULL
            history: indicators warm up on bars before ``start``.
        spy_bars: bars for the regime symbol. May be empty (see
            :func:`_regime_array`).
        cfg: the configuration whose ``account``, ``strategy`` and ``backtest``
            sections drive sizing, rules and costs.
        earnings: next-earnings date per symbol; missing or ``None`` means "no
            blackout known", which Contract 7 defines as never blocked.
        is_etf: per-symbol ETF flag, forwarded to the WP-D rules so ETFs take
            the relaxed template path. Missing symbols default to ``False``.
        start: first simulated date (inclusive). ``None`` means the first bar.
        end: last simulated date (inclusive). ``None`` means the last bar.
        cache: optional :class:`SignalCache` shared across runs over the same
            data — a pure speed knob, it cannot change results.

    Returns:
        An :class:`EngineResult`. Every position is closed in ``trades``; any
        that were still open on the final bar also appear in ``positions``.
    """
    earnings = earnings or {}
    is_etf = is_etf or {}
    cache = cache if cache is not None else SignalCache()

    symbols = sorted(s for s, f in bars_by_symbol.items() if f is not None and not f.empty)
    initial_equity = float(cfg.account.equity)

    calendar = _master_calendar(bars_by_symbol, symbols, start, end)
    if len(calendar) == 0 or not symbols:
        return EngineResult(
            trades=empty_trades(),
            equity=empty_equity(),
            positions=[],
            initial_equity=initial_equity,
        )

    plans: list[_SymbolPlan] = []
    for symbol in symbols:
        plan = _build_plan(
            symbol,
            bars_by_symbol[symbol],
            cfg,
            calendar,
            is_etf=bool(is_etf.get(symbol, False)),
            earnings_date=earnings.get(symbol),
            cache=cache,
        )
        if plan is not None:
            plans.append(plan)
    if not plans:
        return EngineResult(
            trades=empty_trades(),
            equity=empty_equity(),
            positions=[],
            initial_equity=initial_equity,
        )

    index_of_symbol = {plan.symbol: i for i, plan in enumerate(plans)}
    candidates_by_day = _candidates_by_day(plans, bars_by_symbol, calendar)
    regime_ok = _regime_array(spy_bars, cfg, calendar)

    return _simulate(
        plans=plans,
        index_of_symbol=index_of_symbol,
        candidates_by_day=candidates_by_day,
        regime_ok=regime_ok,
        calendar=calendar,
        cfg=cfg,
    )


def _master_calendar(
    bars_by_symbol: dict[str, pd.DataFrame],
    symbols: list[str],
    start: date | None,
    end: date | None,
) -> pd.DatetimeIndex:
    """Union of every symbol's trading days, clipped to [start, end].

    A union rather than an intersection: a symbol that IPO'd mid-window should
    not delete trading days for everything else. Symbols simply have no bar on
    days they did not trade, and the loop skips them there.
    """
    if not symbols:
        return pd.DatetimeIndex([], name="date")
    combined = bars_by_symbol[symbols[0]].index
    for symbol in symbols[1:]:
        combined = combined.union(bars_by_symbol[symbol].index)
    calendar = pd.DatetimeIndex(combined).sort_values()
    if start is not None:
        calendar = calendar[calendar >= pd.Timestamp(start)]
    if end is not None:
        calendar = calendar[calendar <= pd.Timestamp(end)]
    return pd.DatetimeIndex(calendar, name="date")


def _candidates_by_day(
    plans: list[_SymbolPlan],
    bars_by_symbol: dict[str, pd.DataFrame],
    calendar: pd.DatetimeIndex,
) -> list[list[int]]:
    """Invert the per-symbol signal arrays into a per-day candidate list.

    Signals are sparse, so this costs a scan of a boolean array per symbol and
    saves the daily loop from ever touching a symbol that has nothing to say.
    Symbols are visited in sorted order, so each day's list is already in a
    deterministic order before ranking.
    """
    buckets: list[list[int]] = [[] for _ in range(len(calendar))]
    for si, plan in enumerate(plans):
        frame_index = bars_by_symbol[plan.symbol].index
        day_of_row = np.asarray(calendar.get_indexer(frame_index), dtype=np.int64)
        for row in np.flatnonzero(plan.signal):
            day = int(day_of_row[row])
            if day >= 0:
                buckets[day].append(si)
    return buckets


def _rank_key(plan: _SymbolPlan, row: int) -> tuple[int, float, float, str]:
    """Total order over candidates: score desc, proximity desc, symbol asc.

    This reproduces the ordering of :func:`swing.strategy.scoring.rank_candidates`
    (score desc, tiebreak high_prox desc) from Series that were precomputed
    once per symbol, and appends the symbol name so the order is total even
    when two names tie on both floats. Non-finite scores sort last.
    """
    score = plan.score[row]
    prox = plan.high_prox[row]
    finite = 0 if math.isfinite(score) else 1
    score_key = -score if math.isfinite(score) else 0.0
    prox_key = -prox if math.isfinite(prox) else 0.0
    return (finite, score_key, prox_key, plan.symbol)


def _simulate(
    *,
    plans: list[_SymbolPlan],
    index_of_symbol: dict[str, int],
    candidates_by_day: list[list[int]],
    regime_ok: np.ndarray,
    calendar: pd.DatetimeIndex,
    cfg: Config,
) -> EngineResult:
    """The daily loop. See the module docstring for the semantics it implements."""
    from swing.strategy.sizing import size_position

    costs = CostModel.from_config(cfg)
    max_positions = int(cfg.account.max_positions)
    time_stop_days = int(cfg.strategy.time_stop_days)

    cash = float(cfg.account.equity)
    initial_equity = cash
    open_positions: dict[str, _OpenPosition] = {}
    pending: list[_PendingEntry] = []
    trade_rows: list[dict[str, Any]] = []
    equity_values: list[float] = []
    cash_values: list[float] = []
    position_counts: list[int] = []

    last_day = len(calendar) - 1

    for di in range(len(calendar)):
        day = calendar[di]

        # ------------------------------------------------------------------
        # 1. EXITS — priced off the stop as it stood at the close of t-1.
        # ------------------------------------------------------------------
        for symbol in sorted(open_positions):
            position = open_positions[symbol]
            plan = plans[position.symbol_index]
            row = int(plan.row_of_day[di])
            if row < 0:
                # No bar for this symbol today (halt, holiday, late listing):
                # the position simply carries, marked at its last close.
                continue

            open_px = plan.open_[row]
            low_px = plan.low[row]
            stop = position.stop
            # ATR from the last bar whose close was known before this fill.
            atr_for_exit = plan.atr[row - 1] if row > 0 else float("nan")
            stop_reason = (
                EXIT_CHANDELIER if position.stop > position.initial_stop + 1e-12 else EXIT_STOP
            )

            exit_price: float | None = None
            reason = ""
            if _finite(stop) and open_px <= stop:
                # (a) gapped through overnight — you get the open, not the stop.
                exit_price = float(open_px)
                reason = stop_reason
            elif position.time_exit_pending:
                # (b) time stop signalled at yesterday's close, fills at the open.
                exit_price = float(open_px)
                reason = EXIT_TIME
            elif _finite(stop) and low_px <= stop:
                # (c) resting stop order touched intraday — fills AT the stop.
                exit_price = float(stop)
                reason = stop_reason

            if exit_price is None:
                continue

            cash = _close_position(
                position=position,
                exit_day=di,
                exit_date=day,
                exit_price=exit_price,
                reason=reason,
                atr_for_exit=atr_for_exit,
                costs=costs,
                cash=cash,
                trade_rows=trade_rows,
            )
            del open_positions[symbol]

        # ------------------------------------------------------------------
        # 2. ENTRIES — fill the orders decided at yesterday's close, at today's
        #    open. Exits above have already freed cash and slots.
        # ------------------------------------------------------------------
        for order in pending:
            if len(open_positions) >= max_positions:
                break
            plan = plans[order.symbol_index]
            if plan.symbol in open_positions:
                continue  # one position per symbol, ever
            row = int(plan.row_of_day[di])
            if row < 0:
                continue  # no bar to fill against today; the order lapses

            raw_fill = float(plan.open_[row])
            if not _finite(raw_fill) or raw_fill <= 0.0:
                continue
            atr_at_signal = plan.atr[order.signal_row]
            per_share = costs.per_share(raw_fill, atr_at_signal)
            entry_effective = raw_fill + per_share
            stop0 = float(plan.initial_stop[order.signal_row])
            if not _finite(stop0) or stop0 < 0.0 or stop0 >= entry_effective:
                # A stop at or above the entry is not a stop, and a stop below
                # zero is not a price. Refuse the trade rather than invent a
                # risk per share (size_position would raise on either).
                continue

            # Equity as it stands right now: cash (post-exits) plus every open
            # position marked at its last close. That is the information a live
            # trader would have at the opening bell — note that a position
            # opened moments ago on this same bar is marked at its FILL price,
            # not at today's close, which has not happened yet.
            equity_now = cash + sum(
                p.shares * p.last_close for p in open_positions.values() if _finite(p.last_close)
            )
            sized = size_position(equity_now, cash, entry_effective, stop0, cfg)
            shares = int(sized.shares)
            if shares <= 0:
                continue  # unaffordable / capped to nothing — does NOT take a slot

            entry_cost = shares * per_share
            cash -= shares * raw_fill + entry_cost
            open_positions[plan.symbol] = _OpenPosition(
                symbol=plan.symbol,
                symbol_index=order.symbol_index,
                entry_day=di,
                entry_date=day,
                entry_price=raw_fill,
                shares=shares,
                entry_cost=entry_cost,
                initial_stop=stop0,
                stop=stop0,
                last_row=row,
                # Marked at the fill price until the close is in. Marking it at
                # today's close here would let the sizing of the NEXT fill this
                # same morning see a price that has not printed yet.
                last_close=raw_fill,
                last_day=di,
            )
        pending = []

        # ------------------------------------------------------------------
        # 3. CLOSE — ratchet stops, arm time stops, mark the book.
        # ------------------------------------------------------------------
        for symbol in sorted(open_positions):
            position = open_positions[symbol]
            plan = plans[position.symbol_index]
            row = int(plan.row_of_day[di])
            if row >= 0:
                chandelier = plan.chandelier[row]
                if _finite(chandelier):
                    # THE RATCHET: never below yesterday's stop, never below the
                    # stop this position was sized against.
                    position.stop = max(position.stop, float(chandelier), position.initial_stop)
                position.last_row = row
                position.last_close = float(plan.close[row])
                position.last_day = di
            # Days held, counted on the master calendar. Armed at the close, so
            # the fill lands at tomorrow's open (see the exit ladder).
            if di - position.entry_day >= time_stop_days:
                position.time_exit_pending = True

        marked = sum(
            p.shares * p.last_close for p in open_positions.values() if _finite(p.last_close)
        )
        equity_values.append(cash + marked)
        cash_values.append(cash)
        position_counts.append(len(open_positions))

        # ------------------------------------------------------------------
        # 4. SIGNALS for tomorrow. Regime gates ENTRIES ONLY.
        # ------------------------------------------------------------------
        if di < last_day and regime_ok[di]:
            ranked: list[_PendingEntry] = []
            for si in candidates_by_day[di]:
                plan = plans[si]
                if plan.symbol in open_positions:
                    continue
                row = int(plan.row_of_day[di])
                if row < 0:
                    continue
                ranked.append(
                    _PendingEntry(
                        symbol_index=si,
                        signal_row=row,
                        rank_key=_rank_key(plan, row),
                    )
                )
            if ranked:
                ranked.sort(key=lambda order: order.rank_key)
                # Carry at most max_positions orders: tomorrow's exits can free
                # every slot, but never more than that.
                pending = ranked[:max_positions]

    # ----------------------------------------------------------------------
    # END OF DATA — snapshot what is open, then mark it out at the last close.
    # ----------------------------------------------------------------------
    snapshots = [
        Position(
            symbol=p.symbol,
            entry_date=p.entry_date,
            entry_price=p.entry_price,
            shares=p.shares,
            entry_cost=p.entry_cost,
            initial_stop=p.initial_stop,
            stop=p.stop,
            last_close=p.last_close,
            hold_days=max(p.last_day - p.entry_day, 0),
            unrealized_pnl=p.shares * (p.last_close - p.entry_price) - p.entry_cost,
        )
        for p in (open_positions[s] for s in sorted(open_positions))
        if _finite(p.last_close)
    ]

    for symbol in sorted(open_positions):
        position = open_positions[symbol]
        plan = plans[position.symbol_index]
        if position.last_row < 0 or not _finite(position.last_close):
            continue
        cash = _close_position(
            position=position,
            exit_day=position.last_day,
            exit_date=calendar[position.last_day],
            exit_price=position.last_close,
            reason=EXIT_END_OF_DATA,
            # Marking out at a close, so that bar's ATR is legitimately known.
            atr_for_exit=plan.atr[position.last_row],
            costs=costs,
            cash=cash,
            trade_rows=trade_rows,
        )

    trades = _trades_frame(trade_rows)
    equity = _equity_frame(calendar, equity_values, cash_values, position_counts)
    return EngineResult(
        trades=trades,
        equity=equity,
        positions=snapshots,
        initial_equity=initial_equity,
    )


def _close_position(
    *,
    position: _OpenPosition,
    exit_day: int,
    exit_date: pd.Timestamp,
    exit_price: float,
    reason: str,
    atr_for_exit: float,
    costs: CostModel,
    cash: float,
    trade_rows: list[dict[str, Any]],
) -> float:
    """Book one closed trade and return the updated cash balance.

    ``entry_price`` / ``exit_price`` are RAW market prices and the costs are
    reported separately, so a reader can re-derive the P&L by hand::

        pnl = shares * (exit_price - entry_price) - entry_cost - exit_cost
    """
    exit_cost = costs.total(position.shares, exit_price, atr_for_exit)
    proceeds = position.shares * exit_price - exit_cost
    cash += proceeds
    gross = position.shares * (exit_price - position.entry_price)
    pnl = gross - position.entry_cost - exit_cost
    committed = position.shares * position.entry_price + position.entry_cost
    trade_rows.append(
        {
            "symbol": position.symbol,
            "entry_date": position.entry_date,
            "entry_price": position.entry_price,
            "exit_date": exit_date,
            "exit_price": float(exit_price),
            "shares": position.shares,
            "pnl": pnl,
            "pnl_pct": (pnl / committed * 100.0) if committed > 0 else 0.0,
            "hold_days": max(exit_day - position.entry_day, 0),
            "exit_reason": reason,
            "entry_cost": position.entry_cost,
            "exit_cost": exit_cost,
        }
    )
    return cash


def _trades_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Assemble the trades frame, sorted for determinism."""
    if not rows:
        return empty_trades()
    frame = pd.DataFrame(rows)[list(TRADE_COLUMNS)]
    frame["entry_date"] = pd.to_datetime(frame["entry_date"])
    frame["exit_date"] = pd.to_datetime(frame["exit_date"])
    frame["shares"] = frame["shares"].astype("int64")
    frame["hold_days"] = frame["hold_days"].astype("int64")
    # Exit date, then entry date, then symbol: a total order that does not
    # depend on the order positions happened to be closed in.
    frame = frame.sort_values(
        ["exit_date", "entry_date", "symbol"], kind="stable", ignore_index=True
    )
    return frame


def _equity_frame(
    calendar: pd.DatetimeIndex,
    equity_values: list[float],
    cash_values: list[float],
    position_counts: list[int],
) -> pd.DataFrame:
    """Assemble the equity curve and its drawdown column."""
    if len(calendar) == 0:
        return empty_equity()
    equity = pd.Series(equity_values, index=calendar, dtype="float64")
    peak = equity.cummax()
    # Non-positive fraction: -0.12 means "12% below the high-water mark".
    drawdown = (equity / peak.replace(0.0, np.nan)) - 1.0
    frame = pd.DataFrame(
        {
            "equity": equity,
            "cash": pd.Series(cash_values, index=calendar, dtype="float64"),
            "n_positions": pd.Series(position_counts, index=calendar, dtype="int64"),
            "drawdown": drawdown.fillna(0.0),
        }
    )
    frame.index.name = "date"
    return frame[list(EQUITY_COLUMNS)]
