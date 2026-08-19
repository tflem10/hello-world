"""FROZEN CONTRACT 7 (scoring) — momentum ranking of candidates.

The score is volatility-adjusted momentum: a blend of a 6-month and a 3-month
return, each divided by ATR as a percentage of price. Dividing by ATR% is what
makes the number comparable across a $12 biotech and a $600 index ETF — raw
returns would rank the noisiest names highest.

The 6-month leg skips the most recent ``mom_skip_days`` sessions. That skip is
the standard short-term-reversal correction from the momentum literature: the
last week of a stock's move tends to mean-revert, so including it adds noise to
a medium-horizon ranking. The 3-month leg deliberately does *not* skip — it is
there to catch names whose trend is accelerating right now.

Dividing by ATR% is also the one place in this module where the denominator can
collapse. A halted stock, a merger target pinned at the deal price, or a stale
vendor series repeats one close for weeks; Wilder's ATR decays geometrically on
a flat bar, so ATR% heads for zero while the trailing return stays large and the
ratio climbs without limit — the deadest name in the universe ends up ranked
first. ``MIN_ATR_PCT`` is the floor that stops it: below it the name has no
measurable volatility to normalise by, the score is undefined rather than huge,
and the symbol drops out of the ranking exactly the way a warm-up NaN does
(audit BUG-003).

Window constants that the config deliberately does not expose:

* ``MOM_LONG_DAYS`` (126) / ``MOM_SHORT_DAYS`` (63) — 6 and 3 trading months.
* ``SCORE_ATR_WINDOW`` (14) — Wilder's ATR period, fixed so a score means the
  same thing across config variants during ablations. Note this is *not*
  ``strategy.atr_window``: that knob sizes stops, and letting it rescale the
  ranking too would make scores incomparable across ablation variants.
* ``MIN_HISTORY_ROWS`` (260) — a full year of bars plus a small cushion, the
  least history from which every input to the score is defined.
* ``MIN_ATR_PCT`` (0.05) — the smallest ATR%, in percent of price, this module
  is willing to divide by.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from swing.indicators import atr
from swing.strategy.rules import rolling_high_52w

if TYPE_CHECKING:  # pragma: no cover - imports used only by the type checker
    from swing.config import Config

__all__ = [
    "MIN_ATR_PCT",
    "MIN_HISTORY_ROWS",
    "MOM_LONG_DAYS",
    "MOM_SHORT_DAYS",
    "RANKING_COLUMNS",
    "SCORE_ATR_WINDOW",
    "momentum_score",
    "rank_candidates",
]

MOM_LONG_DAYS = 126
MOM_SHORT_DAYS = 63
SCORE_ATR_WINDOW = 14
MIN_HISTORY_ROWS = 260

MIN_ATR_PCT = 0.05
"""Smallest ATR% (percent of close) the score will divide by; below it, no score.

