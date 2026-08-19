"""AC10 — walk-forward folds, the frozen grid, and the "IS only" guarantee.

The load-bearing claim of this whole project is that the headline number came
from parameters chosen without seeing the data they were measured on. The
central test here (:func:`test_tuner_never_sees_an_out_of_sample_bar`) proves it
mechanically: it hooks every single in-sample evaluation and asserts that not
one bar, not one trade, falls outside the in-sample window.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from conftest import build_config
from swing.backtest.engine import empty_equity
from swing.backtest.metrics import PROFIT_FACTOR_CAP
from swing.backtest.walkforward import (
    MIN_IS_TRADES,
    OBJECTIVE_DESCRIPTION,
    SENSITIVITY_PARAMS,
    TUNING_GRID,
    Window,
    grid_points,
    make_windows,
    objective_key,
    run_walkforward,
    sensitivity_table,
    stitch_returns,
    with_params,
)
from test_backtest_engine import ramp_bars, spike_volume

SMALL_GRID = {"atr_stop_mult": (1.5, 2.5), "donchian_window": (15, 25)}


# ---------------------------------------------------------------------------
# the frozen grid
# ---------------------------------------------------------------------------


def test_tuning_grid_is_exactly_the_frozen_specification():
    """Contract 11 amendment. A grid that quietly grows is a curve fit that hides."""
    assert TUNING_GRID == {
        "atr_stop_mult": (1.5, 2.0, 2.5),
        "chandelier_mult": (2.5, 3.0, 3.5),
        "donchian_window": (15, 20, 25),
        "volume_mult": (1.0, 1.3, 1.6),
    }


def test_grid_has_eighty_one_points_in_a_stable_order():
    points = list(grid_points())
    assert len(points) == 81
    assert points == list(grid_points())  # reproducible ordering
    assert points[0] == {
        "atr_stop_mult": 1.5,
        "chandelier_mult": 2.5,
        "donchian_window": 15,
        "volume_mult": 1.0,
    }
    assert len({tuple(sorted(p.items())) for p in points}) == 81  # no duplicates


def test_with_params_returns_a_new_config_and_leaves_the_original_alone(tmp_path):
    cfg = build_config(tmp_path)
    tuned = with_params(cfg, {"atr_stop_mult": 2.5, "donchian_window": 25})
    assert tuned.strategy.atr_stop_mult == 2.5
    assert tuned.strategy.donchian_window == 25
    assert cfg.strategy.atr_stop_mult == 2.0  # untouched
    # Everything else is carried through.
    assert tuned.account is cfg.account
    assert tuned.paths is cfg.paths


def test_with_params_rejects_an_impossible_value_with_the_config_error(tmp_path):
    cfg = build_config(tmp_path)
    with pytest.raises(ValueError, match="atr_stop_mult"):
        with_params(cfg, {"atr_stop_mult": -1.0})


# ---------------------------------------------------------------------------
# the objective
# ---------------------------------------------------------------------------


def test_objective_prefers_higher_profit_factor():
    weak = objective_key({"profit_factor": 1.2, "trades": 30, "max_drawdown_pct": 10.0})
    strong = objective_key({"profit_factor": 1.9, "trades": 30, "max_drawdown_pct": 10.0})
    assert strong > weak


def test_objective_floors_out_thinly_traded_parameter_sets():
    """A profit factor computed from three trades is a rumour, not a measurement."""
    lucky = objective_key(
        {"profit_factor": 99.0, "trades": MIN_IS_TRADES - 1, "max_drawdown_pct": 1.0}
    )
    honest = objective_key(
        {"profit_factor": 1.1, "trades": MIN_IS_TRADES, "max_drawdown_pct": 20.0}
    )
    assert honest > lucky


def test_objective_tiebreaks_on_trade_count_then_drawdown():
    fewer = objective_key({"profit_factor": 1.5, "trades": 20, "max_drawdown_pct": 10.0})
    more = objective_key({"profit_factor": 1.5, "trades": 40, "max_drawdown_pct": 10.0})
    assert more > fewer

    deep = objective_key({"profit_factor": 1.5, "trades": 20, "max_drawdown_pct": 30.0})
    shallow = objective_key({"profit_factor": 1.5, "trades": 20, "max_drawdown_pct": 5.0})
    assert shallow > deep


def test_objective_is_documented_for_the_report():
    """Contract 11 requires the objective to be written down in the report."""
    assert "profit factor" in OBJECTIVE_DESCRIPTION
    assert str(MIN_IS_TRADES) in OBJECTIVE_DESCRIPTION


# ---------------------------------------------------------------------------
# windows
# ---------------------------------------------------------------------------


def test_windows_are_adjacent_and_never_overlap():
    windows = make_windows(date(2010, 1, 1), date(2020, 12, 31), is_years=3, oos_years=1)
    assert len(windows) == 8

    first = windows[0]
    assert first == Window(
        is_start=date(2010, 1, 1),
        is_end=date(2012, 12, 31),
        oos_start=date(2013, 1, 1),
        oos_end=date(2013, 12, 31),
    )
    for window in windows:
        # In-sample ends the day before out-of-sample begins: disjoint by
        # construction, with no shared bar anywhere.
        assert window.is_end < window.oos_start
        assert (window.oos_start - window.is_end).days == 1
        assert window.oos_end <= date(2020, 12, 31)


def test_windows_step_forward_by_one_out_of_sample_period():
    windows = make_windows(date(2010, 1, 1), date(2020, 12, 31), is_years=3, oos_years=1)
    starts = [w.is_start for w in windows]
    assert starts == [date(year, 1, 1) for year in range(2010, 2018)]
    # Each fold's OOS picks up exactly where the previous one left off.
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert (later.oos_start - earlier.oos_start).days in (365, 366)


def test_a_truncated_final_fold_is_dropped_not_shortened():
    """Half an OOS year next to three IS years would flatter the headline."""
    windows = make_windows(date(2010, 1, 1), date(2013, 6, 30), is_years=3, oos_years=1)
    assert windows == []  # the first OOS year would run to 2013-12-31


def test_too_short_a_span_gives_no_windows_rather_than_an_error():
    assert make_windows(date(2020, 1, 1), date(2020, 6, 30)) == []


def test_leap_day_start_does_not_crash():
    windows = make_windows(date(2016, 2, 29), date(2024, 12, 31), is_years=3, oos_years=1)
    assert windows
    assert windows[0].is_start == date(2016, 2, 29)
    assert windows[0].oos_start == date(2019, 2, 28)  # clamped


def test_window_serialises_to_iso_strings():
    window = Window(date(2020, 1, 1), date(2022, 12, 31), date(2023, 1, 1), date(2023, 12, 31))
    assert window.as_dict() == {
        "is_start": "2020-01-01",
        "is_end": "2022-12-31",
        "oos_start": "2023-01-01",
        "oos_end": "2023-12-31",
    }


def test_window_arguments_must_be_at_least_one_year():
    with pytest.raises(ValueError, match="at least 1"):
        make_windows(date(2010, 1, 1), date(2020, 1, 1), is_years=0, oos_years=1)


# ---------------------------------------------------------------------------
# stitching
# ---------------------------------------------------------------------------


def fold_curve(values, start):
    index = pd.DatetimeIndex(pd.bdate_range(start=start, periods=len(values)), name="date")
    equity = pd.Series([float(v) for v in values], index=index)
    return pd.DataFrame(
        {
            "equity": equity,
            "cash": equity.copy(),
            "n_positions": pd.Series([0] * len(values), index=index, dtype="int64"),
            "drawdown": equity / equity.cummax() - 1.0,
        }
    )


def test_stitching_compounds_returns_instead_of_gluing_dollar_levels():
    # Fold 1: 100k -> 110k (+10%). Fold 2 is simulated from 100k again and
    # makes +20%. Compounded, the account should finish at 100k * 1.1 * 1.2
    # = 132k -- NOT at 120k, which is what naively concatenating levels gives.
    first = fold_curve([100_000.0, 105_000.0, 110_000.0], "2021-01-04")
    second = fold_curve([100_000.0, 110_000.0, 120_000.0], "2022-01-03")

    stitched = stitch_returns([first, second], 100_000.0)
    assert float(stitched["equity"].iloc[0]) == pytest.approx(100_000.0)
    assert float(stitched["equity"].iloc[2]) == pytest.approx(110_000.0)
    assert float(stitched["equity"].iloc[-1]) == pytest.approx(132_000.0)
    assert stitched.index.is_monotonic_increasing


def test_stitched_drawdown_is_measured_against_the_whole_history():
    """A fold's own drawdown column only knows about that fold."""
    first = fold_curve([100_000.0, 120_000.0], "2021-01-04")
    second = fold_curve([100_000.0, 90_000.0], "2022-01-03")
    stitched = stitch_returns([first, second], 100_000.0)
    # Peak is 120k at the end of fold 1; fold 2 loses 10% from there -> 108k.
    assert float(stitched["equity"].iloc[-1]) == pytest.approx(108_000.0)
    assert float(stitched["drawdown"].iloc[-1]) == pytest.approx(-0.10)


