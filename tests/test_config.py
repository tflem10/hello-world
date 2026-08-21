"""Tests for FROZEN CONTRACT 1 — swing.config.

Two jobs here. First, pin the contract: every section, attribute name and
default value that other work packages code against. Second, prove that bad
input produces a readable sentence rather than a stack trace.
"""

from __future__ import annotations

import dataclasses
import math
import tomllib
from datetime import date, timedelta
from pathlib import Path

import pytest

from swing.config import (
    MAX_LOOKBACK_BARS,
    MAX_MOMENTUM_LOOKBACK_BARS,
    MAX_TUNING_COMBINATIONS,
    TUNABLE_PARAMS,
    AccountCfg,
    AlertsCfg,
    BacktestCfg,
    Config,
    ConfigError,
    DataCfg,
    ExecutionCfg,
    GatesCfg,
    PathsCfg,
    RegimeCfg,
    ScheduleCfg,
    SchwabCfg,
    StrategyCfg,
    UniverseCfg,
    config_search_paths,
    find_example_config,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config.example.toml"


# ---------------------------------------------------------------------------
# the contract itself
# ---------------------------------------------------------------------------


def test_config_has_exactly_the_twelve_contract_sections() -> None:
    expected = {
        "account": AccountCfg,
        "universe": UniverseCfg,
        "data": DataCfg,
        "strategy": StrategyCfg,
        "regime": RegimeCfg,
        "backtest": BacktestCfg,
        "gates": GatesCfg,
        "alerts": AlertsCfg,
        "schwab": SchwabCfg,
        "execution": ExecutionCfg,
        "schedule": ScheduleCfg,
        "paths": PathsCfg,
    }
    cfg = Config()
    assert {f.name for f in dataclasses.fields(Config)} == set(expected)
    for name, cls in expected.items():
        assert isinstance(getattr(cfg, name), cls)


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        (
            "account",
            {"equity": 100.0, "risk_pct": 2.5, "max_positions": 4, "max_position_pct": 25.0},
        ),
        (
            "universe",
            {"sp500": True, "sp400": True, "sp600": True, "etfs": True, "extra_symbols": ()},
        ),
        (
            "data",
            {
                "provider": "yfinance",
                "start_date": date(2010, 1, 1),
                "retries": 3,
                "retry_backoff": 0.5,
                "download_batch": 200,
            },
        ),
        (
            "strategy",
            {
                "min_price": 5.0,
                "min_dollar_volume": 5_000_000.0,
                "sma_fast": 50,
                "sma_mid": 150,
                "sma_slow": 200,
                "sma_slow_rising_days": 21,
                "min_above_low_mult": 1.25,
                "max_below_high_pct": 25.0,
                "adx_min": 20.0,
                "donchian_window": 20,
                "breakout_proximity_pct": 2.0,
                "volume_mult": 1.3,
                "volume_avg_window": 50,
                "atr_window": 14,
                "atr_stop_mult": 2.0,
                "chandelier_mult": 3.0,
                "time_stop_days": 40,
                "earnings_blackout_days": 10,
                "mom_weight_126": 0.6,
                "mom_weight_63": 0.4,
                "mom_skip_days": 5,
                "rsi2_enabled": False,
                "fundamentals_filter": True,
            },
        ),
        ("regime", {"enabled": True, "symbol": "SPY", "sma_window": 200}),
        (
            "backtest",
            {
                "start": date(2010, 1, 1),
                "end": None,
                "slippage_bps": 5.0,
                "spread_atr_frac": 0.05,
                "is_years": 3,
                "oos_years": 1,
                "initial_equity": 10_000.0,
            },
        ),
        ("gates", {"min_profit_factor": 1.3, "max_drawdown_pct": 35.0, "min_trades": 30}),
        (
            "alerts",
            {
                "ntfy_topic": "",
                "smtp_host": "",
                "smtp_port": 587,
                "smtp_user": "",
                "smtp_password": "",
                "email_to": "",
                "sms_gateway_address": "",
                "macos_notify": True,
            },
        ),
        (
            "schwab",
            {
                "api_key": "",
                "app_secret": "",
                "callback_url": "https://127.0.0.1:8182",
                "account_index": 0,
            },
        ),
        (
            "execution",
            {
                "enabled": False,
                "autopilot": False,
                "max_orders_per_day": 3,
                "max_new_exposure_pct": 50.0,
                "max_quote_drift_atr": 1.0,
                "max_quote_drift_pct": 3.0,
            },
        ),
        (
            "schedule",
            {"scan_time": "17:30", "confirm_time": "09:00", "timezone": "America/New_York"},
        ),
    ],
)
def test_contract_defaults(section: str, expected: dict) -> None:
    actual = getattr(Config(), section)
    for key, value in expected.items():
        assert getattr(actual, key) == value, f"{section}.{key}"