Five basis points of daily range on a $100 stock is a five-cent ATR — a price
that has effectively stopped moving. Dividing a live 126-day return by a number
that small does not measure volatility-adjusted momentum, it measures how long
the quote has been frozen, and it is monotonically increasing in exactly that
(audit BUG-003). Names under the floor score NaN and are dropped from the
ranking, the same treatment a symbol still inside its warm-up gets.
"""

RANKING_COLUMNS: tuple[str, ...] = ("score", "atr", "close", "high_prox", "rank")


def momentum_score(
    bars: pd.DataFrame, cfg: Config, *, atr_series: pd.Series | None = None
) -> pd.Series:
    """Volatility-adjusted momentum: ``mom_weight_126`` times the 126-day return skipping
    the last ``mom_skip_days``, plus ``mom_weight_63`` times the 63-day return, each
    expressed in percent and divided by ATR(14) as a percent of close.

    The ATR window is :data:`SCORE_ATR_WINDOW`, a module constant — the score is
    deliberately *not* affected by ``cfg.strategy.atr_window``, which governs
    stop placement only (audit DEBT-010).

    Args:
        bars: Contract 3 OHLCV frame.
        cfg: the loaded configuration.
        atr_series: an already-computed ``atr(bars, SCORE_ATR_WINDOW)`` aligned
            to ``bars.index``, purely so a caller that needs ATR anyway does not
            pay for it twice (audit PERF-009). Passing anything else — a
            different window, a different frame — silently changes the score, so
            this is an internal optimisation hook, not a second knob. ``None``
            (the default) computes it here.

    Returns:
        Float Series aligned to ``bars.index``, NaN until
        ``mom_skip_days + 126`` bars of history exist and on any bar whose ATR%
        is under :data:`MIN_ATR_PCT`. Higher is better; a negative score means
        the name fell over the measured window.
    """
    strategy = cfg.strategy
    close = bars["close"]
    skip = strategy.mom_skip_days

    atr_values = atr(bars, SCORE_ATR_WINDOW) if atr_series is None else atr_series
    atr_pct = atr_values / close * 100.0
    # Below the floor the divisor stops describing volatility, so the score is
    # undefined rather than enormous (audit BUG-003). NaN in, NaN out: `where`
    # already treats a warm-up NaN the same way, since NaN >= x is False.
    divisor = atr_pct.where(atr_pct >= MIN_ATR_PCT)
    long_return = (close.shift(skip) / close.shift(skip + MOM_LONG_DAYS) - 1.0) * 100.0
    short_return = (close / close.shift(MOM_SHORT_DAYS) - 1.0) * 100.0

    score = strategy.mom_weight_126 * (long_return / divisor) + strategy.mom_weight_63 * (
        short_return / divisor
    )
    return score.astype(float).rename("momentum_score")


def _empty_ranking() -> pd.DataFrame:
    """An empty ranking table with the Contract 7 columns and dtypes."""
    return pd.DataFrame(
        {
            "score": pd.Series(dtype="float64"),
            "atr": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "high_prox": pd.Series(dtype="float64"),
            "rank": pd.Series(dtype="int64"),
        },
        index=pd.Index([], dtype="object", name="symbol"),
    )


def rank_candidates(
    bars_by_symbol: dict[str, pd.DataFrame], asof: pd.Timestamp, cfg: Config
) -> pd.DataFrame:
    """Rank every symbol with at least 260 bars up to ``asof`` by momentum score, best first.

    Each symbol's history is truncated at ``asof`` before anything is computed,
    so a ranking for a past date can never see the future — the backtest engine
    and the nightly scanner call this identically.

    Args:
        bars_by_symbol: Contract 3 OHLCV frames keyed by symbol.
        asof: the as-of date; the last bar at or before it is used.
        cfg: the loaded configuration.

    Returns:
        DataFrame indexed by symbol with columns ``score``, ``atr``, ``close``,
        ``high_prox`` (close divided by the 52-week closing high, 1.0 = at the
        high) and ``rank`` (1 = best). Sorted by score descending, ties broken
        by ``high_prox`` descending and then by symbol ascending so the order is
        fully deterministic. Symbols with too little history, or whose score is
        NaN or infinite, are omitted; an empty input gives an empty frame with
        these columns.

        The finiteness test is ``np.isfinite``, not ``pd.isna``: an infinite
        score passes ``pd.isna`` and would sort *first*, while the backtest
        engine sorts non-finite scores last. Both consumers of this contract
        have to agree that a non-finite score never wins (audit BUG-003).
    """
    cutoff = pd.Timestamp(asof)
    rows: list[dict[str, Any]] = []

    for symbol in sorted(bars_by_symbol):
        bars = bars_by_symbol[symbol]
        history = bars.loc[bars.index <= cutoff]
        if len(history) < MIN_HISTORY_ROWS:
            continue

        # One ATR per symbol: the score divides by it and the table reports it
        # (audit PERF-009).
        atr_series = atr(history, SCORE_ATR_WINDOW)
        score = momentum_score(history, cfg, atr_series=atr_series).iloc[-1]
        if not np.isfinite(score):
            continue

        close = float(history["close"].iloc[-1])
        yearly_high = rolling_high_52w(history["close"]).iloc[-1]
        high_prox = close / float(yearly_high) if yearly_high > 0 else float("nan")
        rows.append(
            {
                "symbol": symbol,
                "score": float(score),
                "atr": float(atr_series.iloc[-1]),
                "close": close,
                "high_prox": float(high_prox),
            }
        )

    if not rows:
        return _empty_ranking()

    table = pd.DataFrame(rows).sort_values(
        ["score", "high_prox", "symbol"], ascending=[False, False, True]
    )
    table = table.set_index("symbol")
    table.index.name = "symbol"
    table["rank"] = np.arange(1, len(table) + 1, dtype="int64")
    return table[list(RANKING_COLUMNS)]
