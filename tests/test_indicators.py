"""AC5 — every indicator checked against independently computed reference values.

The point of this module is that nothing here is allowed to be circular. Each
Wilder-smoothed indicator is computed twice:

* once by :mod:`swing.indicators`, which is vectorized pandas built on
  ``ewm(alpha=1/n, adjust=False)``; and
* once by a from-scratch reference implementation at the top of this file that
  uses plain Python lists and an explicit loop, transcribed directly from the
  published recursion and touching neither pandas nor numpy.

Those two must agree, *and* both must agree with literal expected floats to 4
decimal places on fixed data. A regression would have to break both an
``ewm``-based and a loop-based implementation in exactly the same way, and then
also match a frozen table of numbers, to slip through.

Reference sources for the formulas (values re-derived here, not scraped):

* J. Welles Wilder Jr., *New Concepts in Technical Trading Systems* (Trend
  Research, 1978) — the origin of RSI, ATR and ADX and of the 1/n smoothing.
* StockCharts ChartSchool, "Relative Strength Index (RSI)", which states the
  smoothing verbatim as
  ``Average Gain = [(previous Average Gain) x 13 + current Gain] / 14``:
  https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/relative-strength-index-rsi
* StockCharts ChartSchool, "Average True Range (ATR)", for the three true-range
  candidates and ``Current ATR = [(Prior ATR x 13) + Current TR] / 14``:
  https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/average-true-range-atr
* StockCharts ChartSchool, "Average Directional Index (ADX)", for the +DM/-DM
  rules, ``First TR14 = Sum of first 14 periods of TR1``,
  ``DX = 100 x |+DI14 - -DI14| / (+DI14 + -DI14)`` and
  ``First ADX14 = 14 period Average of DX``:
  https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/average-directional-index-adx

:data:`WILDER_RSI_CLOSES` is the 33-close series that Wilder's RSI worked
example is built on and that ChartSchool reproduces in its calculation
spreadsheet; its first 14-period RSI is published rounded as ``70.53``, which
:func:`test_rsi_matches_wilder_worked_example` pins to 4 decimals as
``70.5328``.

One documented deviation from the ChartSchool ATR page: that page fills the
*first* bar's true range with ``High - Low`` because there is no prior close.
This module returns NaN there instead — see
:func:`test_true_range_is_undefined_on_the_first_bar` — so that ATR and ADX
start from the same bar and neither is seeded with a value that silently
ignores the gap term. The consequence is a one-bar-later warm-up than
ChartSchool's spreadsheet and agreement with TA-Lib.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd
import pytest

from conftest import make_bars
from swing.indicators import (
    _directional_movement,
    adx,
    atr,
    donchian_high,
    donchian_low,
    ema,
    obv,
    roc,
    rsi,
    sma,
    true_range,
)

# ---------------------------------------------------------------------------
# from-scratch reference implementations: plain Python, explicit loops,
# no pandas and no numpy, transcribed from the published recursions
# ---------------------------------------------------------------------------

Number = float | None


def ref_wilder(values: Sequence[Number], n: int) -> list[Number]:
    """Wilder's running average: mean of the first n observations, then 1/n updates."""
    out: list[Number] = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None or start + n > len(values):
        return out
    seed_pos = start + n - 1
    window = [v for v in values[start : start + n] if v is not None]
    avg = sum(window) / n
    out[seed_pos] = avg
    for i in range(seed_pos + 1, len(values)):
        value = values[i]
        assert value is not None
        avg = avg + (value - avg) / n
        out[i] = avg
    return out


def ref_sma(values: Sequence[float], n: int) -> list[Number]:
    """SMA_t = mean of the n most recent values."""
    return [None if i < n - 1 else sum(values[i - n + 1 : i + 1]) / n for i in range(len(values))]


def ref_ema(values: Sequence[float], n: int) -> list[Number]:
    """EMA with alpha = 2/(n+1), seeded with the simple mean of the first n values."""
    out: list[Number] = [None] * len(values)
    if len(values) < n:
        return out
    alpha = 2.0 / (n + 1.0)
    avg = sum(values[:n]) / n
    out[n - 1] = avg
    for i in range(n, len(values)):
        avg = avg + alpha * (values[i] - avg)
        out[i] = avg
    return out


def ref_rsi(closes: Sequence[float], n: int) -> list[Number]:
    """RSI = 100 - 100/(1 + avg_gain/avg_loss), both averages Wilder-smoothed."""
    gains: list[Number] = [None]
    losses: list[Number] = [None]
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = ref_wilder(gains, n)
    avg_loss = ref_wilder(losses, n)

    out: list[Number] = []
    for g, ln in zip(avg_gain, avg_loss, strict=True):
        if g is None or ln is None:
            out.append(None)
        elif g + ln == 0.0:
            out.append(50.0)
        elif ln == 0.0:
            out.append(100.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + g / ln))
    return out


def ref_true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[Number]:
    """TR = max(H-L, |H - prior close|, |L - prior close|); undefined on bar 0."""
    out: list[Number] = [None]
    for i in range(1, len(closes)):
        prior = closes[i - 1]
        out.append(max(highs[i] - lows[i], abs(highs[i] - prior), abs(lows[i] - prior)))
    return out


def ref_atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int
) -> list[Number]:
    """ATR = Wilder average of true range."""
    return ref_wilder(ref_true_range(highs, lows, closes), n)


