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

import contextlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from conftest import build_config
from swing.backtest import runner
from swing.backtest.gate import check, latest_path
from swing.backtest.runner import ABLATION_PREFIX, config_hash, data_hash, run_backtest
from swing.backtest.walkforward import TUNING_GRID
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


def test_the_written_report_is_legible_in_a_dark_browser(tmp_path, wired):
    """BUG-051, as it reaches disk.

    ``tests/test_html_contrast.py`` measures the stylesheet; this measures the
    wiring. The report used to set ``color: #222`` on ``body`` and no
    background, so a dark-mode browser painted its own near-black canvas behind
    near-black text and the whole file went invisible. Cheap enough to survive
    any refactor of how the stylesheet reaches the template.
    """
    cfg = runner_cfg(tmp_path)
    html = (go(cfg, label="dark-mode") / "report.html").read_text()

    assert "@media (prefers-color-scheme: dark)" in html
    assert "color-scheme: light dark" in html
    assert "background: var(--bg); color: var(--ink);" in html
    # The charts sit on a plate painted the same colour as the PNG itself, so
    # they never read as a hole punched in the page.
    assert html.count('<figure class="plate">') == 2


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
# index membership — what universe did this report actually trade?
# ---------------------------------------------------------------------------


def membership_of(tmp_path, wired_cfg=None, **kwargs):
    """Run a backtest and hand back its ``summary.json`` membership block."""
    cfg = wired_cfg if wired_cfg is not None else runner_cfg(tmp_path)
    directory = go(cfg, **kwargs)
    return json.loads((directory / "summary.json").read_text())["membership"]


def test_every_summary_says_which_universe_it_traded(tmp_path, wired):
    """Contract: a report can never be misread about the universe behind it."""
    block = membership_of(tmp_path, label="membership-off")
    assert block["mode"] == "off"
    assert block["applied"] is False
    assert block["unknown_policy"] == "exclude"
    assert block["symbols_excluded"] == 0


def test_the_membership_block_publishes_the_date_quality_behind_it(tmp_path, wired):
    """The composition of the evidence, not just the mode that read it.

    A point-in-time number is only as good as the dates under it, and most of
    the stints under this one rest on an upper bound rather than a stated day.
    Published so a reader sees that beside the result instead of discovering it
    in a log line.
    """
    block = membership_of(tmp_path, label="membership-quality")

    assert block["bounded_policy"] == "unknown", "the conservative default moved"
    assert block["symbols_bounded_join"] >= 0

    quality = block["stint_date_quality"]
    # Rows of the real membership files, so no exact figures are asserted —
    # those CSVs belong to another package and move when it rebuilds them.
    assert quality["stints"] == quality["exact"] + quality["bounded"] + quality["undated"]
    if quality["stints"]:
        share = 100.0 * (quality["bounded"] + quality["undated"]) / quality["stints"]
        assert quality["approximate_pct"] == pytest.approx(share, abs=1e-6)
    assert set(quality["by_source"]) <= {"sp500", "sp400", "sp600"}


def test_the_date_quality_describes_the_files_not_the_policy(tmp_path, wired):
    """It must not move when a policy moves: a file is what it is."""
    strict = membership_of(tmp_path, label="quality-strict")
    loose = membership_of(
        tmp_path,
        runner_cfg(tmp_path, universe={"membership_bounded": "exact"}),
        label="quality-loose",
    )

    assert strict["bounded_policy"] == "unknown"
    assert loose["bounded_policy"] == "exact"
    assert strict["stint_date_quality"] == loose["stint_date_quality"]


def test_a_degraded_membership_block_still_names_both_policies(tmp_path, wired, monkeypatch):
    """Both come from config, so they survive an unreadable file."""
    from swing.universe import UniverseError

    def broken(cfg, **kwargs):
        raise UniverseError("sp500-membership.csv is missing")

    monkeypatch.setattr("swing.universe.membership_coverage", broken)
    block = membership_of(tmp_path, label="quality-degraded")

    assert block["error"]
    assert block["unknown_policy"] == "exclude"
    assert block["bounded_policy"] == "unknown"


def test_the_membership_block_separates_gated_stocks_from_ungated_etfs(tmp_path, wired):
    """AAA is an S&P 500 stock; BBB is an ETF and was never in an index."""
    block = membership_of(tmp_path, label="membership-counts")
    assert block["symbols_gated"] == 1
    assert block["symbols_ungated"] == 1
    # AAA is invented, so no committed membership file has a row for it — a
    # coverage hole, counted rather than waved through.
    assert block["symbols_no_membership_row"] == 1
    assert block["member_years_point_in_time"] == 0.0


def test_member_years_cover_the_whole_window_when_the_mode_is_off(tmp_path, wired):
    """With the mode off the run really does trade every symbol for every day."""
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="membership-years")
    summary = json.loads((directory / "summary.json").read_text())
    block = summary["membership"]
    days = (date.fromisoformat(summary["end"]) - date.fromisoformat(summary["start"])).days + 1
    expected = round(block["symbols_gated"] * days / runner.DAYS_PER_YEAR, 6)
    assert block["member_years"] == expected == block["member_years_nominal"]


def test_an_etf_only_run_reports_no_member_years_rather_than_a_missing_number(tmp_path, wired):
    """ETFs are not index constituents; zero is the answer, not an absence."""
    block = membership_of(tmp_path, label="membership-etf", universe="etf")
    assert block["symbols_gated"] == 0
    assert block["symbols_ungated"] == 1
    assert block["member_years"] == 0.0
    assert block["join_date_coverage_pct"] == 0.0


@pytest.mark.parametrize("universe", ["full", "etf", "stocks"])
def test_the_block_counts_the_symbols_that_had_bars_not_the_ones_requested(
    tmp_path, wired, universe
):
    """``symbols_gated + symbols_ungated == n_symbols``, so a reader can check it.

    A symbol the config asked for and the provider could not serve trades
    nothing, so counting it would overstate the exposure the run really had.
    """
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label=f"membership-count-{universe}", universe=universe)
    summary = json.loads((directory / "summary.json").read_text())
    block = summary["membership"]
    assert block["symbols_gated"] + block["symbols_ungated"] == summary["n_symbols"]


def test_a_symbol_the_provider_could_not_serve_is_left_out_of_the_count(tmp_path, monkeypatch):
    """CCC is in the universe and has no bars: it cannot have traded a member-year."""
    bars = fake_universe()
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: FakeProvider(bars))
    monkeypatch.setattr(
        "swing.universe.load",
        lambda cfg: [*INSTRUMENTS, Instrument("CCC", "Gamma", "stock", "sp500")],
    )
    cfg = runner_cfg(tmp_path)
    summary = json.loads((go(cfg, label="membership-nodata") / "summary.json").read_text())
    assert summary["n_symbols"] == 2
    assert summary["membership"]["symbols_gated"] == 1


def test_the_block_publishes_the_size_of_the_look_ahead_on_the_real_universe(tmp_path):
    """The honest number an 'off' report has to carry: how much is unvouched-for.

    No exact figures — the membership CSVs are another package's and are being
    improved — but a stock run must show a materially smaller point-in-time
    exposure than the one it actually traded, and the coverage must be
    reported per index so an uneven fix is visible.
    """
    from swing import universe as universe_module

    cfg = build_config(tmp_path)
    stocks = [i for i in universe_module.load(cfg) if i.kind != "etf"]
    block = runner.membership_block(cfg, stocks, date(2010, 1, 1), date(2025, 12, 31))

    assert block["symbols_gated"] == len(stocks) > 1_400
    assert block["member_years_nominal"] > 20_000
    # Loose thresholds on purpose: better join-date coverage moves these, and
    # the membership CSVs belong to another package. What is asserted is what
    # cannot change — hundreds of today's members joined after 2010, so the
    # provable exposure is materially below the nominal one either way.
    assert block["member_years_point_in_time"] < block["member_years_nominal"] * 0.9
    assert 50.0 < block["join_date_coverage_pct"] <= 100.0
    assert block["join_date_coverage"]["sp500"]["with_join_date"] > 480
    # The size of the unknown-date choice, measured rather than inferred: never
    # smaller than the unknown-join count, because a stint whose *end* is
    # unstated is relaxed by the permissive policy too.
    assert block["symbols_policy_sensitive"] >= block["symbols_unknown_join"]


