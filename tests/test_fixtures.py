"""Tests for the shared test infrastructure in ``tests/conftest.py``.

Every other work package leans on ``make_bars``, ``test_cfg`` and the network
block, so those helpers get their own coverage.
"""

from __future__ import annotations

import inspect
import socket
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

from conftest import NetworkAccessAttempted, build_config, make_bars

# --- make_bars -------------------------------------------------------------


def test_make_bars_is_deterministic() -> None:
    pd.testing.assert_frame_equal(make_bars(50, trend=0.001), make_bars(50, trend=0.001))


def test_make_bars_matches_contract_3_format() -> None:
    bars = make_bars(120, start="2021-03-01")

    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert all(str(dtype) == "float64" for dtype in bars.dtypes)
    assert isinstance(bars.index, pd.DatetimeIndex)
    assert bars.index.tz is None
    assert bars.index.is_monotonic_increasing
    assert len(bars) == 120
    assert bars.index[0] == pd.Timestamp("2021-03-01")
    # business days only
    assert (bars.index.dayofweek < 5).all()


def test_make_bars_rows_are_internally_consistent() -> None:
    bars = make_bars(300, trend=-0.001, base=42.0, seed=11)
    body_high = bars[["open", "close"]].max(axis=1)
    body_low = bars[["open", "close"]].min(axis=1)

    assert (bars["high"] >= body_high).all()
    assert (bars["low"] <= body_low).all()
    assert (bars["high"] >= bars["low"]).all()
    assert (bars["low"] > 0).all()
    assert (bars["volume"] > 0).all()


def test_make_bars_trend_argument_controls_direction() -> None:
    rising = make_bars(250, trend=0.003)
    falling = make_bars(250, trend=-0.003)
    flat = make_bars(250, trend=0.0)

    assert rising["close"].iloc[-1] > rising["close"].iloc[0]
    assert falling["close"].iloc[-1] < falling["close"].iloc[0]
    assert 0.5 < flat["close"].iloc[-1] / flat["close"].iloc[0] < 2.0


def test_make_bars_seed_changes_the_path_but_not_the_shape() -> None:
    a = make_bars(60, seed=1)
    b = make_bars(60, seed=2)
    assert not a["close"].equals(b["close"])
    assert a.index.equals(b.index)


def test_make_bars_rejects_nonsense_arguments() -> None:
    with pytest.raises(ValueError):
        make_bars(0)
    with pytest.raises(ValueError):
        make_bars(10, base=0.0)


# --- config fixtures -------------------------------------------------------


def test_test_cfg_keeps_every_path_inside_tmp_path(test_cfg, tmp_path: Path) -> None:
    for directory in (
        test_cfg.paths.state_dir,
        test_cfg.paths.reports_dir,
        test_cfg.data.cache_dir,
    ):
        assert tmp_path in directory.parents or directory == tmp_path
        assert directory.is_dir()
    assert tmp_path in test_cfg.schwab.token_path.parents


def test_cfg_factory_applies_overrides(cfg_factory, tmp_path: Path) -> None:
    cfg = cfg_factory(account={"equity": 25_000.0, "risk_pct": 1.0}, gates={"min_trades": 5})

    assert cfg.account.equity == 25_000.0
    assert cfg.account.risk_pct == 1.0
    assert cfg.gates.min_trades == 5
    # untouched sections keep their defaults, paths stay sandboxed
    assert cfg.strategy.sma_slow == 200
    assert cfg.paths.state_dir == tmp_path / "state"


def test_build_config_rejects_an_unknown_section(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="not a config section"):
        build_config(tmp_path, nonsense={"x": 1})


# --- the network block -----------------------------------------------------


def test_tcp_connections_are_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(NetworkAccessAttempted):
        sock.connect(("example.com", 80))
    sock.close()


def test_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkAccessAttempted):
        socket.create_connection(("example.com", 80), timeout=1)


def test_http_requests_are_blocked() -> None:
    with pytest.raises(Exception) as excinfo:  # noqa: B017 - urllib wraps our error
        urllib.request.urlopen("http://example.com", timeout=1)  # noqa: S310
    assert "offline" in str(excinfo.value) or isinstance(excinfo.value, NetworkAccessAttempted)


# --- the curl_cffi hole ------------------------------------------------------
#
# yfinance 1.6 issues HTTP through curl_cffi, which hands the URL to libcurl in
# C: no Python socket is ever constructed, so the socket patches above see
# nothing. An un-seamed test fetched a live quote straight through this gap.


def test_curl_cffi_module_level_get_is_blocked() -> None:
    curl_requests = pytest.importorskip("curl_cffi.requests")
    with pytest.raises(NetworkAccessAttempted) as excinfo:
        curl_requests.get("https://query1.finance.yahoo.com/v8/finance/chart/AAPL", timeout=1)
    message = str(excinfo.value)
    assert "curl_cffi" in message
    assert "network_ok" in message  # it says how to opt out


def test_curl_cffi_session_request_is_blocked() -> None:
    curl_requests = pytest.importorskip("curl_cffi.requests")
    session = curl_requests.Session()
    with pytest.raises(NetworkAccessAttempted):
        session.request("GET", "https://example.com", timeout=1)
    with pytest.raises(NetworkAccessAttempted) as excinfo:
        session.request(method="GET", url="https://example.com", timeout=1)
    assert "https://example.com" in str(excinfo.value)  # the refusal names the URL


def test_curl_cffi_perform_is_blocked_as_the_backstop() -> None:
    """Anything that skips the requests layer still cannot reach libcurl."""
    curl_cffi = pytest.importorskip("curl_cffi")
    if not hasattr(curl_cffi, "Curl"):  # pragma: no cover - depends on the release
        pytest.skip("this curl_cffi has no Curl handle to guard")
    handle = curl_cffi.Curl()
    with pytest.raises(NetworkAccessAttempted):
        handle.perform()


@pytest.mark.network_ok
def test_the_marker_lifts_the_curl_cffi_block_too() -> None:
    """The escape hatch has to cover both mechanisms, or it covers neither.

    This asserts the guard is *absent*, not that the network works — the test
    stays offline and fast.
    """
    curl_requests = pytest.importorskip("curl_cffi.requests")
    source = inspect.getsource(curl_requests.Session.request)
    assert "NetworkAccessAttempted" not in source
