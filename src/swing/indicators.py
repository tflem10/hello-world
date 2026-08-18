"""Technical indicators, hand-rolled in pandas.

Why not ta-lib or pandas-ta? ta-lib needs a C library that is a recurring
install headache on Apple silicon, and pandas-ta has been broken against
numpy 2.x. Every indicator here is 3-15 lines, so the dependency is not worth
the fragility. Correctness is pinned by :mod:`tests.test_indicators`, which
checks these against published worked examples and against values that can be
derived analytically.

Smoothing conventions
---------------------
Wilder's indicators (ATR, RSI, ADX, DI) use *Wilder smoothing*: seed with a
simple average of the first ``n`` values, then ``next = (prev * (n-1) + x) / n``.
That is equivalent to an EMA with ``alpha = 1/n``, and it is what every charting
package means by "14-period ATR". Using a plain EMA with ``alpha = 2/(n+1)``
instead would quietly produce different stops than the numbers you see in
thinkorswim.

All functions:
* take/return pandas objects indexed like the input,
* emit ``NaN`` for the warm-up window rather than a partial value, and
* never look ahead — the value at bar *t* uses only bars ``<= t``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "ema",
    "wilder_smooth",
    "true_range",
    "atr",
    "atr_pct",
    "rsi",
    "adx",
    "directional_indicators",
    "donchian_high",
    "donchian_low",
    "rolling_high",
    "rolling_low",
    "dollar_volume",
    "momentum",
    "macd",
    "obv",
    "slope_positive",
]


# ---------------------------------------------------------------------------
# moving averages
# ---------------------------------------------------------------------------
def sma(series: pd.Series, length: int) -> pd.Series:
    """Simple moving average."""
    _check_length(length)
    return series.rolling(window=length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    """Exponential moving average, ``alpha = 2/(length+1)``, seeded with an SMA.

    pandas' ``ewm(adjust=False)`` seeds with the *first observation*, which is
    not what StockCharts, TradingView or thinkorswim do — they seed with an SMA
    of the first ``length`` bars. On a 200-day EMA that difference is visible
    for hundreds of bars, so we seed explicitly.
    """
    _check_length(length)
    values = series.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    if len(values) < length:
        return pd.Series(out, index=series.index)

    valid = ~np.isnan(values)
    first = _first_window_end(valid, length)
    if first is None:
        return pd.Series(out, index=series.index)

    alpha = 2.0 / (length + 1.0)
    out[first] = np.nanmean(values[first - length + 1 : first + 1])
    for i in range(first + 1, len(values)):
        x = values[i]
        out[i] = out[i - 1] if np.isnan(x) else out[i - 1] + alpha * (x - out[i - 1])
    return pd.Series(out, index=series.index)


def wilder_smooth(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing: SMA seed, then ``(prev*(n-1) + x)/n``.

    Implemented via pandas' EWM rather than a Python loop. The recursion
    ``next = prev + (x - prev)/n`` *is* an exponential moving average with
    ``alpha = 1/n``; the only thing that differs from a plain ``ewm`` call is
    the seed. So we overwrite the first in-window observation with the SMA seed
    and let ``ewm(adjust=False)`` — which seeds from its own first observation
    — carry the identical recursion in C. On a 5,000-bar series this is roughly
    20x faster than the loop, and the exact-value indicator tests pin that it
    produces the same numbers.
    """
    _check_length(length)
    values = series.to_numpy(dtype="float64")
    out = np.full(len(values), np.nan)
    if len(values) < length:
        return pd.Series(out, index=series.index)

    # The seed is the mean of the first `length` consecutive non-NaN values.
    valid = ~np.isnan(values)
    first = _first_window_end(valid, length)
    if first is None:
        return pd.Series(out, index=series.index)

    tail = values[first:].copy()
    tail[0] = np.nanmean(values[first - length + 1 : first + 1])
    smoothed = (
        pd.Series(tail).ewm(alpha=1.0 / length, adjust=False, ignore_na=True).mean()
    )
    out[first:] = smoothed.to_numpy()
    return pd.Series(out, index=series.index)


def _first_window_end(valid: np.ndarray, length: int) -> int | None:
    run = 0
    for i, ok in enumerate(valid):
        run = run + 1 if ok else 0
        if run == length:
            return i
    return None


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------
def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(H-L, |H-prevC|, |L-prevC|). First bar falls back to H-L."""
    prev_close = close.shift(1)
    ranges = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = float(high.iloc[0] - low.iloc[0]) if len(high) else np.nan
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average True Range (Wilder). The unit of risk for stops and sizing."""
    return wilder_smooth(true_range(high, low, close), length)


