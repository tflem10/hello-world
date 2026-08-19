"""AC7 — the rule set produces exactly the expected signals on synthetic fixtures.

Every assertion here is a *known answer*: the fixtures are built so that the
right answer can be worked out by hand (a monotone trend, a constant true
range, a single engineered breakout bar), and the test asserts that exact
answer — the signal date, the stop price, the first bar that passes. Nothing
compares the implementation against a re-implementation of itself.

Two fixture tricks are used repeatedly and are worth knowing:

* **Monotone geometric trend.** ``close = 100 * 1.002**t`` with the high and low
  a fixed fraction away makes every trend-template condition analytic: the SMAs
  stack by construction, ADX pins at 100 (every bar has +DM and no -DM), and the
  252-bar window is the binding warm-up.
* **Constant-band bars.** Holding high and low fixed far from close makes the
  true range constant (``high - low`` always dominates the gap terms), so ATR is
  an exact integer and every ATR-derived level is a hand-computable literal.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from conftest import make_bars
from swing.indicators import adx, atr
from swing.strategy import rules
from swing.strategy.rules import (
    ADX_WINDOW,
    CHANDELIER_WINDOW,
    RSI2_THRESHOLD,
    chandelier_stop,
    earnings_blackout,
    entry_signal,
    fundamentals_ok,
    initial_stop,
    liquidity_ok,
    trend_template,
)

# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def build_bars(
    close: object,
    *,
    high: object = None,
    low: object = None,
    volume: object = 1_000_000.0,
    start: str = "2018-01-02",
) -> pd.DataFrame:
    """A Contract 3 frame built from an explicit close series.

    ``high``/``low`` default to close (a degenerate but valid bar) and accept a
    scalar or an array; ``volume`` likewise. The index is business days.
    """
    close_values = np.asarray(close, dtype=float)
    n = close_values.size

    def spread(value: object, fallback: np.ndarray) -> np.ndarray:
        if value is None:
            return fallback.copy()
        return np.broadcast_to(np.asarray(value, dtype=float), (n,)).astype(float).copy()

    return pd.DataFrame(
        {
            "open": close_values.copy(),
            "high": spread(high, close_values),
            "low": spread(low, close_values),
            "close": close_values,
            "volume": spread(volume, np.full(n, 1_000_000.0)),
        },
        index=pd.bdate_range(start=start, periods=n),
    )


def uptrend_bars(n: int = 400, rate: float = 0.002) -> pd.DataFrame:
    """A strictly rising geometric series: SMAs stack, ADX pins at 100."""
    close = 100.0 * (1.0 + rate) ** np.arange(n)
    return build_bars(close, high=close * 1.005, low=close * 0.995)


def constant_band_bars(close: object, *, high: float, low: float) -> pd.DataFrame:
    """Bars whose true range is exactly ``high - low`` on every bar, so ATR is exact."""
    return build_bars(close, high=high, low=low)


# ---------------------------------------------------------------------------
# liquidity_ok
# ---------------------------------------------------------------------------


def test_liquidity_passes_a_liquid_name_after_the_20_day_warm_up(cfg_factory) -> None:
    cfg = cfg_factory()
    # $10 x 1M shares = $10M/day, comfortably over the $5M floor.
    flags = liquidity_ok(build_bars(np.full(40, 10.0)), cfg, is_etf=False)

    assert not flags.iloc[:19].any()  # 20-day mean undefined
    assert flags.iloc[19:].all()
    assert flags.dtype == bool


def test_liquidity_rejects_a_sub_min_price_stock(cfg_factory) -> None:
    cfg = cfg_factory()
    # $4 < $5 min_price, even though $4 x 2M = $8M/day clears the dollar floor.
    flags = liquidity_ok(build_bars(np.full(40, 4.0), volume=2_000_000.0), cfg, is_etf=False)

    assert not flags.any()


def test_liquidity_rejects_a_thinly_traded_stock(cfg_factory) -> None:
    cfg = cfg_factory()
    # $10 x 400k = $4M/day, under the $5M floor, though the price is fine.
    flags = liquidity_ok(build_bars(np.full(40, 10.0), volume=400_000.0), cfg, is_etf=False)

    assert not flags.any()


def test_liquidity_thresholds_are_inclusive(cfg_factory) -> None:
    cfg = cfg_factory(strategy={"min_price": 5.0, "min_dollar_volume": 5_000_000.0})
    # exactly $5.00 and exactly $5,000,000/day: both bounds are ">=".
    flags = liquidity_ok(build_bars(np.full(40, 5.0), volume=1_000_000.0), cfg, is_etf=False)

    assert flags.iloc[19:].all()


def test_liquidity_uses_a_20_day_mean_not_the_latest_bar(cfg_factory) -> None:
    """Dollar volume steps from $1M to $10M on bar 30; the 20-bar mean crosses $5M on bar 38.

    Nine bars at $10M plus eleven at $1M average $5.05M, which passes; eight and
    twelve average $4.6M, which does not.
    """
    cfg = cfg_factory()
    volume = np.concatenate([np.full(30, 100_000.0), np.full(30, 1_000_000.0)])
    flags = liquidity_ok(build_bars(np.full(60, 10.0), volume=volume), cfg, is_etf=False)

    assert not flags.iloc[:38].any()
    assert flags.iloc[38:].all()


def test_liquidity_applies_the_same_thresholds_to_etfs(cfg_factory) -> None:
    """The ETF relaxation is fundamentals-only; liquidity is judged identically."""
    cfg = cfg_factory()
    bars = make_bars(120, base=40.0)

    pd.testing.assert_series_equal(
        liquidity_ok(bars, cfg, is_etf=True), liquidity_ok(bars, cfg, is_etf=False)
    )


# ---------------------------------------------------------------------------
# trend_template
# ---------------------------------------------------------------------------


def test_trend_template_turns_true_on_the_exact_252nd_bar(cfg_factory) -> None:
    """On a clean uptrend the 52-week window is the binding warm-up, so the first
    True is bar 252 (index 251) — not one bar earlier, not one later.

    The other conditions come good sooner: SMA(200) at index 199, its 21-bar
    rise at 220, ADX(14) at 27.
    """
    cfg = cfg_factory()
    flags = trend_template(uptrend_bars(400), cfg, is_etf=False)

    assert not flags.iloc[:251].any()
    assert flags.iloc[251:].all()
    assert flags.index[251] == flags.idxmax()


def test_trend_template_rejects_a_downtrend(cfg_factory) -> None:
    cfg = cfg_factory()
    close = 100.0 * 0.998 ** np.arange(400)
    bars = build_bars(close, high=close * 1.005, low=close * 0.995)

    assert not trend_template(bars, cfg, is_etf=False).any()


def test_trend_template_enforces_the_distance_above_the_52_week_low(cfg_factory) -> None:
    """A +0.2%/day trend gains 65% over 252 bars, so it clears a 1.25x floor but not 1.9x."""
    bars = uptrend_bars(400)

    assert trend_template(bars, cfg_factory(), is_etf=False).iloc[251:].all()
    strict = cfg_factory(strategy={"min_above_low_mult": 1.9})
    assert not trend_template(bars, strict, is_etf=False).any()


def test_trend_template_enforces_the_distance_below_the_52_week_high(cfg_factory) -> None:
    """One bar dips 1% off the running high: fine under the 25% default, rejected
    when the allowance is tightened to 0.5%, while the bar before it still passes."""
    close = 100.0 * 1.002 ** np.arange(400)
    close[300] = close[299] * 0.99
    bars = build_bars(close, high=close * 1.005, low=close * 0.995)

    default = trend_template(bars, cfg_factory(), is_etf=False)
    assert bool(default.iloc[299])
    assert bool(default.iloc[300])

    tight = trend_template(bars, cfg_factory(strategy={"max_below_high_pct": 0.5}), is_etf=False)
    assert bool(tight.iloc[299])
    assert not bool(tight.iloc[300])


def test_trend_template_adx_floor_is_inclusive(cfg_factory) -> None:
    """ADX pins at 100 on a one-directional series, and a floor of exactly 100 still passes."""
    bars = uptrend_bars(400)

    assert (
        trend_template(bars, cfg_factory(strategy={"adx_min": 100.0}), is_etf=False)
        .iloc[251:]
        .all()
    )


def test_trend_template_enforces_the_adx_floor(cfg_factory) -> None:
    """The first passing bar of the seeded noisy uptrend has ADX just over 20.

    Lifting the floor past that value is the only change, and it is enough to
    reject the bar — proof the ADX term is doing work rather than riding along.
    """
    bars = make_bars(400, trend=0.002)
    first_pass = trend_template(bars, cfg_factory(), is_etf=False).idxmax()
    assert 20.0 < adx(bars, 14)[first_pass] < 21.0

    lenient = trend_template(bars, cfg_factory(strategy={"adx_min": 20.4}), is_etf=False)
    strict = trend_template(bars, cfg_factory(strategy={"adx_min": 21.0}), is_etf=False)

    assert bool(lenient[first_pass])
    assert not bool(strict[first_pass])


def test_trend_template_does_not_compute_adx_when_the_floor_is_zero(
    cfg_factory, monkeypatch
) -> None:
    """ADX is ~80% of this function's cost and a no-op at ``adx_min=0`` (audit PERF-010).

    Every ``adx_off`` ablation fold used to pay for it. The counter proves the
    call is gone when the filter is off and still there when it is on.
    """
    calls: list[int] = []
    real_adx = rules.adx

    def counting_adx(bars: pd.DataFrame, n: int) -> pd.Series:
        calls.append(n)
        return real_adx(bars, n)

    monkeypatch.setattr(rules, "adx", counting_adx)
    bars = uptrend_bars(400)

    trend_template(bars, cfg_factory(strategy={"adx_min": 0.0}), is_etf=False)
    assert calls == []

    trend_template(bars, cfg_factory(strategy={"adx_min": 20.0}), is_etf=False)
    assert calls == [ADX_WINDOW]


@pytest.mark.parametrize("bars_builder", [lambda: uptrend_bars(400), lambda: make_bars(400, 0.002)])
def test_skipping_adx_at_a_zero_floor_changes_no_bar(cfg_factory, bars_builder) -> None:
    """The short-circuit is provably equivalent, not merely close (audit PERF-010).

    ``adx >= 0`` is False only where ADX is NaN — bars 0 to 2*14-2 — and those
    bars are already False on ``above_low``/``near_high``, whose 252-bar warm-up
    strictly contains them. Re-imposing the dropped conjunct on the result must
    therefore change nothing, on every bar, warm-up included.
    """
    bars = bars_builder()
    flags = trend_template(bars, cfg_factory(strategy={"adx_min": 0.0}), is_etf=False)

    with_the_conjunct = flags & (adx(bars, ADX_WINDOW) >= 0.0)

    pd.testing.assert_series_equal(flags, with_the_conjunct.rename("trend_template"))
    assert not flags.iloc[: 2 * ADX_WINDOW - 1].any()  # the only window it could have touched


def test_trend_template_rejects_a_flat_series_on_the_moving_average_stack(cfg_factory) -> None:
    """Flat prices leave close equal to every SMA, and the stack demands strict >."""
    cfg = cfg_factory()

    assert not trend_template(build_bars(np.full(400, 100.0)), cfg, is_etf=False).any()


def test_trend_template_is_identical_for_etfs(cfg_factory) -> None:
    cfg = cfg_factory()
    bars = uptrend_bars(400)

    pd.testing.assert_series_equal(
        trend_template(bars, cfg, is_etf=True), trend_template(bars, cfg, is_etf=False)
    )


def test_trend_template_returns_a_bool_series_aligned_to_the_bars(cfg_factory) -> None:
    bars = make_bars(400, trend=0.002)
    flags = trend_template(bars, cfg_factory(), is_etf=False)

    assert flags.dtype == bool
    assert not flags.isna().any()
    pd.testing.assert_index_equal(flags.index, bars.index)


# ---------------------------------------------------------------------------
# entry_signal — breakout path
# ---------------------------------------------------------------------------


def flat_with_event(
    n: int = 80,
    event_index: int = 60,
    *,
    close: float = 100.0,
    event_close: float = 105.0,
    volume: float = 1_000_000.0,
    event_volume: float = 2_000_000.0,
) -> pd.DataFrame:
    """Flat $100 bars with one engineered event bar — the classic breakout fixture."""
    closes = np.full(n, close)
    closes[event_index] = event_close
    volumes = np.full(n, volume)
    volumes[event_index] = event_volume
    return build_bars(closes, volume=volumes)


def test_entry_fires_on_the_engineered_breakout_bar_only(cfg_factory) -> None:
    """Bar 60 closes $105 above a $100 20-day high on 2x volume: entry that day, and
    only that day. The bar before fails on volume; the bar after fails on both."""
    cfg = cfg_factory()
    bars = flat_with_event()
    entries = entry_signal(bars, cfg)

    assert bool(entries.iloc[60])
    assert not bool(entries.iloc[59])
    assert not bool(entries.iloc[61])
    assert entries.sum() == 1
    assert entries.index[60] == entries.idxmax()


def test_entry_fires_within_the_proximity_band(cfg_factory) -> None:
    """$98.50 is 1.5% under the $100 prior high — inside the 2% band, so it counts."""
    cfg = cfg_factory()
    bars = flat_with_event(event_close=98.5, event_volume=5_000_000.0)
    entries = entry_signal(bars, cfg)

    assert bool(entries.iloc[60])
    assert entries.sum() == 1


def test_entry_does_not_fire_outside_the_proximity_band(cfg_factory) -> None:
    """$97.00 is 3% under the $100 prior high — outside the 2% band, so no entry."""
    cfg = cfg_factory()
    bars = flat_with_event(event_close=97.0, event_volume=5_000_000.0)

    assert not entry_signal(bars, cfg).any()


def test_entry_requires_volume_confirmation(cfg_factory) -> None:
    """A genuine $105 breakout on 1.2x volume is refused: the floor is 1.3x."""
    cfg = cfg_factory()
    bars = flat_with_event(event_volume=1_200_000.0)

    assert not entry_signal(bars, cfg).any()


def test_entry_is_false_through_the_volume_average_warm_up(cfg_factory) -> None:
    """Nothing can fire before the 50-bar volume average exists."""
    cfg = cfg_factory()
    bars = flat_with_event(event_index=30)

    assert not entry_signal(bars, cfg).iloc[:49].any()


def test_entry_breakout_level_excludes_today(cfg_factory) -> None:
    """donchian_high is shifted one bar, so a new high does not gate itself.

    Prices ramp to $110 and stay; the last bar's own high is irrelevant to the
    level it must clear, which is the prior 20 bars.
    """
    cfg = cfg_factory(strategy={"donchian_window": 5, "volume_avg_window": 5})
    closes = np.concatenate([np.full(20, 100.0), np.full(10, 110.0)])
    bars = build_bars(closes, volume=np.concatenate([np.full(20, 1e6), np.full(10, 5e6)]))
    entries = entry_signal(bars, cfg)

    assert bool(entries.iloc[20])  # clears the prior 5-bar $100 high


# ---------------------------------------------------------------------------
# entry_signal — RSI(2) pullback overlay (orchestrator contract amendment)
# ---------------------------------------------------------------------------


def rsi2_pullback_bars() -> pd.DataFrame:
    """A steady 0.4%/day riser that sells off 3% a day at the end.

    Volume is flat, so the breakout path is dead everywhere (it needs 1.3x the
    average) and every signal is attributable to the pullback path. By
    construction: bar 80 is oversold-ish but RSI(2) is 11.7 (above the
    threshold), bar 81 has RSI(2) 4.3 and still holds above SMA(50), and bars 82+
    are deeply oversold but have lost SMA(50).
    """
    close = list(100.0 * 1.004 ** np.arange(80))
    for _ in range(6):
        close.append(close[-1] * 0.97)
    return build_bars(np.array(close))


def test_rsi2_path_is_inert_when_the_overlay_is_disabled(cfg_factory) -> None:
    """Deeply oversold bars produce nothing without a breakout — the default config."""
    cfg = cfg_factory(strategy={"rsi2_enabled": False})

    assert not entry_signal(rsi2_pullback_bars(), cfg).any()


def test_rsi2_path_fires_on_the_engineered_pullback_bar(cfg_factory) -> None:
    """Bar 81: RSI(2) = 4.3 < 10 and close is still above SMA(50) — one entry, no volume needed."""
    cfg = cfg_factory(strategy={"rsi2_enabled": True})
    entries = entry_signal(rsi2_pullback_bars(), cfg)

    assert bool(entries.iloc[81])
    assert entries.sum() == 1


def test_rsi2_path_requires_close_above_the_fast_sma(cfg_factory) -> None:
    """Bars 82-85 are far more oversold than bar 81 but sit below SMA(50), so they are refused."""
    cfg = cfg_factory(strategy={"rsi2_enabled": True})
    entries = entry_signal(rsi2_pullback_bars(), cfg)

    assert not entries.iloc[82:].any()


def test_rsi2_path_respects_the_oversold_threshold(cfg_factory) -> None:
    """Bar 80 holds above SMA(50) but its RSI(2) of 11.7 is above the 10.0 threshold."""
    from swing.indicators import rsi

    bars = rsi2_pullback_bars()
    rsi2 = rsi(bars["close"], 2)
    assert rsi2.iloc[80] > RSI2_THRESHOLD > rsi2.iloc[81]

    entries = entry_signal(bars, cfg_factory(strategy={"rsi2_enabled": True}))
    assert not bool(entries.iloc[80])


def test_breakout_path_is_unaffected_by_the_overlay(cfg_factory) -> None:
    """Turning the overlay on may only ever add entries, never move the breakout ones."""
    bars = flat_with_event()
    off = entry_signal(bars, cfg_factory(strategy={"rsi2_enabled": False}))
    on = entry_signal(bars, cfg_factory(strategy={"rsi2_enabled": True}))

    assert bool(off.iloc[60])
    assert bool(on.iloc[60])
    assert (on | off).equals(on)  # the overlay is additive


@pytest.mark.parametrize("proximity_pct", [0.0, 0.5, 2.0, 10.0, 25.0])
@pytest.mark.parametrize(
    "bars_builder",
    [lambda: flat_with_event(), lambda: uptrend_bars(200), lambda: make_bars(200, trend=0.001)],
)
def test_the_strict_breakout_test_is_subsumed_by_the_proximity_band(
    cfg_factory, proximity_pct: float, bars_builder
) -> None:
    """``close > prior_high`` was a dead disjunct beside the band (audit DEBT-012).

    ``breakout_proximity_pct`` is capped at 25, so ``proximity`` never leaves
    [0.75, 1.0] and prices are non-negative: ``proximity * prior_high`` is
    therefore at or below ``prior_high``, and nothing can clear the level
    outright without also clearing the band. Asserted here on every bar of three
    fixtures across the full legal range of the knob, which is the proof that
    deleting the disjunct changed no signal.
    """
    from swing.indicators import donchian_high

    cfg = cfg_factory(strategy={"breakout_proximity_pct": proximity_pct})
    bars = bars_builder()

    prior_high = donchian_high(bars, cfg.strategy.donchian_window)
    strict = bars["close"] > prior_high
    within_band = bars["close"] >= (1.0 - proximity_pct / 100.0) * prior_high

    assert not (strict & ~within_band).any()


def test_a_strict_breakout_still_fires_with_the_proximity_band_at_zero(cfg_factory) -> None:
    """The disjunct is gone; the case it read as carrying still signals.

    At ``breakout_proximity_pct = 0`` the band collapses onto the level itself,
    so the surviving comparison is ``close >= prior_high`` — which is what the
    old two-disjunct expression already evaluated to.
    """
    cfg = cfg_factory(strategy={"breakout_proximity_pct": 0.0})
    entries = entry_signal(flat_with_event(event_close=105.0), cfg)

    assert bool(entries.iloc[60])
    assert entries.sum() == 1


# ---------------------------------------------------------------------------
# stops
# ---------------------------------------------------------------------------


def test_initial_stop_is_close_minus_two_atr(cfg_factory) -> None:
    """Constant $20 true range: ATR is exactly 20, so the stop is 150 - 2*20 = 110."""
    cfg = cfg_factory()
    bars = constant_band_bars(np.full(60, 150.0), high=160.0, low=140.0)
    stops = initial_stop(bars, cfg)

    assert stops.iloc[30] == pytest.approx(110.0)
    assert stops.iloc[59] == pytest.approx(110.0)


def test_initial_stop_honours_the_multiplier(cfg_factory) -> None:
    cfg = cfg_factory(strategy={"atr_stop_mult": 1.5})
    bars = constant_band_bars(np.full(60, 150.0), high=160.0, low=140.0)

    assert initial_stop(bars, cfg).iloc[59] == pytest.approx(120.0)  # 150 - 1.5*20


def test_chandelier_stop_is_the_22_bar_high_minus_three_atr(cfg_factory) -> None:
    """One $152 bar lifts the level for exactly 22 bars, then it falls back.

    ATR is pinned at 20 by the constant band, so the level is a literal:
    150 - 3*20 = 90 normally, 152 - 60 = 92 while the spike is inside the window.
    """
    cfg = cfg_factory()
    closes = np.full(81, 150.0)
    closes[40] = 152.0
    stops = chandelier_stop(constant_band_bars(closes, high=160.0, low=140.0), cfg)

    assert stops.iloc[39] == pytest.approx(90.0)
    assert stops.iloc[40] == pytest.approx(92.0)
    assert stops.iloc[40 + CHANDELIER_WINDOW - 1] == pytest.approx(92.0)  # last bar in window
    assert stops.iloc[40 + CHANDELIER_WINDOW] == pytest.approx(90.0)  # spike aged out


def test_chandelier_stop_honours_the_multiplier(cfg_factory) -> None:
    cfg = cfg_factory(strategy={"chandelier_mult": 2.0})
    bars = constant_band_bars(np.full(60, 150.0), high=160.0, low=140.0)

    assert chandelier_stop(bars, cfg).iloc[59] == pytest.approx(110.0)  # 150 - 2*20


def test_chandelier_stop_does_not_ratchet(cfg_factory) -> None:
    """The level follows the bars back down; ratcheting is the consumer's job."""
    cfg = cfg_factory()
    closes = np.full(81, 150.0)
    closes[40] = 152.0
    stops = chandelier_stop(constant_band_bars(closes, high=160.0, low=140.0), cfg)

    assert stops.iloc[80] < stops.iloc[45]