def ref_adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int
) -> list[Number]:
    """ADX = Wilder average of DX, where DX = 100*|+DI - -DI|/(+DI + -DI)."""
    plus_dm: list[Number] = [None]
    minus_dm: list[Number] = [None]
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0.0) else 0.0)
        minus_dm.append(down if (down > up and down > 0.0) else 0.0)

    smooth_tr = ref_wilder(ref_true_range(highs, lows, closes), n)
    smooth_plus = ref_wilder(plus_dm, n)
    smooth_minus = ref_wilder(minus_dm, n)

    dx: list[Number] = []
    for tr, p, m in zip(smooth_tr, smooth_plus, smooth_minus, strict=True):
        if tr is None or p is None or m is None or tr == 0.0:
            dx.append(None)
            continue
        plus_di = 100.0 * p / tr
        minus_di = 100.0 * m / tr
        total = plus_di + minus_di
        dx.append(0.0 if total == 0.0 else 100.0 * abs(plus_di - minus_di) / total)
    return ref_wilder(dx, n)


def ref_roc(values: Sequence[float], n: int) -> list[Number]:
    """ROC = 100 * (x_t / x_{t-n} - 1)."""
    return [
        None if i < n else 100.0 * (values[i] / values[i - n] - 1.0) for i in range(len(values))
    ]


def ref_obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """OBV: running total, plus volume on an up close, minus on a down close."""
    out = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out.append(out[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            out.append(out[-1] - volumes[i])
        else:
            out.append(out[-1])
    return out


# ---------------------------------------------------------------------------
# fixed reference data
# ---------------------------------------------------------------------------

#: Wilder's 14-period RSI worked example (New Concepts, 1978), the same 33
#: closes ChartSchool reproduces in its RSI calculation spreadsheet.
# fmt: off
# These are frozen data tables, laid out as tables on purpose: the row shape is
# how a human checks them against a spreadsheet. The formatter would put one
# number per line and make them unreadable, so it is switched off until the
# `fmt: on` below.
WILDER_RSI_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245,
    45.8433, 46.0826, 45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028,
    46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515,
    45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628,
    43.1314,
]

#: Published 14-period RSI for :data:`WILDER_RSI_CLOSES`, to 4 decimals. The
#: first value is the familiar 70.53 of the textbook table.
WILDER_RSI_EXPECTED = [
    70.5328, 66.3186, 66.5498, 69.4063, 66.3552, 57.9749, 62.9296, 63.2571,
    56.0593, 62.3771, 54.7076, 50.4228, 39.9898, 41.4605, 41.8689, 45.4632,
    37.3040, 33.0795, 37.7730,
]

#: A fixed 42-bar OHLCV path — a clean uptrend, an 8-bar pullback, then a
#: sideways drift — so ADX has something real to measure. Frozen literals: these
#: exact numbers are what the expected values below were derived from.
HIGHS = [
    51.67, 52.72, 54.19, 55.22, 56.06, 56.47, 58.15,
    58.59, 59.97, 61.20, 60.82, 62.41, 63.16, 63.87,
    64.26, 65.32, 64.93, 64.68, 63.59, 63.12, 61.92,
    61.26, 60.13, 59.37, 59.99, 58.58, 58.46, 59.33,
    59.02, 59.02, 59.71, 59.76, 60.31, 61.29, 60.99,
    61.26, 61.87, 62.70, 63.64, 63.20, 63.58, 62.83,
]
LOWS = [
    49.97, 51.69, 53.14, 53.68, 54.65, 55.45, 56.61,
    57.77, 58.70, 59.19, 59.95, 61.12, 62.09, 62.46,
    63.24, 63.78, 63.94, 63.50, 62.41, 61.91, 60.78,
    60.05, 59.15, 57.77, 58.60, 57.18, 56.92, 57.92,
    57.58, 57.49, 58.33, 58.46, 59.18, 59.73, 59.73,
    60.21, 60.63, 61.89, 61.59, 62.25, 62.12, 61.85,
]
CLOSES = [
    50.90, 52.07, 53.55, 53.99, 55.26, 55.97, 56.97,
    58.13, 59.30, 60.24, 60.36, 61.82, 62.48, 63.03,
    63.81, 64.92, 64.57, 63.96, 62.91, 62.53, 61.55,
    60.38, 59.53, 58.35, 58.92, 58.19, 57.79, 58.36,
    58.35, 58.22, 58.83, 59.10, 59.58, 60.40, 60.53,
    60.87, 61.26, 62.21, 61.93, 62.62, 62.72, 62.46,
]
VOLUMES = [
    845000.0, 822000.0, 960000.0, 1175000.0, 897000.0, 1043000.0, 804000.0,
    929000.0, 918000.0, 958000.0, 776000.0, 997000.0, 1052000.0, 1046000.0,
    857000.0, 886000.0, 1033000.0, 920000.0, 1090000.0, 1069000.0, 1093000.0,
    819000.0, 874000.0, 1057000.0, 756000.0, 792000.0, 1102000.0, 973000.0,
    1189000.0, 893000.0, 1079000.0, 1084000.0, 899000.0, 819000.0, 992000.0,
    963000.0, 968000.0, 1199000.0, 964000.0, 975000.0, 881000.0, 947000.0,
]

#: 14-period ATR of the fixed table, first value on bar 14, to 4 decimals.
ATR14_EXPECTED = [
    1.6743, 1.6647, 1.6165, 1.5853, 1.5828, 1.5562, 1.5700, 1.5650, 1.5411,
    1.5567, 1.5627, 1.5753, 1.5728, 1.5705, 1.5611, 1.5589, 1.5540, 1.5359,
    1.5126, 1.5267, 1.5076, 1.4749, 1.4582, 1.4569, 1.4992, 1.4829, 1.4812,
    1.4454,
]

#: 14-period ADX of the fixed table, first value on bar 27 (= 2n - 1).
ADX14_EXPECTED = [
    51.6436, 48.5794, 45.6694, 43.3911, 41.3063, 39.7131, 38.7944, 37.9414,
    37.3061, 37.0614, 37.2627, 37.8774, 38.4483, 39.1509, 39.4536,
]
# fmt: on


def fixed_bars() -> pd.DataFrame:
    """The frozen OHLCV table as a Contract 3 bars frame."""
    opens = [round((h + ln) / 2.0, 2) for h, ln in zip(HIGHS, LOWS, strict=True)]
    return pd.DataFrame(
        {
            "open": opens,
            "high": HIGHS,
            "low": LOWS,
            "close": CLOSES,
            "volume": VOLUMES,
        },
        index=pd.bdate_range("2021-03-01", periods=len(CLOSES)),
        dtype="float64",
    )


def assert_close(actual: pd.Series, expected: Sequence[Number], places: int = 10) -> None:
    """Compare a Series against a reference list, matching NaN against ``None``."""
    assert len(actual) == len(expected)
    for i, (got, want) in enumerate(zip(actual.tolist(), expected, strict=True)):
        if want is None:
            assert math.isnan(got), f"position {i}: expected NaN, got {got}"
        else:
            assert not math.isnan(got), f"position {i}: expected {want}, got NaN"
            assert round(got, places) == round(want, places), f"position {i}: {got} != {want}"


def valid_values(s: pd.Series, places: int = 4) -> list[float]:
    """Non-NaN values of a Series, rounded, for comparison against literals."""
    return [round(v, places) for v in s.dropna().tolist()]


def first_valid_position(s: pd.Series) -> int | None:
    """Index position of the first non-NaN value, or None if there is none."""
    mask = s.notna().to_numpy()
    return int(np.argmax(mask)) if mask.any() else None


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------


def test_sma_hand_computed_literals() -> None:
    """SMA-3 of a series anyone can average in their head."""
    s = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0, 0.0])
    # windows: (2,4,6)/3=4, (4,6,8)/3=6, (6,8,10)/3=8, (8,10,0)/3=6
    assert_close(sma(s, 3), [None, None, 4.0, 6.0, 8.0, 6.0])


