"""FROZEN CONTRACT 7 (rules) — the trend/breakout rule set, as vectorized Series.

Every rule here returns a Series aligned to the input bars, so the *same*
function serves the nightly scanner (which reads ``.iloc[-1]``) and the
backtest engine (which reads any date). Nothing in this module looks at the
wall clock, fetches data, or mutates its inputs.

Warm-up policy: a rule is ``False`` (never ``NaN``) until every indicator it
depends on has enough history. That way a consumer can index any date without
special-casing the first year of a series.

Window constants that the config deliberately does not expose:

* ``DOLLAR_VOLUME_WINDOW`` (20) — the averaging window for the liquidity screen.
* ``LOOKBACK_52W`` (252) — trading days in a year, for the 52-week high/low.
* ``ADX_WINDOW`` (14) — Wilder's original ADX period; the config has no knob.
* ``CHANDELIER_WINDOW`` (22) — the standard Chandelier Exit lookback (~1 month).
* ``RSI2_PERIOD`` / ``RSI2_THRESHOLD`` (2 / 10.0) — the optional pullback entry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from swing.indicators import adx, atr, donchian_high, rsi, sma

if TYPE_CHECKING:  # pragma: no cover - imports used only by the type checker
    import datetime as dt

    from swing.config import Config
    from swing.data.provider import Fundamentals

__all__ = [
    "ADX_WINDOW",
    "CHANDELIER_WINDOW",
    "DOLLAR_VOLUME_WINDOW",
    "LOOKBACK_52W",
    "RSI2_PERIOD",
    "RSI2_THRESHOLD",
    "chandelier_stop",
    "earnings_blackout",
    "entry_signal",
    "fundamentals_ok",
    "initial_stop",
    "liquidity_ok",
    "rolling_high_52w",
    "rolling_low_52w",
    "trend_template",
]

DOLLAR_VOLUME_WINDOW = 20
LOOKBACK_52W = 252
ADX_WINDOW = 14
CHANDELIER_WINDOW = 22

RSI2_PERIOD = 2
"""Lookback for the optional RSI(2) pullback entry — Connors' original 2-period RSI."""

RSI2_THRESHOLD = 10.0
"""Oversold level below which the optional RSI(2) pullback entry fires.

Deliberately *not* a config knob in v1. The overlay itself is already gated by
``strategy.rsi2_enabled`` (off by default), and exposing a second free parameter
on a module that exists to be ablated rather than tuned would invite curve
fitting. See ``docs/indicator-research.md`` §5 for the 5-10 range in the
published literature and the reasons this system ships the overlay disabled.
"""


def _as_bool(flags: pd.Series, name: str) -> pd.Series:
    """Force a rule result to a named ``bool`` Series with ``False`` for warm-up."""
    return flags.fillna(False).astype(bool).rename(name)


def rolling_high_52w(close: pd.Series) -> pd.Series:
    """Highest close of the trailing 252 trading days, NaN until 252 bars exist."""
    return close.rolling(LOOKBACK_52W, min_periods=LOOKBACK_52W).max()


def rolling_low_52w(close: pd.Series) -> pd.Series:
    """Lowest close of the trailing 252 trading days, NaN until 252 bars exist."""
    return close.rolling(LOOKBACK_52W, min_periods=LOOKBACK_52W).min()


def liquidity_ok(bars: pd.DataFrame, cfg: Config, *, is_etf: bool) -> pd.Series:
    """True when close is at least ``min_price`` and the 20-day average dollar volume
    (close x volume) is at least ``min_dollar_volume``.

    Args:
        bars: Contract 3 OHLCV frame.
        cfg: the loaded configuration.
        is_etf: accepted for interface symmetry; ETFs face the identical
            thresholds, because the only ETF relaxation in this system is
            skipping the fundamentals screen (handled by the caller).

    Returns:
        Boolean Series aligned to ``bars.index``, False for the first 19 bars.
    """
    del is_etf  # thresholds are identical for stocks and ETFs by design
    close = bars["close"]
    dollar_volume = close * bars["volume"]
    average = dollar_volume.rolling(DOLLAR_VOLUME_WINDOW, min_periods=DOLLAR_VOLUME_WINDOW).mean()
    priced_ok = close >= cfg.strategy.min_price
    traded_ok = average >= cfg.strategy.min_dollar_volume
    return _as_bool(priced_ok & traded_ok, "liquidity_ok")


def trend_template(bars: pd.DataFrame, cfg: Config, *, is_etf: bool) -> pd.Series:
    """True when close > SMA(fast) > SMA(mid) > SMA(slow), the slow SMA is higher than it
    was ``sma_slow_rising_days`` bars ago, close is at least ``min_above_low_mult`` times
    the 52-week closing low and no more than ``max_below_high_pct`` below the 52-week
    closing high, and ADX(14) is at least ``adx_min``.

    Args:
        bars: Contract 3 OHLCV frame.
        cfg: the loaded configuration.
        is_etf: accepted for interface symmetry; the template is identical for
            ETFs (their relaxation is the fundamentals screen, applied
            elsewhere). Kept so callers need not branch and so a future ETF
            variant is a one-line change here.

    Returns:
        Boolean Series aligned to ``bars.index``, False throughout the ~252-bar warm-up.
    """
    del is_etf  # the template is identical for ETFs in v1
    strategy = cfg.strategy
    close = bars["close"]
    fast = sma(close, strategy.sma_fast)
    mid = sma(close, strategy.sma_mid)
    slow = sma(close, strategy.sma_slow)

    stacked = (close > fast) & (fast > mid) & (mid > slow)
    slow_rising = slow > slow.shift(strategy.sma_slow_rising_days)
    above_low = close >= strategy.min_above_low_mult * rolling_low_52w(close)
    near_high = close >= (1.0 - strategy.max_below_high_pct / 100.0) * rolling_high_52w(close)
    trending = adx(bars, ADX_WINDOW) >= strategy.adx_min

    return _as_bool(stacked & slow_rising & above_low & near_high & trending, "trend_template")


