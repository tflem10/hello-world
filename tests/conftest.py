"""Shared fixtures: synthetic price series and a call-counting fake provider.

Nothing in the test suite touches the network. Every price series is generated
deterministically so a test can assert on *exact* trades, not "roughly".
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from swing.config import Config, load_config
from swing.data.provider import Fundamentals, Quote

TRADING_DAYS_PER_YEAR = 252


def business_days(start: str, n: int) -> pd.DatetimeIndex:
    return pd.bdate_range(start=start, periods=n, name="date")


def make_bars(
    closes,
    start: str = "2020-01-01",
    volume: float = 1_000_000.0,
    high_mult: float = 1.01,
    low_mult: float = 0.99,
) -> pd.DataFrame:
    """Build a canonical bar frame from a close series.

    Open is the prior close (a flat-gap world) so that "fill at next open"
    tests have an unambiguous expected price.
    """
    closes = np.asarray(closes, dtype="float64")
    idx = business_days(start, len(closes))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    vols = np.full(len(closes), float(volume)) if np.isscalar(volume) else np.asarray(
        volume, dtype="float64"
    )
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(closes, opens) * high_mult,
            "low": np.minimum(closes, opens) * low_mult,
            "close": closes,
            "volume": vols,
        },
        index=idx,
    )


def trending_bars(
    n: int = 400,
    start_price: float = 50.0,
    daily_drift: float = 0.002,
    noise: float = 0.0,
    seed: int = 0,
    start: str = "2020-01-01",
    volume: float = 2_000_000.0,
) -> pd.DataFrame:
    """A smooth uptrend (optionally with reproducible noise)."""
    rng = np.random.default_rng(seed)
    steps = np.full(n, daily_drift)
    if noise:
        steps = steps + rng.normal(0.0, noise, n)
    closes = start_price * np.exp(np.cumsum(steps))
    return make_bars(closes, start=start, volume=volume)


def flat_bars(n: int = 400, price: float = 50.0, start: str = "2020-01-01") -> pd.DataFrame:
    return make_bars(np.full(n, price), start=start)


class FakeProvider:
    """In-memory provider that records every call it receives."""

    name = "fake"

    def __init__(
        self,
        bars: dict[str, pd.DataFrame] | None = None,
        quotes: dict[str, float] | None = None,
        earnings: dict[str, date | None] | None = None,
        fundamentals: dict[str, Fundamentals] | None = None,
    ):
        self._bars = bars or {}
        self._quotes = quotes or {}
        self._earnings = earnings or {}
        self._fundamentals = fundamentals or {}
        self.bar_calls: list[tuple[tuple[str, ...], date, date]] = []
        self.quote_calls: list[tuple[str, ...]] = []

    def daily_bars(self, symbols, start, end):
        self.bar_calls.append((tuple(symbols), start, end))
        out = {}
        for sym in symbols:
            df = self._bars.get(sym)
            if df is None:
                continue
            window = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
            if len(window):
                out[sym] = window
        return out

    def quotes(self, symbols):
        self.quote_calls.append(tuple(symbols))
        return {
            s: Quote(symbol=s, price=self._quotes[s]) for s in symbols if s in self._quotes
        }

    def earnings_dates(self, symbols):
        return {s: self._earnings.get(s) for s in symbols}

    def fundamentals(self, symbols):
        return {
            s: self._fundamentals.get(s, Fundamentals(symbol=s)) for s in symbols
        }


@pytest.fixture
def base_config(tmp_path) -> Config:
    """A config pointed at a throwaway cache dir, with a tiny universe."""
    cfg = load_config()
    data = cfg.as_dict()
    data["data"]["cache_dir"] = str(tmp_path / "cache")
    data["data"]["request_pause_sec"] = 0.0
    data["universe"].update(
        {
            "sp500": False,
            "sp400": False,
            "sp600": False,
            "etfs": False,
            "extra_symbols": ["AAA", "BBB"],
            "max_symbols": 0,
        }
    )
    return Config(data)


@pytest.fixture
def today() -> date:
    return date(2021, 7, 1)


def days_after(start: str, n: int) -> date:
    return (date.fromisoformat(start) + timedelta(days=n))


def engine_config(
    initial_equity: float = 10_000.0,
    **overrides,
) -> Config:
    """A config with the *filters* switched off so a test can isolate one rule.

    Trend template, regime and volume confirmation each independently suppress
    entries; leaving them on makes it impossible to tell which rule a failing
    assertion is actually testing. Tests that care about a filter turn that one
    back on explicitly.
    """
    data = load_config().as_dict()
    data["backtest"].update(
        initial_equity=initial_equity,
        slippage_bps=5.0,
        spread_atr_frac=0.02,
        commission_per_trade=0.0,
    )
    data["account"].update(
        equity=initial_equity, risk_pct=0.02, max_position_pct=1.0,
        max_concurrent_positions=4,
    )
    data["strategy"]["regime"]["enabled"] = False
    data["strategy"]["trend_template"]["enabled"] = False
    data["strategy"]["entry"]["volume_mult"] = 0.0
    data["strategy"]["fundamentals"]["enabled"] = False

    for path, value in overrides.items():
        node = data
        parts = path.split("__")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return Config(data)


def breakout_series(
    flat_bars: int = 200,
    flat_price: float = 100.0,
    breakout_price: float = 110.0,
    after: list[float] | None = None,
    start: str = "2020-01-01",
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """Flat, then one clean breakout bar, then whatever ``after`` says.

    Flat bars produce no signal (the prior high is never exceeded), so the
    entry date is unambiguous: the bar after the breakout.
    """
    closes = [flat_price] * flat_bars + [breakout_price] + list(after or [])
    return make_bars(closes, start=start, volume=volume)