def test_sma_matches_reference_implementation() -> None:
    close = fixed_bars()["close"]
    for n in (2, 5, 14, 20, 50):
        assert_close(sma(close, n), ref_sma(CLOSES, n))


def test_sma_warm_up_is_exactly_n_minus_one_bars() -> None:
    close = fixed_bars()["close"]
    for n in (3, 14, 20):
        assert first_valid_position(sma(close, n)) == n - 1


def test_sma_window_longer_than_the_series_is_all_nan() -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    assert sma(s, 10).isna().all()


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------


def test_ema_hand_computed_literals() -> None:
    """EMA-3 seeded with the SMA of the first 3 bars, then alpha = 2/(3+1) = 0.5."""
    s = pd.Series([10.0, 20.0, 30.0, 40.0, 100.0])
    # seed = (10+20+30)/3 = 20; 20 + 0.5*(40-20) = 30; 30 + 0.5*(100-30) = 65
    assert_close(ema(s, 3), [None, None, 20.0, 30.0, 65.0])


def test_ema_alpha_is_two_over_n_plus_one_not_wilder() -> None:
    """EMA-4 uses alpha = 0.4; Wilder's 1/4 = 0.25 would give a different number."""
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 14.0])
    # seed = (1+2+3+4)/4 = 2.5; 2.5 + 0.4*(14-2.5) = 7.1
    assert_close(ema(s, 4), [None, None, None, 2.5, 7.1])
    wilder_would_be = 2.5 + 0.25 * (14.0 - 2.5)
    assert ema(s, 4).iloc[-1] != pytest.approx(wilder_would_be)


def test_ema_matches_reference_implementation() -> None:
    close = fixed_bars()["close"]
    for n in (3, 9, 12, 26):
        assert_close(ema(close, n), ref_ema(CLOSES, n))


def test_ema_warm_up_is_exactly_n_minus_one_bars() -> None:
    close = fixed_bars()["close"]
    for n in (5, 12, 26):
        assert first_valid_position(ema(close, n)) == n - 1


# ---------------------------------------------------------------------------
# RSI — Wilder's worked example
# ---------------------------------------------------------------------------


def test_rsi_matches_wilder_worked_example() -> None:
    """AC5: RSI-14 on Wilder's published series, pinned to 4 decimals.

    Wilder, *New Concepts in Technical Trading Systems* (1978); the same table
    ChartSchool reproduces, whose first published value is 70.53.
    """
    result = rsi(pd.Series(WILDER_RSI_CLOSES), 14)
    assert valid_values(result) == WILDER_RSI_EXPECTED
    assert round(result.iloc[14], 2) == 70.53