def test_stitching_nothing_gives_a_well_formed_empty_curve():
    stitched = stitch_returns([], 100_000.0)
    assert stitched.empty
    assert list(stitched.columns) == list(empty_equity().columns)


# ---------------------------------------------------------------------------
# the real thing, on synthetic data
# ---------------------------------------------------------------------------


def wf_universe(n: int = 1500, spike_every: int = 25) -> dict[str, pd.DataFrame]:
    """Three long ramps with a volume spike every ``spike_every`` bars.

    Regular spikes give a steady trickle of entries in every window, which is
    what makes the fold arithmetic worth checking.
    """
    universe: dict[str, pd.DataFrame] = {}
    for offset, (symbol, growth) in enumerate((("AAA", 0.0015), ("BBB", 0.0020), ("CCC", 0.0025))):
        frame = ramp_bars(n=n, growth=growth)
        for bar in range(300 + offset * 7, n - 1, spike_every):
            frame = spike_volume(frame, bar)
        universe[symbol] = frame
    return universe


def wf_cfg(tmp_path, **overrides):
    sections = {
        "account": {"equity": 200_000.0, "max_position_pct": 20.0},
        "backtest": {"is_years": 1, "oos_years": 1},
    }
    for name, values in overrides.items():
        sections.setdefault(name, {}).update(values)
    return build_config(tmp_path, **sections)


