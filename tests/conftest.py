"""Shared test fixtures for the whole suite.

Three things live here, and every test module in this project is expected to
use them:

1. ``_block_network`` — an autouse fixture that makes any outbound socket
   connection raise. Tests that hit the network are slow, flaky and
   occasionally expensive; here they simply fail. Mark a test with
   ``@pytest.mark.network_ok`` to opt out (nothing in this repo does).
2. ``test_cfg`` — a :class:`swing.config.Config` whose state, report and cache
   directories all live under ``tmp_path``, so no test can touch ``~/.swing``.
   Use ``cfg_factory`` when you need to override a few settings.
3. ``make_bars`` — a deterministic synthetic OHLCV builder in the Contract 3
   format. It is a plain function, so any test module can
   ``from conftest import make_bars``.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from swing.config import (
    AccountCfg,
    AlertsCfg,
    BacktestCfg,
    Config,
    DataCfg,
    ExecutionCfg,
    GatesCfg,
    PathsCfg,
    RegimeCfg,
    ScheduleCfg,
    SchwabCfg,
    StrategyCfg,
    UniverseCfg,
)

__all__ = ["build_config", "make_bars"]

SECTION_CLASSES: dict[str, type] = {
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


# ---------------------------------------------------------------------------
# 1. no network, ever
# ---------------------------------------------------------------------------


class NetworkAccessAttempted(RuntimeError):
    """Raised when a test tries to open a network connection."""


def _refuse(kind: str, target: Any) -> NetworkAccessAttempted:
    """The one sentence every blocked mechanism raises."""
    return NetworkAccessAttempted(
        f"This test tried to open a {kind} connection to {target!r}. Tests must run "
        f"offline: mock the provider, or use the committed fixtures. If a test genuinely "
        f"needs the network, mark it with @pytest.mark.network_ok."
    )


def _block_curl_cffi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block curl_cffi, which reaches the network without ever touching a Python socket.

    Patching ``socket.socket.connect`` blocks anything built on Python's socket
    module — ``urllib``, ``requests``, ``httpx``. It does **not** block
    curl_cffi: that hands the URL to libcurl in C, which opens its own socket
    with no Python object involved. yfinance 1.6 moved to curl_cffi, so the
    suite's offline guarantee had a hole wide enough for a live AAPL quote to
    come back through it mid-run.

    Two seams, because one is not enough. ``Session.request`` is where every
    documented entry point ends up (the module-level ``get``/``post`` helpers
    build a ``Session`` and call it), and ``Curl.perform`` is the backstop for
    anything that skips the requests layer or subclasses ``Session``.

    curl_cffi is imported defensively so this fixture stays dependency-agnostic:
    a checkout without it is simply a checkout with one fewer way out.
    """
    try:
        import curl_cffi
        from curl_cffi import requests as curl_requests
    except ImportError:  # pragma: no cover - curl_cffi ships as a yfinance dependency
        return

    def _url_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        """`request(self, method, url, ...)` — however the caller spelled it."""
        if "url" in kwargs:
            return kwargs["url"]
        return args[1] if len(args) > 1 else "an unnamed URL"

    def guard_request(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise _refuse("curl_cffi", _url_of(args, kwargs))

    def guard_perform(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise _refuse("curl_cffi", "the URL set on this curl handle")

    for owner, name in (
        (getattr(curl_requests, "Session", None), "request"),
        (getattr(curl_requests, "AsyncSession", None), "request"),
    ):
        if owner is not None and hasattr(owner, name):
            monkeypatch.setattr(owner, name, guard_request)

    curl_class = getattr(curl_cffi, "Curl", None)
    if curl_class is not None and hasattr(curl_class, "perform"):
        monkeypatch.setattr(curl_class, "perform", guard_perform)


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every outbound network connection fail loudly.

    Two mechanisms are covered: Python sockets, and curl_cffi's C-level libcurl
    path (see :func:`_block_curl_cffi`). ``@pytest.mark.network_ok`` lifts both
    at once — the marker check happens before either is installed.

    Unix-domain sockets are left alone: they are local IPC, not the network,
    and blocking them breaks unrelated machinery.
    """
    if request.node.get_closest_marker("network_ok"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard_connect(self: socket.socket, address: Any) -> None:
        if self.family == socket.AF_UNIX:
            real_connect(self, address)
            return
        raise _refuse("socket", address)

    def guard_connect_ex(self: socket.socket, address: Any) -> int:
        if self.family == socket.AF_UNIX:
            return real_connect_ex(self, address)
        raise _refuse("socket", address)

    def guard_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        raise _refuse("TCP", address)

    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guard_create_connection)
    _block_curl_cffi(monkeypatch)


# ---------------------------------------------------------------------------
# 2. configuration rooted in tmp_path
# ---------------------------------------------------------------------------


def build_config(tmp_path: Path, **overrides: dict[str, Any]) -> Config:
    """Build a :class:`Config` whose every writable path is under ``tmp_path``.

    Override any section by passing it as a keyword whose value is a dict of
    field names, for example::

        cfg = build_config(tmp_path, account={"equity": 25_000.0},
                           gates={"min_trades": 1})

    The state, reports and cache directories are created, so callers can write
    to them immediately.
    """
    state_dir = tmp_path / "state"
    sections: dict[str, dict[str, Any]] = {
        "data": {"cache_dir": tmp_path / "cache"},
        "paths": {"reports_dir": tmp_path / "reports", "state_dir": state_dir},
        "schwab": {"token_path": state_dir / "schwab_token.json"},
    }
    for name, values in overrides.items():
        if name not in SECTION_CLASSES:
            raise KeyError(
                f"{name!r} is not a config section. Valid sections: {', '.join(SECTION_CLASSES)}."
            )
        sections.setdefault(name, {}).update(values)

    cfg = Config(**{name: SECTION_CLASSES[name](**values) for name, values in sections.items()})
    for directory in (cfg.paths.state_dir, cfg.paths.reports_dir, cfg.data.cache_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def cfg_factory(tmp_path: Path):
    """Return a callable that builds configs rooted in this test's ``tmp_path``."""

    def _factory(**overrides: dict[str, Any]) -> Config:
        return build_config(tmp_path, **overrides)

    return _factory


@pytest.fixture
def test_cfg(tmp_path: Path) -> Config:
    """A default :class:`Config` with all paths under ``tmp_path``."""
    return build_config(tmp_path)


# ---------------------------------------------------------------------------
# 3. deterministic synthetic bars (Contract 3 format)
# ---------------------------------------------------------------------------


def make_bars(
    n_days: int,
    start: str = "2020-01-02",
    trend: float = 0.0,
    base: float = 100.0,
    seed: int = 7,
) -> pd.DataFrame:
    """Build ``n_days`` of synthetic daily bars in the Contract 3 format.

    Args:
        n_days: number of business days to generate.
        start: first business day, ``YYYY-MM-DD``.
        trend: per-day drift as a fraction, e.g. ``0.002`` for +0.2%/day.
        base: price level the series starts from.
        seed: RNG seed — the same arguments always give the same frame.

    Returns:
        A DataFrame indexed by a tz-naive ascending ``DatetimeIndex`` of
        business days, with float columns exactly ``open, high, low, close,
        volume`` and ``high >= max(open, close) >= min(open, close) >= low``
        on every row.
    """
    if n_days < 1:
        raise ValueError("n_days must be at least 1")
    if base <= 0:
        raise ValueError("base must be greater than 0")

    index = pd.bdate_range(start=start, periods=n_days)
    rng = np.random.default_rng(seed)

    steps = np.clip(1.0 + trend + rng.normal(0.0, 0.01, n_days), 0.5, 1.5)
    close = base * np.cumprod(steps)

    previous = np.empty(n_days, dtype=float)
    previous[0] = base
    previous[1:] = close[:-1]
    open_ = previous * (1.0 + rng.normal(0.0, 0.002, n_days))

    upper = np.abs(rng.normal(0.0, 0.004, n_days))
    lower = np.abs(rng.normal(0.0, 0.004, n_days))
    high = np.maximum(open_, close) * (1.0 + upper)
    low = np.minimum(open_, close) * (1.0 - lower)
    volume = np.round(1_000_000.0 * (1.0 + np.abs(rng.normal(0.0, 0.25, n_days))))

    return pd.DataFrame(
        {
            "open": open_.astype(float),
            "high": high.astype(float),
            "low": low.astype(float),
            "close": close.astype(float),
            "volume": volume.astype(float),
        },
        index=index,
    )


@pytest.fixture
def bars() -> pd.DataFrame:
    """A year of flat, seeded synthetic bars — handy default for quick tests."""
    return make_bars(260)
