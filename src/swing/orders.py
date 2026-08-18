"""Drafted Schwab orders.

Every pick carries three ready-to-place order payloads:

``bracket_stop``
    A ``TRIGGER`` (one-triggers-other) order: a buy LIMIT that, once filled,
    submits a GTC SELL STOP child at the initial stop.
``bracket_stop_limit``
    Same shape, but the child is a STOP_LIMIT whose limit sits
    ``stop_limit_offset_pct`` below the stop. Protects against a terrible fill
    in a flash crash; risks not filling at all in a real one. Both failure
    modes are real, which is why both variants are drafted and neither is
    chosen for you.
``bracket_trailing``
    Child is a native Schwab TRAILING_STOP with a fixed dollar offset.

**The native trailing stop is not the Chandelier exit.** Schwab's trailing stop
tracks the *last price* continuously, including intraday spikes. The Chandelier
exit in the strategy trails the *highest daily close* and only ratchets once a
day. They will diverge — typically the native trail is looser to intraday noise
in one direction and tighter in the other. The backtest models the Chandelier
version. If you place the native trailing stop, you are trading a slightly
different system than the one that was tested. The difference is documented
here, in the pick sheet, and in docs/runbook.md rather than hidden.

Field names follow the Schwab Trader API order schema. **Verify them against
the live schwab-py documentation before the first real order** — the schema has
changed before, and a rejected order is the good failure mode. Run
``swing execute`` in dry-run (the default) to see exactly what would be sent.
"""

from __future__ import annotations

from typing import Any

EQUITY = "EQUITY"
SESSION_NORMAL = "NORMAL"
DURATION_DAY = "DAY"
DURATION_GTC = "GOOD_TILL_CANCEL"


class OrderValidationError(ValueError):
    """Raised when a drafted order is structurally wrong."""


def _leg(instruction: str, quantity: int, symbol: str) -> dict[str, Any]:
    return {
        "instruction": instruction,
        "quantity": int(quantity),
        "instrument": {"symbol": symbol.upper(), "assetType": EQUITY},
    }


def _price(value: float) -> str:
    """Schwab wants prices as strings, and equities trade in penny increments.

    Sending 104.55714285714286 gets the order rejected; rounding it here means
    the number in the pick sheet is the number that reaches the broker.
    """
    return f"{round(float(value) + 1e-9, 2):.2f}"


def buy_limit_price(reference: float, slippage_pct: float) -> float:
    """Limit a hair above the reference so a normal open still fills.

    Market orders are never drafted: on a thin small cap at 09:30 a market
    order is an invitation.
    """
    return round(float(reference) * (1.0 + float(slippage_pct)), 2)


def stop_child(symbol: str, shares: int, stop: float) -> dict[str, Any]:
    return {
        "orderType": "STOP",
        "session": SESSION_NORMAL,
        "duration": DURATION_GTC,
        "orderStrategyType": "SINGLE",
        "stopPrice": _price(stop),
        "orderLegCollection": [_leg("SELL", shares, symbol)],
    }


def stop_limit_child(symbol: str, shares: int, stop: float, limit: float) -> dict[str, Any]:
    return {
        "orderType": "STOP_LIMIT",
        "session": SESSION_NORMAL,
        "duration": DURATION_GTC,
        "orderStrategyType": "SINGLE",
        "stopPrice": _price(stop),
        "price": _price(limit),
        "orderLegCollection": [_leg("SELL", shares, symbol)],
    }


def trailing_stop_child(symbol: str, shares: int, offset: float) -> dict[str, Any]:
    """Native Schwab trailing stop, offset expressed in dollars from the last price."""
    return {
        "orderType": "TRAILING_STOP",
        "session": SESSION_NORMAL,
        "duration": DURATION_GTC,
        "orderStrategyType": "SINGLE",
        "stopPriceLinkBasis": "LAST",
        "stopPriceLinkType": "VALUE",
        "stopPriceOffset": round(float(offset), 2),
        "orderLegCollection": [_leg("SELL", shares, symbol)],
    }


def bracket_order(
    symbol: str,
    shares: int,
    limit_price: float,
    child: dict[str, Any],
    duration: str = DURATION_DAY,
) -> dict[str, Any]:
    """Buy LIMIT that triggers a protective child order once it fills."""
    return {
        "orderType": "LIMIT",
        "session": SESSION_NORMAL,
        "duration": duration,
        "orderStrategyType": "TRIGGER",
        "price": _price(limit_price),
        "orderLegCollection": [_leg("BUY", shares, symbol)],
        "childOrderStrategies": [child],
    }


def draft_orders(
    symbol: str,
    shares: int,
    reference_price: float,
    stop: float,
    trail_offset: float,
    stop_limit_offset_pct: float = 0.005,
    limit_slippage_pct: float = 0.003,
) -> dict[str, dict[str, Any]]:
    """All three order variants for one pick."""
    if shares < 1:
        raise OrderValidationError(
            f"{symbol}: cannot draft an order for {shares} shares"
        )
    if stop >= reference_price:
        raise OrderValidationError(
            f"{symbol}: stop {stop:.2f} is not below the reference {reference_price:.2f}"
        )

    limit = buy_limit_price(reference_price, limit_slippage_pct)
    stop_limit = round(stop * (1.0 - stop_limit_offset_pct), 2)

    return {
        "bracket_stop": bracket_order(symbol, shares, limit, stop_child(symbol, shares, stop)),
        "bracket_stop_limit": bracket_order(
            symbol, shares, limit, stop_limit_child(symbol, shares, stop, stop_limit)
        ),
        "bracket_trailing": bracket_order(
            symbol, shares, limit, trailing_stop_child(symbol, shares, trail_offset)
        ),
    }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------
