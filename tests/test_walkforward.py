"""Walk-forward windowing, parameter selection, chaining, and the gate."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from swing.backtest.gate import check_gate, find_reports
from swing.backtest.metrics import compute_metrics
from swing.backtest.report import build_report, report_dir
from swing.backtest.walkforward import (
    apply_overrides,
    grid_points,
    make_windows,
    objective_value,
    run_walk_forward,
)

from .conftest import engine_config, make_bars


# ---------------------------------------------------------------------------
# windowing
# ---------------------------------------------------------------------------
def test_windows_are_contiguous_and_non_overlapping_out_of_sample():
    windows = make_windows(date(2010, 1, 1), date(2020, 12, 31), 3, 1, 1)
    assert len(windows) == 8
    for w in windows:
        assert w.is_start < w.is_end < w.oos_start <= w.oos_end
    for a, b in zip(windows, windows[1:], strict=False):
        assert b.oos_start > a.oos_start
        # No out-of-sample year is reused.
        assert b.oos_start > a.oos_end


def test_in_sample_never_overlaps_its_own_out_of_sample():
    for w in make_windows(date(2010, 1, 1), date(2022, 1, 1), 3, 1, 1):
        assert w.is_end < w.oos_start


def test_too_little_history_yields_no_windows():
    assert make_windows(date(2020, 1, 1), date(2021, 1, 1), 3, 1, 1) == []


def test_leap_day_start_does_not_explode():
    windows = make_windows(date(2016, 2, 29), date(2024, 1, 1), 3, 1, 1)
    assert windows


# ---------------------------------------------------------------------------
# grid / objective
# ---------------------------------------------------------------------------
def test_grid_points_is_the_cartesian_product():
    grid = {"a": [1, 2], "b": [10, 20, 30]}
    points = grid_points(grid)
    assert len(points) == 6
    assert {tuple(sorted(p.items())) for p in points} == {
        (("a", a), ("b", b)) for a in (1, 2) for b in (10, 20, 30)
    }


def test_empty_grid_gives_one_no_op_point():
    assert grid_points({}) == [{}]


def test_apply_overrides_sets_nested_paths_without_mutating_the_original():
    cfg = engine_config()
    before = cfg.strategy.exit.initial_stop_atr
    changed = apply_overrides(cfg, {"strategy.exit.initial_stop_atr": 3.5})
    assert changed.strategy.exit.initial_stop_atr == 3.5
    assert cfg.strategy.exit.initial_stop_atr == before


def test_infinite_profit_factor_does_not_win_the_grid():
    """No losing trades means too small a sample, not the best parameters."""
    class _M:
        profit_factor = float("inf")
        sharpe = 1.0
        calmar = 1.0
        expectancy_r = 1.0
        cagr = 1.0
        total_return = 1.0

    assert objective_value(_M(), "profit_factor") == 0.0


def test_unknown_objective_is_rejected():
    class _M:
        profit_factor = 1.0
        sharpe = 1.0
        calmar = 1.0
        expectancy_r = 1.0
        cagr = 1.0
        total_return = 1.0

    with pytest.raises(ValueError, match="objective"):
        objective_value(_M(), "vibes")


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------
def _wf_universe(n_symbols: int = 5, n_bars: int = 1600, seed: int = 8):
    rng = np.random.default_rng(seed)
    bars = {}
    for i in range(n_symbols):
        closes = 40.0 * np.exp(
            np.cumsum(np.full(n_bars, 0.0004) + rng.normal(0, 0.017, n_bars))
        )
        bars[f"S{i:02d}"] = make_bars(
            closes, start="2014-01-01", volume=rng.uniform(2e6, 9e6, n_bars)
        )
    return bars


def _wf_config(**kw):
    cfg = engine_config(**kw)
    data = cfg.as_dict()
    data["backtest"]["start"] = "2014-01-01"
    data["backtest"]["end"] = ""
    data["backtest"]["walk_forward"].update(
        in_sample_years=2,
        out_of_sample_years=1,
        step_years=1,
        optimize=True,
        min_is_trades=0,
        grid={"strategy.exit.initial_stop_atr": [1.5, 2.5]},
    )
    from swing.config import Config

    return Config(data)


def test_walk_forward_produces_one_segment_per_window():
    bars = _wf_universe()
    result = run_walk_forward(_wf_config(), bars)
    assert len(result.segments) == len(result.windows) == len(result.chosen_params)
    assert len(result.oos_metrics) == len(result.windows)


def test_walk_forward_only_reports_out_of_sample_bars():
    bars = _wf_universe()
    cfg = _wf_config()
    result = run_walk_forward(cfg, bars)
    first_oos_start = result.windows[0].oos_start
    assert result.equity.index[0].date() >= first_oos_start
    # No in-sample bar leaks into the headline curve.
    assert (result.equity.index.date >= first_oos_start).all()


def test_walk_forward_chains_equity_across_segments():
    bars = _wf_universe()
    result = run_walk_forward(_wf_config(), bars)
    for a, b in zip(result.segments, result.segments[1:], strict=False):
        # Each segment starts where the previous one finished (whole-share
        # effects therefore compound realistically).
        assert float(b.equity.iloc[0]) == pytest.approx(
            float(a.equity.iloc[-1]), rel=0.15
        )


def test_walk_forward_picks_parameters_from_the_configured_grid():
    bars = _wf_universe()
    result = run_walk_forward(_wf_config(), bars)
    for params in result.chosen_params:
        assert set(params) <= {"strategy.exit.initial_stop_atr"}
        if params:
            assert params["strategy.exit.initial_stop_atr"] in (1.5, 2.5)


def test_disabling_optimisation_uses_config_defaults_everywhere():
    bars = _wf_universe()
    cfg = _wf_config()
    data = cfg.as_dict()
    data["backtest"]["walk_forward"]["optimize"] = False
    from swing.config import Config

    result = run_walk_forward(Config(data), bars)
    assert all(params == {} for params in result.chosen_params)


def test_walk_forward_is_deterministic():
    bars = _wf_universe()
    cfg = _wf_config()
    a = run_walk_forward(cfg, bars)
    b = run_walk_forward(cfg, bars)
    assert a.equity.to_csv() == b.equity.to_csv()
    assert a.chosen_params == b.chosen_params


def test_walk_forward_needs_enough_history():
    bars = _wf_universe(n_bars=300)
    cfg = _wf_config()
    data = cfg.as_dict()
    data["backtest"]["walk_forward"]["in_sample_years"] = 5
    from swing.config import Config

    with pytest.raises(ValueError, match="not enough history"):
        run_walk_forward(Config(data), bars)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------
def _gate_config(tmp_path, **gate_overrides):
    cfg = engine_config()
    data = cfg.as_dict()
    data["reports"]["dir"] = str(tmp_path / "reports")
    data["backtest"]["gate"].update(
        enabled=True, min_profit_factor=1.3, max_drawdown_pct=0.35,
        min_trades=30, min_sharpe=0.4,
    )
    data["backtest"]["gate"].update(gate_overrides)
    from swing.config import Config

    return Config(data)


def _write_fake_report(cfg, metrics_overrides: dict, config_hash: str | None = None):
    """Write a minimal walk-forward manifest the gate can read."""
    import pandas as pd

    equity = pd.Series(
        np.linspace(10_000, 12_000, 300),
        index=pd.bdate_range("2020-01-01", periods=300),
    )
    trades = pd.DataFrame(
        {
            "symbol": ["A"] * 40, "entry_date": equity.index[:40],
            "exit_date": equity.index[1:41], "entry_price": [10.0] * 40,
            "exit_price": [11.0] * 40, "shares": [10] * 40,
            "initial_stop": [9.0] * 40, "exit_reason": ["stop"] * 40,
            "final_stop": [9.0] * 40, "pnl": [10.0] * 40,
            "return_pct": [0.1] * 40, "r_multiple": [1.0] * 40,
            "hold_days": [5] * 40, "mae": [-0.02] * 40, "mfe": [0.12] * 40,
            "rank_score": [1.0] * 40, "atr_at_entry": [0.5] * 40,
            "risk_dollars": [10.0] * 40,
        }
    )
    metrics = compute_metrics(equity, trades)
    for key, value in metrics_overrides.items():
        setattr(metrics, key, value)

    report = build_report(cfg, "fake walk-forward", type("R", (), {
        "equity": equity, "trades": trades, "exposure": None,
        "open_positions": None, "warnings": [], "metrics": metrics,
    })(), kind="walk_forward")
    out = report.write(report_dir(cfg, "walkforward"))
    if config_hash is not None:
        manifest = json.loads((out / "manifest.json").read_text())
        manifest["config_hash"] = config_hash
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return out


def test_gate_blocks_when_no_report_exists(tmp_path):
    status = check_gate(_gate_config(tmp_path))
    assert not status.passed
    assert "no walk-forward report" in status.reasons[0]


def test_gate_passes_when_metrics_clear_the_thresholds(tmp_path):
    cfg = _gate_config(tmp_path)
    _write_fake_report(cfg, {"profit_factor": 1.8, "max_drawdown": 0.20,
                             "n_trades": 60, "sharpe": 0.9})
    status = check_gate(cfg)
    assert status.passed, status.describe()
    assert status.report_path is not None


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"profit_factor": 1.0}, "profit factor"),
        ({"max_drawdown": 0.60}, "drawdown"),
        ({"n_trades": 5}, "trades"),
        ({"sharpe": 0.1}, "Sharpe"),
    ],
)
def test_gate_blocks_on_each_individual_threshold(tmp_path, overrides, expected):
    cfg = _gate_config(tmp_path)
    base = {"profit_factor": 1.8, "max_drawdown": 0.20, "n_trades": 60, "sharpe": 0.9}
    base.update(overrides)
    _write_fake_report(cfg, base)
    status = check_gate(cfg)
    assert not status.passed
    assert any(expected in r for r in status.reasons), status.describe()


def test_gate_blocks_when_the_config_changed_since_validation(tmp_path):
    """Editing a strategy parameter must re-lock the gate."""
    cfg = _gate_config(tmp_path)
    _write_fake_report(
        cfg,
        {"profit_factor": 1.8, "max_drawdown": 0.20, "n_trades": 60, "sharpe": 0.9},
        config_hash="deadbeefcafe",
    )
    status = check_gate(cfg)
    assert not status.passed
    assert "hash" in status.reasons[0]


def test_an_infinite_profit_factor_does_not_pass_the_gate(tmp_path):
    cfg = _gate_config(tmp_path)
    _write_fake_report(cfg, {"profit_factor": float("inf"), "max_drawdown": 0.10,
                             "n_trades": 60, "sharpe": 0.9})
    status = check_gate(cfg)
    assert not status.passed


def test_gate_can_be_disabled_explicitly(tmp_path):
    cfg = _gate_config(tmp_path, enabled=False)
    status = check_gate(cfg)
    assert status.passed
    assert "disabled" in status.reasons[0]


def test_gate_ignores_non_walk_forward_reports(tmp_path):
    cfg = _gate_config(tmp_path)
    import pandas as pd

    equity = pd.Series([10_000.0, 11_000.0],
                       index=pd.bdate_range("2020-01-01", periods=2))
    report = build_report(cfg, "full period", type("R", (), {
        "equity": equity, "trades": pd.DataFrame(), "exposure": None,
        "open_positions": None, "warnings": [],
    })(), kind="full_period")
    report.write(report_dir(cfg, "fullperiod"))

    assert len(find_reports(cfg)) == 1
    assert find_reports(cfg, kind="walk_forward") == []
    assert not check_gate(cfg).passed


def test_gate_status_describe_is_readable(tmp_path):
    cfg = _gate_config(tmp_path)
    _write_fake_report(cfg, {"profit_factor": 1.8, "max_drawdown": 0.20,
                             "n_trades": 60, "sharpe": 0.9})
    text = check_gate(cfg).describe()
    assert "PASS" in text and "profit_factor" in text
