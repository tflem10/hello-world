"""Walk-forward analysis.

The headline number for this system is the **concatenated out-of-sample equity
curve**, not the full-period backtest. The full-period run is reported too, but
it is the number that lies: any parameter you chose after looking at the whole
history is fitted to that history.

Procedure
---------
For each step, take ``in_sample_years`` of history, grid-search the configured
parameters on that block only, then run the following ``out_of_sample_years``
with the winning parameters and keep *only* that out-of-sample segment. Step
forward by ``step_years`` and repeat. Segments are chained at their realised
equity, so whole-share effects compound the way they actually would — which
matters enormously on a small account, where a 3% gain may not buy another
share of anything.

Two honest caveats:

* Walk-forward does not make an overfit strategy safe. It bounds how much you
  can fool yourself, and a small grid bounds it further. That is why the grid
  in the shipped config has 27 points, not 27,000.
* Each out-of-sample block is one year. A handful of years is a small sample,
  and the confidence interval on any of these statistics is wide.
* The objective surface is **discontinuous**. A change of one part in 10^13 in
  an indicator — well inside floating-point noise — has been observed to change
  which grid point wins a window, and therefore the whole out-of-sample trade
  set. Runs are byte-reproducible, but the *parameter choice* is not robust,
  which is itself the finding: if the grid points cannot be told apart by more
  than noise, the optimiser has not found an optimum. Near-ties are reported as
  warnings; see docs/indicator-research.md §14.
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ..config import Config
from ..logging_setup import get_logger
from .engine import BacktestResult, run_backtest
from .metrics import Metrics, compute_metrics

log = get_logger("swing.walkforward")


@dataclass
class Window:
    is_start: date
    is_end: date
    oos_start: date
    oos_end: date

    def __str__(self) -> str:
        return (
            f"IS {self.is_start}..{self.is_end} -> OOS {self.oos_start}..{self.oos_end}"
        )


@dataclass
class WalkForwardResult:
    windows: list[Window]
    chosen_params: list[dict]
    segments: list[BacktestResult]
    equity: pd.Series
    trades: pd.DataFrame
    metrics: Metrics
    is_metrics: list[Metrics]
    oos_metrics: list[Metrics]
    warnings: list[str] = field(default_factory=list)

    def window_table(self) -> pd.DataFrame:
        rows = []
        for w, params, is_m, oos_m in zip(
            self.windows, self.chosen_params, self.is_metrics, self.oos_metrics,
            strict=False,
        ):
            rows.append(
                {
                    "in_sample": f"{w.is_start} .. {w.is_end}",
                    "out_of_sample": f"{w.oos_start} .. {w.oos_end}",
                    "params": ", ".join(f"{k.split('.')[-1]}={v}" for k, v in params.items()),
                    "is_pf": round(is_m.profit_factor, 2) if is_m else None,
                    "oos_return": round(oos_m.total_return, 4) if oos_m else None,
                    "oos_pf": round(oos_m.profit_factor, 2) if oos_m else None,
                    "oos_trades": oos_m.n_trades if oos_m else 0,
                    "oos_maxdd": round(oos_m.max_drawdown, 4) if oos_m else None,
                }
            )
        return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# config plumbing
# ---------------------------------------------------------------------------
def apply_overrides(cfg: Config, overrides: dict) -> Config:
    """Return a copy of ``cfg`` with dotted-path keys replaced.

    ``apply_overrides(cfg, {"strategy.exit.initial_stop_atr": 2.5})``
    """
    data = cfg.as_dict()
    for path, value in overrides.items():
        node = data
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data, source=cfg.source)


def grid_points(grid: dict) -> list[dict]:
    """Cartesian product of a ``{dotted.path: [values]}`` grid."""
    if not grid:
        return [{}]
    keys = sorted(grid)
    combos = itertools.product(*(grid[k] for k in keys))
    return [dict(zip(keys, values, strict=True)) for values in combos]


# Two parameter sets whose objective differs by less than this are treated as
# indistinguishable, and the first in sorted order wins.
#
# This is not cosmetic. Grid scores routinely land within a rounding error of
# each other, and without a tie band the "winner" flips on floating-point
# associativity — a 1e-13 change in an indicator implementation was observed to
# reshuffle an entire out-of-sample trade set. A result that fragile is not a
# result. Holding the incumbent makes selection reproducible AND makes the
# near-ties visible, which is the honest signal: if six parameter sets are
# indistinguishable in-sample, the optimiser has not found anything.
TIE_BAND = 1e-6


def objective_value(metrics: Metrics, objective: str) -> float:
    value = {
        "profit_factor": metrics.profit_factor,
        "sharpe": metrics.sharpe,
        "calmar": metrics.calmar,
        "expectancy_r": metrics.expectancy_r,
        "cagr": metrics.cagr,
        "total_return": metrics.total_return,
    }.get(objective)
    if value is None:
        raise ValueError(f"unknown walk-forward objective {objective!r}")
    # An infinite profit factor means "no losing trades yet" — a sample-size
    # artefact, not a winner. Do not let it take the grid.
    if value == float("inf"):
        return 0.0
    return float(value)


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------
def make_windows(
    start: date, end: date, in_sample_years: int, oos_years: int, step_years: int
) -> list[Window]:
    windows: list[Window] = []
    is_start = start
    while True:
        is_end = _add_years(is_start, in_sample_years)
        oos_end = _add_years(is_end, oos_years)
        if is_end >= end:
            break
        windows.append(
            Window(
                is_start=is_start,
                is_end=is_end - pd.Timedelta(days=1).to_pytimedelta(),
                oos_start=is_end,
                oos_end=min(oos_end - pd.Timedelta(days=1).to_pytimedelta(), end),
            )
        )
        if oos_end >= end:
            break
        is_start = _add_years(is_start, step_years)
    return windows


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:                      # 29 February
        return d.replace(year=d.year + years, day=28)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def run_walk_forward(
    cfg: Config,
    bars: dict[str, pd.DataFrame],
    meta: dict | None = None,
    benchmark: pd.DataFrame | None = None,
    earnings: dict | None = None,
    start: date | None = None,
    end: date | None = None,
) -> WalkForwardResult:
    wf = cfg.backtest.walk_forward
    start = start or date.fromisoformat(str(cfg.backtest.start))
    end = end or _default_end(cfg, bars)

    windows = make_windows(
        start,
        end,
        int(wf.in_sample_years),
        int(wf.out_of_sample_years),
        int(wf.step_years),
    )
    if not windows:
        raise ValueError(
            f"not enough history between {start} and {end} for a "
            f"{wf.in_sample_years}y/{wf.out_of_sample_years}y walk-forward"
        )

    grid = wf.get("grid", {}) or {}
    if hasattr(grid, "as_dict"):
        grid = grid.as_dict()
    combos = grid_points(grid) if bool(wf.get("optimize", True)) else [{}]
    objective = str(wf.get("objective", "profit_factor"))
    min_is_trades = int(wf.get("min_is_trades", 0))

    log.info(
        "walk-forward: %d windows x %d parameter combinations", len(windows), len(combos)
    )

    feature_cache: dict = {}
    warnings: list[str] = []
    chosen: list[dict] = []
    segments: list[BacktestResult] = []
    is_metrics: list[Metrics] = []
    oos_metrics: list[Metrics] = []

    running_equity = float(cfg.backtest.initial_equity)

    for n, window in enumerate(windows, start=1):
        log.info("window %d/%d  %s", n, len(windows), window)

        best_params, best_metrics, best_score = {}, None, float("-inf")
        near_ties = 0
        for params in combos:
            trial_cfg = apply_overrides(cfg, params)
            result = run_backtest(
                trial_cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
                start=window.is_start, end=window.is_end,
                label=f"IS{n}", feature_cache=feature_cache,
            )
            m = compute_metrics(result.equity, result.trades, result.exposure,
                                result.open_positions)
            if m.n_trades < min_is_trades:
                continue
            score = objective_value(m, objective)
            band = TIE_BAND * max(1.0, abs(best_score)) if best_score > float("-inf") else 0.0
            if score > best_score + band:
                best_params, best_metrics, best_score = params, m, score
                near_ties = 0
            elif abs(score - best_score) <= band:
                near_ties += 1

        if near_ties:
            warnings.append(
                f"window {n}: {near_ties} parameter set(s) scored within {TIE_BAND:g} "
                f"of the winner on {objective}. The optimiser could not distinguish "
                "them, so the first in sorted order was kept. Treat the 'chosen' "
                "parameters for this window as arbitrary among the tied set."
            )

        if best_metrics is None:
            warnings.append(
                f"window {n} ({window}): no parameter set produced at least "
                f"{min_is_trades} in-sample trades; fell back to config defaults"
            )
            best_params = {}
            trial_cfg = cfg
            result = run_backtest(
                cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
                start=window.is_start, end=window.is_end, label=f"IS{n}",
                feature_cache=feature_cache,
            )
            best_metrics = compute_metrics(result.equity, result.trades,
                                           result.exposure, result.open_positions)

        oos_cfg = apply_overrides(cfg, best_params)
        oos_data = oos_cfg.as_dict()
        oos_data["backtest"]["initial_equity"] = running_equity
        oos_cfg = Config(oos_data, source=cfg.source)

        oos = run_backtest(
            oos_cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
            start=window.oos_start, end=window.oos_end,
            label=f"OOS{n}", feature_cache=feature_cache,
        )
        running_equity = float(oos.equity.iloc[-1])

        chosen.append(best_params)
        segments.append(oos)
        is_metrics.append(best_metrics)
        oos_metrics.append(
            compute_metrics(oos.equity, oos.trades, oos.exposure, oos.open_positions)
        )
        warnings.extend(w for w in oos.warnings if w not in warnings)

    equity = pd.concat([seg.equity for seg in segments])
    equity = equity[~equity.index.duplicated(keep="first")].sort_index()
    trades = pd.concat([seg.trades for seg in segments], ignore_index=True)
    exposure = pd.concat([seg.exposure for seg in segments])
    exposure = exposure[~exposure.index.duplicated(keep="first")].sort_index()
    open_positions = pd.concat([seg.open_positions for seg in segments])
    open_positions = open_positions[~open_positions.index.duplicated(keep="first")].sort_index()

    return WalkForwardResult(
        windows=windows,
        chosen_params=chosen,
        segments=segments,
        equity=equity,
        trades=trades,
        metrics=compute_metrics(equity, trades, exposure, open_positions),
        is_metrics=is_metrics,
        oos_metrics=oos_metrics,
        warnings=warnings,
    )


def _default_end(cfg: Config, bars: dict[str, pd.DataFrame]) -> date:
    configured = str(cfg.backtest.get("end", "") or "")
    if configured:
        return date.fromisoformat(configured)
    latest = max((b.index[-1] for b in bars.values() if len(b)), default=None)
    return latest.date() if latest is not None else date.today()


def parameter_sensitivity(
    cfg: Config,
    bars: dict[str, pd.DataFrame],
    paths: list[str],
    meta: dict | None = None,
    benchmark: pd.DataFrame | None = None,
    earnings: dict | None = None,
    start: date | None = None,
    end: date | None = None,
    deltas: tuple[float, ...] = (-0.25, 0.0, 0.25),
) -> pd.DataFrame:
    """Vary each parameter by +/-25% one at a time and report the damage.

    A strategy whose profit factor collapses when a stop multiple moves 25% is
    not a strategy, it is a curve fit sitting on a knife edge. This table is
    meant to be read for *flatness*, not for the best cell.
    """
    feature_cache: dict = {}
    rows = []
    for path in paths:
        base_value = _get_path(cfg, path)
        if not isinstance(base_value, (int, float)):
            log.warning("skipping non-numeric sensitivity parameter %s", path)
            continue
        for delta in deltas:
            value = base_value * (1.0 + delta)
            value = int(round(value)) if isinstance(base_value, int) else round(value, 4)
            trial = apply_overrides(cfg, {path: value})
            result = run_backtest(
                trial, bars, meta=meta, benchmark=benchmark, earnings=earnings,
                start=start, end=end, label=f"{path}={value}", feature_cache=feature_cache,
            )
            m = compute_metrics(result.equity, result.trades, result.exposure,
                                result.open_positions)
            rows.append(
                {
                    "parameter": path,
                    "delta": f"{delta:+.0%}",
                    "value": value,
                    "trades": m.n_trades,
                    "cagr": round(m.cagr, 4),
                    "profit_factor": round(m.profit_factor, 3),
                    "sharpe": round(m.sharpe, 3),
                    "max_drawdown": round(m.max_drawdown, 4),
                }
            )
    return pd.DataFrame(rows)


def _get_path(cfg: Config, path: str):
    node = cfg.as_dict()
    for part in path.split("."):
        node = node[part]
    return node


def deep_copy_config(cfg: Config) -> Config:
    return Config(copy.deepcopy(cfg.as_dict()), source=cfg.source)
