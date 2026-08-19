"""FROZEN CONTRACT 5 — hand-rolled technical indicators.

Everything here is written from the published formulas with pandas/numpy only:
no ta-lib, no pandas-ta. That is deliberate. An indicator we cannot read, audit
and reproduce by hand is an indicator we cannot defend when a backtest looks too
good, so every function below states its formula and its smoothing choice in the
docstring and is unit-tested against independently computed reference values.

Two smoothing families appear:

**Wilder's smoothing** (RSI, ATR, ADX). J. Welles Wilder Jr., *New Concepts in
Technical Trading Systems* (1978), uses a running average that is seeded with a
simple average of the first ``n`` observations and then updated with

    avg_t = avg_{t-1} + (x_t - avg_{t-1}) / n

which is exactly an exponentially weighted mean with ``alpha = 1/n`` and
``adjust=False`` once it has been given that seed. Wilder's published tables use
running *sums* for ADX (``sum_t = sum_{t-1} - sum_{t-1}/n + x_t``); sums are just
``n`` times the running average, so the DI ratios below are identical either way.

**Classic EMA smoothing** (``ema``) uses ``alpha = 2/(n+1)``, seeded with the
simple average of the first ``n`` values — the seeding TA-Lib uses, and the one
that makes the first published value hand-checkable.

Every function returns a ``float64`` Series aligned to the input index, named
after the indicator, with ``NaN`` through the warm-up period — and, for the
smoothed families, ``NaN`` on any bar whose input was missing, so a gap in the
data is always visible as a gap in the indicator (audit BUG-031). Nothing here
looks at the clock, draws a random number, or loops over rows, so the same input
always produces bit-identical output.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "adx",
    "atr",
    "donchian_high",
    "donchian_low",
    "ema",
    "obv",
    "roc",
    "rsi",
    "sma",
    "true_range",
]

_OHLC = ("open", "high", "low", "close")


# ---------------------------------------------------------------------------
# validation + shared smoothing machinery
# ---------------------------------------------------------------------------


def _check_window(n: int, name: str, minimum: int = 1) -> int:
    """Validate a lookback window, raising a plain-English error if it is unusable."""
    if isinstance(n, bool) or not isinstance(n, int | np.integer):
        raise ValueError(f"The {name} lookback must be a whole number of bars, but it is {n!r}.")
    n = int(n)
    if n < minimum:
        raise ValueError(
            f"The {name} lookback must be at least {minimum} bar"
            f"{'s' if minimum != 1 else ''}, but it is {n}."
        )
    return n


def _as_float_series(s: pd.Series, what: str) -> pd.Series:
    """Coerce an input Series to float64, with a plain-English error on bad input."""
    if not isinstance(s, pd.Series):
        raise ValueError(f"{what} must be a pandas Series, but it is a {type(s).__name__}.")
    try:
        return s.astype("float64")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{what} must hold numbers, but it holds {s.dtype} values that cannot be "
            f"read as numbers."
        ) from exc


def _columns(bars: pd.DataFrame, needed: tuple[str, ...], what: str) -> tuple[pd.Series, ...]:
    """Pull the named float columns out of a Contract 3 bars frame."""
    if not isinstance(bars, pd.DataFrame):
        raise ValueError(f"{what} needs a bars DataFrame, but it got a {type(bars).__name__}.")
    missing = [c for c in needed if c not in bars.columns]
    if missing:
        raise ValueError(
            f"{what} needs the column{'s' if len(missing) > 1 else ''} "
            f"{', '.join(repr(m) for m in missing)}, but the bars frame only has "
            f"{', '.join(repr(c) for c in bars.columns)}."
        )
    return tuple(_as_float_series(bars[c], f"The {c!r} column") for c in needed)


def _first_complete_window(valid: np.ndarray, n: int) -> int | None:
    """Start index of the first run of ``n`` consecutive true flags, or None.

    Used to place a smoothing seed: a window that straddles a gap would average
    fewer than ``n`` observations while wearing an ``n``-bar label.
    """
    counts = np.concatenate(([0], np.cumsum(valid, dtype=np.int64)))
    if len(counts) <= n:
        return None
    complete = np.flatnonzero(counts[n:] - counts[:-n] == n)
    return int(complete[0]) if complete.size else None


def _seeded_ewm(values: pd.Series, n: int, alpha: float) -> pd.Series:
    """Exponential mean seeded with the simple average of the first ``n`` observations.

    The seed is placed on the last bar of the first *complete* ``n``-bar window
    — ``n`` consecutive observations with no gap in them; everything before it
    stays NaN, and everything after it is folded in recursively by
    ``avg_t = avg_{t-1} + alpha * (x_t - avg_{t-1})``. pandas' ``ewm`` with
    ``adjust=False`` starts its recursion at the first non-NaN value, so handing
    it a series that is NaN up to the seed reproduces the textbook tables exactly
    while staying fully vectorized.

    Two things guard against a missing bar being smoothed away silently (audit
    BUG-031). The seed window must be complete, because ``mean()`` skips NaN and
    would otherwise average, say, 13 true ranges and call the answer a 14-bar
    average. And the output is re-masked wherever the input was missing, because
    ``ewm`` carries the previous mean forward across a NaN and would otherwise
    return a rescaled series with no NaN in it at all. A bar after a gap resumes
    the recursion with pandas' gap-aware weighting; the gap itself is now
    visible, which is what stops a bad vendor bar from quietly moving stops.
    """
    numbers = values.astype("float64")
    valid = numbers.notna().to_numpy()

    start = _first_complete_window(valid, n)
    if start is None:
        return pd.Series(np.nan, index=values.index, dtype="float64")

    seed_pos = start + n - 1
    seeded = numbers.copy()
    seeded.iloc[:seed_pos] = np.nan
    seeded.iloc[seed_pos] = float(numbers.iloc[start : start + n].mean())
    return seeded.ewm(alpha=alpha, adjust=False).mean().mask(~valid)


def _wilder(values: pd.Series, n: int) -> pd.Series:
    """Wilder's running average: seed with the mean of the first ``n``, then alpha = 1/n."""
    return _seeded_ewm(values, n, 1.0 / n)