def test_the_policy_sensitive_count_is_the_symbols_the_two_readings_disagree_about(tmp_path):
    """It is a property of the data, so both policies report the same figure."""
    from swing import universe as universe_module

    cfg = build_config(tmp_path)
    stocks = [i for i in universe_module.load(cfg) if i.kind != "etf"]
    start, end = date(2010, 1, 1), date(2025, 12, 31)

    strict = runner.membership_block(cfg, stocks, start, end)["symbols_policy_sensitive"]
    loose = runner.membership_block(
        build_config(tmp_path, backtest={"membership_unknown": "include"}), stocks, start, end
    )["symbols_policy_sensitive"]
    assert strict == loose

    expected = sum(
        1
        for symbol, window in universe_module.membership_windows(
            cfg, instruments=stocks, unknown="exclude"
        ).items()
        if universe_module.membership_windows(cfg, instruments=stocks, unknown="include")[symbol]
        != window
    )
    assert strict == expected


def test_with_the_mode_on_the_block_reports_the_restricted_universe(tmp_path):
    """With the mode on, the exclusions become real and ``member_years`` drops
    to the exposure a membership file can actually vouch for.
    """
    from swing import universe as universe_module

    stocks = [i for i in universe_module.load(build_config(tmp_path)) if i.kind != "etf"]
    start, end = date(2010, 1, 1), date(2025, 12, 31)

    off = runner.membership_block(build_config(tmp_path), stocks, start, end)
    on = runner.membership_block(
        build_config(tmp_path, backtest={"membership": "point_in_time"}), stocks, start, end
    )
    loose = runner.membership_block(
        build_config(
            tmp_path,
            backtest={"membership": "point_in_time", "membership_unknown": "include"},
        ),
        stocks,
        start,
        end,
    )

    assert (off["applied"], on["applied"]) == (False, True)
    assert off["symbols_excluded"] == 0
    assert on["symbols_excluded"] == on["symbols_unknown_join"] > 0
    assert on["member_years"] == on["member_years_point_in_time"] < off["member_years"]
    # The permissive policy excludes nobody and vouches for strictly more.
    assert loose["symbols_excluded"] == 0
    assert on["member_years"] < loose["member_years"] < off["member_years"]


def test_an_unreadable_membership_file_does_not_stop_the_run(tmp_path, wired, monkeypatch):
    """Provenance must never be the thing that kills forty minutes of simulation.

    It degrades to an ``error`` key rather than to a quiet row of zeros, so the
    block can never be read as "measured, and there is no bias".
    """
    from swing.universe import UniverseError

    def broken(*args, **kwargs):
        raise UniverseError("sp500-membership.csv is missing")

    monkeypatch.setattr("swing.universe.membership_coverage", broken)
    block = membership_of(tmp_path, label="membership-broken")
    assert block["error"] == "sp500-membership.csv is missing"
    assert block["applied"] is False
    assert "member_years" not in block


def test_a_bad_label_is_still_refused_before_the_membership_mode(tmp_path, wired):
    """Ordering: the cheapest check first, so the message names the real mistake."""
    cfg = runner_cfg(tmp_path, backtest={"membership": "point_in_time"})
    with pytest.raises(ValueError, match="not usable as a directory name"):
        go(cfg, label="../escape")


# ---------------------------------------------------------------------------
# point-in-time membership, enforced
#
# The runner's job is the translation: membership windows -> a per-bar boolean
# mask per symbol -> ``run_engine(eligible=...)``. Everything below is built on
# synthetic membership files so the boundaries can be asserted to the day, and
# on one fixture whose trade timeline is fixed and known:
#
#   spike on bar 300 (2021-02-25) -> enter 2021-02-26, time-stop out 2021-04-26
#   spike on bar 360 (2021-05-20) -> enter 2021-05-21, out at the end of data
#
# so a membership date placed between them decides exactly which of the two
# trades may happen.
# ---------------------------------------------------------------------------

EARLY_ENTRY = pd.Timestamp("2021-02-26")
EARLY_EXIT = pd.Timestamp("2021-04-26")
LATE_ENTRY = pd.Timestamp("2021-05-21")

#: One stint per case that matters. Dates are chosen against the timeline above:
#: 2021-03-15 falls *inside* the first trade, and 2021-04-01 falls between the
#: two.
MEMBERSHIP_FILES = {
    "sp500-membership": (
        "symbol,name,added,removed\n"
        "ALWAYS,Always In,1990-01-02,\n"
        "JOINER,Joined Midway,2021-04-01,\n"
        "LEAVER,Left Midway,1990-01-02,2021-03-15\n"
    ),
    "sp400-membership": (
        "symbol,name,added,removed\n"
        "MOVER,Promoted From 600,2021-03-16,\n"
        "NEWCOMER,Brand New,2021-03-16,\n"
    ),
    "sp600-membership": (
        "symbol,name,added,removed\n"
        "MOVER,Promoted From 600,2010-01-01,2021-03-15\n"
        "NODATE,No Join Date,,\n"
    ),
}


def membership_bars(symbols) -> dict[str, pd.DataFrame]:
    """One identical two-signal ramp per symbol, plus SPY.

    Identical on purpose: any difference between two symbols' trades in these
    tests is then attributable to membership and to nothing else.
    """
    bars = {symbol: spike_volume(spike_volume(ramp_bars(), 300), 360) for symbol in symbols}
    bars["SPY"] = ramp_bars()
    return bars


@pytest.fixture
def membership_wired(tmp_path, monkeypatch):
    """Serve :data:`MEMBERSHIP_FILES` in place of the committed membership CSVs.

    Returns a callable taking ``{symbol: source}``; it wires up the matching
    instruments, bars and provider and hands back the provider. The parse cache
    is cleared on the way in and on the way out, so no synthetic stint can leak
    into a test that reads the real files.
    """
    from swing import universe as universe_module

    real = universe_module._snapshot_path
    served: dict[str, Path] = {}
    assets = tmp_path / "membership"
    assets.mkdir(exist_ok=True)

    @contextlib.contextmanager
    def fake(stem: str):
        if stem in served:
            yield served[stem]
            return
        with real(stem) as path:
            yield path

    monkeypatch.setattr(universe_module, "_snapshot_path", fake)

    def install(symbols: dict[str, str], files: dict[str, str] = MEMBERSHIP_FILES):
        for stem, text in files.items():
            path = assets / f"{stem}.csv"
            path.write_text(text, encoding="utf-8")
            served[stem] = path
        universe_module._read_membership.cache_clear()
        instruments = [
            Instrument(symbol=symbol, name=symbol.title(), kind="stock", source=source)
            for symbol, source in symbols.items()
        ]
        provider = FakeProvider(membership_bars(symbols))
        monkeypatch.setattr("swing.universe.load", lambda cfg: list(instruments))
        monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: provider)
        return provider

    universe_module._read_membership.cache_clear()
    yield install
    universe_module._read_membership.cache_clear()


def pit_cfg(tmp_path, policy: str = "exclude", **overrides):
    """A runner config with point-in-time membership switched on."""
    backtest = {"membership": "point_in_time", "membership_unknown": policy}
    backtest.update(overrides.pop("backtest", {}))
    return runner_cfg(tmp_path, backtest=backtest, **overrides)


