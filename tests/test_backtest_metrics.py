"""Metric tests built on tiny hand-constructed frames.

Every expectation here is derived arithmetically in a comment. If a metric ever
changes meaning, one of these breaks and says which.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from swing.backtest.engine import EQUITY_COLUMNS, TRADE_COLUMNS, empty_equity, empty_trades
from swing.backtest.metrics import (
    METRIC_KEYS,
    PROFIT_FACTOR_CAP,
    by_year_table,
    compute_metrics,
    empty_metrics,
    max_drawdown,
)


def make_equity(values, start="2020-01-01", cash=None):
    """An equity frame with the engine's columns from a list of equity values."""
    index = pd.DatetimeIndex(pd.bdate_range(start=start, periods=len(values)), name="date")
    equity = pd.Series([float(v) for v in values], index=index)
    peak = equity.cummax()
    cash_series = (
        pd.Series([float(c) for c in cash], index=index) if cash is not None else equity.copy()
    )
    return pd.DataFrame(
        {
            "equity": equity,
            "cash": cash_series,
            "n_positions": pd.Series([0] * len(values), index=index, dtype="int64"),
            "drawdown": (equity / peak - 1.0),
        }
    )[list(EQUITY_COLUMNS)]


def make_trades(rows):
    """A trades frame from ``(symbol, entry, exit, pnl, hold_days)`` tuples."""
    records = [
        {
            "symbol": symbol,
            "entry_date": pd.Timestamp(entry),
            "entry_price": 100.0,
            "exit_date": pd.Timestamp(exit_),
            "exit_price": 100.0 + pnl,
            "shares": 1,
            "pnl": float(pnl),
            "pnl_pct": float(pnl),
            "hold_days": int(hold),
            "exit_reason": "stop",
            "entry_cost": 0.0,
            "exit_cost": 0.0,
        }
        for symbol, entry, exit_, pnl, hold in rows
    ]
    if not records:
        return empty_trades()
    return pd.DataFrame(records)[list(TRADE_COLUMNS)]


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------


def test_metric_keys_are_exactly_the_contract_keys():
    """Contract 11 fixes the key set; the gate and the report both index by name."""
    assert set(METRIC_KEYS) == {
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
    }


def test_every_contract_key_is_present_in_a_real_result():
    equity = make_equity([100.0, 101.0, 102.0])
    trades = make_trades([("AAA", "2020-01-01", "2020-01-02", 2.0, 1)])
    metrics = compute_metrics(trades, equity)
    for key in METRIC_KEYS:
        assert key in metrics, key
    assert "profit_factor_capped" in metrics


def test_empty_inputs_give_a_complete_zeroed_result():
    metrics = compute_metrics(empty_trades(), empty_equity())
    assert metrics == empty_metrics()
    assert all(key in metrics for key in METRIC_KEYS)


# ---------------------------------------------------------------------------
# trade statistics
# ---------------------------------------------------------------------------


def test_win_rate_profit_factor_and_averages_by_hand():
    # Five trades: +100, +50, -40, -10, +10  ->  3 wins / 5 = 60% win rate
    # gross wins  = 100 + 50 + 10 = 160
    # gross losses= 40 + 10       =  50
    # profit factor = 160 / 50    = 3.2
    # avg win  = 160 / 3 = 53.3333...
    # avg loss = -50 / 2 = -25.0   (negative, by convention)
    # avg hold = (1 + 2 + 3 + 4 + 5) / 5 = 3.0
    trades = make_trades(
        [
            ("AAA", "2020-01-01", "2020-01-02", 100.0, 1),
            ("BBB", "2020-01-01", "2020-01-03", 50.0, 2),
            ("CCC", "2020-01-01", "2020-01-06", -40.0, 3),
            ("DDD", "2020-01-01", "2020-01-07", -10.0, 4),
            ("EEE", "2020-01-01", "2020-01-08", 10.0, 5),
        ]
    )
    equity = make_equity([1000.0] * 6)
    metrics = compute_metrics(trades, equity)

    assert metrics["trades"] == 5
    assert metrics["win_rate"] == pytest.approx(60.0)
    assert metrics["profit_factor"] == pytest.approx(3.2)
    assert metrics["profit_factor_capped"] is False
    assert metrics["avg_win"] == pytest.approx(160.0 / 3.0)
    assert metrics["avg_loss"] == pytest.approx(-25.0)
    assert metrics["avg_hold_days"] == pytest.approx(3.0)