# ---------------------------------------------------------------------------
# moving averages
# ---------------------------------------------------------------------------


def sma(s: pd.Series, n: int) -> pd.Series:
    """Simple moving average.

    Formula: ``SMA_t = (x_t + x_{t-1} + ... + x_{t-n+1}) / n``.

    Smoothing: none — every bar in the window carries weight ``1/n``. The first
    ``n - 1`` bars are NaN because the window is not full yet.

    Args:
        s: the series to average, usually a close series.
        n: window length in bars, at least 1.

    Returns:
        A float Series named ``sma_<n>`` on the input index.
    """
    n = _check_window(n, "moving-average")
    values = _as_float_series(s, "The series passed to sma()")
    return values.rolling(window=n, min_periods=n).mean().rename(f"sma_{n}")


def ema(s: pd.Series, n: int) -> pd.Series:
    """Exponential moving average, seeded with a simple average.

    Formula: ``EMA_t = EMA_{t-1} + alpha * (x_t - EMA_{t-1})`` with the classic
    ``alpha = 2 / (n + 1)``.

    Smoothing: NOT Wilder's — this is the standard 2/(n+1) EMA. The recursion is
    seeded with ``EMA_{n-1} = mean(x_0 ... x_{n-1})`` (the same seeding TA-Lib
    uses), so the first published value sits on bar ``n - 1`` and the first
    ``n - 1`` bars are NaN.

    Args:
        s: the series to smooth.
        n: span in bars, at least 1.

    Returns:
        A float Series named ``ema_<n>`` on the input index.
    """
    n = _check_window(n, "moving-average")
    values = _as_float_series(s, "The series passed to ema()")
    return _seeded_ewm(values, n, 2.0 / (n + 1.0)).rename(f"ema_{n}")