def test_stops_are_nan_only_during_the_atr_warm_up(cfg_factory) -> None:
    cfg = cfg_factory()
    bars = make_bars(120)
    warm_up = atr(bars, cfg.strategy.atr_window).isna()

    pd.testing.assert_series_equal(initial_stop(bars, cfg).isna(), warm_up, check_names=False)
    pd.testing.assert_series_equal(chandelier_stop(bars, cfg).isna(), warm_up, check_names=False)


# ---------------------------------------------------------------------------
# earnings_blackout
# ---------------------------------------------------------------------------


CALENDAR = pd.date_range("2021-01-01", periods=40, freq="D")
EARNINGS = dt.date(2021, 1, 20)


def test_earnings_blackout_covers_the_ten_days_before_and_the_day_itself(cfg_factory) -> None:
    cfg = cfg_factory()  # earnings_blackout_days = 10
    blocked = earnings_blackout(CALENDAR, EARNINGS, cfg)

    assert bool(blocked[pd.Timestamp("2021-01-20")])  # earnings day
    assert bool(blocked[pd.Timestamp("2021-01-10")])  # exactly 10 days before
    assert not bool(blocked[pd.Timestamp("2021-01-09")])  # 11 days before
    assert not bool(blocked[pd.Timestamp("2021-01-21")])  # the day after
    assert blocked.sum() == 11