def traded(directory) -> pd.DataFrame:
    """The trades a run wrote, with real timestamps."""
    frame = pd.read_csv(directory / "trades.csv", parse_dates=["entry_date", "exit_date"])
    return frame.sort_values(["symbol", "entry_date"]).reset_index(drop=True)


def entries(directory, symbol: str) -> list[pd.Timestamp]:
    frame = traded(directory)
    return list(frame.loc[frame["symbol"] == symbol, "entry_date"])


def test_window_ends_are_inclusive_on_both_sides(tmp_path):
    """The boundary rule, asserted on the mask itself rather than inferred.

    A symbol is a member ON its join date and ON its removal date. Signals are
    decided at a close and filled at the next open, so the removal date's own
    signal still fills the following morning — the same one-bar lag every other
    gate carries.
    """
    index = pd.bdate_range("2021-01-04", periods=10)  # Mon 4th .. Fri 15th
    mask = runner._window_mask(((date(2021, 1, 6), date(2021, 1, 8)),), index)
    assert list(index[mask]) == [
        pd.Timestamp("2021-01-06"),
        pd.Timestamp("2021-01-07"),
        pd.Timestamp("2021-01-08"),
    ]

    # An open end at either side, and "no window at all" meaning nothing.
    assert runner._window_mask(((None, None),), index).all()
    assert not runner._window_mask((), index).any()
    assert runner._window_mask(((None, date(2021, 1, 5)),), index).sum() == 2
    assert runner._window_mask(((date(2021, 1, 14), None),), index).sum() == 2

    # Two disjoint stints union rather than overwrite one another.
    two = runner._window_mask(
        ((date(2021, 1, 4), date(2021, 1, 5)), (date(2021, 1, 14), date(2021, 1, 15))), index
    )
    assert two.sum() == 4


def test_a_tz_aware_bar_index_does_not_raise(tmp_path):
    """BUG-045 was exactly this: a tz-aware index minus a naive Timestamp raises.

    There it surfaced as "no blackout", permitting an entry inside one. Here it
    would surface as a crashed run, but the fix is the same and it is cheaper to
    pin than to rediscover.
    """
    naive = pd.bdate_range("2021-01-04", periods=10)
    aware = naive.tz_localize("America/New_York")
    window = ((date(2021, 1, 6), date(2021, 1, 8)),)
    assert list(runner._window_mask(window, aware)) == list(runner._window_mask(window, naive))


def test_point_in_time_now_runs_and_the_summary_says_it_was_applied(tmp_path, membership_wired):
    """The mode used to refuse. It runs, and the block records that it bit."""
    membership_wired({"ALWAYS": "sp500"})
    directory = go(pit_cfg(tmp_path), label="pit-runs")

    summary = json.loads((directory / "summary.json").read_text())
    assert summary["membership"]["mode"] == "point_in_time"
    assert summary["membership"]["applied"] is True
    assert not traded(directory).empty


def test_the_unrestricted_control_symbol_trades_the_same_either_way(tmp_path, membership_wired):
    """A symbol that was a member throughout must be untouched by the correction.

    Without this the tests below would prove only that something changed, not
    that the *right* thing changed.
    """
    membership_wired({"ALWAYS": "sp500"})
    off = go(runner_cfg(tmp_path), label="always-off")
    on = go(pit_cfg(tmp_path), label="always-on")
    pd.testing.assert_frame_equal(traded(off), traded(on))
    assert entries(on, "ALWAYS") == [EARLY_ENTRY, LATE_ENTRY]


def test_a_symbol_cannot_be_entered_before_its_join_date(tmp_path, membership_wired):
    """JOINER joined on 2021-04-01, between its two signals.

    The signal before that date may not become a trade; the one after it must.
    """
    membership_wired({"JOINER": "sp500"})

    off = go(runner_cfg(tmp_path), label="joiner-off")
    assert entries(off, "JOINER") == [EARLY_ENTRY, LATE_ENTRY]

    on = go(pit_cfg(tmp_path), label="joiner-on")
    assert entries(on, "JOINER") == [LATE_ENTRY]


def test_a_symbol_cannot_be_entered_after_it_leaves_but_its_position_is_managed_out(
    tmp_path, membership_wired
):
    """LEAVER was removed on 2021-03-15, inside its first trade.

    Two claims in one fixture, because they are the two halves of "gates
    entries only":

    * the signal after the removal date may not become a trade;
    * the position that was already open ON that date is **not** force-closed.
      It exits on 2021-04-26 by the time stop, exactly as it would have with no
      correction at all — being dropped from an index is not a sell order.
    """
    membership_wired({"LEAVER": "sp500"})

    off = traded(go(runner_cfg(tmp_path), label="leaver-off"))
    assert list(off["entry_date"]) == [EARLY_ENTRY, LATE_ENTRY]

    directory = go(pit_cfg(tmp_path), label="leaver-on")
    on = traded(directory)
    assert list(on["entry_date"]) == [EARLY_ENTRY]

    # The surviving trade is the uncorrected one, price for price and reason
    # for reason — the exit ladder never learned about membership.
    first = off.iloc[0]
    assert on["exit_date"].iloc[0] == EARLY_EXIT
    assert on["exit_date"].iloc[0] > pd.Timestamp("2021-03-15")
    for column in ("entry_price", "exit_price", "shares", "exit_reason", "pnl"):
        assert on[column].iloc[0] == first[column], column


def test_a_move_between_indices_is_continuous_membership_not_an_arrival(tmp_path, membership_wired):
    """MOVER and NEWCOMER are both S&P 400 members as of 2021-03-16.

    Only one of them *arrived* then. MOVER was in the S&P 600 until the day
    before, so the union of its stints spans the whole period and its earlier
    signal is a legitimate trade. Reading only the index a symbol sits in today
    would delete it — that is the survivorship error this rule exists to avoid.
    """
    membership_wired({"MOVER": "sp400", "NEWCOMER": "sp400"})
    directory = go(pit_cfg(tmp_path), label="mover-on")

    assert entries(directory, "MOVER") == [EARLY_ENTRY, LATE_ENTRY]
    assert entries(directory, "NEWCOMER") == [LATE_ENTRY]


def test_the_unknown_policy_decides_whether_an_undated_symbol_trades_at_all(
    tmp_path, membership_wired
):
    """NODATE has a row but no stated join date, and the policy is the whole story.

    ``exclude`` drops it entirely — the conservative over-correction. ``include``
    back-dates it to the beginning of the data, which reinstates every trade.
    Both directions are asserted, because a policy that only ever moved one way
    would be indistinguishable from a bug.
    """
    membership_wired({"NODATE": "sp600"})

    strict = go(pit_cfg(tmp_path, policy="exclude"), label="nodate-exclude")
    assert traded(strict).empty

    loose = go(pit_cfg(tmp_path, policy="include"), label="nodate-include")
    assert entries(loose, "NODATE") == [EARLY_ENTRY, LATE_ENTRY]

    # `include` is not merely "more trades": it reproduces the uncorrected run.
    off = go(runner_cfg(tmp_path), label="nodate-off")
    pd.testing.assert_frame_equal(traded(off), traded(loose))


def test_a_universe_with_no_eligible_day_writes_an_empty_report_not_an_exception(
    tmp_path, membership_wired
):
    """`exclude` really can empty a whole universe. That is a result, not a crash."""
    membership_wired({"NODATE": "sp600"})
    directory = go(pit_cfg(tmp_path), label="empty-universe")

    assert traded(directory).empty
    for name in REPORT_FILES:
        assert (directory / name).is_file(), name
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["membership"]["applied"] is True
    assert summary["membership"]["symbols_excluded"] == 1
    assert summary["oos"]["trades"] == 0