def test_profit_factor_with_no_losses_is_capped_and_flagged():
    """Infinity is not JSON. Contract 11 says 9999.0 plus an explicit flag."""
    trades = make_trades(
        [
            ("AAA", "2020-01-01", "2020-01-02", 10.0, 1),
            ("BBB", "2020-01-01", "2020-01-03", 20.0, 2),
        ]
    )
    metrics = compute_metrics(trades, make_equity([100.0, 110.0, 130.0]))
    assert metrics["profit_factor"] == PROFIT_FACTOR_CAP
    assert metrics["profit_factor_capped"] is True
    assert math.isfinite(metrics["profit_factor"])


def test_no_trades_means_profit_factor_zero_not_infinite():
    """An untested strategy must fail the gate, not ace it."""
    metrics = compute_metrics(empty_trades(), make_equity([100.0, 100.0]))
    assert metrics["profit_factor"] == 0.0
    assert metrics["profit_factor_capped"] is False
    assert metrics["trades"] == 0


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------


def test_max_drawdown_by_hand():
    # 100 -> 120 -> 90 -> 130 : the trough is 90 against a peak of 120
    # (90 / 120) - 1 = -0.25  ->  reported as +25.0 percent
    equity = pd.Series(
        [100.0, 120.0, 90.0, 130.0],
        index=pd.bdate_range("2020-01-01", periods=4),
    )
    depth, _duration = max_drawdown(equity)
    assert depth == pytest.approx(25.0)


def test_max_drawdown_duration_counts_calendar_days_to_recovery():
    # Peak on day 0 (Wed 2020-01-01), under water on days 1-3, back to the peak
    # on day 4. Business days: Jan 1, 2, 3, 6, 7 -> the last underwater bar is
    # Jan 6, which is 5 calendar days after the Jan 1 peak.
    equity = pd.Series(
        [100.0, 90.0, 80.0, 95.0, 100.0],
        index=pd.bdate_range("2020-01-01", periods=5),
    )
    _depth, duration = max_drawdown(equity)
    assert duration == 5


