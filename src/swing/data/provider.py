"""The :class:`DataProvider` seam.

Everything above this module works in terms of a normalised daily-bar frame,
so swapping yfinance for Schwab (once the developer app is approved) is a
one-line config change and cannot alter strategy behaviour.

Normalised bar frame contract
-----------------------------
* index: ``DatetimeIndex`` named ``date``, tz-naive, sorted, unique, daily
* columns: exactly ``open, high, low, close, volume`` (float64/float64/.../float64)
* prices are **split- and dividend-adjusted** so that a backtest over a decade
  does not trip over a 4-for-1 split
* no NaNs in ``close``; rows that arrive incomplete are dropped
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

import pandas as pd

BARS_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Quote:
    """A point-in-time quote used by pre-open confirmation and execution."""

    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    timestamp: datetime | None = None
    stale: bool = False

    @property
    def spread_pct(self) -> float | None:
        if self.bid is None or self.ask is None or self.ask <= 0:
            return None
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2.0)


@dataclass(frozen=True)
class Fundamentals:
    """The handful of fundamental fields used as a *soft* filter.

    Free fundamental data is patchy, so every field is optional and the
    strategy treats ``None`` as "no opinion", never as "fails".
    """

    symbol: str
    trailing_eps: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    market_cap: float | None = None
    sector: str | None = None
    is_etf: bool = False


@runtime_checkable
class DataProvider(Protocol):
    """Minimal surface every data source must implement."""

    name: str

    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """Adjusted daily OHLCV per symbol. Missing symbols are simply absent."""

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Latest available price per symbol."""

    def earnings_dates(self, symbols: list[str]) -> dict[str, date | None]:
        """Next confirmed/estimated earnings date, or ``None`` if unknown."""

    def fundamentals(self, symbols: list[str]) -> dict[str, Fundamentals]:
        """Coarse fundamentals for the soft filter."""


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a provider frame into the canonical schema.

    Accepts the common spellings (``Open``/``Adj Close``/``Date`` index or
    column) and raises if the required fields simply are not present.
    """
    if df is None or len(df) == 0:
        return empty_bars()

    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        # yfinance hands back (field, ticker) or (ticker, field) for batches;
        # by the time we get here a single symbol has been sliced out, so any
        # remaining level is redundant.
        out.columns = [
            c[-1] if c[0] in ("", None) else c[0] for c in out.columns.to_flat_index()
        ]

    rename = {}
    for col in out.columns:
        key = str(col).strip().lower().replace(" ", "_")
        if key in ("adj_close", "adjclose"):
            rename[col] = "adj_close"
        elif key in ("open", "high", "low", "close", "volume"):
            rename[col] = key
    out = out.rename(columns=rename)

    if "close" not in out.columns and "adj_close" in out.columns:
        out["close"] = out["adj_close"]

    missing = [c for c in BARS_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"bar frame missing required columns: {missing}")

    out = out[BARS_COLUMNS]

    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    idx = out.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "date"

    out = out.astype("float64")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["close"])
    # Zero/negative prices are data errors, not tradable bars.
    out = out[(out[["open", "high", "low", "close"]] > 0).all(axis=1)]
    out["volume"] = out["volume"].fillna(0.0)
    return out


def empty_bars() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], name="date")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BARS_COLUMNS}, index=idx)


def get_provider(cfg) -> DataProvider:
    """Instantiate the provider named in ``[data] provider``."""
    name = str(cfg.data.provider).lower()
    if name == "yfinance":
        from .yfinance_provider import YFinanceProvider

        return YFinanceProvider(cfg)
    if name == "schwab":
        from .schwab_provider import SchwabProvider

        return SchwabProvider(cfg)
    if name == "stooq":
        from .stooq_provider import StooqProvider

        return StooqProvider(cfg)
    raise ValueError(
        f"unknown data.provider {name!r} (expected 'yfinance', 'schwab' or 'stooq')"
    )