def test_path_defaults_live_under_the_home_directory() -> None:
    cfg = Config()
    assert cfg.data.cache_dir == Path.home() / ".swing" / "cache"
    assert cfg.paths.state_dir == Path.home() / ".swing"
    assert cfg.paths.reports_dir == Path("reports")
    assert cfg.schwab.token_path == Path.home() / ".swing" / "schwab_token.json"


def test_sections_are_frozen() -> None:
    cfg = Config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.account.equity = 1.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.strategy.sma_slow = 10  # type: ignore[misc]


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_an_explicit_file_and_overrides_only_what_is_given(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.toml",
        """
        [account]
        equity = 25000
        risk_pct = 1.0

        [universe]
        sp400 = false
        extra_symbols = ["brk.b", " gld "]

        [data]
        provider = "schwab"
        start_date = 2015-06-01
        cache_dir = "~/somewhere/cache"
        """,
    )
    cfg = load_config(path)

    assert cfg.account.equity == 25000.0
    assert isinstance(cfg.account.equity, float)  # int in TOML, float in the contract
    assert cfg.account.risk_pct == 1.0
    assert cfg.account.max_positions == 4  # untouched default
    assert cfg.universe.sp400 is False
    assert cfg.universe.sp500 is True
    assert cfg.universe.extra_symbols == ("BRK.B", "GLD")  # upper-cased and stripped
    assert cfg.data.provider == "schwab"
    assert cfg.data.start_date == date(2015, 6, 1)
    assert cfg.data.cache_dir == Path.home() / "somewhere" / "cache"  # ~ expanded


def test_adx_min_zero_disables_the_filter(tmp_path: Path) -> None:
    """Ablation sweeps switch the ADX filter off with 0.0, which must be legal."""
    assert StrategyCfg(adx_min=0.0).adx_min == 0.0

    cfg = load_config(_write(tmp_path / "c.toml", "[strategy]\nadx_min = 0.0\n"))
    assert cfg.strategy.adx_min == 0.0

    # written as a bare integer it still loads, as a float
    cfg = load_config(_write(tmp_path / "c2.toml", "[strategy]\nadx_min = 0\n"))
    assert cfg.strategy.adx_min == 0.0
    assert isinstance(cfg.strategy.adx_min, float)


def test_adx_min_below_zero_is_still_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        StrategyCfg(adx_min=-0.1)
    message = str(excinfo.value)
    assert "strategy.adx_min" in message
    assert "at least 0.0" in message
    assert "0 disables the filter" in message  # the message says how to switch it off
    assert message.endswith(".")


def test_adx_min_above_one_hundred_is_rejected() -> None:
    with pytest.raises(ConfigError, match="strategy.adx_min"):
        StrategyCfg(adx_min=100.1)