def test_earnings_blackout_is_all_false_when_the_date_is_unknown(cfg_factory) -> None:
    """None blocks nothing — the caller tags the pick 'earnings unknown' instead."""
    blocked = earnings_blackout(CALENDAR, None, cfg_factory())

    assert not blocked.any()
    assert blocked.dtype == bool
    pd.testing.assert_index_equal(blocked.index, CALENDAR)


def test_earnings_blackout_window_is_configurable(cfg_factory) -> None:
    zero = earnings_blackout(
        CALENDAR, EARNINGS, cfg_factory(strategy={"earnings_blackout_days": 0})
    )
    three = earnings_blackout(
        CALENDAR, EARNINGS, cfg_factory(strategy={"earnings_blackout_days": 3})
    )

    assert zero.sum() == 1
    assert bool(zero[pd.Timestamp("2021-01-20")])
    assert three.sum() == 4
    assert bool(three[pd.Timestamp("2021-01-17")])
    assert not bool(three[pd.Timestamp("2021-01-16")])


def test_earnings_blackout_ignores_dates_outside_the_index(cfg_factory) -> None:
    far_future = earnings_blackout(CALENDAR, dt.date(2022, 6, 1), cfg_factory())
    long_past = earnings_blackout(CALENDAR, dt.date(2020, 6, 1), cfg_factory())

    assert not far_future.any()
    assert not long_past.any()