def test_rsi_worked_example_agrees_with_from_scratch_reference() -> None:
    """The pandas ewm path and the plain-Python loop path must agree exactly."""
    assert_close(rsi(pd.Series(WILDER_RSI_CLOSES), 14), ref_rsi(WILDER_RSI_CLOSES, 14))


def test_rsi_first_value_is_the_simple_average_seed() -> None:
    """The seed really is the simple mean of the first 14 gains and losses."""
    closes = WILDER_RSI_CLOSES
    deltas = [closes[i] - closes[i - 1] for i in range(1, 15)]
    avg_gain = sum(max(d, 0.0) for d in deltas) / 14
    avg_loss = sum(max(-d, 0.0) for d in deltas) / 14
    expected = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    assert rsi(pd.Series(closes), 14).iloc[14] == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("n", [2, 3, 5, 14, 21])
def test_rsi_matches_reference_across_windows(n: int) -> None:
    assert_close(rsi(pd.Series(CLOSES), n), ref_rsi(CLOSES, n))


def test_rsi_first_value_lands_on_bar_n() -> None:
    """One bar for the first close-to-close change plus n-1 more to average."""
    for n in (2, 14, 21):
        assert first_valid_position(rsi(pd.Series(CLOSES), n)) == n


def test_rsi_window_of_two_is_supported() -> None:
    """RSI(2) is a documented mean-reversion window; it must not be a special case."""
    result = rsi(pd.Series(WILDER_RSI_CLOSES), 2)
    assert first_valid_position(result) == 2
    assert_close(result, ref_rsi(WILDER_RSI_CLOSES, 2))
    assert result.dropna().between(0.0, 100.0).all()


def test_rsi_is_one_hundred_when_every_close_rises() -> None:
    s = pd.Series([float(i) for i in range(1, 30)])
    assert (rsi(s, 14).dropna() == 100.0).all()


def test_rsi_is_zero_when_every_close_falls() -> None:
    s = pd.Series([float(i) for i in range(30, 1, -1)])
    assert (rsi(s, 14).dropna() == 0.0).all()


def test_rsi_is_fifty_on_a_perfectly_flat_series() -> None:
    """No gains and no losses is 0/0; the neutral reading is the sane answer."""
    s = pd.Series([25.0] * 30)
    assert (rsi(s, 14).dropna() == 50.0).all()


def test_rsi_stays_within_zero_and_one_hundred() -> None:
    result = rsi(make_bars(400, trend=0.001)["close"], 14).dropna()
    assert not result.empty
    assert result.between(0.0, 100.0).all()


# ---------------------------------------------------------------------------
# True range and ATR
# ---------------------------------------------------------------------------


def test_true_range_hand_computed_literals() -> None:
    """Each of the three true-range candidates wins on one of these bars."""
    bars = pd.DataFrame(
        {
            "open": [10.0, 11.0, 10.0, 12.0],
            "high": [11.0, 12.0, 11.0, 13.0],
            "low": [9.0, 11.5, 9.5, 12.5],
            "close": [10.0, 11.8, 9.6, 12.8],
        }
    )
    # bar 1: H-L = 0.5, |H - 10| = 2.0 (wins), |L - 10| = 1.5
    # bar 2: H-L = 1.5 (wins), |H - 11.8| = 0.8, |L - 11.8| = 2.3 -> 2.3 wins
    # bar 3: H-L = 0.5, |H - 9.6| = 3.4 (wins), |L - 9.6| = 2.9
    assert_close(true_range(bars), [None, 2.0, 2.3, 3.4])


def test_true_range_is_undefined_on_the_first_bar() -> None:
    """Documented deviation: bar 0 is NaN, not High - Low.

    ChartSchool's ATR spreadsheet fills bar 0 with ``High - Low`` because there
    is no prior close. We return NaN so that ATR and ADX warm up from the same
    bar and neither is seeded with a value that ignores the gap term; this
    matches TA-Lib, at the cost of one extra warm-up bar.
    """
    bars = fixed_bars()
    tr = true_range(bars)
    assert math.isnan(tr.iloc[0])
    assert tr.iloc[1:].notna().all()
    # NaN compares unequal to everything, including the value ChartSchool would use.
    assert tr.iloc[0] != HIGHS[0] - LOWS[0]


def test_true_range_matches_reference_implementation() -> None:
    assert_close(true_range(fixed_bars()), ref_true_range(HIGHS, LOWS, CLOSES))


def test_atr_matches_frozen_literals() -> None:
    """AC5: 14-period ATR of the frozen table, to 4 decimals."""
    result = atr(fixed_bars(), 14)
    assert first_valid_position(result) == 14
    assert valid_values(result) == ATR14_EXPECTED


def test_atr_agrees_with_from_scratch_reference() -> None:
    assert_close(atr(fixed_bars(), 14), ref_atr(HIGHS, LOWS, CLOSES, 14))


def test_atr_seed_is_the_simple_average_of_the_first_n_true_ranges() -> None:
    """Wilder's seeding, checked directly rather than through the recursion."""
    bars = fixed_bars()
    tr = ref_true_range(HIGHS, LOWS, CLOSES)
    seed = sum(v for v in tr[1:15] if v is not None) / 14
    assert atr(bars, 14).iloc[14] == pytest.approx(seed, abs=1e-12)


