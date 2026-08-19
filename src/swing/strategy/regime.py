"""FROZEN CONTRACT 7 (regime) — the market-wide entry gate.

One rule, one question: is the broad market healthy enough to be opening new
long positions today? The answer gates *entries only* — an open position is
managed by its own stops, never closed just because the index rolled over.

Like everything in :mod:`swing.strategy`, the answer is a Series so the scanner
can read ``.iloc[-1]`` and the backtest engine can read any historical date from
the identical code path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from swing.indicators import sma

if TYPE_CHECKING:  # pragma: no cover - imports used only by the type checker
    from swing.config import Config

__all__ = ["entries_allowed"]


def entries_allowed(spy_bars: pd.DataFrame, cfg: Config) -> pd.Series:
    """True on days when the regime symbol closes above its ``regime.sma_window`` SMA;
    all True when ``regime.enabled`` is false.

    Args:
        spy_bars: Contract 3 OHLCV frame for ``cfg.regime.symbol`` (SPY by default).
        cfg: the loaded configuration.

    Returns:
        Boolean Series aligned to ``spy_bars.index``. False through the SMA
        warm-up, so a short history blocks entries rather than guessing. When
        the gate is disabled the Series is all True *including* the warm-up,
        because there is then nothing to warm up.
    """
    index = spy_bars.index
    if not cfg.regime.enabled:
        return pd.Series(True, index=index, name="entries_allowed", dtype=bool)

    close = spy_bars["close"]
    allowed = close > sma(close, cfg.regime.sma_window)
    return allowed.fillna(False).astype(bool).rename("entries_allowed")