def test_earnings_blackout_works_on_a_trading_day_index(cfg_factory) -> None:
    """Business-day indexes skip weekends, so only the trading days inside the window block."""
    trading_days = pd.bdate_range("2021-01-01", periods=30)
    blocked = earnings_blackout(trading_days, EARNINGS, cfg_factory())

    assert bool(blocked[pd.Timestamp("2021-01-11")])  # Monday inside the window
    assert not bool(blocked[pd.Timestamp("2021-01-08")])  # Friday, 12 days before
    pd.testing.assert_index_equal(blocked.index, trading_days)


def test_earnings_blackout_survives_a_tz_aware_index(cfg_factory) -> None:
    """A tz-aware index used to raise, and the scanner's broad except then failed *open*.

    Subtracting a naive announcement stamp from a tz-aware index is a TypeError;
    the scanner caught it and carried on with "no blackout", permitting an entry
    inside one. Normalising the index here fails closed instead (audit BUG-045).
    """
    aware = pd.date_range("2021-01-01", periods=40, freq="D", tz="America/New_York")
    cfg = cfg_factory()

    blocked = earnings_blackout(aware, EARNINGS, cfg)

    assert blocked.sum() == 11
    assert bool(blocked.iloc[19])  # 2021-01-20, the earnings day itself
    pd.testing.assert_index_equal(blocked.index, aware)
    # Identical verdicts to the naive calendar, day for day.
    assert blocked.to_numpy().tolist() == earnings_blackout(CALENDAR, EARNINGS, cfg).tolist()