def test_atr_recursion_is_wilder_not_a_simple_average() -> None:
    """ATR_t = (ATR_{t-1} * 13 + TR_t) / 14, stated the way ChartSchool states it."""
    bars = fixed_bars()
    values = atr(bars, 14)
    tr = true_range(bars)
    for pos in range(15, len(values)):
        expected = (values.iloc[pos - 1] * 13.0 + tr.iloc[pos]) / 14.0
        assert values.iloc[pos] == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("n", [2, 5, 14, 20])
def test_atr_matches_reference_across_windows(n: int) -> None:
    assert_close(atr(fixed_bars(), n), ref_atr(HIGHS, LOWS, CLOSES, n))


def test_atr_is_never_negative() -> None:
    result = atr(make_bars(300, trend=-0.001), 14).dropna()
    assert not result.empty
    assert (result >= 0.0).all()


def test_atr_on_too_short_a_frame_is_all_nan() -> None:
    bars = fixed_bars().iloc[:10]
    assert atr(bars, 14).isna().all()


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


def test_adx_matches_frozen_literals() -> None:
    """AC5: 14-period ADX of the frozen table, to 4 decimals."""
    result = adx(fixed_bars(), 14)
    assert valid_values(result) == ADX14_EXPECTED


def test_adx_agrees_with_from_scratch_reference() -> None:
    assert_close(adx(fixed_bars(), 14), ref_adx(HIGHS, LOWS, CLOSES, 14))


def test_adx_first_value_lands_on_bar_two_n_minus_one() -> None:
    """DX starts on bar n; averaging n of those puts the first ADX on bar 2n-1."""
    bars = fixed_bars()
    for n in (5, 10, 14):
        assert first_valid_position(adx(bars, n)) == 2 * n - 1


@pytest.mark.parametrize("n", [3, 7, 14])
def test_adx_matches_reference_across_windows(n: int) -> None:
    assert_close(adx(fixed_bars(), n), ref_adx(HIGHS, LOWS, CLOSES, n))


def test_adx_is_high_in_a_clean_one_way_trend() -> None:
    """A strictly rising staircase has -DM of zero throughout, so DX pins at 100."""
    n = 60
    highs = [100.0 + i for i in range(n)]
    lows = [99.0 + i for i in range(n)]
    closes = [99.5 + i for i in range(n)]
    bars = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1e6] * n}
    )
    assert adx(bars, 14).dropna().iloc[-1] == pytest.approx(100.0, abs=1e-9)


def test_adx_is_zero_on_a_flat_market() -> None:
    """No directional movement at all: DX is defined as 0, not 0/0."""
    n = 60
    bars = pd.DataFrame(
        {
            "open": [50.0] * n,
            "high": [51.0] * n,
            "low": [49.0] * n,
            "close": [50.0] * n,
            "volume": [1e6] * n,
        }
    )
    result = adx(bars, 14).dropna()
    assert not result.empty
    assert (result == 0.0).all()


def test_adx_stays_within_zero_and_one_hundred() -> None:
    result = adx(make_bars(500, trend=0.0008), 14).dropna()
    assert not result.empty
    assert result.between(0.0, 100.0).all()


def test_adx_never_counts_both_directions_on_one_bar() -> None:
    """Wilder's rule: only the larger move counts, so +DM and -DM are never both live.

    An outside day (higher high *and* lower low) is the case that catches a naive
    implementation: it would award directional movement to both sides and let
    +DI and -DI double-count the same bar.
    """
    bars = make_bars(300, trend=0.001)
    plus_dm, minus_dm = _directional_movement(bars["high"], bars["low"])

    outside_days = (bars["high"].diff() > 0) & (-bars["low"].diff() > 0)
    assert outside_days.any(), "fixture should contain at least one outside day"
    assert not ((plus_dm > 0.0) & (minus_dm > 0.0)).any()
    assert (plus_dm.dropna() >= 0.0).all()
    assert (minus_dm.dropna() >= 0.0).all()


# ---------------------------------------------------------------------------
# Donchian channels — the shift-by-1 semantic the breakout rule depends on
# ---------------------------------------------------------------------------


def breakout_bars() -> pd.DataFrame:
    """Ten bars engineered around one breakout day and one breakdown day.

    Bar 5 is the breakout: its close (13) clears the highest high of bars 2-4
    (11) while its *own* high (15) is the biggest number in any window that
    touches it. An implementation that forgot the ``shift(1)`` would compare 13
    against 15 and find no breakout at all; one that shifted twice would flag
    bar 6 instead. Bar 8 is the mirror image on the low side.
    """
    highs = [10.0, 12.0, 11.0, 10.0, 10.0, 15.0, 12.0, 11.0, 11.0, 11.0]
    lows = [9.0, 10.0, 10.0, 9.0, 9.0, 11.0, 10.0, 10.0, 7.0, 10.0]
    closes = [10.0, 11.0, 10.0, 10.0, 9.0, 13.0, 12.0, 11.0, 8.0, 11.0]
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1e6] * len(closes),
        },
        index=pd.bdate_range("2022-01-03", periods=len(closes)),
    )


