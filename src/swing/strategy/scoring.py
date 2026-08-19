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

Window constants that the config deliberately does not expose:

* ``MOM_LONG_DAYS`` (126) / ``MOM_SHORT_DAYS`` (63) — 6 and 3 trading months.
* ``SCORE_ATR_WINDOW`` (14) — Wilder's ATR period, fixed so a score means the
  same thing across config variants during ablations.
* ``MIN_HISTORY_ROWS`` (260) — a full year of bars plus a small cushion, the
  least history from which every input to the score is defined.
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

RANKING_COLUMNS: tuple[str, ...] = ("score", "atr", "close", "high_prox", "rank")


def momentum_score(bars: pd.DataFrame, cfg: Config) -> pd.Series:
    """Volatility-adjusted momentum: ``mom_weight_126`` times the 126-day return skipping
    the last ``mom_skip_days``, plus ``mom_weight_63`` times the 63-day return, each
    expressed in percent and divided by ATR(14) as a percent of close.

    Args:
        bars: Contract 3 OHLCV frame.
        cfg: the loaded configuration.

    Returns:
        Float Series aligned to ``bars.index``, NaN until
        ``mom_skip_days + 126`` bars of history exist. Higher is better; a
        negative score means the name fell over the measured window.
    """
    strategy = cfg.strategy
    close = bars["close"]
    skip = strategy.mom_skip_days

    atr_pct = atr(bars, SCORE_ATR_WINDOW) / close * 100.0
    long_return = (close.shift(skip) / close.shift(skip + MOM_LONG_DAYS) - 1.0) * 100.0
    short_return = (close / close.shift(MOM_SHORT_DAYS) - 1.0) * 100.0

    score = strategy.mom_weight_126 * (long_return / atr_pct) + strategy.mom_weight_63 * (
        short_return / atr_pct
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
        fully deterministic. Symbols with too little history or an undefined
        score are omitted; an empty input gives an empty frame with these
        columns.
    """
    cutoff = pd.Timestamp(asof)
    rows: list[dict[str, Any]] = []

    for symbol in sorted(bars_by_symbol):
        bars = bars_by_symbol[symbol]
        history = bars.loc[bars.index <= cutoff]
        if len(history) < MIN_HISTORY_ROWS:
            continue

        score = momentum_score(history, cfg).iloc[-1]
        if pd.isna(score):
            continue

        close = float(history["close"].iloc[-1])
        yearly_high = rolling_high_52w(history["close"]).iloc[-1]
        high_prox = close / float(yearly_high) if yearly_high > 0 else float("nan")
        rows.append(
            {
                "symbol": symbol,
                "score": float(score),
                "atr": float(atr(history, SCORE_ATR_WINDOW).iloc[-1]),
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
