"""Trading costs — the difference between a backtest and a fantasy.

Every fill in this system pays a cost, charged **per side** (once on the way in,
once on the way out) and expressed per share:

    per_share_cost = price * slippage_bps / 10_000  +  spread_atr_frac * ATR

The two terms model two different frictions:

* ``slippage_bps`` is proportional slippage — the price you actually get is
  worse than the price you saw, by a fixed fraction of the price. Five basis
  points on a $50 stock is 2.5 cents.
* ``spread_atr_frac * ATR`` is the half-spread, scaled by how volatile the name
  is. A quiet ETF and a jumpy small cap do not cost the same to cross, and
  quoting the spread as a fraction of ATR captures that without needing a
  historical quote database (which free data does not give us).

Sign convention: **buys pay up, sells pay down.** A buy fills at
``price + per_share_cost`` and a sell fills at ``price - per_share_cost``. The
engine records the raw market price and the total dollar cost separately, so
every trade can be re-derived by hand from the report.

The ATR used is always the ATR of the last bar whose close was known *before*
the fill — for an entry that is the signal bar, for a stop exit the previous
bar. Never the bar being filled into, which would be lookahead.

These are pure functions with no state: the same inputs always give the same
number, which is what makes the deterministic-rerun guarantee possible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "BPS_PER_UNIT",
    "CostModel",
    "buy_fill_price",
    "per_share_cost",
    "sell_fill_price",
    "total_cost",
]

#: Basis points per unit: 1 bp = 1/10_000.
BPS_PER_UNIT = 10_000.0


def _clean_atr(atr_value: float) -> float:
    """Return a usable ATR, treating NaN/inf/negative warm-up values as zero.

    During the indicator warm-up ATR is NaN. A NaN cost would poison the whole
    equity curve, so we degrade to "slippage only" instead. In practice the
    trend template needs a year of history before it can fire, so this branch
    is a safety net rather than a normal path.
    """
    if atr_value is None:
        return 0.0
    value = float(atr_value)
    if not math.isfinite(value) or value < 0.0:
        return 0.0
    return value


@dataclass(frozen=True)
class CostModel:
    """The per-side cost model, pinned to a pair of config numbers.

    Attributes:
        slippage_bps: proportional slippage per side, in basis points.
        spread_atr_frac: assumed half-spread per side, as a fraction of ATR.
    """

    slippage_bps: float
    spread_atr_frac: float

    @classmethod
    def from_config(cls, cfg: Config) -> CostModel:
        """Build the model from ``cfg.backtest``."""
        return cls(
            slippage_bps=float(cfg.backtest.slippage_bps),
            spread_atr_frac=float(cfg.backtest.spread_atr_frac),
        )

    def per_share(self, price: float, atr_value: float) -> float:
        """Cost of trading ONE share at ``price`` when ATR is ``atr_value``.

        Always non-negative, and always the same on both sides — the direction
        is applied by the caller (see :meth:`buy_price` / :meth:`sell_price`).
        """
        slip = float(price) * self.slippage_bps / BPS_PER_UNIT
        spread = self.spread_atr_frac * _clean_atr(atr_value)
        return slip + spread

    def buy_price(self, price: float, atr_value: float) -> float:
        """Effective price paid when buying — the market price plus the cost."""
        return float(price) + self.per_share(price, atr_value)

    def sell_price(self, price: float, atr_value: float) -> float:
        """Effective price received when selling — the market price minus the cost.

        Clamped at zero: a cost larger than the price would mean paying someone
        to take shares off you, which is not a thing that happens.
        """
        return max(float(price) - self.per_share(price, atr_value), 0.0)

    def total(self, shares: int, price: float, atr_value: float) -> float:
        """Total dollar cost of trading ``shares`` shares at ``price``."""
        return float(shares) * self.per_share(price, atr_value)


# ---------------------------------------------------------------------------
# free-function forms, for callers that only have a Config to hand
# ---------------------------------------------------------------------------


def per_share_cost(price: float, atr_value: float, cfg: Config) -> float:
    """Per-share, per-side cost implied by ``cfg.backtest``."""
    return CostModel.from_config(cfg).per_share(price, atr_value)


def buy_fill_price(price: float, atr_value: float, cfg: Config) -> float:
    """Price actually paid on a buy: ``price`` plus one side of cost."""
    return CostModel.from_config(cfg).buy_price(price, atr_value)


def sell_fill_price(price: float, atr_value: float, cfg: Config) -> float:
    """Price actually received on a sell: ``price`` minus one side of cost."""
    return CostModel.from_config(cfg).sell_price(price, atr_value)


def total_cost(shares: int, price: float, atr_value: float, cfg: Config) -> float:
    """Total dollar cost of one side of a ``shares``-share trade."""
    return CostModel.from_config(cfg).total(shares, price, atr_value)
