"""Tests for FROZEN CONTRACT 1 — swing.config.

Two jobs here. First, pin the contract: every section, attribute name and
default value that other work packages code against. Second, prove that bad
input produces a readable sentence rather than a stack trace.
"""

from __future__ import annotations

import dataclasses
import tomllib
from datetime import date
from pathlib import Path

import pytest

from swing.config import (
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
        ("data", {"provider": "yfinance", "start_date": date(2010, 1, 1)}),
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

    # and the documented values are the real defaults
    example_cfg = load_config(EXAMPLE)
    assert example_cfg == defaults


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