def test_backtest_initial_equity_is_separate_from_account_equity(tmp_path: Path) -> None:
    """The backtest measures the strategy at a reference capital, not your balance."""
    cfg = load_config(
        _write(
            tmp_path / "c.toml", "[account]\nequity = 100\n\n[backtest]\ninitial_equity = 50000\n"
        )
    )
    assert cfg.account.equity == 100.0
    assert cfg.backtest.initial_equity == 50_000.0
    assert isinstance(cfg.backtest.initial_equity, float)


def test_backtest_initial_equity_accepts_the_lower_bound() -> None:
    assert BacktestCfg(initial_equity=100.0).initial_equity == 100.0


def test_backtest_initial_equity_explains_why_it_has_a_floor() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(initial_equity=25.0)
    message = str(excinfo.value)
    assert "at least 100" in message
    assert "whole-share" in message  # says why, not just what


# ---------------------------------------------------------------------------
# [backtest.tuning_grid] — what the walk-forward is allowed to choose from
# ---------------------------------------------------------------------------


def test_the_tuning_grid_is_unset_by_default() -> None:
    """Unset means the standard grid, so an old config keeps its old meaning."""
    assert BacktestCfg().tuning_grid is None
    assert Config().backtest.tuning_grid is None


def test_a_tuning_grid_loads_from_a_file(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path / "c.toml",
            "[backtest.tuning_grid]\natr_stop_mult = [1.5, 2.5, 4.0]\ndonchian_window = [15, 30]\n",
        )
    )
    assert cfg.backtest.tuning_grid == {
        "atr_stop_mult": (1.5, 2.5, 4.0),
        "donchian_window": (15, 30),
    }


def test_a_tuning_grid_may_name_only_some_of_the_tunable_parameters() -> None:
    """The rest are simply not tuned; they keep their [strategy] value."""
    cfg = BacktestCfg(tuning_grid={"atr_stop_mult": [1.5, 2.0]})
    assert set(cfg.tuning_grid) == {"atr_stop_mult"}


def test_tuning_grid_keys_are_stored_in_a_canonical_order() -> None:
    """The tuner breaks ties on grid order, so file order must not decide anything.

    Two configs listing the same candidates in a different order have to search
    them in the same sequence, or identical data could select different
    parameters depending on how someone typed their TOML.
    """
    first = BacktestCfg(tuning_grid={"volume_mult": [1.0, 1.3], "atr_stop_mult": [1.5, 2.0]})
    second = BacktestCfg(tuning_grid={"atr_stop_mult": [1.5, 2.0], "volume_mult": [1.0, 1.3]})
    assert list(first.tuning_grid) == list(second.tuning_grid) == ["atr_stop_mult", "volume_mult"]


def test_tuning_grid_values_are_coerced_the_way_strategy_values_are() -> None:
    cfg = BacktestCfg(tuning_grid={"atr_stop_mult": [2, 3], "donchian_window": [20.0, 25]})
    assert cfg.tuning_grid["atr_stop_mult"] == (2.0, 3.0)
    assert all(isinstance(v, float) for v in cfg.tuning_grid["atr_stop_mult"])
    assert cfg.tuning_grid["donchian_window"] == (20, 25)
    assert all(isinstance(v, int) for v in cfg.tuning_grid["donchian_window"])


def test_a_tuning_grid_that_is_not_a_table_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid="atr_stop_mult")
    assert "must be a table of parameter names" in str(excinfo.value)


def test_a_tuning_grid_naming_an_untunable_parameter_lists_the_valid_ones() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid={"adx_min": [15.0, 20.0]})
    message = str(excinfo.value)
    assert "'adx_min'" in message
    assert "cannot tune" in message
    for name in TUNABLE_PARAMS:
        assert name in message


def test_an_empty_tuning_grid_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid={})
    message = str(excinfo.value)
    assert "is empty" in message
    assert "nothing to choose between" in message


def test_an_empty_candidate_list_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid={"volume_mult": []})
    assert "backtest.tuning_grid.volume_mult is an empty list" in str(excinfo.value)