_REQUIRED_TOP = ("orderType", "session", "duration", "orderStrategyType", "orderLegCollection")
_VALID_ORDER_TYPES = {"LIMIT", "MARKET", "STOP", "STOP_LIMIT", "TRAILING_STOP"}
_VALID_INSTRUCTIONS = {"BUY", "SELL"}
_VALID_DURATIONS = {"DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL"}
_VALID_SESSIONS = {"NORMAL", "AM", "PM", "SEAMLESS"}


def validate_order(order: dict[str, Any], path: str = "order") -> None:
    """Structural validation of a drafted order.

    This is not a substitute for Schwab's own validation — it cannot know that
    a symbol is untradable or that the account lacks buying power. It catches
    the class of error that would otherwise only surface as a cryptic 400 at
    the worst possible moment: a missing field, a negative quantity, a market
    order that should never have been drafted, or a protective child that would
    sell a different number of shares than were bought.
    """
    for key in _REQUIRED_TOP:
        if key not in order:
            raise OrderValidationError(f"{path}: missing required field {key!r}")

    order_type = order["orderType"]
    if order_type not in _VALID_ORDER_TYPES:
        raise OrderValidationError(f"{path}: unknown orderType {order_type!r}")
    if order_type == "MARKET":
        raise OrderValidationError(
            f"{path}: market orders are never drafted by this system "
            "(see execution.order_type)"
        )
    if order["session"] not in _VALID_SESSIONS:
        raise OrderValidationError(f"{path}: unknown session {order['session']!r}")
    if order["duration"] not in _VALID_DURATIONS:
        raise OrderValidationError(f"{path}: unknown duration {order['duration']!r}")

    legs = order["orderLegCollection"]
    if not isinstance(legs, list) or not legs:
        raise OrderValidationError(f"{path}: orderLegCollection must be a non-empty list")
    for i, leg in enumerate(legs):
        _validate_leg(leg, f"{path}.leg[{i}]")

    if order_type in ("LIMIT", "STOP_LIMIT") and "price" not in order:
        raise OrderValidationError(f"{path}: {order_type} requires a price")
    if order_type in ("STOP", "STOP_LIMIT") and "stopPrice" not in order:
        raise OrderValidationError(f"{path}: {order_type} requires a stopPrice")
    if order_type == "TRAILING_STOP":
        for key in ("stopPriceLinkBasis", "stopPriceLinkType", "stopPriceOffset"):
            if key not in order:
                raise OrderValidationError(f"{path}: TRAILING_STOP requires {key}")
        if float(order["stopPriceOffset"]) <= 0:
            raise OrderValidationError(f"{path}: stopPriceOffset must be positive")

    for key in ("price", "stopPrice"):
        if key in order:
            try:
                value = float(order[key])
            except (TypeError, ValueError) as exc:
                raise OrderValidationError(f"{path}: {key} is not numeric") from exc
            if value <= 0:
                raise OrderValidationError(f"{path}: {key} must be positive")

    children = order.get("childOrderStrategies") or []
    if order["orderStrategyType"] == "TRIGGER" and not children:
        raise OrderValidationError(
            f"{path}: a TRIGGER order with no child would leave the position unprotected"
        )
    buy_qty = sum(leg["quantity"] for leg in legs if leg["instruction"] == "BUY")
    for i, child in enumerate(children):
        validate_order(child, f"{path}.child[{i}]")
        sell_qty = sum(
            leg["quantity"]
            for leg in child["orderLegCollection"]
            if leg["instruction"] == "SELL"
        )
        if buy_qty and sell_qty != buy_qty:
            raise OrderValidationError(
                f"{path}.child[{i}]: protective order sells {sell_qty} shares but the "
                f"entry buys {buy_qty} — the position would be left partly unprotected"
            )
        if child["duration"] != DURATION_GTC:
            raise OrderValidationError(
                f"{path}.child[{i}]: a protective stop must be "
                f"{DURATION_GTC}, not {child['duration']} — a DAY stop evaporates "
                "at the close and leaves the position naked overnight"
            )


def _validate_leg(leg: dict[str, Any], path: str) -> None:
    for key in ("instruction", "quantity", "instrument"):
        if key not in leg:
            raise OrderValidationError(f"{path}: missing {key!r}")
    if leg["instruction"] not in _VALID_INSTRUCTIONS:
        raise OrderValidationError(
            f"{path}: this system is long only; unexpected instruction "
            f"{leg['instruction']!r}"
        )
    quantity = leg["quantity"]
    if not isinstance(quantity, int) or quantity < 1:
        raise OrderValidationError(
            f"{path}: quantity must be a whole number >= 1 (Schwab does not accept "
            f"fractional shares through the Trader API); got {quantity!r}"
        )
    instrument = leg["instrument"]
    if instrument.get("assetType") != EQUITY:
        raise OrderValidationError(f"{path}: unsupported assetType {instrument.get('assetType')!r}")
    if not instrument.get("symbol"):
        raise OrderValidationError(f"{path}: instrument has no symbol")


def describe_order(order: dict[str, Any]) -> str:
    """One-line human summary, used by the confirm prompt in `swing execute`."""
    leg = order["orderLegCollection"][0]
    parts = [
        f"{leg['instruction']} {leg['quantity']} {leg['instrument']['symbol']}",
        order["orderType"],
    ]
    if "price" in order:
        parts.append(f"@ {order['price']}")
    if "stopPrice" in order:
        parts.append(f"stop {order['stopPrice']}")
    if "stopPriceOffset" in order:
        parts.append(f"trail {order['stopPriceOffset']}")
    parts.append(order["duration"])
    text = " ".join(parts)
    for child in order.get("childOrderStrategies") or []:
        text += "  ->  " + describe_order(child)
    return text
