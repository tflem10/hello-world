"""Strategy rules: filters, entry signal, ranking, exits.

**This module is the single source of truth for "what is a trade".** The
backtester and the live scanner both import it. There is no second copy of the
logic to drift out of sync, which is the usual way backtested edges evaporate
in production.

Everything is vectorised over one symbol's bar history and returns a feature
frame aligned to that symbol's index. The cross-sectional part (rank the
universe, cap concurrent positions) lives in the engine, because it needs all
symbols at once.

Default strategy: "Trend-Momentum Core"
---------------------------------------
1. **Regime gate** — only take new entries when SPY is above its 200-day SMA.
   Existing positions ride a regime-off stretch out; they keep trailing. Setting
   ``[strategy.regime] exit_on_regime_off`` reverses that and closes the book at
   the next open, which is what you want when the trail has been switched off
   and nothing else is protecting an open gain.
2. **Liquidity** — price >= $5, 20-day average dollar volume >= $5M.
3. **Trend template** (Minervini) — close above 50 > 150 > 200 SMA, the 200-SMA
   itself rising, at least 25% above the 52-week low, within 25% of the 52-week
   high, ADX(14) >= 20.
4. **Entry** — close breaks the prior 20-day high (or is within 2% of it) on
   volume at least 1.3x its 50-day average.
5. **Rank** — risk-adjusted momentum, 0.6 x 126-day return (skipping the last
   week) + 0.4 x 63-day return, each divided by ATR%.
6. **Exit** — initial stop 2 ATR below entry; Chandelier trail 3 ATR below the
   highest close since entry, ratcheting up only; time stop at 40 trading days.

Every number above is a config key, and every one traces to a citation or an
ablation in docs/indicator-research.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import indicators as ind

# Columns produced by compute_features(). The engine depends on these names.
FEATURE_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "atr", "atr_pct", "adx", "dollar_volume",
    "rank_score", "liquid", "trend_ok", "entry_signal", "eligible",
]


@dataclass(frozen=True)
class SymbolMeta:
    """What the rules need to know about an instrument beyond its bars."""

    symbol: str
    is_etf: bool = False
    fundamentals_ok: bool | None = None   # None = unknown -> treated as "no opinion"


def compute_features(bars: pd.DataFrame, cfg, meta: SymbolMeta | None = None) -> pd.DataFrame:
    """Compute every per-symbol feature the strategy needs, vectorised.

    The returned frame has the same index as ``bars``. Warm-up bars carry NaN
    for numeric features and ``False`` for boolean gates — never a partial
    value that would silently admit a trade on 3 bars of history.
    """
    meta = meta or SymbolMeta(symbol="?")
    s = cfg.strategy
    out = pd.DataFrame(index=bars.index)

    close, high, low, volume = bars["close"], bars["high"], bars["low"], bars["volume"]
    out["open"] = bars["open"]
    out["high"] = high
    out["low"] = low
    out["close"] = close
    out["volume"] = volume

    # -- volatility --------------------------------------------------------
    atr_len = int(s.exit.atr_len)
    out["atr"] = ind.atr(high, low, close, atr_len)
    out["atr_pct"] = out["atr"] / close

    # -- liquidity ---------------------------------------------------------
    out["dollar_volume"] = ind.dollar_volume(close, volume, 20)
    out["liquid"] = (
        (close >= float(cfg.universe.min_price))
        & (out["dollar_volume"] >= float(cfg.universe.min_dollar_volume))
    ).fillna(False)

    # -- trend / ADX -------------------------------------------------------
    # ADX is computed once here and kept in the frame: the scanner reports it
    # on every pick, and recomputing it per candidate was measurable.
    out["adx"] = ind.adx(high, low, close, int(s.trend_template.adx_len))
    out["trend_ok"] = _trend_template(bars, s, out)

    # -- entry -------------------------------------------------------------
    out["entry_signal"] = _entry_signal(bars, s)

    # -- ranking -----------------------------------------------------------
    out["rank_score"] = _rank_score(close, out["atr_pct"], s)

    # -- fundamentals (soft, stocks only) ----------------------------------
    fundamentals_block = False
    if (
        bool(s.fundamentals.get("enabled", True))
        and not meta.is_etf
        and meta.fundamentals_ok is False
    ):
        fundamentals_block = True

    out["eligible"] = (
        out["liquid"]
        & out["trend_ok"]
        & out["atr"].notna()
        & (out["atr"] > 0)
        & (not fundamentals_block)
    )
    return out


def _trend_template(bars: pd.DataFrame, s, out: pd.DataFrame) -> pd.Series:
    """Minervini-style stage-2 template. All-or-nothing; no partial credit.

    Reads ``out["adx"]``, which :func:`compute_features` has already populated.
    """
    tt = s.trend_template
    close = bars["close"]
    if not bool(tt.get("enabled", True)):
        return pd.Series(True, index=bars.index)

    fast = ind.sma(close, int(tt.sma_fast))
    mid = ind.sma(close, int(tt.sma_mid))
    slow = ind.sma(close, int(tt.sma_slow))

    rising = ind.slope_positive(slow, int(tt.slow_rising_lookback)).fillna(False).astype(bool)

    lookback_52w = 252
    hi_52w = ind.rolling_high(close, lookback_52w)
    lo_52w = ind.rolling_low(close, lookback_52w)

    adx_series = out["adx"]

    conditions = (
        (close > fast)
        & (fast > mid)
        & (mid > slow)
        & rising
        & (close >= lo_52w * (1.0 + float(tt.min_pct_above_52w_low)))
        & (close >= hi_52w * (1.0 - float(tt.max_pct_below_52w_high)))
        & (adx_series >= float(tt.adx_min))
    )
    return conditions.fillna(False).astype(bool)


def _entry_signal(bars: pd.DataFrame, s) -> pd.Series:
    """Today's close triggers an entry (to be filled at the next open)."""
    mode = str(s.entry.get("mode", "donchian_breakout"))
    close, high, volume = bars["close"], bars["high"], bars["volume"]

    if mode == "rsi2_pullback":
        cfg2 = s.entry.rsi2
        rsi_series = ind.rsi(close, int(cfg2.rsi_len))
        trend = ind.sma(close, int(cfg2.trend_ma))
        sig = (rsi_series <= float(cfg2.rsi_max)) & (close > trend)
        return sig.fillna(False).astype(bool)

    if mode != "donchian_breakout":
        raise ValueError(f"unknown strategy.entry.mode {mode!r}")

    channel = ind.donchian_high(high, int(s.entry.donchian_len), exclude_current=True)
    tolerance = float(s.entry.get("breakout_tolerance", 0.0))
    # A breakout needs two things, and dropping either one is a real bug:
    #   1. the prior 20-day high was actually exceeded today, and
    #   2. the close held within `tolerance` of that level.
    # Condition 2 alone would fire on any quiet, flat series — a stock that has
    # gone nowhere for a month sits permanently "within 2% of its 20-day high".
    # Condition 1 alone would buy every failed breakout that spiked and closed
    # at the low of the day.
    breaking_out = (high > channel) & (close >= channel * (1.0 - tolerance))

    vol_avg = volume.rolling(int(s.entry.volume_len), min_periods=int(s.entry.volume_len)).mean()
    volume_confirms = volume >= vol_avg * float(s.entry.volume_mult)

    return (breaking_out & volume_confirms).fillna(False).astype(bool)


