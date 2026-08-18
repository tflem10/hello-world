"""Walk-forward windowing, parameter selection, chaining, and the gate."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from swing.backtest.gate import check_gate, find_reports
from swing.backtest.metrics import compute_metrics
from swing.backtest.report import (
    bootstrap_extras,
    bootstrap_settings,
    build_report,
    report_dir,
)
from swing.backtest.runner import _benchmark_series, load_earnings
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


def test_bootstrap_intervals_are_identical_across_runs():
    """The bootstrap must not be the thing that breaks byte-reproducibility:
    a manifest that changes between two identical runs cannot be diffed."""
    bars = _wf_universe()
    cfg = _bootstrap_config(n_resamples=50, block_days=10)
    a = run_walk_forward(cfg, bars)
    b = run_walk_forward(cfg, bars)

    tables_a, manifest_a, warnings_a = bootstrap_extras(cfg, a.equity)
    tables_b, manifest_b, warnings_b = bootstrap_extras(cfg, b.equity)

    assert manifest_a == manifest_b
    assert warnings_a == warnings_b
    assert list(tables_a) == list(tables_b)
    for name, table in tables_a.items():
        assert table.equals(tables_b[name])
    assert manifest_a["bootstrap"]["n_resamples"] == 50


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


# ---------------------------------------------------------------------------
# bootstrap wiring ([reports.bootstrap], NOT [backtest] — see Config.hash)
# ---------------------------------------------------------------------------
def _bootstrap_config(**bootstrap):
    from swing.config import Config

    data = _wf_config().as_dict()
    if bootstrap:
        data["reports"]["bootstrap"] = {"enabled": True, "seed": 7, **bootstrap}
    return Config(data)


@pytest.mark.parametrize(
    "shipped",
    [
        pytest.param({}, id="stanza_absent"),
        pytest.param(
            {"bootstrap": {"enabled": False, "n_resamples": 3, "block_days": 2,
                           "seed": 99}},
            id="stanza_documented_live",
        ),
    ],
)
def test_bootstrap_settings_fall_back_to_code_defaults(shipped):
    """The fallback is a property of the code, not of today's example config.

    ``[reports.bootstrap]`` may be shipped commented out or live at any time;
    either way, a config that does not carry the section must get exactly the
    code defaults. The parametrisation simulates both shipped states and then
    removes the section, so nothing in ``config.example.toml`` can make this
    test pass or fail.
    """
    from swing.config import Config, load_config

    data = load_config().as_dict()
    data["reports"].update(shipped)
    data["reports"].pop("bootstrap", None)
    cfg = Config(data)

    assert "bootstrap" not in cfg.reports
    assert bootstrap_settings(cfg) == {
        "enabled": True, "n_resamples": 1000, "block_days": 20, "seed": 7,
    }


def test_bootstrap_settings_read_partial_overrides():
    cfg = _bootstrap_config(n_resamples=25)
    assert bootstrap_settings(cfg)["n_resamples"] == 25
    assert bootstrap_settings(cfg)["block_days"] == 20      # still the default


def test_bootstrap_knobs_do_not_change_the_config_hash():
    """Adding a knob under [account][universe][strategy][backtest] would
    silently invalidate every user's existing gate validation."""
    assert _wf_config().hash == _bootstrap_config(n_resamples=25).hash


def test_bootstrap_can_be_switched_off_entirely():
    from swing.config import Config

    data = _wf_config().as_dict()
    data["reports"]["bootstrap"] = {"enabled": False}
    cfg = Config(data)
    equity = pd.Series(
        np.linspace(10_000, 12_000, 400),
        index=pd.bdate_range("2020-01-01", periods=400),
    )
    assert bootstrap_extras(cfg, equity) == ({}, {}, [])


def test_a_short_curve_warns_instead_of_reporting_an_interval():
    cfg = _wf_config()
    equity = pd.Series(
        np.linspace(10_000, 11_000, 30),
        index=pd.bdate_range("2020-01-01", periods=30),
    )
    tables, manifest, warnings = bootstrap_extras(cfg, equity)
    assert tables == {} and manifest == {}
    assert "too small to bootstrap" in warnings[0]