def test_turning_the_mode_on_moves_the_config_hash(tmp_path, membership_wired):
    """A different universe is a different experiment, so it must not share a hash."""
    off = config_hash(runner_cfg(tmp_path))
    strict = config_hash(pit_cfg(tmp_path, policy="exclude"))
    loose = config_hash(pit_cfg(tmp_path, policy="include"))
    assert len({off, strict, loose}) == 3


def test_a_symbol_with_bars_but_no_membership_window_is_refused(
    tmp_path, membership_wired, monkeypatch
):
    """The one failure mode worth crashing over: a silently shrunken universe.

    A symbol whose bars are simulated but whose eligibility nobody computed
    would be masked to all-False and vanish from the run without a word. The
    runner refuses instead.
    """
    membership_wired({"ALWAYS": "sp500"})
    monkeypatch.setattr("swing.universe.membership_windows", lambda *a, **k: {"OTHER": ()})
    with pytest.raises(ValueError, match="no membership windows"):
        go(pit_cfg(tmp_path), label="pit-mismatch")


def test_an_unreadable_membership_file_stops_an_enforced_run(
    tmp_path, membership_wired, monkeypatch
):
    """The opposite of the rule for the summary block, and deliberately so.

    There the block is provenance and degrading to an ``error`` key is right.
    Here the file IS simulation input: a run labelled point-in-time that quietly
    fell back to the full universe would be the exact lie this feature exists to
    prevent, so the error propagates.
    """
    from swing.universe import UniverseError

    membership_wired({"ALWAYS": "sp500"})

    def broken(*args, **kwargs):
        raise UniverseError("sp500-membership.csv is missing")

    monkeypatch.setattr("swing.universe.membership_windows", broken)
    with pytest.raises(UniverseError, match="sp500-membership.csv is missing"):
        go(pit_cfg(tmp_path), label="pit-broken")


def test_an_unreadable_membership_file_costs_a_sentence_not_a_data_load(
    tmp_path, membership_wired, monkeypatch
):
    """The membership files are read BEFORE a single bar is fetched.

    A real stocks run spends minutes loading 1,500 symbols. Discovering only
    after that that the correction cannot be applied would be a bad trade, and
    it is the reason the window read is hoisted above ``_load_bars`` rather
    than left where the masks are built. The provider is the witness: it must
    never be asked for anything.
    """
    from swing.universe import UniverseError

    provider = membership_wired({"ALWAYS": "sp500"})

    def broken(*args, **kwargs):
        raise UniverseError("sp600-membership.csv is missing")

    monkeypatch.setattr("swing.universe.membership_windows", broken)
    with pytest.raises(UniverseError, match="sp600-membership.csv is missing"):
        go(pit_cfg(tmp_path), label="pit-broken-early")

    assert provider.requested == [], "bars were fetched before the refusal"
    assert not (tmp_path / "reports" / "backtest" / "pit-broken-early").exists()


def test_the_default_mode_never_reads_a_membership_file_for_the_simulation(
    tmp_path, membership_wired, monkeypatch
):
    """With the mode off, nothing on the simulation path touches the files.

    The ``membership`` summary block still reads them — it is written for every
    run — but it catches its own errors. A file that cannot be read must not be
    able to stop a default run, which is the behaviour every report in the repo
    was produced under.
    """
    from swing.universe import UniverseError

    membership_wired({"ALWAYS": "sp500"})

    def broken(*args, **kwargs):
        raise UniverseError("every membership file is missing")

    monkeypatch.setattr("swing.universe.membership_windows", broken)
    monkeypatch.setattr("swing.universe.membership_coverage", broken)

    directory = go(runner_cfg(tmp_path), label="off-with-broken-files")
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["membership"]["error"] == "every membership file is missing"
    assert not traded(directory).empty


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


#: ``config_hash`` of the shipping defaults, recorded from the code as it stood
#: before ``[backtest.tuning_grid]`` existed. Every backtest report in the repo
#: carries this string; if the knob had leaked into the hash, none of them would
#: be comparable with a run made today.
SHIPPING_CONFIG_HASH = "c6782f8db70ba7e61d4715dcf5f32f7c0c269a66ea8d0d02362ef38c0cf46f48"


def test_the_shipping_config_still_hashes_to_what_it_always_did(tmp_path):
    assert config_hash(build_config(tmp_path)) == SHIPPING_CONFIG_HASH, (
        "config_hash for the default configuration changed. If you deliberately changed a "
        "[strategy], [backtest] or [gates] default, update this digest and expect every existing "
        "report to stop matching. If you did not, something has leaked into the hash and every "
        "report written before your change is no longer comparable with one written after it."
    )


def test_the_bounded_date_policy_reaches_the_hash_when_membership_is_enforced(tmp_path):
    """``[universe] membership_bounded`` changes results, so it must change the hash.

    It decides whether a join date a source states only as an upper bound is
    read as that date or as no date at all, and on the shipping membership
    files that moves vouchable exposure from 12,807 member-years to 8,672 and
    symbols with no usable join date from 96 to 350. It lives in ``[universe]``,
    which ``config_hash`` does not otherwise enumerate, so two point-in-time
    runs differing only in this policy used to hash identically — the same
    invisible-dependency failure as the earnings history.
    """
    enforced = {"membership": "point_in_time"}
    strict = build_config(tmp_path, backtest=enforced, universe={"membership_bounded": "unknown"})
    loose = build_config(tmp_path, backtest=enforced, universe={"membership_bounded": "exact"})

    assert strict.universe.membership_bounded == "unknown", "the conservative default moved"
    assert config_hash(strict) != config_hash(loose), (
        "two point-in-time runs reading bounded join dates differently are different "
        "experiments and must not share a config_hash"
    )


def test_the_bounded_date_policy_is_invisible_while_membership_is_off(tmp_path):
    """Inert knobs stay out, exactly as ``membership_unknown`` does.

    With no windows built the policy cannot change what the strategy did, and
    hashing it anyway would retire every report ever written.
    """
    base = build_config(tmp_path)
    flipped = build_config(tmp_path, universe={"membership_bounded": "exact"})

    assert base.backtest.membership == "off"
    assert config_hash(flipped) == config_hash(base) == SHIPPING_CONFIG_HASH


def test_an_unset_or_default_tuning_grid_does_not_move_the_hash(tmp_path):
    """Same rules, same hash: the knob's default must be invisible to provenance."""
    base = build_config(tmp_path)
    spelled_out = build_config(
        tmp_path, backtest={"tuning_grid": {k: list(v) for k, v in TUNING_GRID.items()}}
    )
    assert base.backtest.tuning_grid is None
    assert config_hash(spelled_out) == config_hash(base) == SHIPPING_CONFIG_HASH


@pytest.mark.parametrize(
    "grid",
    [
        {"atr_stop_mult": [1.5, 2.0, 2.5, 3.0, 3.5]},  # the wider-stops hypothesis
        {**{k: list(v) for k, v in TUNING_GRID.items()}, "volume_mult": [1.0, 1.3]},  # narrower
        {"atr_stop_mult": [1.5, 2.0, 2.5]},  # a strict subset of the default
    ],
)
def test_a_custom_tuning_grid_changes_the_hash(tmp_path, grid):
    """A different search is a different experiment, and must not look identical."""
    base = build_config(tmp_path)
    assert config_hash(build_config(tmp_path, backtest={"tuning_grid": grid})) != config_hash(base)


def test_the_membership_defaults_do_not_move_the_hash(tmp_path):
    """Same rules, same hash: a knob whose default is 'behave as before' is invisible.

    Every report in this repo was written before ``[backtest] membership``
    existed, and all of them describe the same experiment — today's index
    membership applied to all of history. Leaking the new keys into the hash
    would have made every one of them incomparable with a run made today.
    """
    base = build_config(tmp_path)
    spelled_out = build_config(
        tmp_path, backtest={"membership": "off", "membership_unknown": "exclude"}
    )
    assert (base.backtest.membership, base.backtest.membership_unknown) == ("off", "exclude")
    assert config_hash(spelled_out) == config_hash(base) == SHIPPING_CONFIG_HASH


