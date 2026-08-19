"""Cost model tests — every expected number is arithmetic done by hand in a comment.

The cost model is three lines of code that decide whether the whole backtest is
optimistic. It gets literal tests.
"""

from __future__ import annotations

import math

import pytest

from conftest import build_config
from swing.backtest.costs import (
    BPS_PER_UNIT,
    CostModel,
    buy_fill_price,
    per_share_cost,
    sell_fill_price,
    total_cost,
)


def test_bps_constant_is_ten_thousand():
    assert BPS_PER_UNIT == 10_000.0


def test_per_share_cost_is_slippage_plus_atr_spread():
    model = CostModel(slippage_bps=5.0, spread_atr_frac=0.05)
    # 100.00 * 5 / 10_000 = 0.05 slippage
    # 0.05 * 2.00                = 0.10 half-spread
    #                            = 0.15 per share, per side
    assert model.per_share(100.0, 2.0) == pytest.approx(0.15)


def test_buy_pays_up_and_sell_pays_down_by_the_same_amount():
    model = CostModel(slippage_bps=5.0, spread_atr_frac=0.05)
    buy = model.buy_price(100.0, 2.0)
    sell = model.sell_price(100.0, 2.0)
    assert buy == pytest.approx(100.15)
    assert sell == pytest.approx(99.85)
    # Symmetric: the round trip costs exactly two sides.
    assert buy - sell == pytest.approx(2 * model.per_share(100.0, 2.0))


def test_total_cost_scales_with_share_count():
    model = CostModel(slippage_bps=10.0, spread_atr_frac=0.1)
    # 50.00 * 10 / 10_000 = 0.05 ; 0.1 * 1.5 = 0.15 ; total per share 0.20
    # 137 shares * 0.20   = 27.40
    assert model.per_share(50.0, 1.5) == pytest.approx(0.20)
    assert model.total(137, 50.0, 1.5) == pytest.approx(27.40)


def test_zero_config_means_zero_cost():
    model = CostModel(slippage_bps=0.0, spread_atr_frac=0.0)
    assert model.per_share(1234.5, 9.9) == 0.0
    assert model.buy_price(1234.5, 9.9) == 1234.5
    assert model.sell_price(1234.5, 9.9) == 1234.5


@pytest.mark.parametrize("bad_atr", [float("nan"), float("inf"), -1.0, None])
def test_unusable_atr_degrades_to_slippage_only(bad_atr):
    """Warm-up NaN must not poison the equity curve with NaN cash."""
    model = CostModel(slippage_bps=5.0, spread_atr_frac=0.05)
    cost = model.per_share(100.0, bad_atr)
    assert math.isfinite(cost)
    assert cost == pytest.approx(0.05)  # slippage only


def test_sell_price_never_goes_negative():
    """A cost bigger than the price would mean being paid to sell. Clamp at zero."""
    model = CostModel(slippage_bps=0.0, spread_atr_frac=10.0)
    assert model.sell_price(1.0, 5.0) == 0.0


def test_free_functions_match_the_model(tmp_path):
    cfg = build_config(tmp_path, backtest={"slippage_bps": 7.5, "spread_atr_frac": 0.02})
    model = CostModel.from_config(cfg)
    assert model.slippage_bps == 7.5
    assert model.spread_atr_frac == 0.02

    # 200.00 * 7.5 / 10_000 = 0.15 ; 0.02 * 3.0 = 0.06 ; per share 0.21
    assert per_share_cost(200.0, 3.0, cfg) == pytest.approx(0.21)
    assert buy_fill_price(200.0, 3.0, cfg) == pytest.approx(200.21)
    assert sell_fill_price(200.0, 3.0, cfg) == pytest.approx(199.79)
    assert total_cost(10, 200.0, 3.0, cfg) == pytest.approx(2.10)


def test_cost_is_pure(tmp_path):
    """Same inputs, same answer, forever — the basis of the determinism guarantee."""
    cfg = build_config(tmp_path)
    first = [per_share_cost(p, a, cfg) for p, a in ((10.0, 0.5), (250.0, 4.0), (7.5, 0.1))]
    second = [per_share_cost(p, a, cfg) for p, a in ((10.0, 0.5), (250.0, 4.0), (7.5, 0.1))]
    assert first == second