def test_the_bootstrap_table_lands_in_the_report(tmp_path):
    from swing.config import Config

    data = _bootstrap_config(n_resamples=30, block_days=10).as_dict()
    data["reports"]["dir"] = str(tmp_path / "reports")
    cfg = Config(data)

    equity = pd.Series(
        np.linspace(10_000, 13_000, 400),
        index=pd.bdate_range("2020-01-01", periods=400),
    )
    tables, manifest, warnings = bootstrap_extras(cfg, equity)
    report = build_report(
        cfg, "wf", type("R", (), {
            "equity": equity, "trades": pd.DataFrame(), "exposure": None,
            "open_positions": None, "warnings": warnings,
        })(), kind="walk_forward", extra_tables=tables, manifest_extra=manifest,
    )
    out = report.write(report_dir(cfg, "walkforward"))

    assert "bootstrap (n=30, block=10d)" in (out / "report.md").read_text()
    assert (out / "bootstrap_n_30_block_10d.csv").exists()
    written = json.loads((out / "manifest.json").read_text())
    assert written["bootstrap"]["n_resamples"] == 30
    assert "floor on the uncertainty" in " ".join(written["warnings"])


# ---------------------------------------------------------------------------
# benchmark comparison window
# ---------------------------------------------------------------------------
def test_benchmark_covers_exactly_the_reported_window():
    cfg = _wf_config()
    equity = pd.Series(
        np.linspace(10_000, 12_000, 200),
        index=pd.bdate_range("2016-01-01", periods=200),
    )
    bench_bars = make_bars(np.linspace(100, 130, 800), start="2014-01-01")
    series, warnings = _benchmark_series(cfg, bench_bars, equity)
    assert warnings == []
    assert list(series.index) == list(equity.index)
    assert float(series.iloc[0]) == pytest.approx(10_000.0)


def test_a_benchmark_outside_the_window_warns_instead_of_crashing():
    cfg = _wf_config()
    equity = pd.Series(
        np.linspace(10_000, 12_000, 50),
        index=pd.bdate_range("2016-01-01", periods=50),
    )
    late = make_bars(np.linspace(100, 130, 50), start="2024-01-01")
    for benchmark in (None, late, pd.DataFrame()):
        series, warnings = _benchmark_series(cfg, benchmark, equity)
        assert series is None
        assert "no bars in the reported window" in warnings[0]


# ---------------------------------------------------------------------------
# earnings-calendar wiring
# ---------------------------------------------------------------------------
def _install_calendar_module(monkeypatch, calendar):
    """Provide ``swing.data.earnings_calendar`` for the wiring tests.

    The loader itself is owned by another package; this test only cares that
    the runner calls the frozen contract and handles what it returns (or
    raises). If the real module is already in the tree it is patched in place,
    so the test keeps working once that lands.
    """
    import importlib
    import sys
    import types
    from pathlib import Path

    name = "swing.data.earnings_calendar"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError:
        module = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)

    def _load(path):
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return calendar

    monkeypatch.setattr(module, "load_earnings_calendar", _load, raising=False)
    return module


def test_no_earnings_calendar_key_changes_nothing():
    assert load_earnings(_wf_config(), {"S00": None}) == (None, {}, [])


def test_an_empty_earnings_calendar_path_changes_nothing():
    from swing.config import Config

    data = _wf_config().as_dict()
    data["data"]["earnings_calendar"] = "   "
    assert load_earnings(Config(data), {"S00": None}) == (None, {}, [])


def test_a_missing_earnings_calendar_file_exits_naming_the_path(tmp_path, monkeypatch):
    from swing.config import Config

    _install_calendar_module(monkeypatch, {})
    missing = tmp_path / "earnings-2010-2024.csv"
    data = _wf_config().as_dict()
    data["data"]["earnings_calendar"] = str(missing)

    with pytest.raises(SystemExit) as excinfo:
        load_earnings(Config(data), {})
    assert str(missing) in str(excinfo.value)


def test_a_supplied_calendar_is_loaded_and_counted(tmp_path, monkeypatch):
    from swing.config import Config

    calendar = {"S00": [date(2016, 5, 4), date(2016, 8, 3)], "S01": [date(2016, 5, 5)]}
    _install_calendar_module(monkeypatch, calendar)
    path = tmp_path / "earnings.csv"
    path.write_text("symbol,date\n")

    data = _wf_config().as_dict()
    data["data"]["earnings_calendar"] = str(path)
    loaded, manifest, warnings = load_earnings(
        Config(data), {"S00": None, "S01": None}
    )

    assert loaded == calendar
    assert manifest["earnings_calendar"] == str(path)
    assert manifest["earnings_calendar_symbols"] == 2
    assert manifest["earnings_calendar_dates"] == 3
    assert manifest["earnings_calendar_covered"] == 2
    assert manifest["earnings_calendar_universe"] == 2
    assert warnings == []            # full coverage: nothing to flag