def test_the_unknown_policy_cannot_move_the_hash_while_the_mode_is_off(tmp_path):
    """It is inert with the mode off, so it cannot have changed what happened."""
    base = build_config(tmp_path)
    loosened = build_config(tmp_path, backtest={"membership_unknown": "include"})
    assert config_hash(loosened) == config_hash(base) == SHIPPING_CONFIG_HASH


@pytest.mark.parametrize("unknown", ["exclude", "include"])
def test_turning_point_in_time_on_changes_the_hash(tmp_path, unknown):
    """A different universe is a different experiment and must not look identical."""
    base = build_config(tmp_path)
    corrected = build_config(
        tmp_path, backtest={"membership": "point_in_time", "membership_unknown": unknown}
    )
    assert config_hash(corrected) != config_hash(base)


def test_the_two_unknown_policies_hash_differently(tmp_path):
    """With the mode on, the policy decides the universe, so it is part of the rules."""
    strict = build_config(tmp_path, backtest={"membership": "point_in_time"})
    loose = build_config(
        tmp_path, backtest={"membership": "point_in_time", "membership_unknown": "include"}
    )
    assert config_hash(strict) != config_hash(loose)


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


def test_a_walkforward_summary_records_the_grid_it_actually_searched(tmp_path, wf_wired):
    """A report must never be readable as having searched the default when it did not."""
    cfg = runner_cfg(tmp_path, backtest={"is_years": 1, "oos_years": 1})
    directory = go(
        cfg, label="recorded", walkforward=True, start=date(2021, 6, 1), end=date(2025, 5, 31)
    )
    summary = json.loads((directory / "summary.json").read_text())
    # ``wf_wired`` stands in a two-parameter grid for the 81-point default; the
    # summary reports the grid the tuner was handed, not the one in the source.
    assert summary["tuning_grid"] == {"atr_stop_mult": [1.5, 2.5], "donchian_window": [15, 25]}


def test_a_configured_tuning_grid_reaches_the_run_and_the_report(tmp_path, wf_wired):
    """End to end: config -> tuner -> chosen parameters -> summary.json."""
    grid = {"atr_stop_mult": [4.0, 5.0], "donchian_window": [11, 13]}
    cfg = runner_cfg(tmp_path, backtest={"is_years": 1, "oos_years": 1, "tuning_grid": grid})
    directory = go(
        cfg, label="configured", walkforward=True, start=date(2021, 6, 1), end=date(2025, 5, 31)
    )
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["tuning_grid"] == grid
    assert summary["windows"]
    for fold in summary["windows"]:
        assert set(fold["params"]) == {"atr_stop_mult", "donchian_window"}
        assert fold["params"]["atr_stop_mult"] in grid["atr_stop_mult"]
        assert fold["params"]["donchian_window"] in grid["donchian_window"]
    # And the report says it was a different experiment from the standard run.
    assert summary["config_hash"] != SHIPPING_CONFIG_HASH


def test_two_runs_of_a_configured_grid_are_byte_identical(tmp_path, wf_wired):
    """AC9 still holds with the new key in the payload."""
    cfg = runner_cfg(
        tmp_path,
        backtest={"is_years": 1, "oos_years": 1, "tuning_grid": {"atr_stop_mult": [1.5, 3.0]}},
    )
    kwargs = {"walkforward": True, "start": date(2021, 6, 1), "end": date(2025, 5, 31)}
    first = go(cfg, label="once", **kwargs)
    second = go(cfg, label="twice", **kwargs)

    assert (first / "trades.csv").read_bytes() == (second / "trades.csv").read_bytes()
    assert (first / "equity.csv").read_bytes() == (second / "equity.csv").read_bytes()
    payloads = [json.loads((d / "summary.json").read_text()) for d in (first, second)]
    for payload in payloads:
        payload.pop("label")  # the one legitimate difference between the two
    assert payloads[0] == payloads[1]
    assert payloads[0]["tuning_grid"] == {"atr_stop_mult": [1.5, 3.0]}


def test_a_non_walkforward_run_records_no_tuning_grid(tmp_path, wired):
    """Nothing was tuned, so the grid block is empty — like ``objective``."""
    directory = go(runner_cfg(tmp_path), label="untuned", walkforward=False)
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["tuning_grid"] == {}
    assert summary["objective"] == ""


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


# ---------------------------------------------------------------------------
# BUG-042 — a label is one directory name, and latest.json has one home
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "sub/run-1",  # relocates latest.json; the real one goes stale
        "sub/ablate-x",  # buries the prefix so the ablation guard never sees it
        "../escaped",  # writes outside the reports tree entirely
        "/absolute",
        "with space",
        "trailing/",
        "",  # not the default: an explicit empty label is a mistake
        ".",
        "..",
    ],
)
def test_a_label_that_is_not_a_directory_name_is_refused(tmp_path, wired, label):
    cfg = runner_cfg(tmp_path)
    with pytest.raises(ValueError, match="not usable as a directory name"):
        go(cfg, label=label)


def test_the_ablate_guard_cannot_be_bypassed_with_a_path_separator(tmp_path, wired):
    """BUG-042 repro: ``--label sub/ablate-x`` slipped past ``startswith('ablate')``."""
    cfg = runner_cfg(tmp_path)
    go(cfg, label="baseline")
    baseline_bytes = latest_path(cfg).read_bytes()

    with pytest.raises(ValueError, match="not usable as a directory name"):
        go(cfg, label=f"sub/{ABLATION_PREFIX}-x")

    assert latest_path(cfg).read_bytes() == baseline_bytes
    assert not (cfg.paths.reports_dir / "backtest" / "sub").exists()


def test_a_bad_label_is_refused_before_any_work_happens(tmp_path, wired):
    """A sentence, not forty minutes of simulation followed by a sentence."""
    cfg = runner_cfg(tmp_path)
    with pytest.raises(ValueError, match="not usable as a directory name"):
        go(cfg, label="reports/../../etc/run")
    assert wired.requested == []


@pytest.mark.parametrize(
    "label", ["baseline", "backtest-20240101-120000", "ablate-no-regime", "run_1.v2", "A"]
)
def test_ordinary_labels_are_accepted(tmp_path, wired, label):
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label=label)
    assert directory.name == label
    assert directory.parent == cfg.paths.reports_dir / "backtest"


def test_latest_json_is_written_where_the_gate_looks_for_it(tmp_path, wired):
    """BUG-042: the reference file location comes from the gate, never from the run dir."""
    from swing.backtest.runner import write_report

    cfg = runner_cfg(tmp_path)
    summary = json.loads((go(cfg, label="source") / "summary.json").read_text())
    latest_path(cfg).unlink()

    elsewhere = tmp_path / "somewhere" / "else" / "deep"
    write_report(cfg, elsewhere, summary, pd.DataFrame(), pd.DataFrame())

    assert latest_path(cfg).is_file()
    assert json.loads(latest_path(cfg).read_text())["label"] == "source"
    assert not (elsewhere.parent / "latest.json").exists()


# ---------------------------------------------------------------------------
# BUG-043 — the headline names the period the numbers cover
# ---------------------------------------------------------------------------


@pytest.fixture
def wf_wired(monkeypatch):
    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: FakeProvider(wf_bars()))
    monkeypatch.setattr(
        "swing.backtest.walkforward.TUNING_GRID",
        {"atr_stop_mult": (1.5, 2.5), "donchian_window": (15, 25)},
    )


