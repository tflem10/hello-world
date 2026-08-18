"""Drafted Schwab orders and their validation.

The validator's job is to turn "cryptic 400 from the broker at 09:31" into
"clear error at 17:30 the night before". Every test here is a failure mode
that would otherwise only surface with money on the line.
"""

from __future__ import annotations

import pytest

from swing.orders import (
    OrderValidationError,
    bracket_order,
    buy_limit_price,
    describe_order,
    draft_orders,
    stop_child,
    stop_limit_child,
    trailing_stop_child,
    validate_order,
)


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------
def test_draft_produces_all_three_variants():
    orders = draft_orders("AAPL", 10, 190.0, 182.0, 5.7)
    assert set(orders) == {"bracket_stop", "bracket_stop_limit", "bracket_trailing"}
    for name, order in orders.items():
        validate_order(order, path=name)


def test_entry_is_always_a_limit_never_a_market_order():
    for order in draft_orders("AAPL", 10, 190.0, 182.0, 5.7).values():
        assert order["orderType"] == "LIMIT"


def test_limit_sits_just_above_the_reference():
    assert buy_limit_price(100.0, 0.003) == pytest.approx(100.30)
    orders = draft_orders("AAPL", 10, 190.0, 182.0, 5.7, limit_slippage_pct=0.003)
    assert float(orders["bracket_stop"]["price"]) == pytest.approx(190.57, abs=0.01)


def test_prices_are_rounded_to_pennies():
    orders = draft_orders("AAPL", 10, 190.123456, 182.987654, 5.7)
    for order in orders.values():
        assert len(order["price"].split(".")[1]) == 2
        child = order["childOrderStrategies"][0]
        if "stopPrice" in child:
            assert len(child["stopPrice"].split(".")[1]) == 2


def test_stop_limit_child_sits_below_its_stop():
    orders = draft_orders("AAPL", 10, 190.0, 182.0, 5.7, stop_limit_offset_pct=0.005)
    child = orders["bracket_stop_limit"]["childOrderStrategies"][0]
    assert float(child["price"]) < float(child["stopPrice"])
    assert float(child["price"]) == pytest.approx(182.0 * 0.995, abs=0.01)


def test_trailing_child_uses_a_dollar_offset_from_the_last_price():
    child = draft_orders("AAPL", 10, 190.0, 182.0, 5.7)["bracket_trailing"][
        "childOrderStrategies"
    ][0]
    assert child["orderType"] == "TRAILING_STOP"
    assert child["stopPriceLinkBasis"] == "LAST"
    assert child["stopPriceLinkType"] == "VALUE"
    assert child["stopPriceOffset"] == pytest.approx(5.7)


def test_protective_children_are_good_till_cancelled():
    """A DAY stop evaporates at the close and leaves the position naked."""
    for order in draft_orders("AAPL", 10, 190.0, 182.0, 5.7).values():
        assert order["childOrderStrategies"][0]["duration"] == "GOOD_TILL_CANCEL"


def test_entry_and_protective_quantities_match():
    for order in draft_orders("AAPL", 7, 190.0, 182.0, 5.7).values():
        assert order["orderLegCollection"][0]["quantity"] == 7
        assert order["childOrderStrategies"][0]["orderLegCollection"][0]["quantity"] == 7


def test_symbols_are_upper_cased():
    order = draft_orders("aapl", 1, 190.0, 182.0, 5.7)["bracket_stop"]
    assert order["orderLegCollection"][0]["instrument"]["symbol"] == "AAPL"


def test_cannot_draft_a_zero_share_order():
    with pytest.raises(OrderValidationError, match="0 shares"):
        draft_orders("AAPL", 0, 190.0, 182.0, 5.7)


def test_cannot_draft_with_a_stop_above_the_entry():
    with pytest.raises(OrderValidationError, match="not below"):
        draft_orders("AAPL", 10, 180.0, 190.0, 5.7)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
def _good_order():
    return draft_orders("AAPL", 10, 190.0, 182.0, 5.7)["bracket_stop"]


@pytest.mark.parametrize(
    "field", ["orderType", "session", "duration", "orderStrategyType", "orderLegCollection"]
)
def test_missing_required_field_is_rejected(field):
    order = _good_order()
    del order[field]
    with pytest.raises(OrderValidationError, match=field):
        validate_order(order)


def test_market_orders_are_refused_outright():
    order = _good_order()
    order["orderType"] = "MARKET"
    with pytest.raises(OrderValidationError, match="market orders are never drafted"):
        validate_order(order)


def test_fractional_quantity_is_rejected_with_the_reason():
    order = _good_order()
    order["orderLegCollection"][0]["quantity"] = 1.5
    with pytest.raises(OrderValidationError, match="fractional shares"):
        validate_order(order)


def test_zero_quantity_is_rejected():
    order = _good_order()
    order["orderLegCollection"][0]["quantity"] = 0
    with pytest.raises(OrderValidationError, match="quantity"):
        validate_order(order)


def test_short_sales_are_rejected_because_this_system_is_long_only():
    order = _good_order()
    order["orderLegCollection"][0]["instruction"] = "SELL_SHORT"
    with pytest.raises(OrderValidationError, match="long only"):
        validate_order(order)


