"""Metric definitions, checked against hand-computable series."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing.backtest.metrics import (
    TRADING_DAYS,
    compute_metrics,
    drawdown_series,
    drawdown_stats,
    exit_reason_table,
    monthly_returns,
    trade_stats,
    yearly_table,
)


def _equity(values, start="2020-01-01"):
    return pd.Series(
        [float(v) for v in values],
        index=pd.bdate_range(start=start, periods=len(values)),
        name="equity",
    )


def _trades(pnls, r_multiples=None, hold=5):
    n = len(pnls)
    idx = pd.bdate_range("2020-01-01", periods=max(n, 1))
    return pd.DataFrame(
        {
            "symbol": ["A"] * n,
            "entry_date": idx[:n],
            "exit_date": idx[:n],
            "pnl": [float(p) for p in pnls],
            "return_pct": [float(p) / 100.0 for p in pnls],
            "r_multiple": list(r_multiples) if r_multiples else [float(p) / 100 for p in pnls],
            "hold_days": [hold] * n,
            "exit_reason": ["stop"] * n,
        }
    )


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------
def test_drawdown_of_a_monotonic_rise_is_zero():
    eq = _equity([100, 110, 120, 130])
    assert drawdown_series(eq).min() == pytest.approx(0.0)
    assert drawdown_stats(eq) == (0.0, 0)


def test_max_drawdown_is_hand_computable():
    # Peak 200, trough 150 -> 25% drawdown, 2 days underwater.
    eq = _equity([100, 200, 150, 180, 250])
    max_dd, days = drawdown_stats(eq)
    assert max_dd == pytest.approx(0.25)
    assert days == 2


def test_drawdown_duration_counts_the_longest_underwater_run():
    eq = _equity([100, 90, 95, 101, 90, 85, 80, 102])
    _, days = drawdown_stats(eq)
    assert days == 3


def test_drawdown_is_never_reported_as_negative_zero():
    max_dd, _ = drawdown_stats(_equity([100, 101, 102]))
    assert max_dd == 0.0
    assert not np.signbit(max_dd)


# ---------------------------------------------------------------------------
# trade statistics
# ---------------------------------------------------------------------------
def test_profit_factor_is_gross_wins_over_gross_losses():
    stats = trade_stats(_trades([100, 100, -50, -50]))
    assert stats["profit_factor"] == pytest.approx(2.0)
    assert stats["win_rate"] == pytest.approx(0.5)


def test_profit_factor_is_infinite_with_no_losers_not_silently_clipped():
    stats = trade_stats(_trades([10, 20, 30]))
    assert stats["profit_factor"] == float("inf")


def test_profit_factor_is_zero_with_no_trades():
    stats = trade_stats(pd.DataFrame())
    assert stats["profit_factor"] == 0.0
    assert stats["n_trades"] == 0


def test_expectancy_is_the_mean_r_multiple():
    stats = trade_stats(_trades([100, -50, 200], r_multiples=[2.0, -1.0, 3.0]))
    assert stats["expectancy_r"] == pytest.approx(4.0 / 3.0)


def test_infinite_r_multiples_are_excluded_from_expectancy():
    stats = trade_stats(_trades([100, -50], r_multiples=[float("inf"), -1.0]))
    assert stats["expectancy_r"] == pytest.approx(-1.0)


def test_largest_win_and_loss():
    stats = trade_stats(_trades([5, -300, 900, -20]))
    assert stats["largest_win"] == 900
    assert stats["largest_loss"] == -300


# ---------------------------------------------------------------------------
# returns
# ---------------------------------------------------------------------------
def test_cagr_of_a_doubling_over_one_year():
    eq = _equity(np.linspace(100, 200, TRADING_DAYS))
    m = compute_metrics(eq, pd.DataFrame())
    assert m.years == pytest.approx(1.0, abs=0.01)
    assert m.cagr == pytest.approx(1.0, rel=0.02)
    assert m.total_return == pytest.approx(1.0)


def test_metrics_of_a_flat_curve_are_all_zero():
    m = compute_metrics(_equity([100] * 100), pd.DataFrame())
    assert m.cagr == 0.0
    assert m.sharpe == 0.0
    assert m.max_drawdown == 0.0
    assert m.n_trades == 0


def test_metrics_survive_a_one_point_curve():
    m = compute_metrics(_equity([100]), pd.DataFrame())
    assert m.n_trades == 0
    assert m.final_equity == 100.0


def test_sharpe_is_positive_for_a_rising_noisy_curve():
    rng = np.random.default_rng(1)
    values = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.008, 500)))
    m = compute_metrics(_equity(values), pd.DataFrame())
    assert m.sharpe > 0.5
    assert m.volatility > 0


def test_a_positive_risk_free_rate_lowers_sharpe():
    rng = np.random.default_rng(2)
    eq = _equity(100 * np.exp(np.cumsum(rng.normal(0.0006, 0.008, 500))))
    plain = compute_metrics(eq, pd.DataFrame(), risk_free_rate=0.0)
    adjusted = compute_metrics(eq, pd.DataFrame(), risk_free_rate=0.05)
    assert adjusted.sharpe < plain.sharpe


def test_sortino_ignores_upside_volatility():
    rng = np.random.default_rng(3)
    eq = _equity(100 * np.exp(np.cumsum(rng.normal(0.0008, 0.01, 600))))
    m = compute_metrics(eq, pd.DataFrame())
    assert m.sortino > m.sharpe


def test_exposure_and_time_in_market_are_reported():
    eq = _equity(np.linspace(100, 120, 100))
    exposure = pd.Series(np.full(100, 0.4), index=eq.index)
    open_positions = pd.Series([0] * 50 + [2] * 50, index=eq.index)
    m = compute_metrics(eq, pd.DataFrame(), exposure, open_positions)
    assert m.avg_exposure == pytest.approx(0.4)
    assert m.time_in_market == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------
def test_yearly_table_splits_by_calendar_year():
    eq = _equity(np.linspace(100, 200, 600), start="2020-01-01")
    table = yearly_table(eq, _trades([1, 2, 3]))
    assert list(table.index) == [2020, 2021, 2022]
    assert (table["return"] > 0).all()


def test_monthly_returns_pivot_shape():
    eq = _equity(np.linspace(100, 130, 300), start="2021-01-01")
    table = monthly_returns(eq)
    assert table.index.name == "year"
    assert set(table.columns) <= set(range(1, 13))


def test_exit_reason_table_groups_and_sorts():
    trades = _trades([10, -5, 20, -5])
    trades["exit_reason"] = ["stop", "stop", "trailing_stop", "time_stop"]
    table = exit_reason_table(trades)
    assert table.index[0] == "stop"
    assert int(table.loc["stop", "trades"]) == 2


def test_summary_lines_are_all_strings():
    m = compute_metrics(_equity(np.linspace(100, 150, 300)), _trades([10, -5]))
    lines = m.summary_lines()
    assert all(isinstance(line, str) for line in lines)
    assert any("CAGR" in line for line in lines)