def _rank_score(close: pd.Series, atr_pct: pd.Series, s) -> pd.Series:
    """Risk-adjusted blended momentum. Higher is better.

    Dividing by ATR% is what makes a 40% move in a quiet utility comparable to
    a 40% move in a biotech that swings 6% a day; without it the ranking is
    just a volatility sort.
    """
    r = s.rank
    skip = int(r.get("skip_recent_days", 0))
    long_mom = ind.momentum(close, int(r.long_lookback), skip=skip)
    short_mom = ind.momentum(close, int(r.short_lookback), skip=skip)

    score = float(r.long_weight) * long_mom + float(r.short_weight) * short_mom
    if bool(r.get("normalize_by_atr", True)):
        denom = atr_pct.replace(0.0, np.nan)
        score = score / denom
    return score


# ---------------------------------------------------------------------------
# exits
# ---------------------------------------------------------------------------
def initial_stop(entry_price: float, atr_value: float, s) -> float:
    """Stop placed at entry: ``entry - initial_stop_atr * ATR``."""
    return float(entry_price) - float(s.exit.initial_stop_atr) * float(atr_value)


def chandelier_stop(highest_close: float, atr_value: float, s) -> float:
    """Trailing stop: ``highest close since entry - chandelier_atr * ATR``.

    Anchored to the highest *close* rather than the highest high, so a single
    spiky intraday wick does not permanently ratchet the stop up into the noise.
    """
    return float(highest_close) - float(s.exit.chandelier_atr) * float(atr_value)


