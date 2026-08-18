"""Indicator correctness.

Three kinds of check, in descending order of strength:

1. **Hand-derived exact values** — the arithmetic is written out in the test so
   a future reader can re-check it without trusting a library.
2. **Published worked examples** — the classic Wilder/StockCharts RSI table.
3. **Analytic properties** — an unbroken advance must give RSI 100, a constant
   range must give ATR equal to that range, and so on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing.indicators import (
    adx,
    atr,
    atr_pct,
    directional_indicators,
    dollar_volume,
    donchian_high,
    donchian_low,
    ema,
    macd,
    momentum,
    obv,
    rsi,
    slope_positive,
    sma,
    true_range,
    wilder_smooth,
)

# The RSI(14) example that appears in Wilder's book and in the StockCharts
# documentation. See test_rsi_matches_hand_computed_seed for the arithmetic.
WILDER_RSI_CLOSES = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
    43.42, 42.66, 43.13,
]
# Values as printed in the StockCharts table.
WILDER_RSI_PUBLISHED = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]


# ---------------------------------------------------------------------------
# moving averages
# ---------------------------------------------------------------------------
def test_sma_exact_and_warmup():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(s, 3)
    assert out.iloc[:2].isna().all()          # no partial windows
    assert out.iloc[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert out.iloc[4] == pytest.approx(4.0)  # (3+4+5)/3


def test_ema_is_seeded_with_an_sma_like_charting_packages():
    s = pd.Series([1.0, 2.0, 3.0, 4.0])
    out = ema(s, 2)
    alpha = 2 / 3
    seed = 1.5                                # SMA(2) of [1, 2], not 1.0
    assert pd.isna(out.iloc[0])
    assert out.iloc[1] == pytest.approx(seed)
    assert out.iloc[2] == pytest.approx(seed + alpha * (3 - seed))
    assert out.iloc[3] == pytest.approx(
        (seed + alpha * (3 - seed)) + alpha * (4 - (seed + alpha * (3 - seed)))
    )


def test_wilder_smooth_matches_its_definition():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    n = 3
    out = wilder_smooth(s, n)
    assert out.iloc[:2].isna().all()
    seed = (1 + 2 + 3) / 3
    assert out.iloc[2] == pytest.approx(seed)
    assert out.iloc[3] == pytest.approx((seed * (n - 1) + 4) / n)
    assert out.iloc[4] == pytest.approx(((seed * 2 + 4) / 3 * 2 + 5) / 3)


def test_wilder_smoothing_is_an_ema_with_alpha_one_over_n():
    s = pd.Series(np.linspace(10, 30, 800))
    n = 14
    ours = wilder_smooth(s, n)
    equivalent = s.ewm(alpha=1 / n, adjust=False).mean()
    # Different seeds, so compare only once both have converged.
    assert ours.iloc[-1] == pytest.approx(equivalent.iloc[-1], rel=1e-9)


# ---------------------------------------------------------------------------
# true range / ATR
# ---------------------------------------------------------------------------
def test_true_range_picks_the_widest_of_the_three():
    high = pd.Series([10.0, 12.0, 9.0])
    low = pd.Series([9.0, 11.0, 8.0])
    close = pd.Series([9.5, 11.5, 8.5])
    tr = true_range(high, low, close)
    assert tr.iloc[0] == pytest.approx(1.0)     # first bar: H-L
    assert tr.iloc[1] == pytest.approx(2.5)     # |12 - 9.5| beats 12-11
    assert tr.iloc[2] == pytest.approx(3.5)     # |8 - 11.5| beats 9-8


def test_atr_of_a_constant_range_equals_that_range():
    n = 60
    close = pd.Series(np.full(n, 100.0))
    high = close + 1.0
    low = close - 1.0
    out = atr(high, low, close, 14)
    assert out.iloc[-1] == pytest.approx(2.0)


def test_atr_warmup_is_exactly_length_bars():
    n = 40
    idx = range(n)
    close = pd.Series(np.linspace(50, 60, n), index=idx)
    out = atr(close + 0.5, close - 0.5, close, 14)
    assert out.iloc[:13].isna().all()
    assert not np.isnan(out.iloc[13])


def test_atr_pct_is_scale_free():
    n = 60
    a = pd.Series(np.full(n, 100.0))
    b = a * 10
    pct_a = atr_pct(a + 1, a - 1, a, 14).iloc[-1]
    pct_b = atr_pct(b + 10, b - 10, b, 14).iloc[-1]
    assert pct_a == pytest.approx(pct_b)
    assert pct_a == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------
def test_rsi_matches_hand_computed_seed():
    """First RSI(14) value, derived by hand from the published closes.

    gains  = .06+.72+.50+.27+.32+.42+.24+.14+.67 = 3.34 -> avg 3.34/14 = 0.2385714
    losses = .25+.54+.19+.42                     = 1.40 -> avg 1.40/14 = 0.1000000
    RS  = 2.3857143
    RSI = 100 - 100/(1+RS) = 70.4641...
    """
    out = rsi(pd.Series(WILDER_RSI_CLOSES), 14).dropna()
    assert out.iloc[0] == pytest.approx(70.4641, abs=1e-4)


def test_rsi_tracks_the_published_table():
    """Every published value is reproduced to within a tenth of a point.

    The residual (~0.07 at the seed, decaying to ~0.02) is the published
    table's own rounding: the closes are printed to 2dp, and re-deriving the
    seed averages from those printed closes gives 70.4641, not the printed
    70.53. Wilder smoothing then bleeds that seed difference away, which is
    exactly the convergence pattern we see.
    """
    out = rsi(pd.Series(WILDER_RSI_CLOSES), 14).dropna().to_numpy()
    published = np.array(WILDER_RSI_PUBLISHED)
    assert len(out) == len(published)
    assert np.abs(out - published).max() < 0.10
    # And the disagreement shrinks as the seed washes out.
    assert abs(out[-1] - published[-1]) < abs(out[0] - published[0])


def test_rsi_is_100_on_an_unbroken_advance():
    s = pd.Series(np.arange(1, 60, dtype="float64"))
    assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_is_zero_on_an_unbroken_decline():
    s = pd.Series(np.arange(100, 40, -1, dtype="float64"))
    assert rsi(s, 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_100_by_convention():
    # No losses at all -> zero denominator. 100 is the conventional treatment.
    s = pd.Series(np.full(40, 25.0))
    assert rsi(s, 14).iloc[-1] == pytest.approx(100.0)


def test_rsi2_is_far_more_reactive_than_rsi14():
    rng = np.random.default_rng(3)
    s = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    assert rsi(s, 2).std() > rsi(s, 14).std() * 2


# ---------------------------------------------------------------------------
# ADX / DI
# ---------------------------------------------------------------------------
def test_adx_saturates_on_a_perfect_uptrend():
    n = 120
    close = pd.Series(np.arange(100, 100 + n, dtype="float64"))
    out = adx(close + 0.5, close - 0.5, close, 14)
    assert out.iloc[-1] > 95.0


def test_adx_is_low_in_a_choppy_market():
    n = 300
    # Alternating up/down: strong movement, no direction.
    close = pd.Series(100 + np.tile([0.0, 2.0], n // 2))
    out = adx(close + 0.5, close - 0.5, close, 14)
    assert out.iloc[-1] < 25.0


def test_adx_is_direction_agnostic():
    n = 120
    up = pd.Series(np.arange(100, 100 + n, dtype="float64"))
    down = pd.Series(np.arange(100 + n, 100, -1, dtype="float64"))
    a_up = adx(up + 0.5, up - 0.5, up, 14).iloc[-1]
    a_down = adx(down + 0.5, down - 0.5, down, 14).iloc[-1]
    assert a_up == pytest.approx(a_down, rel=0.05)


def test_di_points_the_right_way():
    n = 120
    close = pd.Series(np.arange(100, 100 + n, dtype="float64"))
    di = directional_indicators(close + 0.5, close - 0.5, close, 14)
    assert di["plus_di"].iloc[-1] > di["minus_di"].iloc[-1]


def test_adx_warmup_is_about_two_lengths():
    n = 80
    close = pd.Series(np.linspace(50, 70, n))
    out = adx(close + 0.5, close - 0.5, close, 14)
    first_valid = out.notna().idxmax()
    assert 26 <= first_valid <= 30


# ---------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------
def test_donchian_high_excludes_today_by_default():
    high = pd.Series([1.0, 2.0, 3.0, 10.0, 4.0])
    out = donchian_high(high, 3)
    # At index 3 the prior-3 window is [1, 2, 3] -> 3, so today's 10 is a breakout.
    assert out.iloc[3] == pytest.approx(3.0)
    assert out.iloc[4] == pytest.approx(10.0)


def test_donchian_high_can_include_today():
    high = pd.Series([1.0, 2.0, 3.0, 10.0, 4.0])
    out = donchian_high(high, 3, exclude_current=False)
    assert out.iloc[3] == pytest.approx(10.0)


def test_donchian_low_mirrors_high():
    low = pd.Series([10.0, 9.0, 8.0, 1.0, 7.0])
    assert donchian_low(low, 3).iloc[3] == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# momentum / volume / misc
# ---------------------------------------------------------------------------
def test_momentum_is_a_total_return():
    s = pd.Series([100.0, 110.0, 121.0])
    assert momentum(s, 2).iloc[2] == pytest.approx(0.21)


def test_momentum_skip_drops_the_most_recent_bars():
    s = pd.Series([100.0, 110.0, 121.0, 50.0])
    # skip=1 ignores the crash on the last bar entirely.
    assert momentum(s, 2, skip=1).iloc[3] == pytest.approx(0.21)


def test_momentum_rejects_bad_arguments():
    s = pd.Series([1.0, 2.0])
    with pytest.raises(ValueError):
        momentum(s, 0)
    with pytest.raises(ValueError):
        momentum(s, 2, skip=-1)


def test_dollar_volume_is_price_times_shares():
    close = pd.Series([10.0, 10.0, 10.0])
    volume = pd.Series([100.0, 200.0, 300.0])
    assert dollar_volume(close, volume, 3).iloc[2] == pytest.approx(2000.0)


def test_obv_accumulates_signed_volume():
    close = pd.Series([10.0, 11.0, 10.5, 10.5])
    volume = pd.Series([100.0, 200.0, 300.0, 400.0])
    out = obv(close, volume)
    assert out.tolist() == [0.0, 200.0, -100.0, -100.0]


def test_macd_crosses_positive_in_an_uptrend():
    s = pd.Series(np.linspace(50, 100, 200))
    out = macd(s)
    assert out["macd"].iloc[-1] > 0
    assert out["hist"].iloc[-1] == pytest.approx(
        out["macd"].iloc[-1] - out["signal"].iloc[-1]
    )


def test_slope_positive_flags_a_rising_line():
    s = pd.Series([1.0, 2.0, 3.0, 1.5])
    out = slope_positive(s, 2)
    assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
    assert bool(out.iloc[2]) is True        # 3.0 > 1.0
    assert bool(out.iloc[3]) is False       # 1.5 < 2.0


def test_indicators_reject_nonsense_lengths():
    s = pd.Series([1.0, 2.0, 3.0])
    for fn in (sma, ema, rsi):
        with pytest.raises(ValueError):
            fn(s, 0)


def test_no_indicator_looks_ahead():
    """Truncating the series must not change any already-computed value."""
    rng = np.random.default_rng(11)
    n = 300
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, n)))
    high, low = close + 1, close - 1
    cut = 250
    for full, part in (
        (sma(close, 50), sma(close.iloc[:cut], 50)),
        (rsi(close, 14), rsi(close.iloc[:cut], 14)),
        (atr(high, low, close, 14), atr(high.iloc[:cut], low.iloc[:cut], close.iloc[:cut], 14)),
        (adx(high, low, close, 14), adx(high.iloc[:cut], low.iloc[:cut], close.iloc[:cut], 14)),
        (donchian_high(high, 20), donchian_high(high.iloc[:cut], 20)),
    ):
        pd.testing.assert_series_equal(
            full.iloc[:cut], part, check_names=False, rtol=1e-9
        )
