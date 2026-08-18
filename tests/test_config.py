"""Config loader behaviour."""

from __future__ import annotations

import pytest

from swing.config import Config, ConfigError, load_config


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