def walkforward_run(tmp_path, **overrides):
    cfg = runner_cfg(tmp_path, backtest={"is_years": 1, "oos_years": 1})
    directory = go(
        cfg,
        label="span",
        walkforward=True,
        start=date(2021, 6, 1),
        end=date(2025, 5, 31),
        **overrides,
    )
    return cfg, directory, json.loads((directory / "summary.json").read_text())


def test_a_walkforward_summary_records_the_out_of_sample_span(tmp_path, wf_wired):
    """BUG-043: 'Period: 2010-01-01 to 2026-08-18' sat above metrics covering
    2013-01-02 to 2025-12-31 — three years of warm-up and eight unused months
    presented as measured record."""
    _cfg, _directory, summary = walkforward_run(tmp_path)

    assert summary["oos_start"] == summary["windows"][0]["oos_start"]
    assert summary["oos_end"] == summary["windows"][-1]["oos_end"]
    # The measured span is strictly inside the data span, which is the point.
    assert summary["start"] < summary["oos_start"]
    assert summary["oos_end"] <= summary["end"]


def test_the_markdown_headline_names_the_measured_span_not_the_data_span(tmp_path, wf_wired):
    _cfg, directory, summary = walkforward_run(tmp_path)
    text = (directory / "report.md").read_text()

    assert f"**Measured period**: {summary['oos_start']} to {summary['oos_end']}" in text
    # The data span is still recorded — demoted, not deleted.
    assert f"**Data span**: {summary['start']} to {summary['end']}" in text
    assert f"covering {summary['oos_start']} to {summary['oos_end']}" in text


def test_the_html_headline_names_the_measured_span(tmp_path, wf_wired):
    _cfg, directory, summary = walkforward_run(tmp_path)
    html = (directory / "report.html").read_text()

    assert f"{summary['oos_start']} to {summary['oos_end']}" in html
    assert "<td>Data span</td>" in html
    assert f"<td>{summary['start']} to {summary['end']}</td>" in html


def test_print_latest_separates_the_measured_span_from_the_data_span(tmp_path, wf_wired, capsys):
    from swing.backtest.report import print_latest

    cfg, _directory, summary = walkforward_run(tmp_path)
    print_latest(cfg)
    out = capsys.readouterr().out

    assert f"measured   {summary['oos_start']} to {summary['oos_end']}" in out
    assert f"data span  {summary['start']} to {summary['end']}" in out
    assert f"Out-of-sample results ({summary['oos_start']} to {summary['oos_end']})" in out


def test_a_non_walkforward_report_falls_back_to_the_simulated_window(tmp_path, wired):
    """With no folds there is no OOS span, and the headline says so honestly."""
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="in-sample", walkforward=False)
    summary = json.loads((directory / "summary.json").read_text())

    assert "oos_start" not in summary
    text = (directory / "report.md").read_text()
    assert f"**Measured period**: {summary['start']} to {summary['end']}" in text


# ---------------------------------------------------------------------------
# DEBT-016 — the chart is not titled "out-of-sample" on an in-sample run
# ---------------------------------------------------------------------------


def test_an_in_sample_report_does_not_call_its_chart_out_of_sample(tmp_path, wired):
    cfg = runner_cfg(tmp_path)
    html = (go(cfg, label="in-sample-chart", walkforward=False) / "report.html").read_text()
    assert "NOT a walk-forward run" in html
    assert 'alt="Full-period in-sample equity curve"' in html
    assert "Out-of-sample equity curve" not in html


def test_a_walkforward_report_does_call_its_chart_out_of_sample(tmp_path, wf_wired):
    _cfg, directory, _summary = walkforward_run(tmp_path)
    assert 'alt="Out-of-sample equity curve"' in (directory / "report.html").read_text()


# ---------------------------------------------------------------------------
# A12 / BUG-036 — the earnings divergence is declared, not assumed away
# ---------------------------------------------------------------------------


#: ``INSTRUMENTS`` has BBB down as an ETF, and an ETF never announces earnings,
#: so it is exempt from the coverage denominator. Coverage arithmetic needs two
#: symbols that can actually announce.
STOCK_INSTRUMENTS = [
    Instrument(symbol="AAA", name="Alpha", kind="stock", source="sp500"),
    Instrument(symbol="BBB", name="Beta", kind="stock", source="sp500"),
]


def with_earnings(monkeypatch, history, instruments=None):
    """Wire a provider whose ``earnings_history`` returns ``history``.

    ``history`` is either a mapping to serve verbatim or a callable taking the
    requested symbol list. Returns the provider so a test can read back what
    was asked of it.
    """
    provider = FakeProvider(fake_universe())
    provider.earnings_history = lambda symbols, start, end: (
        history(list(symbols)) if callable(history) else dict(history)
    )
    wanted = list(INSTRUMENTS if instruments is None else instruments)
    monkeypatch.setattr("swing.universe.load", lambda cfg: wanted)
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, _p=provider, **kw: _p)
    return provider


def test_a_provider_without_earnings_history_declares_the_divergence(tmp_path, wired):
    """The FakeProvider only knows the next upcoming date, like yfinance did."""
    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="no-history")
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["earnings"]["source"] == runner.EARNINGS_FROM_UPCOMING
    assert summary["earnings_blackout_simulated"] is False
    assert "Earnings blackout not simulated" in (directory / "report.md").read_text()
    assert "Earnings blackout NOT simulated" in (directory / "report.html").read_text()


def test_a_provider_with_earnings_history_is_used_and_not_flagged(tmp_path, monkeypatch):
    """A12: when real announcement dates exist the runner passes SEQUENCES through."""
    seen: dict[str, object] = {}

    def earnings_history(symbols):
        seen["symbols"] = list(symbols)
        return {symbol: (date(2021, 3, 1), date(2021, 6, 1)) for symbol in symbols}

    with_earnings(monkeypatch, earnings_history)

    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="with-history")
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["earnings"]["source"] == runner.EARNINGS_FROM_HISTORY
    # Every requested symbol had dates, so the strict flag is legitimately true.
    assert summary["earnings_blackout_simulated"] is True
    assert seen["symbols"] == ["AAA", "BBB"]
    assert "Earnings blackout not simulated" not in (directory / "report.md").read_text()


def test_a_broken_earnings_history_endpoint_still_does_not_stop_the_run(tmp_path, monkeypatch):
    provider = FakeProvider(fake_universe())

    def boom(symbols, start, end):
        raise RuntimeError("the earnings endpoint is having a day")

    provider.earnings_history = boom
    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: provider)

    cfg = runner_cfg(tmp_path)
    directory = go(cfg, label="history-broken")
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["earnings"]["source"] == runner.EARNINGS_UNAVAILABLE
    assert summary["earnings_blackout_simulated"] is False
    assert pd.read_csv(directory / "trades.csv").shape[0] > 0


# ---------------------------------------------------------------------------
# REPRO-1 — the earnings dependency is visible in the record
#
# The hole these close: earnings dates feed `earnings_blackout`, which is part
# of the entry gate, and `config_hash` / `data_hash` / `code_ref` could all
# match while the dates underneath had moved. It cost a real comparison — two
# runs of one control, 685 trades at PF 1.0663 against 717 at PF 1.0523, every
# hash identical, because the cache refetched between them.
# ---------------------------------------------------------------------------


def test_the_summary_records_a_hash_of_the_earnings_it_read(tmp_path, monkeypatch):
    with_earnings(monkeypatch, lambda symbols: dict.fromkeys(symbols, (date(2021, 3, 1),)))
    summary = json.loads(
        (go(runner_cfg(tmp_path), label="earnings-hashed") / "summary.json").read_text()
    )

    digest = summary["earnings_hash"]
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    # Beside the other three, not instead of them: all four have to match for
    # two reports to be the same experiment.
    assert len(summary["data_hash"]) == len(summary["config_hash"]) == 64