def ratchet(current_stop: float, candidate_stop: float) -> float:
    """A trailing stop only ever moves up. This is the whole discipline."""
    return max(float(current_stop), float(candidate_stop))


def stop_limit_price(stop: float, s) -> float:
    """Limit price for a stop-limit child order, a hair below the stop."""
    return float(stop) * (1.0 - float(s.exit.stop_limit_offset_pct))


# ---------------------------------------------------------------------------
# regime
# ---------------------------------------------------------------------------
def regime_series(benchmark_bars: pd.DataFrame, cfg) -> pd.Series:
    """Boolean series: is the market in an uptrend on this bar?

    Returns all-``True`` when the gate is disabled or the benchmark is missing,
    so a missing SPY file degrades to "no regime filter" rather than "no trades
    ever", which would look identical to a broken strategy.
    """
    regime = cfg.strategy.regime
    if not bool(regime.get("enabled", True)) or benchmark_bars is None or not len(benchmark_bars):
        idx = benchmark_bars.index if benchmark_bars is not None else pd.DatetimeIndex([])
        return pd.Series(True, index=idx, dtype=bool)
    ma = ind.sma(benchmark_bars["close"], int(regime.ma_len))
    return (benchmark_bars["close"] > ma).fillna(False).astype(bool)


def regime_exit_due(regime_ok: bool, cfg) -> bool:
    """Should open positions be closed because the market regime has turned off?

    Off by default: the trail is what normally protects an open position, and
    force-closing the book on a 200-SMA cross would churn it at every whipsaw.
    With the trail disabled a stop never leaves its initial level, so the regime
    is the only thing left that can cut exposure — that is what this option is
    for, and it is a strategy decision, so it lives here rather than in the
    engine or the scanner.

    ``regime_ok`` is the caller's regime state for the bar being decided, which
    is all-``True`` when the gate is off or the benchmark is missing: a missing
    SPY must degrade to "no regime filter", never to "sell everything".
    """
    regime = cfg.strategy.regime
    if not bool(regime.get("enabled", True)):
        return False
    if not bool(regime.get("exit_on_regime_off", False)):
        return False
    return not bool(regime_ok)


# ---------------------------------------------------------------------------
# fundamentals (soft filter)
# ---------------------------------------------------------------------------
def fundamentals_ok(f, s) -> bool | None:
    """Evaluate the soft fundamental filter for one symbol.

    Returns ``None`` when there is not enough data to have an opinion — which
    is common with free data, and must never be conflated with "fails".
    """
    if f is None or not bool(s.fundamentals.get("enabled", True)):
        return None
    if getattr(f, "is_etf", False):
        return True                      # an ETF has no EPS; it is not a failure

    eps = f.trailing_eps
    growth = f.revenue_growth if f.revenue_growth is not None else f.earnings_growth
    if eps is None and growth is None:
        return None

    if not bool(s.fundamentals.get("require_positive_eps_or_growth", True)):
        return True

    positives = [v > 0 for v in (eps, growth) if v is not None]
    return any(positives)
