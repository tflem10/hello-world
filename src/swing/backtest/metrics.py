"""Performance metrics — the numbers the gate actually reads.

Everything here is a pure function of a trades frame plus an equity curve, both
in the shapes :mod:`swing.backtest.engine` produces. No config, no clock, no
state — so a metric can always be re-derived from the two CSVs in a report
directory.

UNITS (stated once, honoured everywhere)
----------------------------------------
* ``cagr``, ``win_rate``, ``max_drawdown_pct``, ``exposure_pct`` are **percent**
  (``12.5`` means 12.5%). ``max_drawdown_pct`` is reported **positive** — a 22%
  drawdown is ``22.0``, which is what makes ``> gates.max_drawdown_pct`` read
  the right way round.
* ``avg_win`` / ``avg_loss`` are **dollars per trade**, and ``avg_loss`` is
  negative (it is the mean of the losing P&Ls, not its magnitude).
* ``sharpe`` and ``sortino`` are annualised from daily returns with rf = 0,
  scaled by sqrt(252).
* ``profit_factor`` is gross wins / |gross losses|, a bare ratio.

PROFIT FACTOR WITH NO LOSSES
-----------------------------
A strategy with zero losing trades has an infinite profit factor, and ``inf``
is not valid JSON. Contract 11's ruling: report ``9999.0`` and set the
companion flag ``profit_factor_capped`` to ``True`` so nobody mistakes the
sentinel for a measurement. With no trades at all the profit factor is ``0.0``
(uncapped) — an untested strategy fails the gate, it does not ace it.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "METRIC_KEYS",
    "PROFIT_FACTOR_CAP",
    "TRADING_DAYS_PER_YEAR",
    "by_year_table",
    "compute_metrics",
    "empty_metrics",
    "max_drawdown",
]

#: Exactly the keys Contract 11 requires inside ``oos`` / ``full_period``.
METRIC_KEYS: tuple[str, ...] = (
    "cagr",
    "sharpe",
    "sortino",
    "max_drawdown_pct",
    "max_dd_duration_days",
    "win_rate",
    "profit_factor",
    "avg_win",
    "avg_loss",
    "avg_hold_days",
    "exposure_pct",
    "trades",
)

#: Sentinel used in place of an infinite profit factor (JSON has no infinity).
PROFIT_FACTOR_CAP = 9999.0

TRADING_DAYS_PER_YEAR = 252
DAYS_PER_YEAR = 365.25


def empty_metrics() -> dict[str, Any]:
    """The metric dict for "nothing happened" — every key present, all zero."""
    return {
        "cagr": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "max_drawdown_pct": 0.0,
        "max_dd_duration_days": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "profit_factor_capped": False,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "avg_hold_days": 0.0,
        "exposure_pct": 0.0,
        "trades": 0,
    }


def _daily_returns(equity: pd.Series) -> pd.Series:
    """Simple daily returns of an equity curve, first day defined as 0."""
    if len(equity) < 2:
        return pd.Series([0.0] * len(equity), index=equity.index, dtype="float64")
    returns = equity.astype("float64").pct_change()
    return returns.fillna(0.0)


def max_drawdown(equity: pd.Series) -> tuple[float, int]:
    """Return ``(max drawdown as a positive percent, longest underwater days)``.

    The duration is measured in **calendar days** between the high-water mark
    and the first bar that regains it. A drawdown that never recovers is
    measured to the last bar in the series, which is the honest reading: it is
    still going.
    """
    if equity.empty:
        return 0.0, 0
    values = equity.astype("float64")
    peak = values.cummax()
    drawdown = (values / peak.replace(0.0, np.nan)) - 1.0
    drawdown = drawdown.fillna(0.0)
    worst = float(drawdown.min())
    max_dd_pct = abs(worst) * 100.0 if worst < 0 else 0.0

    # Underwater stretches: each begins the first bar below a peak and ends on
    # the bar that matches the peak again (or at the end of the series).
    longest = 0
    peak_date = values.index[0]
    peak_value = float(values.iloc[0])
    for stamp, value in values.items():
        value = float(value)
        if value >= peak_value:
            peak_value = value
            peak_date = stamp
        else:
            span = (stamp - peak_date).days
            longest = max(longest, int(span))
    return round(max_dd_pct, 10), longest


def _annualised(returns: pd.Series) -> tuple[float, float]:
    """Return ``(sharpe, sortino)`` for a daily return series, rf = 0."""
    if len(returns) < 2:
        return 0.0, 0.0
    values = returns.to_numpy(dtype="float64")
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1))
    scale = math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe = (mean / std * scale) if std > 0 else 0.0

    downside = np.minimum(values, 0.0)
    downside_dev = float(math.sqrt(float(np.mean(downside**2))))
    sortino = (mean / downside_dev * scale) if downside_dev > 0 else 0.0
    return sharpe, sortino


def _cagr(equity: pd.Series) -> float:
    """Compound annual growth rate in percent, from first bar to last."""
    if len(equity) < 2:
        return 0.0
    start_value = float(equity.iloc[0])
    end_value = float(equity.iloc[-1])
    if start_value <= 0 or end_value <= 0:
        return 0.0
    span_days = (equity.index[-1] - equity.index[0]).days
    if span_days <= 0:
        return 0.0
    years = span_days / DAYS_PER_YEAR
    return ((end_value / start_value) ** (1.0 / years) - 1.0) * 100.0


def _profit_factor(pnl: pd.Series) -> tuple[float, bool]:
    """Gross wins over gross losses, with the no-losses sentinel applied."""
    if pnl.empty:
        return 0.0, False
    wins = float(pnl[pnl > 0].sum())
    losses = float(-pnl[pnl < 0].sum())
    if losses > 0:
        return wins / losses, False
    if wins > 0:
        return PROFIT_FACTOR_CAP, True
    return 0.0, False


def compute_metrics(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    *,
    initial_equity: float | None = None,
) -> dict[str, Any]:
    """Summarise one simulation.

    Args:
        trades: engine trades frame (may be empty).
        equity: engine equity frame, indexed by date, with ``equity`` and
            ``cash`` columns (``cash`` is what makes exposure computable).
        initial_equity: starting capital. When given it is prepended to the
            curve as the bar before the first, so the first day's P&L counts
            toward returns and drawdown. Without it the curve speaks for itself.

    Returns:
        A dict with every key in :data:`METRIC_KEYS`, plus
        ``profit_factor_capped``.
    """
    if equity is None or equity.empty:
        result = empty_metrics()
        result["trades"] = int(len(trades)) if trades is not None else 0
        return result

    curve = equity["equity"].astype("float64")
    if initial_equity is not None and len(curve) > 0:
        # Prepend the opening balance one day before the first bar so that day
        # one's profit is not invisible to returns and drawdown.
        opening_stamp = curve.index[0] - pd.Timedelta(days=1)
        curve = pd.concat([pd.Series([float(initial_equity)], index=[opening_stamp]), curve])

    returns = _daily_returns(curve)
    sharpe, sortino = _annualised(returns)
    max_dd_pct, dd_days = max_drawdown(curve)

    if "cash" in equity.columns:
        invested = (equity["equity"] - equity["cash"]).astype("float64")
        denominator = equity["equity"].astype("float64").replace(0.0, np.nan)
        exposure = float((invested / denominator).fillna(0.0).mean()) * 100.0
    else:  # pragma: no cover - engine always supplies cash
        exposure = 0.0

    n_trades = 0 if trades is None else int(len(trades))
    if n_trades:
        pnl = trades["pnl"].astype("float64")
        wins = pnl[pnl > 0]
        losses = pnl[pnl < 0]
        win_rate = float(len(wins)) / float(n_trades) * 100.0
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
        avg_hold = float(trades["hold_days"].astype("float64").mean())
        profit_factor, capped = _profit_factor(pnl)
    else:
        win_rate = avg_win = avg_loss = avg_hold = 0.0
        profit_factor, capped = 0.0, False

    return {
        "cagr": _cagr(curve),
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": max_dd_pct,
        "max_dd_duration_days": int(dd_days),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "profit_factor_capped": capped,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_hold_days": avg_hold,
        "exposure_pct": exposure,
        "trades": n_trades,
    }


def by_year_table(
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    *,
    initial_equity: float | None = None,
) -> dict[str, dict[str, float]]:
    """Per-calendar-year ``{return_pct, trades, max_dd_pct}``.

    The yearly return compounds that year's daily returns, so it does not
    depend on picking a base bar, and a year that starts mid-stream (the first
    year of a backtest) is handled the same way as any other. ``trades`` counts
    trades by their **exit** date — that is when the P&L is realised. The
    drawdown is measured inside the year only, so a multi-year drawdown shows
    up in each year it passes through, at the depth it reached there.
    """
    if equity is None or equity.empty:
        return {}

    curve = equity["equity"].astype("float64")
    if initial_equity is not None and len(curve) > 0:
        opening_stamp = curve.index[0] - pd.Timedelta(days=1)
        curve = pd.concat([pd.Series([float(initial_equity)], index=[opening_stamp]), curve])
    returns = _daily_returns(curve)

    has_trades = trades is not None and len(trades) > 0
    exits = trades["exit_date"] if has_trades else pd.Series(dtype="datetime64[ns]")
    trade_counts = pd.to_datetime(exits).dt.year.value_counts().to_dict() if len(exits) else {}

    out: dict[str, dict[str, float]] = {}
    for year, group in returns.groupby(returns.index.year):
        compounded = float((1.0 + group).prod() - 1.0)
        year_curve = (1.0 + group).cumprod()
        year_dd, _ = max_drawdown(year_curve)
        out[str(int(year))] = {
            "return_pct": compounded * 100.0,
            "trades": int(trade_counts.get(int(year), 0)),
            "max_dd_pct": year_dd,
        }
    return out