def test_a_candidate_list_that_is_not_a_list_is_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid={"atr_stop_mult": 2.0})
    assert "must be a list of candidate values" in str(excinfo.value)


def test_a_repeated_candidate_is_refused() -> None:
    """A duplicate is a wasted simulation and a candidate count that overstates."""
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid={"atr_stop_mult": [1.5, 2.0, 1.5]})
    assert "more than once" in str(excinfo.value)


@pytest.mark.parametrize(
    ("grid", "expected"),
    [
        ({"atr_stop_mult": [1.5, -1.0]}, "must be greater than 0"),
        ({"atr_stop_mult": [0.0]}, "must be greater than 0"),
        ({"chandelier_mult": [0.0]}, "must be greater than 0"),
        ({"volume_mult": [-0.5]}, "must be greater than 0"),
        ({"donchian_window": [1]}, "must be at least 2"),
        ({"donchian_window": [MAX_LOOKBACK_BARS + 1]}, f"at most {MAX_LOOKBACK_BARS}"),
        ({"donchian_window": [20.5]}, "must be a whole number"),
        ({"atr_stop_mult": ["2.0"]}, "written without quotes"),
        ({"atr_stop_mult": [True]}, "not true or false"),
    ],
)
def test_a_candidate_the_strategy_would_refuse_is_refused_here(grid, expected: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(tuning_grid=grid)
    message = str(excinfo.value)
    assert expected in message
    # And it points at the grid, not at [strategy], because that is where the
    # value was actually written.
    assert "backtest.tuning_grid." in message


@pytest.mark.parametrize("name", TUNABLE_PARAMS)
@pytest.mark.parametrize("value", [-1.0, 0.0, 1, 2, 2.5, 15, MAX_LOOKBACK_BARS, 400, 1000.0])
def test_the_grid_accepts_exactly_what_the_strategy_section_accepts(name: str, value) -> None:
    """Drift guard. The tuner writes its pick straight into StrategyCfg, so a
    value one of them takes and the other refuses is a bug in whichever is
    lagging — the point of the grid is to offer legal parameters."""

    def accepted(build) -> bool:
        try:
            build()
        except ConfigError:
            return False
        return True

    strategy_ok = accepted(lambda: StrategyCfg(**{name: value}))
    grid_ok = accepted(lambda: BacktestCfg(tuning_grid={name: [value]}))
    assert strategy_ok == grid_ok, f"{name}={value!r}: strategy {strategy_ok}, grid {grid_ok}"


def test_a_grid_at_the_combination_ceiling_is_accepted() -> None:
    cfg = BacktestCfg(
        tuning_grid={
            "atr_stop_mult": [round(1.0 + 0.1 * i, 1) for i in range(8)],
            "chandelier_mult": [round(2.0 + 0.1 * i, 1) for i in range(8)],
            "donchian_window": list(range(10, 18)),
            "volume_mult": [round(1.0 + 0.1 * i, 1) for i in range(1)],
        }
    )
    assert math.prod(len(v) for v in cfg.tuning_grid.values()) == MAX_TUNING_COMBINATIONS


def test_a_grid_past_the_combination_ceiling_names_the_count_and_the_ceiling() -> None:
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(
            tuning_grid={
                "atr_stop_mult": [round(1.0 + 0.1 * i, 1) for i in range(9)],
                "chandelier_mult": [round(2.0 + 0.1 * i, 1) for i in range(8)],
                "donchian_window": list(range(10, 18)),
            }
        )
    message = str(excinfo.value)
    assert "576" in message  # the count it asked for
    assert str(MAX_TUNING_COMBINATIONS) in message  # and the ceiling it broke
    assert "combinations x folds x symbols" in message  # and why the ceiling exists


def test_a_tuning_grid_survives_a_round_trip_through_dataclasses_replace() -> None:
    """``dataclasses.replace`` re-runs validation, which ablations rely on."""
    cfg = BacktestCfg(tuning_grid={"atr_stop_mult": [1.5, 2.0]})
    assert dataclasses.replace(cfg, is_years=2).tuning_grid == cfg.tuning_grid


def test_dates_may_be_written_as_strings(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path / "c.toml", '[backtest]\nstart = "2012-01-03"\nend = "2020-12-31"\n')
    )
    assert cfg.backtest.start == date(2012, 1, 3)
    assert cfg.backtest.end == date(2020, 12, 31)


