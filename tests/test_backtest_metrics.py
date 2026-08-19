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
    # Expectation corrected for audit BUG-052: the code stopped at the last
    # underwater bar while this comment and the docstring both said "to
    # recovery". All three now agree on recovery.
    # Peak on day 0 (Wed 2020-01-01), under water on days 1-3, back to the peak
    # on day 4. Business days: Jan 1, 2, 3, 6, 7 -> the drawdown ends on Jan 7,
    # the bar that regains 100.0, which is 6 calendar days after the Jan 1 peak.
    equity = pd.Series(
        [100.0, 90.0, 80.0, 95.0, 100.0],
        index=pd.bdate_range("2020-01-01", periods=5),
    )
    _depth, duration = max_drawdown(equity)
    assert duration == 6


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


# ---------------------------------------------------------------------------
# BUG-052 — the duration measures to the recovery bar
# ---------------------------------------------------------------------------


def reference_duration(equity: pd.Series) -> int:
    """The to-recovery duration, written out longhand.

    The shipped implementation is vectorised for speed (audit PERF-004); this
    is the obvious loop it has to agree with, kept here so the optimisation can
    never quietly drift from the definition.
    """
    longest = 0
    peak_date = equity.index[0]
    peak_value = float(equity.iloc[0])
    underwater = False
    for stamp, value in equity.items():
        value = float(value)
        if value >= peak_value:
            if underwater:
                longest = max(longest, int((stamp - peak_date).days))
                underwater = False
            peak_value, peak_date = value, stamp
        else:
            underwater = True
            longest = max(longest, int((stamp - peak_date).days))
    return longest


def test_the_duration_includes_the_bar_that_regains_the_high_water_mark():
    """BUG-052: the code stopped one bar early, at the last bar still under water."""
    equity = pd.Series(
        [100.0, 90.0, 100.0],
        index=pd.DatetimeIndex(["2020-01-01", "2020-01-10", "2020-01-31"]),
    )
    _depth, duration = max_drawdown(equity)
    # Jan 1 peak -> Jan 31 recovery is 30 days. Stopping at the last underwater
    # bar (Jan 10) reported 9 and called the drawdown three times shorter.
    assert duration == 30


def test_a_rising_curve_has_no_underwater_days():
    rising = pd.Series(
        [100.0, 101.0, 102.0, 103.0],
        index=pd.bdate_range("2020-01-01", periods=4),
    )
    depth, duration = max_drawdown(rising)
    assert depth == 0.0
    assert duration == 0


@pytest.mark.parametrize(
    "values",
    [
        [100.0, 90.0, 80.0, 95.0, 100.0],
        [100.0, 90.0, 90.0, 90.0],
        [100.0] * 5,
        [100.0, 101.0, 102.0],
        [100.0, 120.0, 90.0, 130.0, 120.0, 140.0],
        [100.0, 99.0, 100.0, 98.0, 97.0, 105.0, 104.0],
        [50.0],
    ],
)
def test_the_vectorised_duration_matches_the_longhand_one(values):
    """PERF-004: the vectorisation must be a pure speed change."""
    equity = pd.Series(values, index=pd.bdate_range("2020-01-01", periods=len(values)))
    _depth, duration = max_drawdown(equity)
    assert duration == reference_duration(equity)


def test_the_vectorised_duration_matches_on_a_long_noisy_curve():
    from conftest import make_bars

    curve = make_bars(400, trend=0.0004)["close"]
    _depth, duration = max_drawdown(curve)
    assert duration == reference_duration(curve)
    assert duration > 0


# ---------------------------------------------------------------------------
# BUG-040 — each year's drawdown must include its own first bar
# ---------------------------------------------------------------------------


def test_a_year_that_opens_down_twenty_percent_reports_that_drawdown():
    """BUG-040 repro: ``cumprod`` seeded the peak AFTER the year's first move.

    A year that opened -20% and clawed all of it back reported ``max_dd 0.0``
    — a table whose entire job is answering "was the bad year survivable?"
    saying the worst year of the run was perfectly calm.
    """
    index = pd.DatetimeIndex(["2020-12-31", "2021-01-04", "2021-06-01", "2021-12-31"], name="date")
    equity = pd.DataFrame(
        {
            "equity": [100.0, 80.0, 90.0, 100.0],
            "cash": [100.0, 80.0, 90.0, 100.0],
            "n_positions": [0, 0, 0, 0],
            "drawdown": [0.0, 0.0, 0.0, 0.0],
        },
        index=index,
    )
    table = by_year_table(empty_trades(), equity)

    assert table["2021"]["max_dd_pct"] == pytest.approx(20.0)
    # ...and the year really did end flat, which is why the old zero looked plausible.
    assert table["2021"]["return_pct"] == pytest.approx(0.0, abs=1e-9)


def test_seeding_the_year_curve_does_not_invent_a_drawdown():
    """A year that only ever rises must still report zero."""
    index = pd.DatetimeIndex(["2020-12-31", "2021-03-01", "2021-12-31"], name="date")
    equity = pd.DataFrame(
        {
            "equity": [100.0, 110.0, 130.0],
            "cash": [100.0, 110.0, 130.0],
            "n_positions": [0, 0, 0],
            "drawdown": [0.0, 0.0, 0.0],
        },
        index=index,
    )
    assert by_year_table(empty_trades(), equity)["2021"]["max_dd_pct"] == 0.0