def test_donchian_high_excludes_today() -> None:
    """The value on bar t covers bars t-n .. t-1 and never bar t itself."""
    bars = breakout_bars()
    assert_close(
        donchian_high(bars, 3),
        [None, None, None, 12.0, 12.0, 11.0, 15.0, 15.0, 15.0, 12.0],
    )


def test_donchian_low_excludes_today() -> None:
    bars = breakout_bars()
    assert_close(
        donchian_low(bars, 3),
        [None, None, None, 9.0, 9.0, 9.0, 9.0, 9.0, 10.0, 7.0],
    )


def test_breakout_fires_on_the_right_day() -> None:
    """AC5/Contract 5: ``close > donchian_high`` must be True on exactly bar 5."""
    bars = breakout_bars()
    breakout = bars["close"] > donchian_high(bars, 3)
    assert breakout.sum() == 1
    assert breakout.iloc[5]
    assert breakout.idxmax() == bars.index[5]


def test_breakout_would_be_impossible_without_the_shift() -> None:
    """Proves the shift is load-bearing, not decorative."""
    bars = breakout_bars()
    unshifted = bars["high"].rolling(3, min_periods=3).max()
    assert not (bars["close"] > unshifted).any()
    assert (bars["close"] > donchian_high(bars, 3)).any()


def test_breakdown_fires_on_the_right_day() -> None:
    bars = breakout_bars()
    breakdown = bars["close"] < donchian_low(bars, 3)
    assert breakdown.sum() == 1
    assert breakdown.iloc[8]


def test_donchian_warm_up_is_exactly_n_bars() -> None:
    """n bars to fill the window, then the shift gives one back: first value on bar n."""
    bars = fixed_bars()
    for n in (3, 10, 20):
        assert first_valid_position(donchian_high(bars, n)) == n
        assert first_valid_position(donchian_low(bars, n)) == n


def test_donchian_matches_a_hand_rolled_prior_window_scan() -> None:
    bars = fixed_bars()
    n = 20
    expected_high: list[Number] = [
        None if i < n else max(HIGHS[i - n : i]) for i in range(len(HIGHS))
    ]
    expected_low: list[Number] = [None if i < n else min(LOWS[i - n : i]) for i in range(len(LOWS))]
    assert_close(donchian_high(bars, n), expected_high)
    assert_close(donchian_low(bars, n), expected_low)


def test_donchian_high_is_always_at_or_above_donchian_low() -> None:
    bars = make_bars(300, trend=0.0005)
    high_channel = donchian_high(bars, 20)
    low_channel = donchian_low(bars, 20)
    both = high_channel.notna() & low_channel.notna()
    assert both.any()
    assert (high_channel[both] >= low_channel[both]).all()


# ---------------------------------------------------------------------------
# ROC
# ---------------------------------------------------------------------------


def test_roc_hand_computed_literals() -> None:
    """ROC is a percent, so +5% is 5.0 and not 0.05."""
    s = pd.Series([100.0, 105.0, 110.0, 90.0])
    assert_close(
        roc(s, 1), [None, 5.0, 100.0 * (110.0 / 105.0 - 1.0), 100.0 * (90.0 / 110.0 - 1.0)]
    )
    assert round(roc(s, 1).iloc[2], 4) == 4.7619
    assert round(roc(s, 1).iloc[3], 4) == -18.1818


def test_roc_over_multiple_bars() -> None:
    s = pd.Series([100.0, 105.0, 110.0, 90.0])
    assert_close(roc(s, 2), [None, None, 10.0, 100.0 * (90.0 / 105.0 - 1.0)])
    assert round(roc(s, 2).iloc[3], 4) == -14.2857


def test_roc_matches_reference_implementation() -> None:
    close = fixed_bars()["close"]
    for n in (1, 5, 21, 63):
        assert_close(roc(close, n), ref_roc(CLOSES, n))


def test_roc_warm_up_is_exactly_n_bars() -> None:
    close = fixed_bars()["close"]
    for n in (1, 5, 21):
        assert first_valid_position(roc(close, n)) == n


def test_roc_of_a_flat_series_is_zero() -> None:
    s = pd.Series([42.0] * 10)
    assert (roc(s, 3).dropna() == 0.0).all()


def test_roc_against_a_zero_base_is_nan_not_infinity() -> None:
    """A zero price n bars back would divide by zero; NaN is the honest answer."""
    s = pd.Series([0.0, 1.0, 2.0, 3.0])
    assert math.isnan(roc(s, 3).iloc[3])


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------


def test_obv_hand_computed_literals() -> None:
    """Up close adds volume, down close subtracts it, unchanged close does nothing."""
    bars = pd.DataFrame(
        {
            "open": [10.0, 11.0, 11.0, 9.0, 12.0],
            "high": [10.0, 11.0, 11.0, 9.0, 12.0],
            "low": [10.0, 11.0, 11.0, 9.0, 12.0],
            "close": [10.0, 11.0, 11.0, 9.0, 12.0],
            "volume": [100.0, 200.0, 300.0, 400.0, 500.0],
        }
    )
    assert_close(obv(bars), [0.0, 200.0, 200.0, -200.0, 300.0])


def test_obv_matches_reference_implementation() -> None:
    assert_close(obv(fixed_bars()), ref_obv(CLOSES, VOLUMES))


def test_obv_has_no_warm_up_and_starts_at_zero() -> None:
    """The level is arbitrary by construction; only the slope carries information."""
    result = obv(fixed_bars())
    assert result.notna().all()
    assert result.iloc[0] == 0.0