def test_two_runs_over_the_same_earnings_hash_identically(tmp_path, monkeypatch):
    """The claim AC9 makes, now checkable for the fourth input as well."""
    digests = []
    for label in ("repro-a", "repro-b"):
        with_earnings(monkeypatch, lambda symbols: dict.fromkeys(symbols, (date(2021, 3, 1),)))
        directory = go(runner_cfg(tmp_path), label=label)
        digests.append(json.loads((directory / "summary.json").read_text())["earnings_hash"])

    assert digests[0] == digests[1]


def test_the_identity_hashes_now_pin_the_earnings_history(tmp_path, monkeypatch):
    """The inversion of ``test_the_identity_triple_does_not_pin_...``, which this replaces.

    That test existed to hold a known hole open until someone closed it, and
    said in its own docstring that whoever did should assert the opposite. So:
    two runs whose *only* difference is one announcement date still agree on
    ``config_hash`` and ``data_hash`` — and now disagree on ``earnings_hash``,
    which is the number that explains why the trades differ.
    """
    summaries = []
    for label, announcements in (
        ("earnings-a", (date(2021, 3, 1),)),
        ("earnings-b", (date(2021, 4, 15),)),
    ):
        with_earnings(monkeypatch, lambda symbols, a=announcements: dict.fromkeys(symbols, a))
        directory = go(runner_cfg(tmp_path), label=label)
        summaries.append((json.loads((directory / "summary.json").read_text()), traded(directory)))

    (first, trades_a), (second, trades_b) = summaries
    assert first["data_hash"] == second["data_hash"]
    assert first["config_hash"] == second["config_hash"]
    # The trades differ, as they always did...
    assert not trades_a.equals(trades_b)
    # ...and something in the record finally says so.
    assert first["earnings_hash"] != second["earnings_hash"], (
        "the earnings history is leaking past the identity hashes again: two runs read "
        "different announcement dates, traded differently, and recorded the same provenance"
    )


#: A value the strategy layer accepts and the fingerprint refuses, which is the
#: whole reason the hash is computed defensively. ``rules._announcement_days``
#: normalises with ``pd.Timestamp``, which reads a large integer as nanoseconds
#: since the epoch — this one is 2020-09-13 — while ``as_date`` refuses to
#: guess. So a provider handing back epoch integers produces a run that
#: simulates a real blackout and a digest that cannot be computed.
EPOCH_NANOS = 1_600_000_000_000_000_000


def test_an_unreadable_earnings_payload_costs_the_hash_and_nothing_else(tmp_path, monkeypatch):
    """A digest that cannot be computed must not destroy a completed simulation.

    ``earnings_fingerprint`` refuses a value it cannot read as a date, which is
    correct for a digest and would be a terrible way to end a forty-minute run
    — earnings are optional everywhere else in the runner. The sentinel says
    so in the one place that matters, and the report is written either way.
    """
    with_earnings(monkeypatch, lambda symbols: dict.fromkeys(symbols, [EPOCH_NANOS]))
    directory = go(runner_cfg(tmp_path), label="earnings-garbage")
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["earnings_hash"] == runner.EARNINGS_HASH_UNAVAILABLE
    assert len(summary["earnings_hash"]) != 64, "the sentinel must not look like a digest"
    assert pd.read_csv(directory / "trades.csv").shape[0] > 0
    # Unreadable reads as "no dates": the conservative direction for a coverage
    # figure, and the same reading the digest took. It does mean this run's
    # block understates a blackout the engine really did apply — the price of
    # not letting a diagnostic guess.
    assert summary["earnings"]["symbols_with_dates"] == 0


# ---------------------------------------------------------------------------
# REPRO-2 — coverage, because "the blackout applied" was not a true sentence
# ---------------------------------------------------------------------------


def test_the_summary_counts_how_much_of_the_universe_the_blackout_covered(tmp_path, monkeypatch):
    """One of two symbols has dates, so the run must say 50% rather than "yes".

    The denominator is the set of symbols the run *asked* about, not the keys
    the provider happened to answer with — ``BBB`` is absent from the reply and
    ``ZZZ`` was never requested. Counting the reply instead would turn a
    coverage problem into a perfect score.
    """
    with_earnings(
        monkeypatch,
        {"AAA": (date(2021, 3, 1), date(2021, 6, 1)), "ZZZ": (date(2021, 4, 1),)},
        instruments=STOCK_INSTRUMENTS,
    )
    summary = json.loads(
        (go(runner_cfg(tmp_path), label="coverage-half") / "summary.json").read_text()
    )
    block = summary["earnings"]

    assert block["symbols_requested"] == 2
    assert block["symbols_applicable"] == 2
    assert block["symbols_with_dates"] == 1
    assert block["symbols_without_dates"] == 1
    assert block["coverage_pct"] == 50.0
    # ZZZ's date is not in the universe and must not inflate the total.
    assert block["announcements"] == 2


def test_a_cold_cache_is_no_longer_reported_as_a_simulated_blackout(tmp_path, monkeypatch):
    """The bug in one assertion.

    A provider that *has* a history endpoint and returns nothing from it used
    to stamp ``earnings_blackout_simulated: true`` and render no warning at
    all, because the old flag recorded the endpoint's existence rather than its
    output. Every entry in the run went through the gate unblocked.
    """
    with_earnings(monkeypatch, {}, instruments=STOCK_INSTRUMENTS)
    directory = go(runner_cfg(tmp_path), label="coverage-cold")
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["earnings"]["source"] == runner.EARNINGS_FROM_HISTORY
    assert summary["earnings"]["symbols_with_dates"] == 0
    assert summary["earnings_blackout_simulated"] is False
    assert "Earnings blackout not simulated" in (directory / "report.md").read_text()
    assert "Earnings blackout NOT simulated" in (directory / "report.html").read_text()


def test_partial_coverage_is_reported_as_a_degree_not_as_a_banner(tmp_path, monkeypatch):
    """Half a universe covered is neither "applied" nor "not available".

    The boolean deliberately does not try to carry this: five S&P names have no
    free announcement history and never will, so an all-or-nothing flag would
    warn on every stocks run forever and stop meaning anything. The degree
    lives in the block, where a reader can weigh it.
    """
    with_earnings(monkeypatch, {"AAA": (date(2021, 3, 1),)}, instruments=STOCK_INSTRUMENTS)
    directory = go(runner_cfg(tmp_path), label="coverage-partial")
    summary = json.loads((directory / "summary.json").read_text())

    assert summary["earnings"]["coverage_pct"] == 50.0
    assert summary["earnings"]["symbols_without_dates"] == 1
    assert summary["earnings_blackout_simulated"] is True


@pytest.mark.parametrize("nothing", [None, (), []])
def test_the_spellings_of_no_dates_all_count_as_uncovered(tmp_path, monkeypatch, nothing):
    """``None``, ``()`` and absent are one state, because the blackout cannot tell them apart."""
    with_earnings(
        monkeypatch,
        {"AAA": (date(2021, 3, 1),), "BBB": nothing},
        instruments=STOCK_INSTRUMENTS,
    )
    summary = json.loads(
        (go(runner_cfg(tmp_path), label="coverage-empty") / "summary.json").read_text()
    )

    assert summary["earnings"]["symbols_with_dates"] == 1
    assert summary["earnings"]["coverage_pct"] == 50.0


def test_etfs_are_exempt_from_the_coverage_denominator(tmp_path, monkeypatch):
    """An ETF does not announce earnings, so it is not a coverage hole.

    BBB is an ETF in the shared fixture and gets no dates. Counting it as
    uncovered would report 50% for a run whose every announcing symbol was
    fully covered.
    """
    with_earnings(monkeypatch, {"AAA": (date(2021, 3, 1),)})
    summary = json.loads(
        (go(runner_cfg(tmp_path), label="coverage-etf-exempt") / "summary.json").read_text()
    )
    block = summary["earnings"]

    assert block["symbols_requested"] == 2
    assert block["symbols_exempt"] == 1
    assert block["symbols_applicable"] == 1
    assert block["coverage_pct"] == 100.0
    assert summary["earnings_blackout_simulated"] is True


