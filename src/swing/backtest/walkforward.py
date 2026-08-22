"""Walk-forward analysis — the only backtest number this system trusts.

A single full-period backtest with hand-picked parameters tells you what would
have happened if you had known the answer in advance. Walk-forward tells you
what would have happened if you had not.

THE PROCEDURE
-------------
Starting at ``cfg.backtest.start``, cut the history into overlapping folds that
step forward one OOS period at a time::

    |<--- 3y in-sample --->|<- 1y OOS ->|
              |<--- 3y in-sample --->|<- 1y OOS ->|
                        |<--- 3y in-sample --->|<- 1y OOS ->|

For each fold: grid-search the tuning parameters on the in-sample stretch, take
the single best combination, and run it — untouched — on the out-of-sample
stretch that follows it. Concatenate every OOS stretch end to end and *that* is
the headline result. No OOS bar ever influences the parameters applied to it.

Because each fold's OOS equity is simulated from a fresh starting balance, the
concatenation is done on **returns**, not on dollar levels: the stitched curve
compounds each fold's daily returns onto the previous fold's ending equity.
Stitching dollar levels would inject a fake jump at every seam.

THE TUNING GRID (Contract 11 amendment)
----------------------------------------
``atr_stop_mult`` x ``chandelier_mult`` x ``donchian_window`` x ``volume_mult``
= 3 x 3 x 3 x 3 = 81 combinations per fold, and nothing else is tuned: the
**parameter list** is frozen precisely so it cannot quietly grow when results
disappoint — every parameter added to a grid buys in-sample performance and
sells honesty.

The **candidate values** are not frozen, because a research question like "does
this strategy whipsaw because a 2-ATR stop is too tight for a 16-day hold?" is
unanswerable if the tuner can only be offered stops someone chose in advance.
``[backtest.tuning_grid]`` re-specifies them (see
:data:`swing.config.TUNABLE_PARAMS`), an absent section means exactly
:data:`TUNING_GRID`, and every report records the grid it actually used. The
honesty cost is real and runs the other way from the intuition: a wider grid
makes more in-sample choices per fold, so its out-of-sample record deserves
*more* scepticism, not less. :data:`swing.config.MAX_TUNING_COMBINATIONS` caps
how far that can be pushed in one run.

THE OBJECTIVE (documented, as Contract 11 requires)
----------------------------------------------------
Rank in-sample results by:

1. **profit factor**, but only for combinations with at least
   :data:`MIN_IS_TRADES` (8) in-sample trades. Anything below the floor is
   ranked beneath every combination that clears it, regardless of its ratio —
   a profit factor computed from three trades is a rumour, not a measurement.
2. tiebreak on **more trades** (more evidence for the same edge),
3. then on **shallower maximum drawdown**,
4. then on the grid's own ordering, so the choice is reproducible. (Config
   grids are normalised to a canonical key order for exactly this reason.)

Profit factor rather than CAGR or Sharpe because it is the quantity the gate in
:mod:`swing.backtest.gate` actually tests, and tuning on one thing while gating
on another is how you end up with a system that passes its own exam by luck.

COST
----
81 combinations x ~12 folds is roughly a thousand simulations. Two things make
that tractable: a :class:`~swing.backtest.engine.SignalCache` shared across the
whole fold (the trend template, liquidity, ATR and momentum score do not depend
on any tuned parameter, so they are computed once per symbol per fold), and an
engine whose daily loop costs O(open positions + today's candidates) rather
than O(universe).
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.backtest.engine import EngineResult, SignalCache, empty_equity, empty_trades, run_engine
from swing.backtest.metrics import compute_metrics

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from swing.config import Config

__all__ = [
    "MIN_IS_TRADES",
    "OBJECTIVE_DESCRIPTION",
    "SENSITIVITY_PARAMS",
    "SENSITIVITY_STEP",
    "TUNING_GRID",
    "FoldResult",
    "WalkForwardResult",
    "Window",
    "grid_points",
    "is_default_grid",
    "make_windows",
    "objective_key",
    "resolve_grid",
    "run_walkforward",
    "sensitivity_table",
    "stitch_returns",
    "with_params",
]

log = logging.getLogger(__name__)

#: The default grid, used whenever ``[backtest.tuning_grid]`` is absent.
#:
#: The **keys** are frozen (Contract 11 amendment): do not add parameters to
#: this dict, and keep them identical to :data:`swing.config.TUNABLE_PARAMS`.
#: The **values** may be re-specified per run in config; this dict is what an
#: unconfigured run gets, so changing it here silently rewrites the meaning of
#: every report that did not name its own grid.
TUNING_GRID: dict[str, tuple[Any, ...]] = {
    "atr_stop_mult": (1.5, 2.0, 2.5),
    "chandelier_mult": (2.5, 3.0, 3.5),
    "donchian_window": (15, 20, 25),
    "volume_mult": (1.0, 1.3, 1.6),
}

#: Minimum in-sample trades before a grid point's profit factor is believed.
MIN_IS_TRADES = 8

OBJECTIVE_DESCRIPTION = (
    "In-sample selection maximises profit factor among parameter sets with at least "
    f"{MIN_IS_TRADES} in-sample trades (sets below that floor rank last whatever their "
    "ratio), breaking ties by more trades, then by shallower maximum drawdown, then by "
    "the order the tuning grid lists them in. A parameter set with no losing trades at all "
    "reports the 9999.0 profit-factor sentinel rather than a measurement, so it is ranked as "
    "0.0 and wins only on trade count and drawdown. Profit factor is used because it is the "
    "quantity the deployment gate tests."
)

#: Parameters perturbed one at a time in the sensitivity table.
SENSITIVITY_PARAMS: tuple[str, ...] = (
    "atr_stop_mult",
    "chandelier_mult",
    "donchian_window",
    "volume_mult",
    "adx_min",
)

#: Relative perturbation applied either side of the configured value.
SENSITIVITY_STEP = 0.25


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One walk-forward fold: tune on [is_start, is_end], test on [oos_start, oos_end].

    The two stretches are disjoint and adjacent: ``oos_start`` is the day after
    ``is_end``. Every boundary is inclusive.
    """

    is_start: date
    is_end: date
    oos_start: date
    oos_end: date

    def as_dict(self) -> dict[str, str]:
        """ISO-string form, for the report."""
        return {
            "is_start": self.is_start.isoformat(),
            "is_end": self.is_end.isoformat(),
            "oos_start": self.oos_start.isoformat(),
            "oos_end": self.oos_end.isoformat(),
        }