def test_earnings_calendar_config_does_not_change_the_config_hash(tmp_path):
    """[data] is outside Config.hash, so wiring a calendar in must not re-lock
    a gate that was validated without one."""
    from swing.config import Config

    data = _wf_config().as_dict()
    before = Config(data).hash
    data["data"]["earnings_calendar"] = str(tmp_path / "earnings.csv")
    assert Config(data).hash == before


def _calendar_config(tmp_path, monkeypatch, calendar):
    """A config pointing at an existing calendar file the loader stub returns."""
    from swing.config import Config

    _install_calendar_module(monkeypatch, calendar)
    path = tmp_path / "earnings.csv"
    path.write_text("symbol,date\n")
    data = _wf_config().as_dict()
    data["data"]["earnings_calendar"] = str(path)
    return Config(data)


def test_partial_calendar_coverage_is_warned_about_with_counts(tmp_path, monkeypatch):
    """A calendar covering 3 of 1000 symbols silences the engine's
    all-or-nothing "no calendar" warning for the whole run, leaving 997 names
    unprotected and a report that reads as if the blackout applied everywhere.
    That gap has to be visible on the report, with the counts."""
    universe = {f"S{i:03d}": None for i in range(1000)}
    calendar = {sym: [date(2016, 5, 4)] for sym in ("S000", "S001", "S002")}
    cfg = _calendar_config(tmp_path, monkeypatch, calendar)

    loaded, manifest, warnings = load_earnings(cfg, universe)

    assert loaded == calendar
    assert manifest["earnings_calendar_covered"] == 3
    assert manifest["earnings_calendar_universe"] == 1000
    assert len(warnings) == 1
    assert "covers 3 of 1000" in warnings[0]
    assert "997" in warnings[0]
    assert "NO earnings blackout" in warnings[0]


def test_a_calendar_symbol_outside_the_universe_does_not_count_as_coverage(
    tmp_path, monkeypatch
):
    """Coverage is the intersection: a 5,000-row S&P export applied to a
    two-symbol ETF universe still protects only the symbols actually traded."""
    calendar = {f"X{i:03d}": [date(2016, 5, 4)] for i in range(50)}
    calendar["S00"] = [date(2016, 5, 4)]
    cfg = _calendar_config(tmp_path, monkeypatch, calendar)

    _, manifest, warnings = load_earnings(cfg, {"S00": None, "S01": None})
    assert manifest["earnings_calendar_symbols"] == 51
    assert manifest["earnings_calendar_covered"] == 1
    assert manifest["earnings_calendar_universe"] == 2
    assert "covers 1 of 2" in warnings[0]


def test_full_coverage_produces_no_coverage_warning(tmp_path, monkeypatch):
    universe = {"S00": None, "S01": None}
    calendar = {sym: [date(2016, 5, 4)] for sym in universe}
    cfg = _calendar_config(tmp_path, monkeypatch, calendar)

    _, manifest, warnings = load_earnings(cfg, universe)
    assert warnings == []
    assert manifest["earnings_calendar_covered"] == manifest[
        "earnings_calendar_universe"
    ] == 2


def test_the_partial_coverage_warning_reaches_the_written_report(
    tmp_path, monkeypatch
):
    """The warning is worthless if it stops at the runner."""
    import argparse

    from swing.backtest import runner as runner_module
    from swing.config import Config

    bars = _wf_universe(n_symbols=4, n_bars=1300)
    benchmark = next(iter(bars.values())).copy()
    covered = sorted(bars)[:1]
    cfg = _calendar_config(
        tmp_path, monkeypatch, {sym: [date(2016, 5, 4)] for sym in covered}
    )
    data = cfg.as_dict()
    data["reports"]["dir"] = str(tmp_path / "reports")
    data["reports"]["bootstrap"] = {"enabled": False}
    cfg = Config(data)

    monkeypatch.setattr(
        runner_module, "load_universe_bars",
        lambda cfg, etf_only=False: (bars, None, benchmark),
    )
    args = argparse.Namespace(
        tag=None, start=None, end=None, full=False, ablations=False,
        sensitivity=False, walk_forward=True, etf_only=False,
    )
    runner_module._run_walk_forward(cfg, args, date(2014, 1, 1), None, etf_only=False)

    out = sorted((tmp_path / "reports").glob("*walkforward*"))[-1]
    manifest = json.loads((out / "manifest.json").read_text())

    assert manifest["earnings_calendar_covered"] == 1
    assert manifest["earnings_calendar_universe"] == len(bars)
    assert any("covers 1 of 4" in w for w in manifest["warnings"])
    assert "covers 1 of 4" in (out / "report.md").read_text()
    # The engine's own warning is gone (a calendar *was* supplied) — which is
    # exactly why the coverage warning has to exist.
    assert not any(
        "no historical earnings calendar" in w for w in manifest["warnings"]
    )