def test_obv_is_monotonic_when_every_close_rises() -> None:
    n = 20
    closes = [float(100 + i) for i in range(n)]
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1000.0] * n,
        }
    )
    result = obv(bars)
    assert result.iloc[-1] == 19_000.0
    assert result.diff().dropna().gt(0).all()


# ---------------------------------------------------------------------------
# cross-cutting properties
# ---------------------------------------------------------------------------

SERIES_INDICATORS: list[tuple[str, Callable[[pd.Series], pd.Series]]] = [
    ("sma", lambda s: sma(s, 20)),
    ("ema", lambda s: ema(s, 20)),
    ("rsi", lambda s: rsi(s, 14)),
    ("roc", lambda s: roc(s, 10)),
]

FRAME_INDICATORS: list[tuple[str, Callable[[pd.DataFrame], pd.Series]]] = [
    ("true_range", true_range),
    ("atr", lambda b: atr(b, 14)),
    ("adx", lambda b: adx(b, 14)),
    ("donchian_high", lambda b: donchian_high(b, 20)),
    ("donchian_low", lambda b: donchian_low(b, 20)),
    ("obv", obv),
]


@pytest.mark.parametrize("name,fn", SERIES_INDICATORS, ids=[n for n, _ in SERIES_INDICATORS])
def test_series_indicators_align_to_the_input_index(
    name: str, fn: Callable[[pd.Series], pd.Series]
) -> None:
    bars = make_bars(200)
    result = fn(bars["close"])
    assert isinstance(result, pd.Series)
    assert result.index.equals(bars.index)
    assert result.dtype == np.float64
    assert result.name


@pytest.mark.parametrize("name,fn", FRAME_INDICATORS, ids=[n for n, _ in FRAME_INDICATORS])
def test_frame_indicators_align_to_the_input_index(
    name: str, fn: Callable[[pd.DataFrame], pd.Series]
) -> None:
    bars = make_bars(200)
    result = fn(bars)
    assert isinstance(result, pd.Series)
    assert result.index.equals(bars.index)
    assert result.dtype == np.float64
    assert result.name


@pytest.mark.parametrize("name,fn", FRAME_INDICATORS, ids=[n for n, _ in FRAME_INDICATORS])
def test_frame_indicators_are_deterministic(
    name: str, fn: Callable[[pd.DataFrame], pd.Series]
) -> None:
    bars = make_bars(200)
    first = fn(bars)
    second = fn(bars.copy())
    pd.testing.assert_series_equal(first, second)


@pytest.mark.parametrize("name,fn", FRAME_INDICATORS, ids=[n for n, _ in FRAME_INDICATORS])
def test_frame_indicators_do_not_look_ahead(
    name: str, fn: Callable[[pd.DataFrame], pd.Series]
) -> None:
    """Truncating the future must not change a single past value.

    This is the property the backtest engine depends on: the value the scanner
    reads at ``.iloc[-1]`` today has to be the same number the engine reads for
    that date years later.
    """
    bars = make_bars(200)
    full = fn(bars)
    truncated = fn(bars.iloc[:150])
    pd.testing.assert_series_equal(full.iloc[:150], truncated)


@pytest.mark.parametrize("name,fn", FRAME_INDICATORS, ids=[n for n, _ in FRAME_INDICATORS])
def test_frame_indicators_do_not_mutate_their_input(
    name: str, fn: Callable[[pd.DataFrame], pd.Series]
) -> None:
    bars = make_bars(120)
    before = bars.copy(deep=True)
    fn(bars)
    pd.testing.assert_frame_equal(bars, before)


@pytest.mark.parametrize("name,fn", FRAME_INDICATORS, ids=[n for n, _ in FRAME_INDICATORS])
def test_frame_indicators_survive_a_one_bar_frame(
    name: str, fn: Callable[[pd.DataFrame], pd.Series]
) -> None:
    bars = make_bars(1)
    result = fn(bars)
    assert len(result) == 1


def test_indicators_accept_integer_volume_columns() -> None:
    """yfinance hands back int64 volume; OBV must not choke on it."""
    bars = make_bars(50)
    bars["volume"] = bars["volume"].astype("int64")
    result = obv(bars)
    assert result.dtype == np.float64
    assert result.notna().all()


# ---------------------------------------------------------------------------
# gaps in the input: a missing bar must be visible (audit BUG-031)
# ---------------------------------------------------------------------------


def gapped_series(n: int = 12, gap_at: int = 7) -> pd.Series:
    """``1.0 .. n`` with one interior value missing."""
    values = [float(i + 1) for i in range(n)]
    values[gap_at] = float("nan")
    return pd.Series(values)


def test_a_gap_inside_the_seed_window_pushes_the_seed_to_a_complete_one() -> None:
    """The seed must average ``n`` observations, not ``n`` minus the missing ones.

    With a gap at index 2 and a 5-bar span, the first complete window is
    ``[4, 5, 6, 7, 8]`` at indices 3..7, so the seed is 6.0 on index 7. The old
    code seeded on index 4 from ``mean([1, 2, NaN, 4, 5]) == 3.0`` — four
    observations wearing a five-bar label, which is how one bad vendor bar used
    to rescale ATR for the rest of the series (audit BUG-031).
    """
    smoothed = ema(gapped_series(gap_at=2), 5)

    assert smoothed.iloc[:7].isna().all()
    assert smoothed.iloc[7] == pytest.approx(6.0)
    # alpha = 2/6: 6.0 + (9 - 6.0)/3 = 7.0
    assert smoothed.iloc[8] == pytest.approx(7.0)