def test_tuner_never_sees_an_out_of_sample_bar(tmp_path):
    """AC10, mechanically: hook every IS evaluation and check its span.

    If a single bar outside the in-sample window ever reached the objective,
    one of these assertions fires and names the fold.
    """
    cfg = wf_cfg(tmp_path)
    bars = wf_universe()
    spy = ramp_bars(n=1500)
    seen: list[tuple] = []

    def spy_hook(window, params, result):
        seen.append((window, tuple(sorted(params.items()))))
        if not result.equity.empty:
            assert result.equity.index[0].date() >= window.is_start, window
            assert result.equity.index[-1].date() <= window.is_end, window
        for _, trade in result.trades.iterrows():
            assert trade["entry_date"].date() >= window.is_start, window
            assert trade["exit_date"].date() <= window.is_end, window
            # And, explicitly: nothing at or after the OOS window start.
            assert trade["exit_date"].date() < window.oos_start, window

    result = run_walkforward(
        bars,
        spy,
        cfg,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
        grid=SMALL_GRID,
        on_is_evaluation=spy_hook,
    )

    assert result.folds, "the synthetic span should produce at least one fold"
    # Every fold evaluated every grid point, and nothing else.
    assert len(seen) == len(result.folds) * len(list(grid_points(SMALL_GRID)))


