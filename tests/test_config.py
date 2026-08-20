"""Config loader behaviour."""

from __future__ import annotations

import tomllib

import pytest

from swing.config import EXAMPLE_CONFIG_PATH, Config, ConfigError, load_config


def test_example_config_loads():
    cfg = load_config()
    assert cfg.account.equity > 0
    assert cfg.strategy.exit.atr_len == 14


def test_attribute_and_item_access_agree():
    cfg = load_config()
    assert cfg.strategy.entry.donchian_len == cfg["strategy"]["entry"]["donchian_len"]


def test_missing_key_raises_with_path():
    cfg = load_config()
    with pytest.raises(ConfigError, match="strategy.nope"):
        _ = cfg.strategy.nope


def test_hash_is_stable_and_ignores_alert_settings():
    cfg = load_config()
    base = cfg.hash
    data = cfg.as_dict()
    data["alerts"]["email"]["to_addrs"] = ["someone@example.com"]
    assert Config(data).hash == base
    data["account"]["risk_pct"] = 0.03
    assert Config(data).hash != base


def test_hash_ignores_the_account_balance_but_not_the_risk_settings():
    """Recording a deposit must not re-lock the gate.

    Backtests size from `backtest.initial_equity`; `account.equity` cannot move
    a single out-of-sample number, so hashing it made every balance edit cost a
    walk-forward re-run that could only reproduce the same report. The keys that
    *do* change the trades stay in.
    """
    cfg = load_config()
    base = cfg.hash

    for key, value in (
        ("equity", float(cfg.account.equity) * 3 + 1_000.0),
        ("stale_equity_tolerance_pct", 0.05),
        ("currency", "EUR"),
    ):
        data = cfg.as_dict()
        data["account"][key] = value
        assert Config(data).hash == base, key

    for key, value in (
        ("risk_pct", 0.03),
        ("max_position_pct", 0.50),
        ("max_concurrent_positions", 9),
    ):
        data = cfg.as_dict()
        data["account"][key] = value
        assert Config(data).hash != base, key


def test_the_report_age_limit_is_outside_every_hashed_section():
    """A knob under [account]/[universe]/[strategy]/[backtest] re-locks the gate
    for every user — so the walk-forward staleness limit lives under [reports]."""
    example = tomllib.loads(EXAMPLE_CONFIG_PATH.read_text())
    assert example["reports"]["max_walkforward_age_days"] == 90
    assert all(
        "max_walkforward_age_days" not in example[section]
        for section in ("account", "universe", "strategy", "backtest")
    )

    cfg = load_config()
    data = cfg.as_dict()
    data["reports"]["max_walkforward_age_days"] = 7
    assert Config(data).hash == cfg.hash


def test_user_config_layers_over_example(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[account]\nequity = 500.0\n")
    cfg = load_config(p)
    assert cfg.account.equity == 500.0
    # untouched keys still come from the example
    assert cfg.strategy.exit.chandelier_atr == 3.0


def test_validation_rejects_absurd_risk(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[account]\nrisk_pct = 0.5\n")
    with pytest.raises(ConfigError, match="risk_pct"):
        load_config(p)