def _plus_years(day: date, years: int) -> date:
    """``day`` shifted by whole years, clamping 29 February to the 28th."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 Feb in a non-leap target year
        return day.replace(year=day.year + years, day=28)


def make_windows(start: date, end: date, *, is_years: int = 3, oos_years: int = 1) -> list[Window]:
    """Cut [start, end] into folds stepped forward by ``oos_years`` at a time.

    A fold is emitted only when its **entire** out-of-sample stretch fits inside
    ``end``. A truncated final fold would put a few months of OOS next to three
    years of IS and let a quiet quarter flatter the headline, so it is dropped
    instead.

    Returns:
        Folds in chronological order; empty when the span is too short to hold
        even one complete IS + OOS pair.
    """
    if is_years < 1 or oos_years < 1:
        raise ValueError("is_years and oos_years must both be at least 1.")

    one_day = timedelta(days=1)
    windows: list[Window] = []
    is_start = start
    while True:
        # Half-open internally, inclusive on the way out: the in-sample stretch
        # ends the day before the out-of-sample stretch begins, so the two can
        # never share a bar.
        oos_start = _plus_years(is_start, is_years)
        oos_end = _plus_years(oos_start, oos_years) - one_day
        if oos_end > end:
            break
        windows.append(
            Window(
                is_start=is_start,
                is_end=oos_start - one_day,
                oos_start=oos_start,
                oos_end=oos_end,
            )
        )
        is_start = _plus_years(is_start, oos_years)
    return windows


# ---------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------


def resolve_grid(grid: dict[str, Sequence[Any]] | None = None) -> dict[str, tuple[Any, ...]]:
    """The grid a run will actually search: ``grid``, or :data:`TUNING_GRID`.

    Reads the module-level default at call time rather than binding it at
    import, so a caller (or a test) that replaces :data:`TUNING_GRID` gets the
    replacement.
    """
    if grid is None:
        return dict(TUNING_GRID)
    return {name: tuple(values) for name, values in grid.items()}


def is_default_grid(grid: dict[str, Sequence[Any]] | None) -> bool:
    """True when ``grid`` searches exactly what an unconfigured run searches.

    An absent grid and one that spells the default out are the same experiment,
    and reports of the same experiment should stay comparable — see
    :func:`swing.backtest.runner.config_hash`.
    """
    return resolve_grid(grid) == resolve_grid(None)


def grid_points(grid: dict[str, tuple[Any, ...]] | None = None) -> Iterator[dict[str, Any]]:
    """Yield every combination of ``grid`` in a fixed, reproducible order."""
    grid = TUNING_GRID if grid is None else grid
    names = list(grid)
    for values in itertools.product(*(grid[name] for name in names)):
        yield dict(zip(names, values, strict=True))


def with_params(cfg: Config, params: dict[str, Any]) -> Config:
    """Return a copy of ``cfg`` with ``params`` applied to its strategy section.

    ``dataclasses.replace`` re-runs ``StrategyCfg.__post_init__``, so an
    impossible parameter set fails here with the same plain-English message a
    user would get from a bad config file, rather than deep inside a simulation.
    """
    return replace(cfg, strategy=replace(cfg.strategy, **params))


def objective_key(metrics: dict[str, Any]) -> tuple[int, float, int, float]:
    """Sort key implementing :data:`OBJECTIVE_DESCRIPTION` — larger is better.

    Returned as a tuple to be maximised: ``(clears_floor, profit_factor,
    trades, -max_drawdown_pct)``.

    BUG-041: ``profit_factor`` is :data:`~swing.backtest.metrics.PROFIT_FACTOR_CAP`
    whenever a parameter set happened to take no losing trades, and that
    sentinel is not a measurement — reading it literally makes eight lucky
    trades outrank three hundred honestly measured ones and hands a whole
    out-of-sample year to a curve fit. A capped set therefore scores 0.0 on
    profit factor and has to win on evidence (trades) and drawdown instead.
    """
    trades = int(metrics.get("trades", 0))
    clears_floor = 1 if trades >= MIN_IS_TRADES else 0
    capped = bool(metrics.get("profit_factor_capped", False))
    profit_factor = 0.0 if capped else float(metrics.get("profit_factor", 0.0))
    max_dd = float(metrics.get("max_drawdown_pct", 0.0))
    return (clears_floor, profit_factor, trades, -max_dd)


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FoldResult:
    """One completed fold: what was chosen in-sample and what it did out-of-sample."""

    window: Window
    params: dict[str, Any]
    is_metrics: dict[str, Any]
    oos_metrics: dict[str, Any]
    oos_trades: pd.DataFrame
    oos_equity: pd.DataFrame
    candidates_evaluated: int

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready summary of this fold (no frames)."""
        return {
            **self.window.as_dict(),
            "params": dict(sorted(self.params.items())),
            "is_trades": int(self.is_metrics.get("trades", 0)),
            "is_profit_factor": float(self.is_metrics.get("profit_factor", 0.0)),
            "is_max_drawdown_pct": float(self.is_metrics.get("max_drawdown_pct", 0.0)),
            "oos_trades": int(self.oos_metrics.get("trades", 0)),
            "oos_profit_factor": float(self.oos_metrics.get("profit_factor", 0.0)),
            "oos_max_drawdown_pct": float(self.oos_metrics.get("max_drawdown_pct", 0.0)),
            "candidates_evaluated": self.candidates_evaluated,
        }