def test_search_order_prefers_cwd_then_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "project"
    home = tmp_path / "home"
    (home / ".swing").mkdir(parents=True)
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    _write(home / ".swing" / "config.toml", "[account]\nequity = 500\n")
    assert load_config().account.equity == 500.0  # home is used when cwd has nothing

    _write(cwd / "config.toml", "[account]\nequity = 900\n")
    assert load_config().account.equity == 900.0  # cwd wins

    explicit = _write(tmp_path / "other.toml", "[account]\nequity = 1234\n")
    assert load_config(explicit).account.equity == 1234.0  # explicit beats both


def test_search_paths_are_reported_in_order(tmp_path: Path) -> None:
    paths = config_search_paths(tmp_path / "explicit.toml")
    assert paths[0] == tmp_path / "explicit.toml"
    assert paths[1] == Path.cwd() / "config.toml"
    assert paths[2] == Path.home() / ".swing" / "config.toml"


def test_falls_back_to_example_values_with_a_loud_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))

    with pytest.warns(UserWarning, match="EXAMPLE DEFAULTS"):
        cfg = load_config()

    assert cfg.account.equity == 100.0
    assert cfg.alerts.ntfy_topic == ""


# ---------------------------------------------------------------------------
# plain-English errors
# ---------------------------------------------------------------------------


def test_missing_explicit_file_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.toml")
    message = str(excinfo.value)
    assert "No configuration file at" in message
    assert "config.example.toml" in message