def test_earnings_blackout_accepts_a_tz_aware_announcement(cfg_factory) -> None:
    """Providers hand back tz-aware announcement stamps; the calendar day is what matters."""
    aware_stamp = pd.Timestamp("2021-01-20 16:05", tz="America/New_York")

    blocked = earnings_blackout(CALENDAR, aware_stamp, cfg_factory())

    assert blocked.sum() == 11
    assert bool(blocked[pd.Timestamp("2021-01-20")])


def test_earnings_blackout_blocks_the_window_of_any_date_in_a_sequence(cfg_factory) -> None:
    """Contract amendment A12: a whole announcement history, not just the next one.

    Two announcements 60 days apart give two disjoint 11-day windows; a day
    outside both is clear.
    """
    calendar = pd.date_range("2021-01-01", periods=120, freq="D")
    history = [dt.date(2021, 1, 20), dt.date(2021, 3, 21)]

    blocked = earnings_blackout(calendar, history, cfg_factory())

    assert blocked.sum() == 22
    assert bool(blocked[pd.Timestamp("2021-01-20")])
    assert bool(blocked[pd.Timestamp("2021-03-11")])  # exactly 10 days before the second
    assert not bool(blocked[pd.Timestamp("2021-03-10")])
    assert not bool(blocked[pd.Timestamp("2021-02-15")])  # between the two windows