def test_out_of_sample_results_stay_inside_their_windows(tmp_path):
    cfg = wf_cfg(tmp_path)
    result = run_walkforward(
        wf_universe(),
        ramp_bars(n=1500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
        grid=SMALL_GRID,
    )
    assert result.folds
    for fold in result.folds:
        if fold.oos_equity.empty:
            continue
        assert fold.oos_equity.index[0].date() >= fold.window.oos_start
        assert fold.oos_equity.index[-1].date() <= fold.window.oos_end


def test_chosen_parameters_come_from_the_grid(tmp_path):
    cfg = wf_cfg(tmp_path)
    result = run_walkforward(
        wf_universe(),
        ramp_bars(n=1500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
        grid=SMALL_GRID,
    )
    for fold in result.folds:
        assert set(fold.params) == set(SMALL_GRID)
        for name, value in fold.params.items():
            assert value in SMALL_GRID[name]


def test_concatenated_out_of_sample_record_is_the_headline(tmp_path):
    """The stitched OOS equity and trades are the sum of the folds, in order."""
    cfg = wf_cfg(tmp_path)
    result = run_walkforward(
        wf_universe(),
        ramp_bars(n=1500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
        grid=SMALL_GRID,
    )
    assert result.folds

    expected_trades = sum(len(fold.oos_trades) for fold in result.folds)
    assert len(result.trades) == expected_trades
    assert result.metrics["trades"] == expected_trades
    # Chronological, no duplicated dates across the seams.
    assert result.equity.index.is_monotonic_increasing
    assert not result.equity.index.has_duplicates
    assert result.trades["exit_date"].is_monotonic_increasing


def test_folds_serialise_for_the_report(tmp_path):
    cfg = wf_cfg(tmp_path)
    result = run_walkforward(
        wf_universe(),
        ramp_bars(n=1500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
        grid=SMALL_GRID,
    )
    payload = result.folds[0].as_dict()
    for key in ("is_start", "is_end", "oos_start", "oos_end", "params", "oos_trades"):
        assert key in payload
    assert isinstance(payload["params"], dict)


def test_no_complete_fold_returns_an_empty_but_well_formed_result(tmp_path):
    cfg = wf_cfg(tmp_path, backtest={"is_years": 3, "oos_years": 1})
    result = run_walkforward(
        wf_universe(n=400),
        ramp_bars(),
        cfg,
        start=date(2021, 1, 1),
        end=date(2021, 6, 30),
        grid=SMALL_GRID,
    )
    assert result.folds == []
    assert result.trades.empty
    assert result.equity.empty
    assert result.metrics["trades"] == 0
    assert result.metrics["profit_factor"] == 0.0


def test_walkforward_is_deterministic(tmp_path):
    cfg = wf_cfg(tmp_path)
    bars = wf_universe()
    spy = ramp_bars(n=1500)
    kwargs = {
        "start": date(2021, 6, 1),
        "end": date(2025, 5, 31),
        "grid": SMALL_GRID,
    }
    first = run_walkforward(bars, spy, cfg, **kwargs)
    second = run_walkforward(bars, spy, cfg, **kwargs)
    assert [f.params for f in first.folds] == [f.params for f in second.folds]
    pd.testing.assert_frame_equal(first.trades, second.trades)
    pd.testing.assert_frame_equal(first.equity, second.equity)


# ---------------------------------------------------------------------------
# sensitivity
# ---------------------------------------------------------------------------


def test_sensitivity_covers_the_five_named_parameters_both_ways(tmp_path):
    cfg = wf_cfg(tmp_path)
    rows = sensitivity_table(
        wf_universe(n=500),
        ramp_bars(n=500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2021, 12, 31),
    )
    assert rows[0]["param"] == "baseline"
    perturbed = {(row["param"], row["variant"]) for row in rows[1:]}
    for param in SENSITIVITY_PARAMS:
        assert (param, "-25%") in perturbed, param
        assert (param, "+25%") in perturbed, param


def test_sensitivity_values_are_twenty_five_percent_either_side(tmp_path):
    cfg = wf_cfg(tmp_path)
    rows = sensitivity_table(
        wf_universe(n=500),
        ramp_bars(n=500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2021, 12, 31),
        params=("atr_stop_mult", "donchian_window"),
    )
    values = {(row["param"], row["variant"]): row["value"] for row in rows if row["value"]}
    # 2.0 -> 1.5 / 2.5, and the integer window 20 -> 15 / 25.
    assert values[("atr_stop_mult", "-25%")] == pytest.approx(1.5)
    assert values[("atr_stop_mult", "+25%")] == pytest.approx(2.5)
    assert values[("donchian_window", "-25%")] == 15
    assert values[("donchian_window", "+25%")] == 25


def test_sensitivity_rows_carry_the_metrics_the_report_prints(tmp_path):
    cfg = wf_cfg(tmp_path)
    rows = sensitivity_table(
        wf_universe(n=500),
        ramp_bars(n=500),
        cfg,
        start=date(2021, 6, 1),
        end=date(2021, 12, 31),
        params=("atr_stop_mult",),
    )
    for row in rows:
        for key in (
            "param",
            "variant",
            "value",
            "profit_factor",
            "cagr",
            "max_drawdown_pct",
            "trades",
        ):
            assert key in row


# ---------------------------------------------------------------------------
# BUG-041 — the tuner must not maximise the no-losses sentinel
# ---------------------------------------------------------------------------


def capped(trades: int, max_dd: float = 1.0) -> dict[str, object]:
    """The metrics a parameter set with zero losing trades produces."""
    return {
        "profit_factor": PROFIT_FACTOR_CAP,
        "profit_factor_capped": True,
        "trades": trades,
        "max_drawdown_pct": max_dd,
    }


def test_a_capped_profit_factor_loses_to_a_measured_one():
    """BUG-041: eight lucky zero-loss trades outranked a genuinely measured edge.

    9999.0 is a sentinel standing in for infinity, not a measurement, and the
    parameter set that produced it went on to drive a whole out-of-sample year.
    """
    lucky = objective_key(capped(MIN_IS_TRADES))
    measured = objective_key(
        {
            "profit_factor": 1.5,
            "profit_factor_capped": False,
            "trades": MIN_IS_TRADES,
            "max_drawdown_pct": 20.0,
        }
    )
    assert measured > lucky


def test_a_capped_set_still_beats_one_that_never_cleared_the_trade_floor():
    """Ranking the sentinel at 0.0 must not push it below an unevidenced set."""
    thin = objective_key(
        {
            "profit_factor": 4.0,
            "profit_factor_capped": False,
            "trades": MIN_IS_TRADES - 1,
            "max_drawdown_pct": 1.0,
        }
    )
    assert objective_key(capped(MIN_IS_TRADES)) > thin


def test_between_two_capped_sets_the_better_evidenced_one_wins():
    """With profit factor neutralised the fallback is trades, then drawdown."""
    assert objective_key(capped(300)) > objective_key(capped(MIN_IS_TRADES))
    assert objective_key(capped(50, max_dd=5.0)) > objective_key(capped(50, max_dd=30.0))


def test_a_capped_set_is_still_chosen_when_it_is_the_only_option():
    """Neutralising the sentinel must not make the tuner refuse to choose."""
    only = objective_key(capped(MIN_IS_TRADES))
    assert only > objective_key({"profit_factor": 0.0, "trades": 0, "max_drawdown_pct": 100.0})


def test_the_objective_documents_the_sentinel_rule():
    """Contract 11 requires the objective to be written down in the report."""
    assert "9999" in OBJECTIVE_DESCRIPTION
    assert "no losing trades" in OBJECTIVE_DESCRIPTION
