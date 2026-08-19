"""The trading gate — every failure path, and the one way through.

The gate is the mechanism that stops this system from trading a strategy that
has not proved itself, so each of its refusals gets its own test. A gate that
fails open is worse than no gate, because it looks like one.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from conftest import build_config
from swing.backtest.gate import (
    ABLATION_PREFIX,
    GateResult,
    backtest_dir,
    check,
    latest_path,
    load_latest,
    read_latest,
)


def passing_summary(**overrides):
    """A summary.json payload that clears the default gates comfortably."""
    summary = {
        "label": "unit-test",
        "universe": "etf",
        "start": "2015-01-01",
        "end": "2024-12-31",
        "walkforward": True,
        "oos": {
            "cagr": 12.0,
            "sharpe": 0.9,
            "sortino": 1.4,
            "max_drawdown_pct": 18.0,
            "max_dd_duration_days": 210,
            "win_rate": 46.0,
            "profit_factor": 1.75,
            "avg_win": 320.0,
            "avg_loss": -180.0,
            "avg_hold_days": 17.0,
            "exposure_pct": 55.0,
            "trades": 140,
        },
        "full_period": {},
        "by_year": {},
        "config_hash": "0" * 64,
        "code_ref": "abc123",
        "data_hash": "1" * 64,
    }
    oos_overrides = overrides.pop("oos", {})
    summary.update(overrides)
    summary["oos"].update(oos_overrides)
    return summary


def write_latest(cfg, summary):
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------


def test_gate_result_is_a_frozen_value_object():
    result = GateResult(passed=True, reasons=[], report_path=None)
    assert dataclasses.is_dataclass(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_paths_are_where_contract_11_says_they_are(tmp_path):
    cfg = build_config(tmp_path)
    assert backtest_dir(cfg) == cfg.paths.reports_dir / "backtest"
    assert latest_path(cfg) == cfg.paths.reports_dir / "backtest" / "latest.json"


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


def test_a_good_walkforward_report_opens_the_gate(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary())
    verdict = check(cfg)
    assert verdict.passed is True
    assert verdict.reasons == []


def test_report_path_points_at_the_run_directory_when_it_exists(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(label="backtest-20240101-120000"))
    run_dir = backtest_dir(cfg) / "backtest-20240101-120000"
    run_dir.mkdir(parents=True, exist_ok=True)
    assert check(cfg).report_path == run_dir


def test_report_path_falls_back_to_latest_json(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(label="deleted-run"))
    assert check(cfg).report_path == latest_path(cfg)


def test_metrics_exactly_on_the_thresholds_pass(tmp_path):
    """The thresholds are inclusive bounds, not strict ones."""
    cfg = build_config(tmp_path)
    write_latest(
        cfg,
        passing_summary(
            oos={
                "profit_factor": cfg.gates.min_profit_factor,
                "max_drawdown_pct": cfg.gates.max_drawdown_pct,
                "trades": cfg.gates.min_trades,
            }
        ),
    )
    assert check(cfg).passed is True


# ---------------------------------------------------------------------------
# each failure, on its own
# ---------------------------------------------------------------------------


def test_no_report_at_all_fails_and_says_how_to_fix_it(tmp_path):
    cfg = build_config(tmp_path)
    verdict = check(cfg)
    assert verdict.passed is False
    assert len(verdict.reasons) == 1
    assert "swing backtest" in verdict.reasons[0]
    assert str(latest_path(cfg)) in verdict.reasons[0]


def test_a_non_walkforward_run_never_passes(tmp_path):
    """An in-sample fit is not evidence, however good the numbers look."""
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(walkforward=False))
    verdict = check(cfg)
    assert verdict.passed is False
    assert any("walk-forward" in reason for reason in verdict.reasons)


def test_a_spectacular_non_walkforward_run_still_never_passes(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(
        cfg,
        passing_summary(
            walkforward=False,
            oos={"profit_factor": 9.9, "max_drawdown_pct": 1.0, "trades": 5000},
        ),
    )
    assert check(cfg).passed is False


def test_low_profit_factor_fails_with_the_measured_number(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(oos={"profit_factor": 1.05}))
    verdict = check(cfg)
    assert verdict.passed is False
    reason = next(r for r in verdict.reasons if "profit factor" in r)
    assert "1.05" in reason
    assert f"{cfg.gates.min_profit_factor:.2f}" in reason


def test_deep_drawdown_fails_with_the_measured_number(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(oos={"max_drawdown_pct": 44.0}))
    verdict = check(cfg)
    assert verdict.passed is False
    reason = next(r for r in verdict.reasons if "drawdown" in r)
    assert "44.0%" in reason
    assert "35.0%" in reason


def test_too_few_trades_fails_with_the_measured_number(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(oos={"trades": 11}))
    verdict = check(cfg)
    assert verdict.passed is False
    reason = next(r for r in verdict.reasons if "trades" in r)
    assert "11 out-of-sample trades" in reason
    assert str(cfg.gates.min_trades) in reason


def test_every_failing_metric_is_reported_not_just_the_first(tmp_path):
    """A user fixing one problem should already know about the other two."""
    cfg = build_config(tmp_path)
    write_latest(
        cfg,
        passing_summary(
            oos={"profit_factor": 0.8, "max_drawdown_pct": 60.0, "trades": 4},
        ),
    )
    verdict = check(cfg)
    assert verdict.passed is False
    assert len(verdict.reasons) == 3


def test_custom_gate_thresholds_are_honoured(tmp_path):
    cfg = build_config(
        tmp_path, gates={"min_profit_factor": 2.0, "max_drawdown_pct": 10.0, "min_trades": 500}
    )
    write_latest(cfg, passing_summary())  # PF 1.75, DD 18%, 140 trades
    verdict = check(cfg)
    assert verdict.passed is False
    assert len(verdict.reasons) == 3


# ---------------------------------------------------------------------------
# corrupt and hostile inputs fail CLOSED
# ---------------------------------------------------------------------------


def test_unreadable_json_is_treated_as_no_report(tmp_path):
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert load_latest(cfg) is None
    assert check(cfg).passed is False


def test_a_json_list_instead_of_an_object_is_treated_as_no_report(tmp_path):
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_latest(cfg) is None
    assert check(cfg).passed is False


def test_a_missing_oos_section_fails(tmp_path):
    cfg = build_config(tmp_path)
    summary = passing_summary()
    del summary["oos"]
    write_latest(cfg, summary)
    verdict = check(cfg)
    assert verdict.passed is False
    assert any("out-of-sample" in reason for reason in verdict.reasons)


def test_missing_metrics_fail_closed_not_open(tmp_path):
    """A blank oos block must not sail through on defaults."""
    cfg = build_config(tmp_path)
    write_latest(cfg, {**passing_summary(), "oos": {}})
    verdict = check(cfg)
    assert verdict.passed is False
    # Missing drawdown is read as the worst possible drawdown, not as zero.
    assert any("drawdown" in reason for reason in verdict.reasons)
    assert len(verdict.reasons) == 3


def test_a_nan_profit_factor_fails(tmp_path):
    """Expectation updated for audit BUG-017.

    ``NaN`` is a JSON *extension*, not JSON, and a report containing one is now
    refused whole at parse time rather than being read field by field. The
    refusal names the non-finite number instead of naming the profit factor.
    """
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = passing_summary()
    summary["oos"]["profit_factor"] = float("nan")
    # json.dumps writes bare NaN, which a naive json.load accepts as float('nan').
    path.write_text(json.dumps(summary), encoding="utf-8")
    verdict = check(cfg)
    assert verdict.passed is False
    assert any("non-finite number" in reason for reason in verdict.reasons)


def test_a_string_where_a_number_belongs_fails(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(oos={"profit_factor": "excellent"}))
    assert check(cfg).passed is False


def test_load_latest_returns_none_when_the_file_is_absent(tmp_path):
    assert load_latest(build_config(tmp_path)) is None


# ---------------------------------------------------------------------------
# BUG-017 — the report is untrusted input
# ---------------------------------------------------------------------------


def test_the_string_false_does_not_open_the_walkforward_gate(tmp_path):
    """BUG-017: ``bool("false")`` is True, and that used to open the gate.

    A hand-edited or script-generated ``latest.json`` carrying the *string*
    ``"false"`` described an in-sample fit and was let through. Only the JSON
    literal ``true`` counts now.
    """
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(walkforward="false"))
    verdict = check(cfg)
    assert verdict.passed is False
    assert any("walk-forward" in reason for reason in verdict.reasons)


@pytest.mark.parametrize("truthy", ["true", "yes", 1, [1], {"a": 1}])
def test_no_truthy_stand_in_passes_for_walkforward(tmp_path, truthy):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(walkforward=truthy))
    assert check(cfg).passed is False


def test_an_infinity_literal_is_refused_in_one_sentence_not_a_traceback(tmp_path):
    """BUG-017: ``"trades": Infinity`` used to raise OverflowError out of `swing report`."""
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = passing_summary()
    summary["oos"]["trades"] = float("inf")
    path.write_text(json.dumps(summary), encoding="utf-8")  # writes bare Infinity

    verdict = check(cfg)
    assert verdict.passed is False
    assert len(verdict.reasons) == 1
    assert "non-finite number" in verdict.reasons[0]
    assert load_latest(cfg) is None


def test_an_infinity_profit_factor_does_not_pass_the_gate(tmp_path):
    """BUG-017: the sentinel that "passes" every threshold must fail closed."""
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = passing_summary()
    summary["oos"]["profit_factor"] = float("inf")
    path.write_text(json.dumps(summary), encoding="utf-8")
    assert check(cfg).passed is False


def test_report_command_refuses_a_non_finite_report_cleanly(tmp_path, capsys):
    """The same hostile file through `swing report`, which has no guardrail to absorb it."""
    from swing.backtest.report import print_latest

    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = passing_summary()
    summary["oos"]["trades"] = float("inf")
    path.write_text(json.dumps(summary), encoding="utf-8")

    print_latest(cfg)  # must not raise
    out = capsys.readouterr().out
    assert "cannot be used" in out
    assert "non-finite number" in out
    assert "swing backtest" in out


def test_an_overflowing_literal_still_fails_closed(tmp_path):
    """BUG-017: ``1e400`` parses to inf WITHOUT touching parse_constant.

    This is the case ``parse_constant`` cannot see, so it pins the finite-only
    coercions rather than the parser.
    """
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(passing_summary()).replace('"profit_factor": 1.75', '"profit_factor": 1e400')
    assert "1e400" in text
    path.write_text(text, encoding="utf-8")

    assert load_latest(cfg) is not None  # it parses fine; the value is just infinite
    verdict = check(cfg)
    assert verdict.passed is False
    reason = next(r for r in verdict.reasons if "profit factor" in r)
    assert "0.00" in reason  # read as the worst possible value, not as infinity


def test_an_overflowing_trade_count_fails_closed(tmp_path):
    """BUG-017: ``int(inf)`` raises OverflowError, which _as_int must absorb."""
    cfg = build_config(tmp_path)
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(passing_summary()).replace('"trades": 140', '"trades": 1e400')
    path.write_text(text, encoding="utf-8")

    verdict = check(cfg)
    assert verdict.passed is False
    assert any("0 out-of-sample trades" in reason for reason in verdict.reasons)


def test_an_ablation_label_can_never_open_the_gate(tmp_path):
    """BUG-017: rule 3 was enforced only at write time; copying the file bypassed it."""
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(label=f"{ABLATION_PREFIX}-no-regime"))
    verdict = check(cfg)
    assert verdict.passed is False
    reason = next(r for r in verdict.reasons if "ablation" in r)
    assert "ablate-no-regime" in reason


def test_a_normal_label_is_not_mistaken_for_an_ablation(tmp_path):
    cfg = build_config(tmp_path)
    write_latest(cfg, passing_summary(label="backtest-20240101-120000"))
    assert check(cfg).passed is True


def test_read_latest_separates_absence_from_corruption(tmp_path):
    cfg = build_config(tmp_path)
    assert read_latest(cfg) == (None, None)

    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ nope", encoding="utf-8")
    summary, problem = read_latest(cfg)
    assert summary is None
    assert problem is not None and "valid JSON" in problem