def entry_signal(bars: pd.DataFrame, cfg: Config) -> pd.Series:
    """True on a breakout entry — close clears the prior ``donchian_window``-day high (or
    comes within ``breakout_proximity_pct`` of it) *and* volume is at least ``volume_mult``
    times its ``volume_avg_window``-day average — or, only when ``strategy.rsi2_enabled``,
    on an RSI(2) pullback entry: RSI(close, 2) below 10 while close holds at or above
    SMA(sma_fast).

    ``donchian_high`` is already shifted one bar, so the level being cleared is
    the prior N days and excludes today. The volume average includes today,
    which is the conservative choice: a volume spike raises its own benchmark.

    The pullback path carries **no** volume confirmation on purpose: a
    mean-reversion entry happens on a quiet, sold-out day, so demanding a volume
    surge there would reject exactly the setups the overlay exists to take. The
    overlay ships disabled and exists to be measured by the ``rsi2_on`` ablation
    (``scripts/ablations.py``); see ``docs/strategy-spec.md`` §13.

    Returns:
        Boolean Series aligned to ``bars.index``, False throughout the warm-up.
    """
    strategy = cfg.strategy
    close = bars["close"]
    volume = bars["volume"]

    prior_high = donchian_high(bars, strategy.donchian_window)
    proximity = 1.0 - strategy.breakout_proximity_pct / 100.0
    breakout = (close > prior_high) | (close >= proximity * prior_high)
    average_volume = volume.rolling(
        strategy.volume_avg_window, min_periods=strategy.volume_avg_window
    ).mean()
    volume_confirm = volume >= strategy.volume_mult * average_volume
    breakout_path = breakout & volume_confirm

    if not strategy.rsi2_enabled:
        return _as_bool(breakout_path, "entry_signal")

    oversold = rsi(close, RSI2_PERIOD) < RSI2_THRESHOLD
    above_fast_sma = close >= sma(close, strategy.sma_fast)
    rsi2_pullback_path = oversold & above_fast_sma

    return _as_bool(breakout_path | rsi2_pullback_path, "entry_signal")


def initial_stop(bars: pd.DataFrame, cfg: Config) -> pd.Series:
    """The initial protective stop: close minus ``atr_stop_mult`` times ATR(atr_window)."""
    stop = bars["close"] - cfg.strategy.atr_stop_mult * atr(bars, cfg.strategy.atr_window)
    return stop.astype(float).rename("initial_stop")


def chandelier_stop(bars: pd.DataFrame, cfg: Config) -> pd.Series:
    """The Chandelier exit level: the highest close of the last 22 bars minus
    ``chandelier_mult`` times ATR(atr_window).

    No ratchet is applied here: this is the level implied by the *bars*, not by
    any position. Consumers that hold a position take the running maximum of
    this series since entry so their stop only ever moves up.

    Returns:
        Float Series aligned to ``bars.index``; the 22-bar maximum uses
        ``min_periods=1`` so only the ATR warm-up produces NaN.
    """
    highest_close = bars["close"].rolling(CHANDELIER_WINDOW, min_periods=1).max()
    stop = highest_close - cfg.strategy.chandelier_mult * atr(bars, cfg.strategy.atr_window)
    return stop.astype(float).rename("chandelier_stop")


def earnings_blackout(index: pd.DatetimeIndex, earnings: dt.date | None, cfg: Config) -> pd.Series:
    """True (entries blocked) on each date from ``earnings_blackout_days`` before the
    earnings date through the earnings date itself; all False when the date is unknown.

    Args:
        index: the dates to evaluate.
        earnings: the next scheduled earnings date, or None when unknown. An
            unknown date blocks nothing here — callers tag the pick as
            "earnings unknown" instead of silently skipping it.
        cfg: the loaded configuration.

    Returns:
        Boolean Series aligned to ``index``.
    """
    dates = pd.DatetimeIndex(index)
    if earnings is None:
        return pd.Series(False, index=dates, name="earnings_blackout", dtype=bool)

    days_until = np.asarray((pd.Timestamp(earnings).normalize() - dates.normalize()).days)
    blocked = (days_until >= 0) & (days_until <= cfg.strategy.earnings_blackout_days)
    return pd.Series(blocked, index=dates, name="earnings_blackout", dtype=bool)


def fundamentals_ok(f: Fundamentals | None, rank_below_median: bool, cfg: Config) -> bool:
    """True unless the fundamentals screen is on and this name has *both* EPS growth and
    revenue growth negative while also ranking below the median on momentum.

    The screen is deliberately one-sided: it only ever rejects the worst
    combination (shrinking business *and* weak price momentum). Missing or
    partial data always passes, because free fundamentals are patchy and
    punishing a data gap would quietly bias the universe.

    Args:
        f: the symbol's fundamentals, or None when the provider had none.
        rank_below_median: whether the symbol ranks below the median candidate.
        cfg: the loaded configuration.
    """
    if not cfg.strategy.fundamentals_filter:
        return True
    if f is None:
        return True
    eps_growth = f.eps_growth
    revenue_growth = f.revenue_growth
    if eps_growth is None or revenue_growth is None:
        return True
    return not (eps_growth < 0 and revenue_growth < 0 and rank_below_median)