def test_earnings_blackout_overlapping_dates_do_not_double_count(cfg_factory) -> None:
    """Windows are a union, not a sum — a duplicated date changes nothing."""
    cfg = cfg_factory()
    once = earnings_blackout(CALENDAR, EARNINGS, cfg)

    for repeated in ([EARNINGS, EARNINGS], [EARNINGS, dt.date(2021, 1, 22)]):
        blocked = earnings_blackout(CALENDAR, repeated, cfg)
        assert blocked.sum() >= once.sum()
        assert (blocked | once).equals(blocked)


def test_earnings_blackout_single_date_and_one_element_sequence_agree(cfg_factory) -> None:
    """Backward compatibility: the widened signature must not move the old answer."""
    cfg = cfg_factory()

    pd.testing.assert_series_equal(
        earnings_blackout(CALENDAR, EARNINGS, cfg),
        earnings_blackout(CALENDAR, [EARNINGS], cfg),
    )


def test_earnings_blackout_an_empty_sequence_blocks_nothing(cfg_factory) -> None:
    """An empty history is "unknown", exactly like None."""
    cfg = cfg_factory()

    for nothing in ([], (), None):
        blocked = earnings_blackout(CALENDAR, nothing, cfg)
        assert not blocked.any()
        assert blocked.dtype == bool
        pd.testing.assert_index_equal(blocked.index, CALENDAR)


