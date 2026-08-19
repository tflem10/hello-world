"""AC9 and Contract 11 report plumbing — offline, with a fake provider.

Nothing here touches the network: ``swing.data.get_provider`` and
``swing.universe.load`` are both monkeypatched, so the runner is exercised
end to end over synthetic bars.

The headline test is :func:`test_two_identical_runs_are_byte_identical` — AC9.
It writes two complete report directories from the same inputs and compares the
bytes, which is only possible because nothing in ``summary.json``,
``trades.csv`` or ``equity.csv`` reads a clock.
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from conftest import build_config
from swing.backtest import runner
from swing.backtest.gate import check, latest_path
from swing.backtest.runner import ABLATION_PREFIX, config_hash, data_hash, run_backtest
from swing.universe import Instrument
from test_backtest_engine import ramp_bars, spike_volume

REPORT_FILES = ("summary.json", "report.md", "report.html", "trades.csv", "equity.csv")
DETERMINISTIC_FILES = ("summary.json", "trades.csv", "equity.csv")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeProvider:
    """A DataProvider that serves a fixed dict of frames and counts its calls."""

    def __init__(self, bars: dict[str, pd.DataFrame], *, earnings_raises: bool = False):
        self._bars = bars
        self.earnings_raises = earnings_raises
        self.requested: list[list[str]] = []

    def daily_bars(self, symbols, start, end):
        self.requested.append(list(symbols))
        return {s: self._bars[s].copy() for s in symbols if s in self._bars}

    def earnings_dates(self, symbols):
        if self.earnings_raises:
            raise RuntimeError("the earnings endpoint is having a day")
        return dict.fromkeys(symbols)

    def latest_quotes(self, symbols):  # pragma: no cover - unused by the runner
        return {}

    def fundamentals(self, symbols):  # pragma: no cover - unused by the runner
        return {}


def fake_universe() -> dict[str, pd.DataFrame]:
    """Two tradable ramps plus SPY, all signalling a few times."""
    bars: dict[str, pd.DataFrame] = {}
    for offset, (symbol, growth) in enumerate((("AAA", 0.0018), ("BBB", 0.0024))):
        frame = ramp_bars(growth=growth)
        for bar in (300 + offset * 3, 330 + offset * 3, 360 + offset * 3):
            frame = spike_volume(frame, bar)
        bars[symbol] = frame
    bars["SPY"] = ramp_bars()
    return bars


INSTRUMENTS = [
    Instrument(symbol="AAA", name="Alpha", kind="stock", source="sp500"),
    Instrument(symbol="BBB", name="Beta", kind="etf", source="etf"),
]


@pytest.fixture
def wired(monkeypatch):
    """Patch the universe and the data provider; hand back the fake provider."""
    bars = fake_universe()
    provider = FakeProvider(bars)

    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: provider)
    return provider


def runner_cfg(root, **overrides):
    sections = {
        "account": {"equity": 200_000.0, "max_position_pct": 20.0},
        "gates": {"min_trades": 1},
    }
    for name, values in overrides.items():
        sections.setdefault(name, {}).update(values)
    return build_config(root, **sections)


def go(cfg, **kwargs):
    """Run a quiet backtest with sensible test defaults."""
    kwargs.setdefault("walkforward", False)
    kwargs.setdefault("start", date(2021, 1, 4))
    kwargs.setdefault("progress", lambda _line: None)
    return run_backtest(cfg, **kwargs)


# ---------------------------------------------------------------------------
# reference capital: the backtest measures the strategy, not the bank balance
# ---------------------------------------------------------------------------


def test_backtest_runs_on_reference_capital_not_the_users_account(tmp_path, wired):
    """A real $100 account must not silently empty the backtest.

    Whole-share rounding on $100 rejects every entry in these fixtures, so if
    ``account.equity`` still reached the engine this run would produce no trades
    at all and the report would be measuring the account, not the rules.
    """
    cfg = runner_cfg(
        tmp_path,
        account={"equity": 100.0, "max_position_pct": 20.0},
        backtest={"initial_equity": 10_000.0},
    )
    assert cfg.account.equity == 100.0
    assert cfg.backtest.initial_equity == 10_000.0

    directory = go(cfg, label="rebased")

    trades = pd.read_csv(directory / "trades.csv")
    assert len(trades) > 0, "the $100 account leaked into the simulation"

    equity = pd.read_csv(directory / "equity.csv")
    # The curve starts from the reference capital, give or take day one's P&L.
    assert equity["equity"].iloc[0] == pytest.approx(10_000.0, rel=0.05)
    assert equity["equity"].iloc[0] > 100.0


def test_summary_records_the_initial_equity_it_used(tmp_path, wired):
    cfg = runner_cfg(tmp_path, account={"equity": 100.0}, backtest={"initial_equity": 10_000.0})
    summary = json.loads((go(cfg, label="rebased-summary") / "summary.json").read_text())
    assert summary["initial_equity"] == 10_000.0


def test_the_users_account_equity_cannot_change_the_result(tmp_path, wired):
    """Two users with wildly different balances must get the same backtest."""
    poor = runner_cfg(
        tmp_path / "poor", account={"equity": 100.0}, backtest={"initial_equity": 10_000.0}
    )
    rich = runner_cfg(
        tmp_path / "rich",
        account={"equity": 5_000_000.0},
        backtest={"initial_equity": 10_000.0},
    )
    first = go(poor, label="same")
    second = go(rich, label="same")

    for name in DETERMINISTIC_FILES:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_changing_the_reference_capital_does_change_the_result(tmp_path, wired):
    """The knob that IS supposed to matter, still matters."""
    small = runner_cfg(tmp_path / "small", backtest={"initial_equity": 10_000.0})
    large = runner_cfg(tmp_path / "large", backtest={"initial_equity": 250_000.0})
    first = go(small, label="capital")
    second = go(large, label="capital")

    assert (first / "equity.csv").read_bytes() != (second / "equity.csv").read_bytes()
    # ...and it is part of the provenance hash, so the two reports are
    # distinguishable after the fact.
    assert config_hash(small) != config_hash(large)


# ---------------------------------------------------------------------------
# the report directory
# ---------------------------------------------------------------------------


def test_a_run_writes_every_contract_11_file(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="unit-full")

    assert directory == cfg.paths.reports_dir / "backtest" / "unit-full"
    for name in REPORT_FILES:
        assert (directory / name).is_file(), name
        assert (directory / name).stat().st_size > 0, name


def test_summary_json_has_every_required_key(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="unit-keys")
    summary = json.loads((directory / "summary.json").read_text())

    for key in (
        "label",
        "universe",
        "start",
        "end",
        "walkforward",
        "oos",
        "full_period",
        "by_year",
        "config_hash",
        "code_ref",
        "data_hash",
    ):
        assert key in summary, key

    for block in ("oos", "full_period"):
        for metric in (
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
        ):
            assert metric in summary[block], f"{block}.{metric}"


def test_trades_and_equity_csvs_match_the_engine_columns(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="unit-csv")

    trades = pd.read_csv(directory / "trades.csv")
    assert list(trades.columns) == [
        "symbol",
        "entry_date",
        "entry_price",
        "exit_date",
        "exit_price",
        "shares",
        "pnl",
        "pnl_pct",
        "hold_days",
        "exit_reason",
        "entry_cost",
        "exit_cost",
    ]
    assert len(trades) > 0
    # Dates are ISO strings, not pandas timestamp reprs.
    assert trades["entry_date"].str.match(r"^\d{4}-\d{2}-\d{2}$").all()

    equity = pd.read_csv(directory / "equity.csv")
    assert list(equity.columns) == ["date", "equity", "cash", "n_positions", "drawdown"]
    assert len(equity) > 0


def test_html_report_is_self_contained_and_structured(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="unit-html")
    html = (directory / "report.html").read_text()

    assert html.startswith("<!doctype html>")
    assert "<title>Backtest — unit-html</title>" in html
    # Charts are embedded, not linked: the file survives being emailed.
    assert html.count("data:image/png;base64,") == 2
    assert "http://" not in html and "https://" not in html
    assert "Monthly returns" in html
    assert "Sensitivity" in html
    assert "Provenance" in html
    # generated_at lives here and ONLY here.
    assert "Generated at" in html


def test_markdown_report_names_the_headline_and_provenance(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="unit-md")
    text = (directory / "report.md").read_text()
    assert "# Backtest — unit-md" in text
    assert "Config hash" in text
    assert "Sensitivity" in text


# ---------------------------------------------------------------------------
# AC9 — byte-identical reruns
# ---------------------------------------------------------------------------


def test_two_identical_runs_are_byte_identical(tmp_path, wired):
    """AC9. Same inputs, same bytes — in two different directories."""
    first_cfg = runner_cfg(tmp_path / "run1")
    second_cfg = runner_cfg(tmp_path / "run2")

    first = go(first_cfg, label="determinism")
    second = go(second_cfg, label="determinism")

    for name in DETERMINISTIC_FILES:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_deterministic_files_contain_no_timestamp(tmp_path, wired):
    """A clock reading anywhere in these three files would break AC9."""
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="no-clock")
    for name in DETERMINISTIC_FILES:
        text = (directory / name).read_text()
        assert "generated_at" not in text
        # No wall-clock date either: the fixture data ends years before today.
        assert date.today().isoformat() not in text
    summary = json.loads((directory / "summary.json").read_text())
    assert "generated_at" not in summary
    # ...while the HTML, which is allowed one, has it.
    assert "Generated at" in (directory / "report.html").read_text()


def test_json_is_written_with_sorted_keys_and_rounded_floats(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="stable-json")
    text = (directory / "summary.json").read_text()
    payload = json.loads(text)
    assert text == json.dumps(payload, indent=2, sort_keys=True) + "\n"

    for value in payload["full_period"].values():
        if isinstance(value, float):
            assert value == round(value, runner.FLOAT_PRECISION)


def test_csv_floats_are_written_to_six_places(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="stable-csv")
    line = (directory / "trades.csv").read_text().splitlines()[1]
    price = line.split(",")[2]
    assert len(price.split(".")[1]) == runner.FLOAT_PRECISION


# ---------------------------------------------------------------------------
# latest.json and ablations
# ---------------------------------------------------------------------------


def test_a_normal_run_updates_latest_json(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="baseline")
    assert latest_path(cfg).is_file()
    assert latest_path(cfg).read_bytes() == (directory / "summary.json").read_bytes()


def test_an_ablation_run_never_updates_latest_json(tmp_path, wired):
    """Contract 11 amendment: a deliberately crippled variant must not become the gate's
    reference."""
    cfg = runner_cfg(tmp_path)
    baseline = go(cfg, label="baseline")
    baseline_bytes = latest_path(cfg).read_bytes()

    ablation = go(cfg, label=f"{ABLATION_PREFIX}-no-regime")
    assert ablation.is_dir()
    assert (ablation / "summary.json").is_file()
    # latest.json is untouched, and still describes the baseline.
    assert latest_path(cfg).read_bytes() == baseline_bytes
    assert json.loads(latest_path(cfg).read_text())["label"] == "baseline"
    assert (baseline / "summary.json").is_file()


def test_the_default_label_is_a_timestamped_directory_name(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg)
    assert directory.name.startswith("backtest-")
    assert json.loads((directory / "summary.json").read_text())["label"] == directory.name


# ---------------------------------------------------------------------------
# universe selection and provider behaviour
# ---------------------------------------------------------------------------


def test_etf_universe_requests_only_etfs_plus_the_regime_symbol(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    go(cfg, universe="etf", label="etf-only")
    assert wired.requested[0] == ["BBB", "SPY"]  # AAA is a stock


def test_stock_universe_requests_only_stocks_plus_the_regime_symbol(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    go(cfg, universe="stocks", label="stocks-only")
    assert wired.requested[0] == ["AAA", "SPY"]


def test_full_universe_requests_everything(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    go(cfg, universe="full", label="full")
    assert wired.requested[0] == ["AAA", "BBB", "SPY"]


def test_the_regime_symbol_is_not_itself_tradable(tmp_path, wired):
    """SPY is an input to the filter, not a candidate — unless it is in the universe."""
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, universe="full", label="no-spy-trades")
    trades = pd.read_csv(directory / "trades.csv")
    assert "SPY" not in set(trades["symbol"])
    assert json.loads((directory / "summary.json").read_text())["n_symbols"] == 2


def test_an_unknown_universe_name_is_refused_in_plain_english(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    with pytest.raises(ValueError, match="universe must be one of"):
        go(cfg, universe="crypto")


def test_a_broken_earnings_endpoint_does_not_stop_the_run(tmp_path, monkeypatch):
    """Earnings are optional; losing them degrades the result, it does not kill it."""
    provider = FakeProvider(fake_universe(), earnings_raises=True)
    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: provider)

    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="no-earnings")
    assert (directory / "summary.json").is_file()
    assert pd.read_csv(directory / "trades.csv").shape[0] > 0


def test_an_empty_universe_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("swing.universe.load", lambda cfg: [])
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: FakeProvider({}))
    cfg = runner_cfg(tmp_path)
    with pytest.raises(ValueError, match="is empty"):
        go(cfg, label="nothing")


def test_no_price_history_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: FakeProvider({}))
    cfg = runner_cfg(tmp_path)
    with pytest.raises(ValueError, match="No price history"):
        go(cfg, label="nothing")


# ---------------------------------------------------------------------------
# provenance hashes
# ---------------------------------------------------------------------------


def test_config_hash_tracks_strategy_changes_not_account_size(tmp_path):
    base = build_config(tmp_path)
    tuned = build_config(tmp_path, strategy={"atr_stop_mult": 2.5})
    richer = build_config(tmp_path, account={"equity": 1_000_000.0})

    assert config_hash(base) != config_hash(tuned)
    # Equity does not change what the strategy would have done.
    assert config_hash(base) == config_hash(richer)
    assert len(config_hash(base)) == 64


def test_config_hash_tracks_gate_and_cost_changes(tmp_path):
    base = build_config(tmp_path)
    assert config_hash(base) != config_hash(build_config(tmp_path, gates={"min_trades": 99}))
    assert config_hash(base) != config_hash(build_config(tmp_path, backtest={"slippage_bps": 25.0}))


def test_data_hash_changes_when_the_data_does():
    bars = fake_universe()
    first = data_hash(bars)
    assert first == data_hash(fake_universe())  # same data, same hash

    trimmed = {symbol: frame.iloc[:-1] for symbol, frame in bars.items()}
    assert data_hash(trimmed) != first  # one fewer bar

    fewer = {symbol: frame for symbol, frame in bars.items() if symbol != "AAA"}
    assert data_hash(fewer) != first  # a symbol vanished


def test_code_ref_is_a_hash_or_the_word_unknown():
    ref = runner.code_ref()
    assert ref == "unknown" or len(ref) == 40


# ---------------------------------------------------------------------------
# walk-forward end to end, and the gate that reads it
# ---------------------------------------------------------------------------


def wf_bars() -> dict[str, pd.DataFrame]:
    bars: dict[str, pd.DataFrame] = {}
    for offset, (symbol, growth) in enumerate((("AAA", 0.0018), ("BBB", 0.0024))):
        frame = ramp_bars(n=1500, growth=growth)
        for bar in range(300 + offset * 7, 1499, 25):
            frame = spike_volume(frame, bar)
        bars[symbol] = frame
    bars["SPY"] = ramp_bars(n=1500)
    return bars


def test_a_walkforward_run_writes_windows_and_opens_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: FakeProvider(wf_bars()))
    monkeypatch.setattr(
        "swing.backtest.walkforward.TUNING_GRID",
        {"atr_stop_mult": (1.5, 2.5), "donchian_window": (15, 25)},
    )

    cfg = runner_cfg(tmp_path, backtest={"is_years": 1, "oos_years": 1})
    directory = go(
        cfg,
        label="wf",
        walkforward=True,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
    )
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["walkforward"] is True
    assert summary["windows"], "a walk-forward run must record its folds"
    assert summary["objective"]
    for fold in summary["windows"]:
        assert fold["is_end"] < fold["oos_start"]
        assert set(fold["params"]) == {"atr_stop_mult", "donchian_window"}

    # The report the gate reads is the one that was just written.
    verdict = check(cfg)
    assert verdict.report_path == directory
    assert isinstance(verdict.passed, bool)


def test_a_non_walkforward_run_is_marked_ineligible(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="in-sample-only", walkforward=False)
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["walkforward"] is False
    assert summary["windows"] == []

    verdict = check(cfg)
    assert verdict.passed is False
    assert any("walk-forward" in reason for reason in verdict.reasons)


def test_report_html_warns_loudly_when_the_run_is_not_walkforward(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="warned", walkforward=False)
    html = (directory / "report.html").read_text()
    assert "NOT a walk-forward run" in html
    assert 'class="banner warn"' in html


# ---------------------------------------------------------------------------
# print_latest
# ---------------------------------------------------------------------------


def test_print_latest_reports_the_run_and_the_verdict(tmp_path, wired, capsys):
    from swing.backtest.report import print_latest

    cfg = runner_cfg(tmp_path)
    go(cfg, label="printed")
    print_latest(cfg)
    out = capsys.readouterr().out
    assert "printed" in out
    assert "GATE:" in out
    assert "Profit factor" in out


def test_print_latest_explains_itself_when_there_is_no_report(tmp_path, capsys):
    from swing.backtest.report import print_latest

    print_latest(build_config(tmp_path))
    out = capsys.readouterr().out
    assert "No backtest report found" in out
    assert "swing backtest" in out