@dataclass(frozen=True)
class WalkForwardResult:
    """The stitched out-of-sample record plus everything that produced it."""

    folds: list[FoldResult]
    trades: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict[str, Any]
    initial_equity: float

    @property
    def windows(self) -> list[Window]:
        return [fold.window for fold in self.folds]


# ---------------------------------------------------------------------------
# stitching
# ---------------------------------------------------------------------------


def stitch_returns(curves: Sequence[pd.DataFrame], initial_equity: float) -> pd.DataFrame:
    """Chain fold equity curves together by compounding their daily returns.

    Each fold starts from the same nominal balance, so the raw dollar levels are
    not comparable across folds. Converting each to daily returns and compounding
    them onto a single running balance produces the curve an account would
    actually have followed, seam-free.

    Cash and position counts are carried through scaled by the same factor, so
    ``exposure_pct`` still means what it means.
    """
    frames: list[pd.DataFrame] = []
    running = float(initial_equity)
    for curve in curves:
        if curve is None or curve.empty:
            continue
        equity = curve["equity"].astype("float64")
        # Every fold is simulated from the same nominal balance, so dividing by
        # it converts the fold to a pure growth factor; multiplying by the
        # running balance lands it on the end of the previous fold with no seam.
        opening = float(initial_equity)
        scale = running / opening if opening > 0 else 0.0
        scaled = equity * scale
        frames.append(
            pd.DataFrame(
                {
                    "equity": scaled,
                    "cash": curve["cash"].astype("float64") * scale,
                    "n_positions": curve["n_positions"].astype("int64"),
                    "drawdown": curve["drawdown"].astype("float64"),
                },
                index=curve.index,
            )
        )
        running = float(scaled.iloc[-1]) if len(scaled) else running

    if not frames:
        return empty_equity()

    stitched = pd.concat(frames)
    stitched = stitched[~stitched.index.duplicated(keep="last")].sort_index()
    # Recompute drawdown against the stitched high-water mark: a fold's own
    # drawdown column only knows about that fold.
    peak = stitched["equity"].cummax()
    stitched["drawdown"] = (stitched["equity"] / peak.replace(0.0, float("nan")) - 1.0).fillna(0.0)
    stitched.index.name = "date"
    return stitched[["equity", "cash", "n_positions", "drawdown"]]