def test_earnings_blackout_accepts_a_datetime_as_one_announcement(cfg_factory) -> None:
    """``datetime`` subclasses ``date``, so it is one announcement, not an iterable."""
    blocked = earnings_blackout(CALENDAR, dt.datetime(2021, 1, 20, 16, 5), cfg_factory())

    assert blocked.sum() == 11


def test_earnings_blackout_rejects_a_date_string(cfg_factory) -> None:
    """A string is a sequence of characters; silently blacking out nothing is worse."""
    with pytest.raises(ValueError, match="parse it into a datetime.date"):
        earnings_blackout(CALENDAR, "2021-01-20", cfg_factory())  # type: ignore[arg-type]


def test_earnings_blackout_on_an_empty_index(cfg_factory) -> None:
    """A symbol with no bars must not blow up the sequence path."""
    empty = pd.DatetimeIndex([])

    blocked = earnings_blackout(empty, [EARNINGS], cfg_factory())

    assert len(blocked) == 0
    assert blocked.dtype == bool


# ---------------------------------------------------------------------------
# fundamentals_ok
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundamentalsStub:
    """Contract 3's Fundamentals shape, rebuilt locally so this test never imports swing.data."""

    symbol: str
    eps_growth: float | None
    revenue_growth: float | None


@pytest.mark.parametrize(
    ("eps", "revenue", "below_median", "expected"),
    [
        (-0.10, -0.20, True, False),  # the only rejecting combination
        (-0.10, -0.20, False, True),  # shrinking, but momentum is top half
        (-0.10, 0.20, True, True),  # only one leg negative
        (0.10, -0.20, True, True),  # only one leg negative
        (0.10, 0.20, True, True),  # growing
        (0.0, 0.0, True, True),  # flat is not negative
        (-0.10, None, True, True),  # partial data passes
        (None, -0.20, True, True),  # partial data passes
        (None, None, True, True),  # no data passes
    ],
)
def test_fundamentals_truth_table(
    cfg_factory, eps: float | None, revenue: float | None, below_median: bool, expected: bool
) -> None:
    fundamentals = FundamentalsStub("TEST", eps, revenue)

    assert fundamentals_ok(fundamentals, below_median, cfg_factory()) is expected


def test_fundamentals_missing_object_passes(cfg_factory) -> None:
    assert fundamentals_ok(None, True, cfg_factory()) is True


def test_fundamentals_filter_can_be_switched_off(cfg_factory) -> None:
    """With the filter off even the worst combination passes."""
    cfg = cfg_factory(strategy={"fundamentals_filter": False})

    assert fundamentals_ok(FundamentalsStub("TEST", -0.5, -0.5), True, cfg) is True
    assert fundamentals_ok(None, True, cfg) is True