def test_mismatched_protective_quantity_is_caught():
    """The failure mode: buy 10, protect 5, and 5 shares ride uncovered."""
    order = _good_order()
    order["childOrderStrategies"][0]["orderLegCollection"][0]["quantity"] = 5
    with pytest.raises(OrderValidationError, match="partly unprotected"):
        validate_order(order)


def test_day_duration_on_a_protective_child_is_caught():
    order = _good_order()
    order["childOrderStrategies"][0]["duration"] = "DAY"
    with pytest.raises(OrderValidationError, match="naked overnight"):
        validate_order(order)


def test_trigger_order_without_a_child_is_caught():
    order = _good_order()
    order["childOrderStrategies"] = []
    with pytest.raises(OrderValidationError, match="unprotected"):
        validate_order(order)


def test_negative_and_non_numeric_prices_are_caught():
    order = _good_order()
    order["price"] = "-5.00"
    with pytest.raises(OrderValidationError, match="must be positive"):
        validate_order(order)
    order["price"] = "not a price"
    with pytest.raises(OrderValidationError, match="not numeric"):
        validate_order(order)


def test_stop_order_without_a_stop_price_is_caught():
    child = stop_child("AAPL", 10, 100.0)
    del child["stopPrice"]
    with pytest.raises(OrderValidationError, match="requires a stopPrice"):
        validate_order(child)


def test_trailing_stop_needs_a_positive_offset():
    child = trailing_stop_child("AAPL", 10, 5.0)
    child["stopPriceOffset"] = 0
    with pytest.raises(OrderValidationError, match="must be positive"):
        validate_order(child)


def test_unknown_order_type_is_caught():
    order = _good_order()
    order["orderType"] = "MAGIC"
    with pytest.raises(OrderValidationError, match="unknown orderType"):
        validate_order(order)


def test_unsupported_asset_type_is_caught():
    order = _good_order()
    order["orderLegCollection"][0]["instrument"]["assetType"] = "OPTION"
    with pytest.raises(OrderValidationError, match="assetType"):
        validate_order(order)


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------
def test_describe_shows_the_whole_bracket():
    text = describe_order(_good_order())
    assert "BUY 10 AAPL" in text
    assert "->" in text
    assert "SELL 10 AAPL" in text
    assert "stop" in text


def test_describe_handles_a_bare_child():
    assert "SELL 3 XYZ" in describe_order(stop_limit_child("XYZ", 3, 10.0, 9.95))


def test_bracket_order_helper_is_consistent():
    order = bracket_order("XYZ", 4, 12.34, stop_child("XYZ", 4, 11.0))
    validate_order(order)
    assert order["price"] == "12.34"


# ---------------------------------------------------------------------------
# contract test against schwab-py itself
# ---------------------------------------------------------------------------
def test_our_entry_json_matches_what_schwab_py_would_build():
    """Pin our hand-written JSON to schwab-py's own order builder.

    The Schwab order schema has changed before. This test is what turns "we
    verified the field names once during the build" into something that fails
    loudly the day the vendor renames a field, rather than at 09:31 with money
    on the line. Skipped when the optional extra is not installed.
    """
    pytest.importorskip("schwab", reason="schwab-py is an optional extra")
    from schwab.orders.common import Duration, Session
    from schwab.orders.equities import equity_buy_limit

    theirs = (
        equity_buy_limit("AAPL", 10, "190.50")
        .set_duration(Duration.DAY)
        .set_session(Session.NORMAL)
        .build()
    )
    ours = bracket_order("AAPL", 10, 190.50, stop_child("AAPL", 10, 182.0))

    # Same entry shape; ours additionally carries the protective child, which
    # is what turns SINGLE into TRIGGER.
    for key in ("duration", "session", "orderType", "price", "orderLegCollection"):
        assert ours[key] == theirs[key], key
    assert theirs["orderStrategyType"] == "SINGLE"
    assert ours["orderStrategyType"] == "TRIGGER"


def test_every_enum_value_we_emit_is_one_schwab_py_recognises():
    pytest.importorskip("schwab", reason="schwab-py is an optional extra")
    from schwab.orders.common import (
        Duration,
        OrderStrategyType,
        OrderType,
        Session,
        StopPriceLinkBasis,
        StopPriceLinkType,
    )

    def _values(enum):
        return {e.value for e in enum}

    for order in draft_orders("AAPL", 10, 190.0, 182.0, 5.7).values():
        assert order["orderType"] in _values(OrderType)
        assert order["session"] in _values(Session)
        assert order["duration"] in _values(Duration)
        assert order["orderStrategyType"] in _values(OrderStrategyType)
        child = order["childOrderStrategies"][0]
        assert child["orderType"] in _values(OrderType)
        assert child["duration"] in _values(Duration)
        if child["orderType"] == "TRAILING_STOP":
            assert child["stopPriceLinkBasis"] in _values(StopPriceLinkBasis)
            assert child["stopPriceLinkType"] in _values(StopPriceLinkType)