def test_broken_toml_is_explained(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", "[account\nequity = 1\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "not valid TOML" in str(excinfo.value)


@pytest.mark.parametrize("bad", [0, -1, 10.5, 100])
def test_risk_pct_outside_the_allowed_band_is_rejected_in_a_sentence(bad: float) -> None:
    with pytest.raises(ConfigError) as excinfo:
        AccountCfg(risk_pct=bad)
    message = str(excinfo.value)
    assert "account.risk_pct" in message
    assert "at most 10" in message
    assert str(bad) in message
    assert message.endswith(".")
    assert "config.toml" in message
    assert "Traceback" not in message


def test_risk_pct_at_the_boundaries() -> None:
    assert AccountCfg(risk_pct=10.0).risk_pct == 10.0
    with pytest.raises(ConfigError):
        AccountCfg(risk_pct=0.0)


@pytest.mark.parametrize(
    ("factory", "needle"),
    [
        (lambda: AccountCfg(equity=0), "account.equity"),
        (lambda: AccountCfg(max_positions=0), "account.max_positions"),
        (lambda: AccountCfg(max_position_pct=101), "account.max_position_pct"),
        (lambda: DataCfg(provider="alphavantage"), "data.provider"),
        (lambda: StrategyCfg(sma_fast=200, sma_mid=150), "sma_fast"),
        (lambda: StrategyCfg(min_price=0), "strategy.min_price"),
        (lambda: StrategyCfg(atr_stop_mult=0), "strategy.atr_stop_mult"),
        (lambda: StrategyCfg(mom_weight_126=0.0, mom_weight_63=0.0), "momentum weights"),
        (lambda: GatesCfg(max_drawdown_pct=0), "gates.max_drawdown_pct"),
        (lambda: GatesCfg(min_trades=0), "gates.min_trades"),
        (lambda: RegimeCfg(symbol="  "), "regime.symbol"),
        (lambda: ScheduleCfg(scan_time="25:00"), "schedule.scan_time"),
        (lambda: ScheduleCfg(scan_time="1730"), "schedule.scan_time"),
        (lambda: ScheduleCfg(timezone="Mars/Olympus"), "schedule.timezone"),
        (lambda: SchwabCfg(callback_url="http://127.0.0.1:8182"), "schwab.callback_url"),
        (lambda: ExecutionCfg(autopilot=True), "execution.autopilot"),
        (lambda: BacktestCfg(start=date(2020, 1, 1), end=date(2019, 1, 1)), "backtest.start"),
        (lambda: BacktestCfg(initial_equity=0), "backtest.initial_equity"),
        (lambda: BacktestCfg(initial_equity=-5.0), "backtest.initial_equity"),
        (lambda: BacktestCfg(initial_equity=99.99), "backtest.initial_equity"),
        (
            lambda: UniverseCfg(sp500=False, sp400=False, sp600=False, etfs=False),
            "universe is empty",
        ),
        (lambda: AlertsCfg(email_to="me@example.com"), "smtp_host"),
        (lambda: AlertsCfg(smtp_port=0), "alerts.smtp_port"),
    ],
)
def test_every_validation_failure_is_a_readable_sentence(factory, needle: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        factory()
    message = str(excinfo.value)
    assert needle in message
    assert message.endswith(".")
    assert len(message.split()) > 5  # a sentence, not a code


def test_unknown_key_lists_the_valid_ones(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", "[account]\nequty = 1000\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "'equty'" in message
    assert "[account]" in message
    assert "equity" in message  # the valid names are offered


def test_unknown_section_lists_the_valid_ones(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", "[acount]\nequity = 1000\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert "[acount]" in message
    assert "[account]" in message


def test_wrong_value_type_is_explained(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", '[universe]\nsp500 = "yes"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "true or false" in str(excinfo.value)


def test_bad_date_string_is_explained(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", '[data]\nstart_date = "01/02/2010"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "YYYY-MM-DD" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the example file must mirror the contract 1:1
# ---------------------------------------------------------------------------


def test_example_config_exists_and_parses() -> None:
    assert EXAMPLE.is_file()
    assert find_example_config() is not None
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg, Config)


def test_example_config_has_every_section() -> None:
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert set(raw) == {f.name for f in dataclasses.fields(Config)}


def test_example_config_mirrors_every_key_and_default() -> None:
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    text = EXAMPLE.read_text(encoding="utf-8")
    defaults = Config()

    for section_name, values in raw.items():
        section_defaults = getattr(defaults, section_name)
        field_names = {f.name for f in dataclasses.fields(section_defaults)}
        missing = field_names - set(values)
        for name in missing:
            # a key may be deliberately commented out, but it must be documented
            assert f"# {name} =" in text, f"{section_name}.{name} is missing from the example"
        assert not set(values) - field_names

    # and the documented values are the real defaults. The one difference is
    # deliberate: loading from a file anchors a relative reports_dir to that
    # file's directory (audit BUG-022), which the bare dataclass cannot do.
    example_cfg = load_config(EXAMPLE)
    assert example_cfg.paths.reports_dir == EXAMPLE.parent / "reports"
    unanchored = dataclasses.replace(
        example_cfg,
        paths=dataclasses.replace(example_cfg.paths, reports_dir=Path("reports")),
    )
    assert unanchored == defaults


def test_example_config_contains_no_secrets() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.schwab.api_key == ""
    assert cfg.schwab.app_secret == ""
    assert cfg.alerts.smtp_password == ""
    assert cfg.alerts.smtp_user == ""
    assert cfg.alerts.ntfy_topic == ""


def test_execution_is_off_by_default_in_the_example() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.execution.enabled is False
    assert cfg.execution.autopilot is False


# ---------------------------------------------------------------------------
# network manners (contract amendment A5)
# ---------------------------------------------------------------------------


def test_data_network_knobs_load_from_a_file(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path / "c.toml",
            "[data]\nretries = 5\nretry_backoff = 2\ndownload_batch = 50\n",
        )
    )
    assert cfg.data.retries == 5
    assert cfg.data.retry_backoff == 2.0
    assert isinstance(cfg.data.retry_backoff, float)
    assert cfg.data.download_batch == 50


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"retries": 0}, "data.retries"),
        ({"retries": 11}, "data.retries"),
        ({"retry_backoff": 0.0}, "data.retry_backoff"),
        ({"retry_backoff": 10.5}, "data.retry_backoff"),
        ({"download_batch": 9}, "data.download_batch"),
        ({"download_batch": 501}, "data.download_batch"),
    ],
)
def test_data_network_knobs_have_sane_bounds(kwargs: dict, needle: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        DataCfg(**kwargs)
    assert needle in str(excinfo.value)


def test_data_network_knobs_accept_their_boundaries() -> None:
    assert DataCfg(retries=1).retries == 1
    assert DataCfg(retries=10).retries == 10
    assert DataCfg(retry_backoff=10.0).retry_backoff == 10.0
    assert DataCfg(download_batch=10).download_batch == 10
    assert DataCfg(download_batch=500).download_batch == 500


# ---------------------------------------------------------------------------
# BUG-022 / amendment A14 — reports_dir is anchored to the config file
# ---------------------------------------------------------------------------


def test_a_relative_reports_dir_is_anchored_to_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit BUG-022: `swing confirm` from ~ must find what `swing scan` wrote.

    ``state_dir`` was absolute and ``reports_dir`` was CWD-relative, so the two
    halves of one run pointed at different places the moment the working
    directory changed.
    """
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = _write(project / "config.toml", '[paths]\nreports_dir = "reports"\n')

    monkeypatch.chdir(elsewhere)
    cfg = load_config(path)

    assert cfg.paths.reports_dir == project / "reports"
    assert cfg.paths.reports_dir.is_absolute()


def test_anchoring_also_applies_to_a_discovered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".swing").mkdir(parents=True)
    _write(home / ".swing" / "config.toml", '[paths]\nreports_dir = "reports"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert load_config().paths.reports_dir == home / ".swing" / "reports"


def test_a_relative_config_path_anchors_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "config.toml", '[paths]\nreports_dir = "reports"\n')
    monkeypatch.chdir(project)

    assert load_config(Path("config.toml")).paths.reports_dir == project / "reports"


def test_an_absolute_reports_dir_is_left_exactly_as_written(tmp_path: Path) -> None:
    target = tmp_path / "somewhere" / "else"
    path = _write(tmp_path / "config.toml", f'[paths]\nreports_dir = "{target}"\n')
    assert load_config(path).paths.reports_dir == target


def test_a_home_relative_reports_dir_is_expanded_not_anchored(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.toml", '[paths]\nreports_dir = "~/swing-reports"\n')
    assert load_config(path).paths.reports_dir == Path.home() / "swing-reports"


def test_example_defaults_stay_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no config file there is nothing to anchor to, and the loud warning covers it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))

    with pytest.warns(UserWarning, match="EXAMPLE DEFAULTS"):
        cfg = load_config()

    assert cfg.paths.reports_dir == Path("reports")


# ---------------------------------------------------------------------------
# BUG-029/032 / amendment A16 — settings that validate and then disable the
# strategy, silently and permanently
# ---------------------------------------------------------------------------


def test_breakout_proximity_stops_well_before_the_cliff() -> None:
    """Audit BUG-029: at 100 the breakout test is `close >= 0.0` — always true."""
    with pytest.raises(ConfigError) as excinfo:
        StrategyCfg(breakout_proximity_pct=100.0)
    message = str(excinfo.value)
    assert "strategy.breakout_proximity_pct" in message
    assert "at most 25" in message

    assert StrategyCfg(breakout_proximity_pct=25.0).breakout_proximity_pct == 25.0
    assert StrategyCfg(breakout_proximity_pct=0.0).breakout_proximity_pct == 0.0  # strict breakout


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"sma_slow": 450}, "strategy.sma_slow"),
        ({"sma_slow": 370, "sma_slow_rising_days": 21}, "strategy.sma_slow"),
        ({"volume_avg_window": 5000}, "strategy.volume_avg_window"),
        ({"donchian_window": 400}, "strategy.donchian_window"),
        ({"atr_window": 500}, "strategy.atr_window"),
        ({"mom_skip_days": 5000}, "strategy.mom_skip_days"),
        ({"mom_skip_days": 125}, "strategy.mom_skip_days"),
    ],
)
def test_lookbacks_longer_than_the_fetched_history_are_refused(kwargs: dict, needle: str) -> None:
    """Audit BUG-032: these all loaded fine and produced a permanently empty scan."""
    base = {"sma_fast": 50, "sma_mid": 150, "sma_slow": 200}
    with pytest.raises(ConfigError) as excinfo:
        StrategyCfg(**{**base, **kwargs})
    message = str(excinfo.value)
    assert needle in message
    assert "empty" in message or "ranked" in message  # it says what would go wrong
    assert message.endswith(".")


def test_the_lookback_caps_leave_the_shipped_defaults_and_ablations_room() -> None:
    defaults = StrategyCfg()
    assert defaults.sma_slow + defaults.sma_slow_rising_days <= MAX_LOOKBACK_BARS
    assert defaults.mom_skip_days + 126 <= MAX_MOMENTUM_LOOKBACK_BARS
    # the time-stop ablation runs a 10,000-day sentinel and must stay legal
    assert StrategyCfg(time_stop_days=10_000).time_stop_days == 10_000
    assert StrategyCfg(mom_skip_days=0).mom_skip_days == 0
    assert StrategyCfg(adx_min=0.0).adx_min == 0.0
    assert StrategyCfg(volume_mult=1.0).volume_mult == 1.0


def test_a_backtest_start_in_the_future_is_refused_even_without_an_end() -> None:
    """Audit BUG-032: `start` was only ever checked against `end`."""
    future = date.today() + timedelta(days=1)
    with pytest.raises(ConfigError) as excinfo:
        BacktestCfg(start=future)
    message = str(excinfo.value)
    assert "backtest.start" in message
    assert "empty" in message

    assert BacktestCfg(start=date.today() - timedelta(days=1)).end is None


# ---------------------------------------------------------------------------
# BUG-033 — the wrong kind of value is a sentence, not a TypeError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ('[strategy]\nsma_fast = "50"\n', "strategy.sma_fast"),
        ('[account]\nequity = "25000"\n', "account.equity"),
        ('[account]\nrisk_pct = "2.5"\n', "account.risk_pct"),
        ("[strategy]\nsma_fast = true\n", "strategy.sma_fast"),
        ("[regime]\nsymbol = 5\n", "regime.symbol"),
    ],
)
def test_a_value_of_the_wrong_kind_is_explained_not_raised(
    tmp_path: Path, body: str, needle: str
) -> None:
    """Audit BUG-033: a quoted number is the commonest TOML mistake there is."""
    path = _write(tmp_path / "c.toml", body)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert needle in message
    assert "config.toml" in message
    assert message.endswith(".")
    assert "not supported between instances" not in message  # the old raw TypeError


def test_a_quoted_number_says_how_to_write_it(tmp_path: Path) -> None:
    path = _write(tmp_path / "c.toml", '[strategy]\nsma_fast = "50"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "without quotes" in str(excinfo.value)


def test_a_bare_number_is_still_perfectly_fine(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path / "c.toml", "[strategy]\nsma_fast = 40\nadx_min = 15\n"))
    assert cfg.strategy.sma_fast == 40
    assert cfg.strategy.adx_min == 15.0