def test_unrecovered_drawdown_is_measured_to_the_end():
    equity = pd.Series(
        [100.0, 90.0, 90.0, 90.0],
        index=pd.DatetimeIndex(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-31"]),
    )
    depth, duration = max_drawdown(equity)
    assert depth == pytest.approx(10.0)
    assert duration == 30  # 2020-01-01 -> 2020-01-31


def test_flat_curve_has_no_drawdown():
    flat = pd.Series([100.0] * 5, index=pd.bdate_range("2020-01-01", periods=5))
    depth, duration = max_drawdown(flat)
    assert depth == 0.0
    assert duration == 0


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------


def test_cagr_doubling_in_one_year_is_about_one_hundred_percent():
    index = pd.DatetimeIndex(["2020-01-01", "2021-01-01"])
    equity = pd.DataFrame(
        {
            "equity": [100.0, 200.0],
            "cash": [100.0, 200.0],
            "n_positions": [0, 0],
            "drawdown": [0.0, 0.0],
        },
        index=index,
    )
    metrics = compute_metrics(empty_trades(), equity)
    # 366 calendar days / 365.25 = 1.002 years, so slightly under 100%.
    assert metrics["cagr"] == pytest.approx(99.8, abs=0.5)


def test_zero_variance_curve_does_not_divide_by_zero():
    """A flat curve has no return dispersion at all; Sharpe/Sortino are defined as 0."""
    metrics = compute_metrics(empty_trades(), make_equity([100.0] * 30))
    assert metrics["sharpe"] == 0.0
    assert metrics["sortino"] == 0.0


def test_a_curve_that_never_falls_has_no_downside_deviation():
    """Sortino divides by downside deviation only; with no down days it is defined as 0."""
    values = [100.0 * (1.01**i) for i in range(30)]
    metrics = compute_metrics(empty_trades(), make_equity(values))
    assert math.isfinite(metrics["sharpe"])
    assert metrics["sharpe"] > 0.0
    assert metrics["sortino"] == 0.0  # no negative daily return anywhere


def test_sortino_ignores_upside_volatility():
    """Same mean, but one series has all its variance on the upside."""
    choppy = make_equity([100.0, 90.0, 100.0, 90.0, 100.0, 110.0])
    metrics = compute_metrics(empty_trades(), choppy)
    assert math.isfinite(metrics["sortino"])
    # Downside deviation is smaller than total deviation, so Sortino > Sharpe
    # whenever there is any upside variance at all.
    assert metrics["sortino"] > metrics["sharpe"]


def test_initial_equity_makes_day_one_pnl_visible():
    """Without the opening balance, the first bar's profit is invisible to returns."""
    equity = make_equity([110.0, 110.0, 110.0])
    without = compute_metrics(empty_trades(), equity)
    with_open = compute_metrics(empty_trades(), equity, initial_equity=100.0)
    assert without["cagr"] == 0.0  # flat curve, nothing happened
    assert with_open["cagr"] > 0.0  # 100 -> 110 on day one is real money


# ---------------------------------------------------------------------------
# exposure
# ---------------------------------------------------------------------------


def test_exposure_is_the_mean_invested_fraction():
    # equity 100 every day; cash 100, 50, 0  ->  invested 0%, 50%, 100%
    # mean = 50%
    equity = make_equity([100.0, 100.0, 100.0], cash=[100.0, 50.0, 0.0])
    metrics = compute_metrics(empty_trades(), equity)
    assert metrics["exposure_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# by year
# ---------------------------------------------------------------------------


def test_by_year_table_splits_returns_trades_and_drawdown_per_year():
    index = pd.DatetimeIndex(
        ["2020-12-30", "2020-12-31", "2021-01-04", "2021-06-01", "2021-12-31"],
        name="date",
    )
    equity = pd.DataFrame(
        {
            "equity": [100.0, 110.0, 121.0, 100.0, 150.0],
            "cash": [100.0, 110.0, 121.0, 100.0, 150.0],
            "n_positions": [0, 0, 0, 0, 0],
            "drawdown": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        index=index,
    )
    trades = make_trades(
        [
            ("AAA", "2020-12-01", "2020-12-31", 10.0, 20),
            ("BBB", "2021-01-01", "2021-06-01", -21.0, 5),
            ("CCC", "2021-06-02", "2021-12-31", 50.0, 140),
        ]
    )
    table = by_year_table(trades, equity)

    assert set(table) == {"2020", "2021"}
    # 2020: two bars, 100 -> 110, one daily return of +10%.
    assert table["2020"]["return_pct"] == pytest.approx(10.0)
    assert table["2020"]["trades"] == 1
    # 2021: +10% (110->121), -17.355% (121->100), +50% (100->150)
    # compounded: 1.10 * 0.826446 * 1.5 - 1 = 0.363636...
    assert table["2021"]["return_pct"] == pytest.approx(36.3636, abs=1e-3)
    assert table["2021"]["trades"] == 2
    # Within 2021 the curve runs 1.10 -> 0.909 -> 1.3636 (rebased), so the
    # drawdown is 1 - 0.909/1.10 = 17.355%.
    assert table["2021"]["max_dd_pct"] == pytest.approx(17.3554, abs=1e-3)


def test_by_year_is_empty_for_an_empty_curve():
    assert by_year_table(empty_trades(), empty_equity()) == {}


def test_by_year_keys_are_strings_for_json():
    equity = make_equity([100.0, 101.0], start="2020-01-01")
    table = by_year_table(empty_trades(), equity)
    assert all(isinstance(key, str) for key in table)