def atr_pct(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14
) -> pd.Series:
    """ATR as a fraction of price — comparable across a $5 stock and a $500 one."""
    return atr(high, low, close, length) / close


# ---------------------------------------------------------------------------
# momentum / oscillators
# ---------------------------------------------------------------------------
def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder smoothing).

    Returns 100 when there are no losses in the window (an unbroken advance),
    which is the conventional treatment of a zero denominator.
    """
    _check_length(length)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = wilder_smooth(gain.iloc[1:], length).reindex(close.index)
    avg_loss = wilder_smooth(loss.iloc[1:], length).reindex(close.index)

    rs = avg_gain / avg_loss
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~(avg_gain.isna() | avg_loss.isna()), np.nan)
    return out


def directional_indicators(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14
) -> pd.DataFrame:
    """+DI, -DI and DX (Wilder). Building blocks for :func:`adx`."""
    _check_length(length)
    up = high.diff()
    down = -low.diff()

    plus_dm = pd.Series(
        np.where((up > down) & (up > 0), up, 0.0), index=high.index, dtype="float64"
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0), down, 0.0), index=high.index, dtype="float64"
    )
    # The first bar has no prior high/low, so it contributes no movement.
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan

    tr_s = wilder_smooth(true_range(high, low, close).iloc[1:], length).reindex(high.index)
    plus_s = wilder_smooth(plus_dm.iloc[1:], length).reindex(high.index)
    minus_s = wilder_smooth(minus_dm.iloc[1:], length).reindex(high.index)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_s / tr_s
        minus_di = 100.0 * minus_s / tr_s
        total = plus_di + minus_di
        dx = 100.0 * (plus_di - minus_di).abs() / total
    dx = dx.where(total != 0.0, 0.0)
    dx = dx.where(~total.isna(), np.nan)

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "dx": dx})


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Average Directional Index — trend *strength*, direction-agnostic.

    Used here only as a filter (is this a trend or a drift?), never as a signal.
    Warm-up is ``2*length`` bars: ``length`` for DI, another ``length`` to
    average DX.
    """
    dx = directional_indicators(high, low, close, length)["dx"]
    return wilder_smooth(dx.dropna(), length).reindex(high.index)


def momentum(close: pd.Series, lookback: int, skip: int = 0) -> pd.Series:
    """Total return over ``lookback`` bars, optionally skipping the most recent ``skip``.

    ``skip`` implements the standard "12-1" style construction: the last few
    days are dropped because short-horizon reversal contaminates the momentum
    signal (Jegadeesh 1990; see docs/indicator-research.md).
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    if skip < 0:
        raise ValueError("skip must be >= 0")
    end = close.shift(skip)
    start = close.shift(skip + lookback)
    return end / start - 1.0


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD line, signal line and histogram."""
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume. Included for the ablation study, not the default rules."""
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


# ---------------------------------------------------------------------------
# channels / extremes
# ---------------------------------------------------------------------------
def rolling_high(series: pd.Series, length: int) -> pd.Series:
    _check_length(length)
    return series.rolling(window=length, min_periods=length).max()


def rolling_low(series: pd.Series, length: int) -> pd.Series:
    _check_length(length)
    return series.rolling(window=length, min_periods=length).min()


def donchian_high(high: pd.Series, length: int = 20, exclude_current: bool = True) -> pd.Series:
    """Highest high of the prior ``length`` bars.

    ``exclude_current=True`` shifts the window back one bar so that "today
    closed above the 20-day high" is a real breakout rather than the tautology
    "today's high is the highest high including today".
    """
    source = high.shift(1) if exclude_current else high
    return rolling_high(source, length)


def donchian_low(low: pd.Series, length: int = 20, exclude_current: bool = True) -> pd.Series:
    source = low.shift(1) if exclude_current else low
    return rolling_low(source, length)


# ---------------------------------------------------------------------------
# volume / misc
# ---------------------------------------------------------------------------
def dollar_volume(close: pd.Series, volume: pd.Series, length: int = 20) -> pd.Series:
    """Rolling average of close x volume — the liquidity screen's unit."""
    return (close * volume).rolling(window=length, min_periods=length).mean()


def slope_positive(series: pd.Series, lookback: int) -> pd.Series:
    """True where ``series`` is above its own value ``lookback`` bars ago.

    A deliberately crude "is this line rising?" test. It is what the Minervini
    trend template actually specifies, and it does not need a regression.
    """
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    prior = series.shift(lookback)
    out = series > prior
    return out.where(~(series.isna() | prior.isna()), other=pd.NA).astype("boolean")


def _check_length(length: int) -> None:
    if length < 1:
        raise ValueError(f"length must be >= 1, got {length}")
