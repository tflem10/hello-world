"""Contract 10 — the drafted Schwab order JSON.

The point of these tests is AC13: every draft this system can produce validates
against the documented Schwab TRIGGER/TRAILING_STOP shape. The validator is
hand-rolled rather than jsonschema-based (no extra dependency), so it gets its
own negative tests — a validator that never says no is decoration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import build_config
from swing.alerts import orders
from swing.state import PickRecord


def make_pick(**overrides) -> PickRecord:
    """A sized, tradable pick; override any field."""
    values = {
        "symbol": "ABC",
        "date": "2026-08-18",
        "kind": "pick",
        "entry": 45.10,
        "stop": 41.80,
        "shares": 12,
        "risk_amount": 39.60,
        "score": 1.234,
        "atr": 1.65,
        "earnings_date": "2026-09-02",
        "earnings_known": True,
        "thesis": "Broke the 20d high.",
        "status": "drafted",
    }
    values.update(overrides)
    return PickRecord(**values)


@pytest.fixture
def cfg(tmp_path: Path):
    return build_config(tmp_path)


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------


def test_draft_has_exactly_the_three_contract_keys(cfg) -> None:
    draft = orders.draft_orders(make_pick(), cfg)
    assert set(draft) == {"oto_stop", "oto_stop_limit", "trailing_stop"}
    assert tuple(draft) == orders.DRAFT_KINDS


def test_parent_is_a_buy_limit_trigger_for_the_day(cfg) -> None:
    parent = orders.draft_orders(make_pick(), cfg)["oto_stop"]
    assert parent["orderType"] == "LIMIT"
    assert parent["session"] == "NORMAL"
    assert parent["duration"] == "DAY"
    assert parent["orderStrategyType"] == "TRIGGER"
    assert parent["price"] == "45.10"

    (leg,) = parent["orderLegCollection"]
    assert leg["instruction"] == "BUY"
    assert leg["quantity"] == 12
    assert leg["instrument"] == {"symbol": "ABC", "assetType": "EQUITY"}


def test_stop_child_is_a_gtc_sell_stop(cfg) -> None:
    (child,) = orders.draft_orders(make_pick(), cfg)["oto_stop"]["childOrderStrategies"]
    assert child["orderType"] == "STOP"
    assert child["duration"] == "GOOD_TILL_CANCEL"
    assert child["orderStrategyType"] == "SINGLE"
    assert child["stopPrice"] == "41.80"
    assert child["orderLegCollection"][0]["instruction"] == "SELL"
    assert child["orderLegCollection"][0]["quantity"] == 12


def test_stop_limit_child_prices_the_limit_half_a_percent_under_the_trigger(cfg) -> None:
    (child,) = orders.draft_orders(make_pick(), cfg)["oto_stop_limit"]["childOrderStrategies"]
    assert child["orderType"] == "STOP_LIMIT"
    assert child["stopPrice"] == "41.80"
    # 41.80 * 0.995 == 41.591 -> 41.59
    assert child["price"] == "41.59"
    assert float(child["price"]) < float(child["stopPrice"])


def test_trailing_child_uses_chandelier_atr_distance(cfg) -> None:
    pick = make_pick(atr=1.65)
    (child,) = orders.draft_orders(pick, cfg)["trailing_stop"]["childOrderStrategies"]
    assert child["orderType"] == "TRAILING_STOP"
    assert child["stopPriceLinkBasis"] == "LAST"
    assert child["stopPriceLinkType"] == "VALUE"
    assert child["duration"] == "GOOD_TILL_CANCEL"
    # 3.0 * 1.65 == 4.95
    assert child["stopPriceOffset"] == pytest.approx(round(cfg.strategy.chandelier_mult * 1.65, 2))


def test_prices_are_strings_and_offset_is_a_number(cfg) -> None:
    """Schwab wants price/stopPrice as strings but stopPriceOffset as a number."""
    draft = orders.draft_orders(make_pick(), cfg)
    for name in ("oto_stop", "oto_stop_limit", "trailing_stop"):
        assert isinstance(draft[name]["price"], str)
    stop_child = draft["oto_stop"]["childOrderStrategies"][0]
    assert isinstance(stop_child["stopPrice"], str)
    trailing_child = draft["trailing_stop"]["childOrderStrategies"][0]
    assert isinstance(trailing_child["stopPriceOffset"], float | int)
    assert not isinstance(trailing_child["stopPriceOffset"], str)


def test_prices_always_carry_two_decimals(cfg) -> None:
    draft = orders.draft_orders(make_pick(entry=45.0, stop=40.0), cfg)
    assert draft["oto_stop"]["price"] == "45.00"
    assert draft["oto_stop"]["childOrderStrategies"][0]["stopPrice"] == "40.00"


def test_symbol_is_normalised_to_upper_case(cfg) -> None:
    draft = orders.draft_orders(make_pick(symbol=" brk-b "), cfg)
    leg = draft["oto_stop"]["orderLegCollection"][0]
    assert leg["instrument"]["symbol"] == "BRK-B"


def test_draft_is_json_serialisable(cfg) -> None:
    draft = orders.draft_orders(make_pick(), cfg)
    assert json.loads(json.dumps(draft)) == draft


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


def test_watch_entry_cannot_be_drafted(cfg) -> None:
    with pytest.raises(orders.OrderDraftError, match="watch-list"):
        orders.draft_orders(make_pick(shares=0, kind="watch"), cfg)


def test_stop_above_entry_is_refused(cfg) -> None:
    with pytest.raises(orders.OrderDraftError, match="not below the entry"):
        orders.draft_orders(make_pick(entry=40.0, stop=41.0), cfg)


def test_missing_atr_is_refused(cfg) -> None:
    with pytest.raises(orders.OrderDraftError, match="ATR"):
        orders.draft_orders(make_pick(atr=0.0), cfg)


def test_negative_entry_is_refused(cfg) -> None:
    with pytest.raises(orders.OrderDraftError, match="positive number of dollars"):
        orders.draft_orders(make_pick(entry=-1.0, stop=-5.0), cfg)


def test_blank_symbol_is_refused(cfg) -> None:
    with pytest.raises(orders.OrderDraftError, match="no ticker symbol"):
        orders.draft_orders(make_pick(symbol="  "), cfg)


# ---------------------------------------------------------------------------
# AC13 — every draft validates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "stop", "shares", "atr", "symbol"),
    [
        (45.10, 41.80, 12, 1.65, "ABC"),
        (5.01, 4.55, 1, 0.23, "PENNY"),
        (1234.56, 1100.00, 3, 67.89, "BIGCO"),
        (99.99, 90.01, 250, 4.995, "BRK-B"),
        (7.5, 7.0, 4, 0.25, "ETFX"),
    ],
)
def test_every_generated_draft_validates(cfg, entry, stop, shares, atr, symbol) -> None:
    """AC13: every payload this system can emit passes the structural check."""
    pick = make_pick(symbol=symbol, entry=entry, stop=stop, shares=shares, atr=atr)
    draft = orders.draft_orders(pick, cfg)
    assert orders.validate_order_draft(draft) == []
    for name in orders.DRAFT_KINDS:
        assert orders.validate_one_order(draft[name], name) == []


def test_validator_accepts_a_single_order(cfg) -> None:
    draft = orders.draft_orders(make_pick(), cfg)
    assert orders.validate_order_draft(draft["trailing_stop"]) == []


# ---------------------------------------------------------------------------
# the validator says no
# ---------------------------------------------------------------------------


def _broken(cfg, mutate) -> list[str]:
    draft = orders.draft_orders(make_pick(), cfg)
    mutate(draft)
    return orders.validate_order_draft(draft)


def test_validator_rejects_a_market_parent(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["orderType"] = "MARKET"

    problems = _broken(cfg, mutate)
    assert any("must be 'LIMIT'" in p for p in problems)


def test_validator_rejects_a_day_child(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["childOrderStrategies"][0]["duration"] = "DAY"

    assert any("GOOD_TILL_CANCEL" in p for p in _broken(cfg, mutate))


def test_validator_rejects_zero_quantity(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["orderLegCollection"][0]["quantity"] = 0

    assert any("greater than zero" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_non_equity_instrument(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["orderLegCollection"][0]["instrument"]["assetType"] = "OPTION"

    assert any("assetType" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_float_price(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["price"] = 45.1

    assert any("must be a string" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_badly_formatted_price(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["price"] = "45.1"

    assert any("exactly two decimals" in p for p in _broken(cfg, mutate))


def test_validator_rejects_an_unprotected_entry(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["childOrderStrategies"] = []

    assert any("childOrderStrategies" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_mismatched_child_quantity(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["childOrderStrategies"][0]["orderLegCollection"][0]["quantity"] = 5

    assert any("unmatched exit" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_missing_payload(cfg) -> None:
    draft = orders.draft_orders(make_pick(), cfg)
    del draft["trailing_stop"]
    assert any("missing the trailing_stop" in p for p in orders.validate_order_draft(draft))


def test_validator_rejects_a_percent_trailing_stop(cfg) -> None:
    def mutate(draft):
        draft["trailing_stop"]["childOrderStrategies"][0]["stopPriceLinkType"] = "PERCENT"

    assert any("stopPriceLinkType" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_string_offset(cfg) -> None:
    def mutate(draft):
        draft["trailing_stop"]["childOrderStrategies"][0]["stopPriceOffset"] = "4.95"

    assert any("must be a number of dollars" in p for p in _broken(cfg, mutate))


def test_validator_rejects_a_buying_child(cfg) -> None:
    def mutate(draft):
        draft["oto_stop"]["childOrderStrategies"][0]["orderLegCollection"][0]["instruction"] = "BUY"

    assert any("must be 'SELL'" in p for p in _broken(cfg, mutate))


def test_validator_rejects_nonsense(cfg) -> None:
    assert orders.validate_order_draft("not an order")
    assert orders.validate_order_draft({})