# ---------------------------------------------------------------------------
# momentum
# ---------------------------------------------------------------------------


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index.

    Formula: split each close-to-close change into a gain (``max(delta, 0)``) and
    a loss (``max(-delta, 0)``), smooth both, then

        ``RS = avg_gain / avg_loss``  and  ``RSI = 100 - 100 / (1 + RS)``.

    Smoothing: Wilder's. The first average gain/loss is the simple mean of the
    first ``n`` changes; afterwards ``avg_t = avg_{t-1} + (x_t - avg_{t-1}) / n``
    (i.e. ``alpha = 1/n``). The first change lives on bar 1, so the first RSI
    lands on bar ``n`` and bars ``0 .. n-1`` are NaN.

    Zero average loss gives ``RSI = 100`` (an unbroken run of up closes), and a
    zero average gain against a zero average loss — a perfectly flat stretch —
    gives ``RSI = 50``.

    Args:
        s: close series.
        n: lookback in bars, default 14 (Wilder's original).

    Returns:
        A float Series named ``rsi_<n>`` on the input index, bounded 0..100.
    """
    n = _check_window(n, "RSI")
    close = _as_float_series(s, "The series passed to rsi()")

    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    avg_gain = _wilder(gains, n)
    avg_loss = _wilder(losses, n)

    out = pd.Series(np.nan, index=close.index, dtype="float64")
    known = avg_gain.notna() & avg_loss.notna()
    # Guard the division: a flat window makes both averages zero.
    denominator = avg_gain + avg_loss
    flat = known & (denominator == 0.0)
    live = known & (denominator != 0.0)
    # RSI = 100 * avg_gain / (avg_gain + avg_loss) is algebraically identical to
    # 100 - 100/(1 + avg_gain/avg_loss) but never divides by a zero loss.
    out[live] = 100.0 * avg_gain[live] / denominator[live]
    out[flat] = 50.0
    return out.rename(f"rsi_{n}")


def roc(s: pd.Series, n: int) -> pd.Series:
    """Rate of change, in percent.

    Formula: ``ROC_t = 100 * (x_t / x_{t-n} - 1)``.

    Smoothing: none. The first ``n`` bars are NaN. A zero or missing value ``n``
    bars back yields NaN/inf rather than a silently wrong number.

    Args:
        s: the series to measure, usually a close series.
        n: lookback in bars, at least 1.

    Returns:
        A float Series named ``roc_<n>`` on the input index, in percent
        (``5.0`` means +5%).
    """
    n = _check_window(n, "rate-of-change")
    values = _as_float_series(s, "The series passed to roc()")
    prior = values.shift(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (values / prior.where(prior != 0.0) - 1.0) * 100.0
    return out.rename(f"roc_{n}")


# ---------------------------------------------------------------------------
# range / trend strength
# ---------------------------------------------------------------------------


def true_range(bars: pd.DataFrame) -> pd.Series:
    """Wilder's True Range — the building block under ATR and ADX.

    Formula: ``TR_t = max(High_t - Low_t, |High_t - Close_{t-1}|,
    |Low_t - Close_{t-1}|)``.

    Smoothing: none. The first bar has no prior close, so TR is undefined there
    and is returned as NaN rather than being faked as ``High - Low``; ATR and ADX
    therefore start their averages on bar 1, matching Wilder's tables.

    Args:
        bars: Contract 3 bars frame with ``high``, ``low`` and ``close``.

    Returns:
        A float Series named ``true_range`` on the input index.
    """
    high, low, close = _columns(bars, ("high", "low", "close"), "true_range()")
    prior_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prior_close).abs(), (low - prior_close).abs()],
        axis=1,
    )
    out = ranges.max(axis=1, skipna=False)
    return out.astype("float64").rename("true_range")


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's Average True Range.

    Formula: the Wilder average of :func:`true_range`. The first ATR is the
    simple mean of the first ``n`` true ranges; afterwards

        ``ATR_t = ATR_{t-1} + (TR_t - ATR_{t-1}) / n``

    which some texts write as ``ATR_t = (ATR_{t-1} * (n-1) + TR_t) / n``.

    Smoothing: Wilder's (``alpha = 1/n``). TR starts on bar 1 (bar 0 has no prior
    close), so the first ATR lands on bar ``n`` and bars ``0 .. n-1`` are NaN.

    Args:
        bars: Contract 3 bars frame with ``high``, ``low`` and ``close``.
        n: lookback in bars, default 14 (Wilder's original).

    Returns:
        A float Series named ``atr_<n>`` on the input index, in price units.
    """
    n = _check_window(n, "ATR")
    return _wilder(true_range(bars), n).rename(f"atr_{n}")


def _directional_movement(high: pd.Series, low: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Wilder's +DM / -DM: only the larger of the two moves counts, and never both."""
    up_move = high.diff()
    down_move = -low.diff()

    plus = pd.Series(0.0, index=high.index, dtype="float64")
    minus = pd.Series(0.0, index=high.index, dtype="float64")
    plus[(up_move > down_move) & (up_move > 0.0)] = up_move
    minus[(down_move > up_move) & (down_move > 0.0)] = down_move

    # Bar 0 has no prior bar, so directional movement is undefined there.
    undefined = up_move.isna() | down_move.isna()
    plus[undefined] = np.nan
    minus[undefined] = np.nan
    return plus, minus


def adx(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's Average Directional Index — trend strength, ignoring direction.

    Formula, in Wilder's order:

    1. ``+DM_t = High_t - High_{t-1}`` when that exceeds both ``Low_{t-1} - Low_t``
       and zero, else 0; ``-DM_t = Low_{t-1} - Low_t`` when that exceeds both
       ``High_t - High_{t-1}`` and zero, else 0.
    2. Smooth ``+DM``, ``-DM`` and ``TR`` with Wilder's average over ``n``.
    3. ``+DI = 100 * smoothed(+DM) / smoothed(TR)`` and likewise for ``-DI``.
    4. ``DX = 100 * |+DI - -DI| / (+DI + -DI)``.
    5. ``ADX`` = Wilder average of ``DX`` over ``n``.

    Smoothing: Wilder's throughout (``alpha = 1/n``). Wilder's published tables
    keep running *sums* at step 2 rather than averages; the DI ratio in step 3
    divides that difference out, so the result is identical.

    Warm-up: DM/TR start on bar 1, so the DI lines and DX start on bar ``n`` and
    the first ADX — itself an ``n``-bar average of DX — lands on bar ``2n - 1``
    (bar 27 for the default 14). Everything before that is NaN.

    A completely flat stretch makes both DI lines zero; DX is taken as 0 there
    (no directional edge) rather than 0/0.

    Args:
        bars: Contract 3 bars frame with ``high``, ``low`` and ``close``.
        n: lookback in bars, default 14 (Wilder's original).

    Returns:
        A float Series named ``adx_<n>`` on the input index, bounded 0..100.
    """
    n = _check_window(n, "ADX")
    high, low, _close = _columns(bars, ("high", "low", "close"), "adx()")

    plus_dm, minus_dm = _directional_movement(high, low)
    smoothed_tr = _wilder(true_range(bars), n)
    smoothed_plus = _wilder(plus_dm, n)
    smoothed_minus = _wilder(minus_dm, n)

    tr_live = smoothed_tr.where(smoothed_tr != 0.0)
    plus_di = 100.0 * smoothed_plus / tr_live
    minus_di = 100.0 * smoothed_minus / tr_live

    di_sum = plus_di + minus_di
    dx = pd.Series(np.nan, index=high.index, dtype="float64")
    known = di_sum.notna()
    live = known & (di_sum != 0.0)
    dx[live] = 100.0 * (plus_di[live] - minus_di[live]).abs() / di_sum[live]
    dx[known & (di_sum == 0.0)] = 0.0

    return _wilder(dx, n).rename(f"adx_{n}")


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------


def donchian_high(bars: pd.DataFrame, n: int) -> pd.Series:
    """Highest high of the ``n`` bars *before* today — the upper Donchian channel.

    Formula: ``rolling max of High over n bars, shifted forward by 1``. The shift
    is the whole point: the value on bar ``t`` covers bars ``t-n .. t-1`` and
    deliberately excludes bar ``t`` itself, so a breakout test reads as

        ``close_t > donchian_high_t``  ("today closed above the prior n-day high")

    and can never compare today's high with itself, which would make a breakout
    mathematically impossible on the day it happens.

    Smoothing: none. The first ``n`` bars are NaN (``n`` for the window plus the
    one-bar shift, minus the bar the shift frees up).

    Args:
        bars: Contract 3 bars frame with a ``high`` column.
        n: channel length in bars, at least 1.

    Returns:
        A float Series named ``donchian_high_<n>`` on the input index.
    """
    n = _check_window(n, "Donchian channel")
    (high,) = _columns(bars, ("high",), "donchian_high()")
    return high.rolling(window=n, min_periods=n).max().shift(1).rename(f"donchian_high_{n}")


def donchian_low(bars: pd.DataFrame, n: int) -> pd.Series:
    """Lowest low of the ``n`` bars *before* today — the lower Donchian channel.

    Formula: ``rolling min of Low over n bars, shifted forward by 1``, so the
    value on bar ``t`` covers bars ``t-n .. t-1`` and excludes bar ``t``. This
    mirrors :func:`donchian_high`; a breakdown test reads
    ``close_t < donchian_low_t``.

    Smoothing: none. The first ``n`` bars are NaN.

    Args:
        bars: Contract 3 bars frame with a ``low`` column.
        n: channel length in bars, at least 1.

    Returns:
        A float Series named ``donchian_low_<n>`` on the input index.
    """
    n = _check_window(n, "Donchian channel")
    (low,) = _columns(bars, ("low",), "donchian_low()")
    return low.rolling(window=n, min_periods=n).min().shift(1).rename(f"donchian_low_{n}")


# ---------------------------------------------------------------------------
# volume
# ---------------------------------------------------------------------------


def obv(bars: pd.DataFrame) -> pd.Series:
    """On-Balance Volume — Granville's running signed volume total.

    Formula: ``OBV_t = OBV_{t-1} + volume_t`` when ``close_t > close_{t-1}``,
    ``OBV_t = OBV_{t-1} - volume_t`` when ``close_t < close_{t-1}``, and
    ``OBV_t = OBV_{t-1}`` when the close is unchanged.

    Smoothing: none. There is no warm-up: the running total starts at ``0`` on
    the first bar, which has no prior close and so contributes nothing. The
    absolute level of an OBV line is arbitrary by construction (Granville's
    original starts from an arbitrary base); only its slope and divergences carry
    information, so a different starting constant would say exactly the same
    thing.

    **This strategy does not trade OBV.** It appears in no gate, no signal, no
    ranking and no exit, and no config field governs it; it is provided here as
    a tested primitive for exploratory work and future ablation hypotheses only.
    See ``docs/indicator-research.md`` §8 for the evidence review behind that
    decision — reading its presence in this module as an endorsement is a
    misreading (audit DEBT-011).

    Args:
        bars: Contract 3 bars frame with ``close`` and ``volume``.

    Returns:
        A float Series named ``obv`` on the input index.
    """
    close, volume = _columns(bars, ("close", "volume"), "obv()")
    direction = np.sign(close.diff())
    signed = (direction * volume).fillna(0.0)
    return signed.cumsum().astype("float64").rename("obv")