def test_an_etf_only_run_is_not_warned_about_earnings_it_could_never_have(tmp_path, wired):
    """The ETF-only run is the survivorship lower bound (§7.4); a false alarm on it is its own lie.

    Nothing in the universe announces earnings, so every symbol that needed a
    blackout got one — vacuously, but truthfully. The report must stay quiet
    rather than claim the results are optimistic for want of data that does not
    exist.
    """
    directory = go(runner_cfg(tmp_path), label="coverage-etf-only", universe="etf")
    summary = json.loads((directory / "summary.json").read_text())
    block = summary["earnings"]

    assert block["symbols_applicable"] == 0
    assert block["symbols_without_dates"] == 0
    assert summary["earnings_blackout_simulated"] is True
    assert "Earnings blackout not simulated" not in (directory / "report.md").read_text()
    assert "Earnings blackout NOT simulated" not in (directory / "report.html").read_text()


def test_full_coverage_reads_as_a_simulated_blackout(tmp_path, monkeypatch):
    with_earnings(
        monkeypatch,
        lambda symbols: dict.fromkeys(symbols, (date(2021, 3, 1),)),
        instruments=STOCK_INSTRUMENTS,
    )
    summary = json.loads(
        (go(runner_cfg(tmp_path), label="coverage-full") / "summary.json").read_text()
    )

    assert summary["earnings"]["coverage_pct"] == 100.0
    assert summary["earnings"]["symbols_without_dates"] == 0
    assert summary["earnings_blackout_simulated"] is True


def test_upcoming_dates_are_never_a_simulated_blackout_however_complete(tmp_path, monkeypatch):
    """A12 survives the rewrite: 100% coverage of *next* dates still blocks no historical bar."""
    provider = FakeProvider(fake_universe())
    provider.earnings_dates = lambda symbols: dict.fromkeys(symbols, date(2026, 3, 1))
    monkeypatch.setattr("swing.universe.load", lambda cfg: list(INSTRUMENTS))
    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: provider)

    summary = json.loads(
        (go(runner_cfg(tmp_path), label="coverage-upcoming") / "summary.json").read_text()
    )

    assert summary["earnings"]["source"] == runner.EARNINGS_FROM_UPCOMING
    assert summary["earnings"]["coverage_pct"] == 100.0
    assert summary["earnings_blackout_simulated"] is False


def test_earnings_coverage_and_the_fingerprint_agree_on_what_a_date_is(tmp_path):
    """The two readings must not drift apart.

    ``earnings_block`` counts coverage and ``earnings_fingerprint`` digests the
    dates, and they are separate implementations of "what counts as a usable
    announcement". Pinned behaviourally rather than by importing the data
    layer's private helper: anything the block counts as uncovered must also be
    invisible to the digest.
    """
    from swing.data import earnings_fingerprint

    universe = ["AAA", "BBB"]
    for nothing in (None, (), [pd.NaT, None]):
        block = runner.earnings_block(
            universe, {"AAA": (date(2021, 3, 1),), "BBB": nothing}, runner.EARNINGS_FROM_HISTORY
        )
        assert block["symbols_with_dates"] == 1
        assert earnings_fingerprint(universe, {"AAA": (date(2021, 3, 1),), "BBB": nothing}) == (
            earnings_fingerprint(universe, {"AAA": (date(2021, 3, 1),)})
        )


def test_earnings_coverage_deduplicates_repeated_announcements(tmp_path):
    """A vendor repeating a quarter is one announcement, matching the digest's reading."""
    block = runner.earnings_block(
        ["AAA"],
        {"AAA": [date(2021, 3, 1), pd.Timestamp("2021-03-01"), "2021-03-01", date(2021, 6, 1)]},
        runner.EARNINGS_FROM_HISTORY,
    )
    assert block["announcements"] == 2
    assert block["symbols_with_dates"] == 1


def test_an_empty_universe_reports_zero_coverage_rather_than_dividing_by_it(tmp_path):
    block = runner.earnings_block([], {}, runner.EARNINGS_UNAVAILABLE)
    assert block["symbols_requested"] == 0
    assert block["symbols_applicable"] == 0
    assert block["coverage_pct"] == 0.0


def test_a_symbol_missing_from_the_etf_map_stays_in_the_denominator(tmp_path):
    """The conservative reading: unknown kind counts as something that can announce."""
    block = runner.earnings_block(
        ["AAA", "BBB"], {"AAA": (date(2021, 3, 1),)}, runner.EARNINGS_FROM_HISTORY, is_etf={}
    )
    assert block["symbols_applicable"] == 2
    assert block["symbols_without_dates"] == 1


# ---------------------------------------------------------------------------
# REPRO-2 — an unpinned window is the mechanism, so the runner says so
# ---------------------------------------------------------------------------


def test_a_walkforward_run_with_no_pinned_end_warns_that_it_is_not_comparable(
    tmp_path, wf_wired, caplog
):
    """``end`` defaulting to today collapses the settled-history TTL to its floor.

    Not a behaviour change — the default is deliberately left alone — but the
    hazard is the one that produced the REPRO-1 divergence, so it is stated at
    the point it happens rather than only in the docs.
    """
    cfg = runner_cfg(tmp_path, backtest={"is_years": 1, "oos_years": 1})
    assert cfg.backtest.end is None
    with caplog.at_level("WARNING", logger="swing.backtest.runner"):
        go(cfg, label="unpinned", walkforward=True, start=date(2021, 6, 1), end=None)

    assert "backtest.end" in caplog.text
    assert "earnings" in caplog.text


def test_a_pinned_end_does_not_warn(tmp_path, wf_wired, caplog):
    cfg = runner_cfg(tmp_path, backtest={"is_years": 1, "oos_years": 1})
    with caplog.at_level("WARNING", logger="swing.backtest.runner"):
        go(cfg, label="pinned", walkforward=True, start=date(2021, 6, 1), end=date(2025, 5, 31))

    assert "not safely comparable" not in caplog.text


def test_a_non_walkforward_run_is_not_nagged(tmp_path, wired, caplog):
    """The warning is scoped to the runs people compare: gate references and ablations."""
    with caplog.at_level("WARNING", logger="swing.backtest.runner"):
        go(runner_cfg(tmp_path), label="quick-look", end=None)

    assert "not safely comparable" not in caplog.text


# ---------------------------------------------------------------------------
# LEAK-006 — a failed chart render must not strand the figure
# ---------------------------------------------------------------------------


def test_a_failing_chart_render_does_not_leak_the_figure(monkeypatch):
    """LEAK-006: ``plt.close`` sat after ``savefig`` rather than in a ``finally``.

    A figure that pyplot still has registered is a figure that is still
    allocated — harmless for a one-shot CLI, a slow leak for anything
    long-lived.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from swing.backtest.report import _equity_chart

    equity = pd.DataFrame(
        {"equity": [1.0, 2.0], "drawdown": [0.0, 0.0]},
        index=pd.bdate_range("2020-01-01", periods=2),
    )
    plt.close("all")
    assert plt.get_fignums() == []

    def exploding_savefig(self, *args, **kwargs):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(matplotlib.figure.Figure, "savefig", exploding_savefig)
    with pytest.raises(RuntimeError, match="the disk is full"):
        _equity_chart(equity)

    assert plt.get_fignums() == []  # before the fix this was [1]


def test_a_successful_chart_render_leaves_no_figure_either(tmp_path, wired):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.close("all")
    go(runner_cfg(tmp_path), label="no-leak")
    assert plt.get_fignums() == []