def _concat_trades(frames: Sequence[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate fold trade lists into one chronologically sorted frame."""
    usable = [f for f in frames if f is not None and len(f)]
    if not usable:
        return empty_trades()
    joined = pd.concat(usable, ignore_index=True)
    return joined.sort_values(
        ["exit_date", "entry_date", "symbol"], kind="stable", ignore_index=True
    )


# ---------------------------------------------------------------------------
# the search
# ---------------------------------------------------------------------------


def run_walkforward(
    bars_by_symbol: dict[str, pd.DataFrame],
    spy_bars: pd.DataFrame,
    cfg: Config,
    *,
    earnings: dict[str, date | Sequence[date] | None] | None = None,
    is_etf: dict[str, bool] | None = None,
    start: date | None = None,
    end: date | None = None,
    grid: dict[str, tuple[Any, ...]] | None = None,
    progress: Callable[[str], None] | None = None,
    on_is_evaluation: Callable[[Window, dict[str, Any], EngineResult], None] | None = None,
    eligible: dict[str, np.ndarray] | None = None,
) -> WalkForwardResult:
    """Run the full walk-forward procedure and return the stitched OOS record.

    Args:
        bars_by_symbol: full-history Contract 3 frames. The engine warms its
            indicators up on bars before each window, so pass everything.
        spy_bars: regime symbol bars.
        cfg: base configuration; only the grid parameters are varied.
        earnings: next-earnings dates per symbol.
        is_etf: per-symbol ETF flags.
        start: first date of the first in-sample stretch. Defaults to
            ``cfg.backtest.start``.
        end: last usable date. Defaults to ``cfg.backtest.end`` or the last bar.
        grid: the candidate values to search. Precedence: this argument, then
            ``cfg.backtest.tuning_grid``, then :data:`TUNING_GRID`. Tests pass
            it directly to keep runs small; a real run should express its grid
            in config so the report can record it.
        progress: optional sink for one-line progress messages.
        on_is_evaluation: optional hook fired for every **in-sample**
            evaluation, with the window, the parameters and the raw result.
            Used by the test suite to prove no OOS bar ever reaches the tuner.
        eligible: per-symbol entry gate, forwarded verbatim to every
            :func:`~swing.backtest.engine.run_engine` call — both the in-sample
            tuning runs and the out-of-sample run. The same mask on both sides
            on purpose: tuning against a universe the OOS stretch will not be
            allowed to trade would pick parameters for a different experiment.
            ``None`` (the default) leaves the engine's behaviour untouched.

    Returns:
        A :class:`WalkForwardResult`. With no complete fold the result is empty
        but well-formed — never an exception.
    """
    start = start or cfg.backtest.start
    end = end or cfg.backtest.end or _last_date(bars_by_symbol)
    initial_equity = float(cfg.account.equity)

    if end is None:
        return _empty_walkforward(initial_equity)

    windows = make_windows(
        start, end, is_years=cfg.backtest.is_years, oos_years=cfg.backtest.oos_years
    )
    if not windows:
        log.warning(
            "The backtest span %s to %s is shorter than one %d-year in-sample plus "
            "%d-year out-of-sample fold, so no walk-forward result is possible.",
            start,
            end,
            cfg.backtest.is_years,
            cfg.backtest.oos_years,
        )
        return _empty_walkforward(initial_equity)

    # An explicit argument wins, then whatever config asked for, then the
    # default. Resolving here rather than only in the runner means no caller can
    # accidentally search the default grid while its config says otherwise.
    combos = list(grid_points(resolve_grid(cfg.backtest.tuning_grid if grid is None else grid)))
    folds: list[FoldResult] = []

    for number, window in enumerate(windows, start=1):
        if progress is not None:
            progress(
                f"fold {number}/{len(windows)}: tuning on {window.is_start}..{window.is_end} "
                f"({len(combos)} combinations)"
            )
        # One cache per fold: every symbol's tuning-independent Series is built
        # once and reused across every combination and the OOS run.
        cache = SignalCache()

        best_params: dict[str, Any] | None = None
        best_key: tuple[int, float, int, float] | None = None
        best_metrics: dict[str, Any] = {}

        for params in combos:
            tuned = with_params(cfg, params)
            result = run_engine(
                bars_by_symbol,
                spy_bars,
                tuned,
                earnings=earnings,
                is_etf=is_etf,
                start=window.is_start,
                end=window.is_end,
                cache=cache,
                eligible=eligible,
            )
            if on_is_evaluation is not None:
                on_is_evaluation(window, params, result)
            metrics = compute_metrics(
                result.trades, result.equity, initial_equity=result.initial_equity
            )
            key = objective_key(metrics)
            if best_key is None or key > best_key:
                best_key, best_params, best_metrics = key, params, metrics

        assert best_params is not None  # combos is never empty
        if progress is not None:
            progress(
                f"fold {number}/{len(windows)}: chose {best_params} "
                f"(IS PF {best_metrics.get('profit_factor', 0.0):.2f}, "
                f"{best_metrics.get('trades', 0)} trades); "
                f"testing {window.oos_start}..{window.oos_end}"
            )

        tuned = with_params(cfg, best_params)
        oos = run_engine(
            bars_by_symbol,
            spy_bars,
            tuned,
            earnings=earnings,
            is_etf=is_etf,
            start=window.oos_start,
            end=window.oos_end,
            cache=cache,
            eligible=eligible,
        )
        folds.append(
            FoldResult(
                window=window,
                params=best_params,
                is_metrics=best_metrics,
                oos_metrics=compute_metrics(
                    oos.trades, oos.equity, initial_equity=oos.initial_equity
                ),
                oos_trades=oos.trades,
                oos_equity=oos.equity,
                candidates_evaluated=len(combos),
            )
        )

    trades = _concat_trades([fold.oos_trades for fold in folds])
    equity = stitch_returns([fold.oos_equity for fold in folds], initial_equity)
    metrics = compute_metrics(trades, equity, initial_equity=initial_equity)
    return WalkForwardResult(
        folds=folds,
        trades=trades,
        equity=equity,
        metrics=metrics,
        initial_equity=initial_equity,
    )


def _empty_walkforward(initial_equity: float) -> WalkForwardResult:
    from swing.backtest.metrics import empty_metrics

    return WalkForwardResult(
        folds=[],
        trades=empty_trades(),
        equity=empty_equity(),
        metrics=empty_metrics(),
        initial_equity=initial_equity,
    )


def _last_date(bars_by_symbol: dict[str, pd.DataFrame]) -> date | None:
    """The latest bar date across the whole universe."""
    latest: pd.Timestamp | None = None
    for frame in bars_by_symbol.values():
        if frame is None or frame.empty:
            continue
        candidate = frame.index[-1]
        if latest is None or candidate > latest:
            latest = candidate
    return None if latest is None else latest.date()


# ---------------------------------------------------------------------------
# sensitivity
# ---------------------------------------------------------------------------


def _perturb(value: Any, factor: float) -> Any:
    """Scale a parameter, keeping integers integral."""
    if isinstance(value, bool):  # pragma: no cover - no boolean knobs are perturbed
        return value
    if isinstance(value, int):
        return max(int(round(value * factor)), 2)
    return float(value) * factor


def sensitivity_table(
    bars_by_symbol: dict[str, pd.DataFrame],
    spy_bars: pd.DataFrame,
    cfg: Config,
    *,
    earnings: dict[str, date | Sequence[date] | None] | None = None,
    is_etf: dict[str, bool] | None = None,
    start: date | None = None,
    end: date | None = None,
    params: Sequence[str] = SENSITIVITY_PARAMS,
    step: float = SENSITIVITY_STEP,
    progress: Callable[[str], None] | None = None,
    eligible: dict[str, np.ndarray] | None = None,
) -> list[dict[str, Any]]:
    """Move each parameter +/-25% on its own and report what happens.

    One-at-a-time rather than a joint sweep: the question this table answers is
    "is the result balanced on a knife edge?", and a parameter whose 25% nudge
    halves the profit factor is a red flag no matter what the other parameters
    do. A robust configuration sits on a plateau, not a peak.

    ``eligible`` is the per-symbol entry gate, forwarded to every run in the
    table. It has to be, or the baseline row would describe a different universe
    from the run the table sits beside. ``None`` leaves the engine untouched.

    Returns:
        One row per (parameter, direction) plus a ``baseline`` row, each with
        the parameter value, profit factor, CAGR, max drawdown and trade count.
    """
    cache = SignalCache()
    rows: list[dict[str, Any]] = []

    def _run(label: str, param: str, value: Any, tuned: Config) -> dict[str, Any]:
        result = run_engine(
            bars_by_symbol,
            spy_bars,
            tuned,
            earnings=earnings,
            is_etf=is_etf,
            start=start,
            end=end,
            cache=cache,
            eligible=eligible,
        )
        metrics = compute_metrics(
            result.trades, result.equity, initial_equity=result.initial_equity
        )
        return {
            "param": param,
            "variant": label,
            "value": value,
            "profit_factor": float(metrics["profit_factor"]),
            "cagr": float(metrics["cagr"]),
            "max_drawdown_pct": float(metrics["max_drawdown_pct"]),
            "trades": int(metrics["trades"]),
        }

    if progress is not None:
        progress("sensitivity: baseline")
    rows.append(_run("baseline", "baseline", None, cfg))

    for param in params:
        base_value = getattr(cfg.strategy, param)
        for direction, factor in (("-25%", 1.0 - step), ("+25%", 1.0 + step)):
            value = _perturb(base_value, factor)
            if value == base_value:
                # An integer knob small enough that +/-25% rounds back to itself.
                continue
            if progress is not None:
                progress(f"sensitivity: {param} {direction} -> {value}")
            try:
                tuned = with_params(cfg, {param: value})
            except ValueError as exc:  # a perturbation outside the config's own limits
                log.warning("Skipping %s %s: %s", param, direction, exc)
                continue
            rows.append(_run(direction, param, value, tuned))
    return rows