def test_supplying_earnings_silences_the_missing_calendar_warning():
    """The engine warns loudly when the blackout could not be applied. Once a
    calendar is wired through the runner, that warning must disappear —
    otherwise the report keeps disclaiming something it now does."""
    bars = _wf_universe(n_symbols=3, n_bars=1100)
    cfg = _wf_config()
    assert int(cfg.strategy.earnings.blackout_days_before) > 0

    without = run_walk_forward(cfg, bars)
    assert any("no historical earnings calendar" in w for w in without.warnings)

    calendar = {
        sym: [date(2016, 2, 10), date(2016, 5, 11), date(2017, 2, 8)]
        for sym in bars
    }
    with_calendar = run_walk_forward(cfg, bars, earnings=calendar)
    assert not any(
        "no historical earnings calendar" in w for w in with_calendar.warnings
    )
    assert len(with_calendar.segments) == len(without.segments)


# ---------------------------------------------------------------------------
# the assembled runner path
# ---------------------------------------------------------------------------
def test_the_walk_forward_runner_writes_benchmark_bootstrap_and_earnings(
    tmp_path, monkeypatch
):
    """End-to-end over the runner: one report directory that answers 'did this
    beat buy-and-hold?', carries an interval on the headline numbers, and
    records the earnings calendar it applied."""
    import argparse

    from swing.backtest import runner as runner_module
    from swing.config import Config

    bars = _wf_universe(n_symbols=4, n_bars=1300)
    benchmark = next(iter(bars.values())).copy()
    calendar_path = tmp_path / "earnings.csv"
    calendar_path.write_text("symbol,date\n")
    _install_calendar_module(monkeypatch, {sym: [date(2016, 5, 4)] for sym in bars})

    data = _wf_config().as_dict()
    data["reports"]["dir"] = str(tmp_path / "reports")
    data["reports"]["bootstrap"] = {
        "enabled": True, "n_resamples": 40, "block_days": 10, "seed": 7,
    }
    data["data"]["earnings_calendar"] = str(calendar_path)
    cfg = Config(data)

    monkeypatch.setattr(
        runner_module, "load_universe_bars",
        lambda cfg, etf_only=False: (bars, None, benchmark),
    )
    args = argparse.Namespace(
        tag=None, start=None, end=None, full=False, ablations=False,
        sensitivity=False, walk_forward=True, etf_only=False,
    )
    runner_module._run_walk_forward(cfg, args, date(2014, 1, 1), None, etf_only=False)

    out = sorted((tmp_path / "reports").glob("*walkforward*"))[-1]
    manifest = json.loads((out / "manifest.json").read_text())

    assert manifest["bootstrap"]["n_resamples"] == 40
    assert manifest["earnings_calendar"] == str(calendar_path)
    assert manifest["earnings_calendar_symbols"] == len(bars)
    assert manifest["metrics"]["benchmark_return"] is not None
    assert manifest["metrics"]["excess_cagr"] == pytest.approx(
        manifest["metrics"]["cagr"] - manifest["metrics"]["benchmark_cagr"], abs=1e-6
    )
    # A calendar was supplied, so the engine must not still be disclaiming one.
    assert not any(
        "no historical earnings calendar" in w for w in manifest["warnings"]
    )

    text = (out / "report.md").read_text()
    assert "benchmark B&H" in text
    assert "bootstrap (n=40, block=10d)" in text
    assert (out / "bootstrap_n_40_block_10d.csv").exists()
    assert "vs benchmark" in (out / "report.html").read_text()
