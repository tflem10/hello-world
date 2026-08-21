"""Metric definitions, checked against hand-computable series."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing.backtest.metrics import (
    TRADING_DAYS,
    annualised_return,
    apply_benchmark,
    benchmark_equity,
    compute_metrics,
    drawdown_series,
    drawdown_stats,
    exit_reason_table,
    max_drawdown,
    monthly_returns,
    trade_stats,
    yearly_table,
)

from .conftest import engine_config, make_bars


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


def test_the_array_max_drawdown_agrees_with_drawdown_stats_exactly():
    """The bootstrap calls the array version 1,000 times per report, so the two
    must not be allowed to drift into two definitions of drawdown."""
    rng = np.random.default_rng(0)
    for _ in range(50):
        n = int(rng.integers(2, 400))
        path = 100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.02, n))
        assert max_drawdown(path) == drawdown_stats(_equity(path))[0]

    assert max_drawdown(np.array([100.0, 200.0, 150.0, 180.0, 250.0])) == pytest.approx(
        0.25
    )
    assert max_drawdown(np.array([100.0, 110.0])) == 0.0
    assert max_drawdown(np.array([100.0])) == 0.0        # nothing to fall from


def test_the_array_max_drawdown_skips_gaps_instead_of_reporting_zero():
    """A day without a mark is not a day the strategy fell to nothing.

    A running peak from ``np.maximum.accumulate`` poisons every point after a
    NaN and then reports a confident 0.0 — the one answer a drawdown must never
    be wrong about, because 0.0 reads as "never lost anything".
    """
    gapped = np.array([100.0, 110.0, np.nan, 90.0, 120.0])
    assert max_drawdown(gapped) == pytest.approx(1.0 - 90.0 / 110.0)     # 0.1818
    assert max_drawdown(gapped) == drawdown_stats(_equity(gapped))[0]

    rng = np.random.default_rng(1)
    for _ in range(50):
        n = int(rng.integers(2, 300))
        path = 100.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.02, n))
        path[rng.integers(0, n, size=int(rng.integers(1, max(2, n // 5))))] = np.nan
        assert max_drawdown(path) == drawdown_stats(_equity(path))[0]

    assert max_drawdown(np.array([np.nan, np.nan])) == 0.0
    assert max_drawdown(np.array([np.nan, 100.0, 80.0])) == pytest.approx(0.2)


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


# ---------------------------------------------------------------------------
# benchmark comparison
# ---------------------------------------------------------------------------
def _bench_bars(closes, start="2020-01-01"):
    return make_bars(closes, start=start)


def test_benchmark_equity_starts_at_the_strategy_capital():
    """Both curves must start from the same dollar on the same day, or the
    comparison is between two different accounts."""
    eq = _equity(np.linspace(10_000, 12_000, 50))
    bench = benchmark_equity(_bench_bars(np.linspace(100, 150, 50)), eq.index, 10_000.0)
    assert len(bench) == len(eq)
    assert float(bench.iloc[0]) == pytest.approx(10_000.0)
    assert list(bench.index) == list(eq.index)


def test_a_benchmark_that_doubles_reports_a_100_percent_return():
    """Hand-computable: close 100 -> 200 over exactly one trading year."""
    eq = _equity(np.linspace(10_000, 10_000, TRADING_DAYS))
    bench_bars = _bench_bars(np.linspace(100, 200, TRADING_DAYS))
    bench = benchmark_equity(bench_bars, eq.index, 10_000.0)

    m = apply_benchmark(compute_metrics(eq, pd.DataFrame()), bench)
    assert m.benchmark_return == pytest.approx(1.0)
    assert m.benchmark_cagr == pytest.approx(1.0, rel=0.02)
    assert float(bench.iloc[-1]) == pytest.approx(20_000.0)


def test_excess_cagr_is_strategy_minus_benchmark():
    eq = _equity(np.linspace(10_000, 15_000, TRADING_DAYS))
    bench = benchmark_equity(
        _bench_bars(np.linspace(100, 200, TRADING_DAYS)), eq.index, 10_000.0
    )
    m = apply_benchmark(compute_metrics(eq, pd.DataFrame()), bench)
    assert m.excess_cagr == pytest.approx(m.cagr - m.benchmark_cagr)
    assert m.excess_cagr < 0            # 50% behind a doubling benchmark


def test_benchmark_drawdown_is_measured_on_the_benchmark_not_the_strategy():
    eq = _equity(np.linspace(10_000, 11_000, 5))
    bench = benchmark_equity(_bench_bars([100, 200, 150, 180, 250]), eq.index, 10_000.0)
    m = apply_benchmark(compute_metrics(eq, pd.DataFrame()), bench)
    assert m.benchmark_max_drawdown == pytest.approx(0.25)
    assert m.max_drawdown == 0.0


def test_a_benchmark_with_gaps_is_forward_filled_onto_the_strategy_index():
    """The benchmark not trading on a day the strategy did must not punch a
    hole in the comparison; the last known close carries forward."""
    eq = _equity([10_000.0] * 10)
    sparse = _bench_bars(np.linspace(100, 190, 10)).iloc[[0, 3, 9]]
    bench = benchmark_equity(sparse, eq.index, 10_000.0)
    assert len(bench) == len(eq)
    assert not bench.isna().any()
    # Days 1 and 2 hold day 0's value.
    assert float(bench.iloc[1]) == pytest.approx(float(bench.iloc[0]))
    assert float(bench.iloc[4]) == pytest.approx(float(bench.iloc[3]))


def test_a_benchmark_starting_late_only_covers_the_days_it_has():
    eq = _equity([10_000.0] * 20)
    late = _bench_bars(np.linspace(100, 120, 20)).iloc[10:]
    bench = benchmark_equity(late, eq.index, 10_000.0)
    assert len(bench) == 10
    assert float(bench.iloc[0]) == pytest.approx(10_000.0)


def test_a_benchmark_with_no_bars_in_window_yields_an_empty_series():
    eq = _equity([10_000.0] * 20, start="2020-01-01")
    elsewhere = _bench_bars(np.linspace(100, 120, 20), start="2024-01-01")
    assert len(benchmark_equity(elsewhere, eq.index, 10_000.0)) == 0
    assert len(benchmark_equity(None, eq.index, 10_000.0)) == 0
    assert len(benchmark_equity(pd.DataFrame(), eq.index, 10_000.0)) == 0


def test_benchmark_fields_are_none_without_a_benchmark():
    m = compute_metrics(_equity(np.linspace(100, 120, 50)), pd.DataFrame())
    assert m.benchmark_return is None
    assert m.benchmark_cagr is None
    assert m.benchmark_max_drawdown is None
    assert m.excess_cagr is None
    assert apply_benchmark(m, None).excess_cagr is None
    assert apply_benchmark(m, pd.Series(dtype="float64")).excess_cagr is None


def test_summary_lines_omit_the_benchmark_until_it_is_populated():
    eq = _equity(np.linspace(100, 150, 300))
    m = compute_metrics(eq, _trades([10, -5]))
    assert not any("benchmark" in line for line in m.summary_lines())

    apply_benchmark(m, benchmark_equity(_bench_bars(np.linspace(50, 60, 300)), eq.index, 100.0))
    lines = m.summary_lines()
    assert any("benchmark B&H" in line for line in lines)
    assert any("excess CAGR" in line for line in lines)


def test_annualised_return_matches_compute_metrics():
    eq = _equity(np.linspace(100, 200, TRADING_DAYS))
    m = compute_metrics(eq, pd.DataFrame())
    assert annualised_return(100.0, 200.0, TRADING_DAYS) == pytest.approx(m.cagr)


# ---------------------------------------------------------------------------
# the report survives both cases
# ---------------------------------------------------------------------------
def _report_config(tmp_path):
    from swing.config import Config

    data = engine_config().as_dict()
    data["reports"]["dir"] = str(tmp_path / "reports")
    return Config(data)


def _fake_result(equity, trades):
    return type("R", (), {
        "equity": equity, "trades": trades, "exposure": None,
        "open_positions": None, "warnings": [],
    })()


def test_metrics_json_round_trips_the_absent_benchmark_as_null(tmp_path):
    """``None`` benchmark fields must serialise as JSON null, not crash and not
    silently become 0.0 — a zero would read as 'buy-and-hold went nowhere'."""
    import json

    from swing.backtest.report import build_report, report_dir

    cfg = _report_config(tmp_path)
    eq = _equity(np.linspace(10_000, 12_000, 120))
    report = build_report(cfg, "plain run", _fake_result(eq, _trades([10, -5])))
    out = report.write(report_dir(cfg, "nobench"))

    payload = json.loads((out / "metrics.json").read_text())
    for key in ("benchmark_return", "benchmark_cagr", "benchmark_max_drawdown",
                "excess_cagr"):
        assert key in payload
        assert payload[key] is None
    assert "benchmark B&H" not in (out / "report.md").read_text()
    assert "vs benchmark" not in (out / "report.html").read_text()
    assert (out / "report.html").exists()


def test_the_report_overlays_a_benchmark_when_one_is_supplied(tmp_path):
    import json

    from swing.backtest.report import build_report, report_dir

    cfg = _report_config(tmp_path)
    eq = _equity(np.linspace(10_000, 12_000, 120))
    bench = benchmark_equity(_bench_bars(np.linspace(100, 105, 120)), eq.index, 10_000.0)
    result = _fake_result(eq, _trades([10, -5]))
    report = build_report(cfg, "with benchmark", result, benchmark=bench)
    apply_benchmark(report.metrics, bench)
    out = report.write(report_dir(cfg, "withbench"))

    text = (out / "report.md").read_text()
    assert "benchmark B&H" in text
    assert "excess CAGR" in text
    assert "vs benchmark" in (out / "report.html").read_text()
    payload = json.loads((out / "metrics.json").read_text())
    assert payload["benchmark_return"] == pytest.approx(0.05, rel=1e-3)