def test_a_gap_after_the_seed_is_masked_rather_than_smoothed_over() -> None:
    """``ewm`` carries the previous mean across a NaN; the mask puts the hole back.

    Before the fix this series came back with no NaN at all after the seed —
    a silently rescaled line, which is exactly what moves a stop without anyone
    noticing (audit BUG-031).
    """
    smoothed = ema(gapped_series(gap_at=7), 5)

    assert math.isnan(smoothed.iloc[7])
    # seed 3.0 on index 4, then alpha = 1/3: 4.0, 5.0 — unchanged by the later gap
    assert smoothed.iloc[6] == pytest.approx(5.0)
    assert not math.isnan(smoothed.iloc[8])  # the recursion resumes after the hole


def test_a_gap_does_not_disturb_anything_before_it() -> None:
    """Values ahead of the gap are bit-identical to the ungapped series."""
    clean = pd.Series([float(i + 1) for i in range(12)])
    gapped = clean.copy()
    gapped.iloc[9] = float("nan")

    pd.testing.assert_series_equal(ema(gapped, 5).iloc[:9], ema(clean, 5).iloc[:9])


def test_a_partial_nan_bar_leaves_a_nan_in_the_atr() -> None:
    """The realistic case: a vendor ships one row with a missing close.

    True range on the *next* bar needs that close, so the ATR goes NaN there.
    The level after the gap does move — smoothing across a hole cannot be
    undone — but it is no longer a silent move, which is the whole complaint.
    """
    clean = fixed_bars()
    gapped = clean.copy()
    gapped.loc[gapped.index[25], "close"] = np.nan

    clean_atr = atr(clean, 14)
    gapped_atr = atr(gapped, 14)

    assert math.isnan(gapped_atr.iloc[26])
    assert not math.isnan(clean_atr.iloc[26])
    pd.testing.assert_series_equal(gapped_atr.iloc[:26], clean_atr.iloc[:26])
    assert gapped_atr.iloc[27] != pytest.approx(clean_atr.iloc[27])


@pytest.mark.parametrize(
    "name,fn",
    [
        ("ema", lambda b: ema(b["close"], 14)),
        ("rsi", lambda b: rsi(b["close"], 14)),
        ("atr", lambda b: atr(b, 14)),
        ("adx", lambda b: adx(b, 14)),
    ],
)
def test_every_smoothed_indicator_reports_an_interior_gap(
    name: str, fn: Callable[[pd.DataFrame], pd.Series]
) -> None:
    """No member of the Wilder/EMA family may return a gapless line over gapped data."""
    bars = make_bars(200)
    bars.loc[bars.index[120], ["open", "high", "low", "close"]] = np.nan

    result = fn(bars)

    assert result.iloc[100:140].isna().any(), f"{name} smoothed straight over the gap"


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -14])
def test_windows_below_one_are_rejected(bad: int) -> None:
    s = pd.Series([1.0, 2.0, 3.0])
    bars = fixed_bars()
    for call in (
        lambda: sma(s, bad),
        lambda: ema(s, bad),
        lambda: rsi(s, bad),
        lambda: roc(s, bad),
        lambda: atr(bars, bad),
        lambda: adx(bars, bad),
        lambda: donchian_high(bars, bad),
        lambda: donchian_low(bars, bad),
    ):
        with pytest.raises(ValueError, match="at least 1 bar"):
            call()


@pytest.mark.parametrize("bad", [2.5, "14", None, True])
def test_non_integer_windows_are_rejected(bad: object) -> None:
    with pytest.raises(ValueError, match="whole number of bars"):
        sma(pd.Series([1.0, 2.0, 3.0]), bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fn,needed",
    [
        (lambda b: true_range(b), "high"),
        (lambda b: atr(b, 14), "high"),
        (lambda b: adx(b, 14), "high"),
        (lambda b: donchian_high(b, 20), "high"),
        (lambda b: donchian_low(b, 20), "low"),
        (lambda b: obv(b), "close"),
    ],
)
def test_missing_columns_are_reported_by_name(
    fn: Callable[[pd.DataFrame], pd.Series], needed: str
) -> None:
    bars = fixed_bars().drop(columns=[needed])
    with pytest.raises(ValueError, match=needed):
        fn(bars)


def test_passing_a_frame_where_a_series_is_expected_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be a pandas Series"):
        sma(fixed_bars(), 20)  # type: ignore[arg-type]


def test_passing_a_series_where_a_frame_is_expected_is_rejected() -> None:
    with pytest.raises(ValueError, match="bars DataFrame"):
        atr(fixed_bars()["close"], 14)  # type: ignore[arg-type]


def test_an_all_nan_series_yields_all_nan_rather_than_raising() -> None:
    """A symbol with no history should degrade quietly, not blow up the scan."""
    s = pd.Series([np.nan] * 30)
    assert sma(s, 14).isna().all()
    assert ema(s, 14).isna().all()
    assert rsi(s, 14).isna().all()
