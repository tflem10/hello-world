"""Configuration loading.

One annotated ``config.toml`` drives everything. The loader is deliberately
thin: it parses TOML into nested :class:`Section` objects that support both
attribute and dict access, fills in defaults from ``config.example.toml`` so an
older user config never crashes on a newly added key, and exposes a stable
hash used to tie backtest reports to the exact parameters that produced them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.toml"


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or structurally invalid."""


class Section:
    """Nested config node with attribute *and* mapping access."""

    def __init__(self, data: dict[str, Any], path: str = ""):
        self._data = data
        self._path = path

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as exc:  # pragma: no cover - defensive
            where = f"{self._path}.{name}" if self._path else name
            raise ConfigError(f"missing config key: {where}") from exc
        if isinstance(value, dict):
            child = f"{self._path}.{name}" if self._path else name
            return Section(value, child)
        return value

    def __getitem__(self, name: str) -> Any:
        return getattr(self, name)

    def __iter__(self):
        # Without this, iteration falls back to __getitem__(0), which raises a
        # confusing TypeError deep inside whatever tried to iterate a Section.
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def items(self):
        return {k: getattr(self, k) for k in self._data}.items()

    def values(self):
        return [getattr(self, k) for k in self._data]

    def get(self, name: str, default: Any = None) -> Any:
        if name not in self._data:
            return default
        return getattr(self, name)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def keys(self):
        return self._data.keys()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Section({self._path or 'root'}: {sorted(self._data)})"


class Config(Section):
    """Root configuration object."""

    def __init__(self, data: dict[str, Any], source: Path | None = None):
        super().__init__(data)
        self.source = source

    @property
    def hash(self) -> str:
        """Stable short hash of the *strategy-relevant* configuration.

        Alerts, SMTP credentials and schedule times do not change trade
        outcomes, so they are excluded — otherwise flipping an email address
        would invalidate a perfectly good backtest.
        """
        relevant = {
            k: self._data.get(k)
            for k in ("account", "universe", "strategy", "backtest")
        }
        blob = json.dumps(relevant, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def expand_path(self, value: str) -> Path:
        """Expand ``~`` and env vars; resolve relative paths against the repo."""
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration, layering the user's file over the shipped example."""
    defaults: dict[str, Any] = {}
    if EXAMPLE_CONFIG_PATH.exists():
        with EXAMPLE_CONFIG_PATH.open("rb") as fh:
            defaults = tomllib.load(fh)

    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if not defaults:
            raise ConfigError(
                f"no config found at {cfg_path} and no example config to fall back on"
            )
        # Running straight from a fresh clone: defaults are enough for
        # --help, tests and dry runs. Anything touching secrets will fail
        # loudly on the empty string.
        return Config(defaults, source=None)

    with cfg_path.open("rb") as fh:
        user = tomllib.load(fh)
    merged = _deep_merge(defaults, user)
    _validate(merged, cfg_path)
    return Config(merged, source=cfg_path)


def _validate(data: dict[str, Any], source: Path) -> None:
    acct = data.get("account", {})
    if acct.get("equity", 0) <= 0:
        raise ConfigError(f"{source}: account.equity must be > 0")
    risk = acct.get("risk_pct", 0)
    if not 0 < risk <= 0.10:
        raise ConfigError(
            f"{source}: account.risk_pct must be in (0, 0.10]; got {risk}. "
            "Risking more than 10% per trade is not a swing strategy, it is a coin flip."
        )
    if not 0 < acct.get("max_position_pct", 0) <= 1.0:
        raise ConfigError(f"{source}: account.max_position_pct must be in (0, 1]")
    if acct.get("max_concurrent_positions", 0) < 1:
        raise ConfigError(f"{source}: account.max_concurrent_positions must be >= 1")

    exitc = data.get("strategy", {}).get("exit", {})
    if exitc.get("initial_stop_atr", 0) <= 0:
        raise ConfigError(f"{source}: strategy.exit.initial_stop_atr must be > 0")
    if exitc.get("chandelier_atr", 0) <= 0:
        raise ConfigError(f"{source}: strategy.exit.chandelier_atr must be > 0")
