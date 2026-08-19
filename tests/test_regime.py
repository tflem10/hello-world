"""AC7 — the market regime gate flips on exactly the dates it should.

The headline test uses a three-bar SMA over a hand-written price path, so every
flip date can be checked with mental arithmetic and is written out in the test.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from conftest import make_bars
from swing.indicators import sma
from swing.strategy.regime import entries_allowed


def spy_bars(close: object, start: str = "2020-01-02") -> pd.DataFrame:
    """A Contract 3 frame for the regime symbol; only ``close`` matters to the gate."""
    close_values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": close_values.copy(),
            "high": close_values * 1.01,
            "low": close_values * 0.99,
            "close": close_values,
            "volume": np.full(close_values.size, 80_000_000.0),
        },
        index=pd.bdate_range(start=start, periods=close_values.size),
    )


def test_regime_flips_on_the_exact_dates(cfg_factory) -> None:
    """A 3-bar SMA over 10, 11, 12, 9, 8, 20, 21, 22.

    SMA(3):     -,  -, 11, 10.67, 9.67, 12.33, 16.33, 21
    close>SMA:  F,  F,  T,     F,    F,     T,     T,  T

    The first two bars are False because the average does not exist yet, not
    because the market is weak — a short history must block entries.
    """
    cfg = cfg_factory(regime={"sma_window": 3})
    bars = spy_bars([10.0, 11.0, 12.0, 9.0, 8.0, 20.0, 21.0, 22.0])

    allowed = entries_allowed(bars, cfg)

    expected = pd.Series(
        [False, False, True, False, False, True, True, True],
        index=bars.index,
        name="entries_allowed",
    )
    pd.testing.assert_series_equal(allowed, expected)


def test_regime_disabled_allows_everything_including_the_warm_up(cfg_factory) -> None:
    cfg = cfg_factory(regime={"enabled": False})
    bars = spy_bars(100.0 * 0.99 ** np.arange(300))  # a bear market

    allowed = entries_allowed(bars, cfg)

    assert allowed.all()
    assert allowed.dtype == bool
    pd.testing.assert_index_equal(allowed.index, bars.index)


def test_regime_blocks_through_the_sma_warm_up(cfg_factory) -> None:
    """With the default 200-bar window nothing is allowed until bar 200 exists."""
    cfg = cfg_factory()
    bars = spy_bars(100.0 * 1.001 ** np.arange(300))

    allowed = entries_allowed(bars, cfg)

    assert not allowed.iloc[:199].any()
    assert allowed.iloc[199:].all()
    assert allowed.index[199] == allowed.idxmax()


def test_regime_blocks_a_falling_market(cfg_factory) -> None:
    """A monotone decline never closes above its own trailing average."""
    cfg = cfg_factory()

    assert not entries_allowed(spy_bars(100.0 * 0.999 ** np.arange(300)), cfg_factory()).any()
    assert not entries_allowed(spy_bars(100.0 * 0.999 ** np.arange(300)), cfg).any()


def test_regime_requires_a_strict_close_above_the_average(cfg_factory) -> None:
    """A flat market sits exactly on its average, and 'equal' is not 'above'."""
    cfg = cfg_factory()

    assert not entries_allowed(spy_bars(np.full(300, 100.0)), cfg).any()


def test_regime_flip_dates_track_a_round_trip(cfg_factory) -> None:
    """Up for 60 bars, down for 60, up again: allowed, blocked, allowed."""
    cfg = cfg_factory(regime={"sma_window": 20})
    close = np.concatenate(
        [
            100.0 * 1.004 ** np.arange(60),
            100.0 * 1.004**59 * 0.99 ** np.arange(1, 61),
            100.0 * 1.004**59 * 0.99**60 * 1.004 ** np.arange(1, 61),
        ]
    )
    bars = spy_bars(close)

    allowed = entries_allowed(bars, cfg)

    assert bool(allowed.iloc[59])  # still rising
    assert not bool(allowed.iloc[80])  # deep into the decline
    assert bool(allowed.iloc[150])  # recovered well above the average
    # the gate must switch state exactly twice over this path (after warm-up)
    assert int(allowed.iloc[19:].astype(int).diff().abs().sum()) == 2


def test_regime_honours_a_custom_window(cfg_factory) -> None:
    """A shorter window reacts sooner: 50 bars of warm-up instead of 200."""
    bars = spy_bars(100.0 * 1.001 ** np.arange(300))

    fast = entries_allowed(bars, cfg_factory(regime={"sma_window": 50}))
    slow = entries_allowed(bars, cfg_factory(regime={"sma_window": 200}))

    assert bool(fast.iloc[60])
    assert not bool(slow.iloc[60])


def test_regime_matches_the_indicator_it_documents(cfg_factory) -> None:
    """The gate is exactly 'close > sma(close, window)' on seeded random bars."""
    cfg = cfg_factory(regime={"sma_window": 50})
    bars = make_bars(300, trend=0.0005)

    allowed = entries_allowed(bars, cfg)
    expected = (bars["close"] > sma(bars["close"], 50)).fillna(False)

    assert allowed.tolist() == expected.tolist()
    assert not allowed.isna().any()
