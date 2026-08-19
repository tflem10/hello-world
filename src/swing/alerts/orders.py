"""FROZEN CONTRACT 10 — Schwab order drafts.

One pick becomes three ready-to-send order payloads, and the human decides which
one to use:

``oto_stop``
    Buy limit at the entry price; once it fills, a plain stop-market sell sits
    below it good-till-cancelled. Simplest, always exits, worst fill in a gap.
``oto_stop_limit``
    Same, but the protective child is a stop-*limit* whose limit sits half a
    percent under the trigger. Better fills, and a real chance of not filling at
    all when the stock gaps through — that trade-off is the user's to make.
``trailing_stop``
    Same buy, but the child is a trailing stop offset by
    ``chandelier_mult * ATR`` from the last price, which is the Chandelier exit
    expressed in the broker's own language.

Every payload is the *whole* order: a ``TRIGGER`` parent carrying exactly one
child in ``childOrderStrategies``. Nothing here talks to a broker — that is
WP-G's job. This module only builds JSON and checks its own work.

Field names and enum spellings were verified against the schwab-py OrderBuilder
reference (August 2026):

* ``duration`` is ``GOOD_TILL_CANCEL`` — Schwab does not accept ``"GTC"``.
* ``price`` and ``stopPrice`` are **strings**: "the Schwab API expects price as
  a string, whereas schwab-py allows setting prices as a floating point number".
  Two decimal places for anything at or above $1.
* ``stopPriceOffset`` is a **number**, not a string — it is a distance, not a
  price, and both the schwab-py builder and the published order schema treat it
  numerically.
* ``stopPriceLinkBasis="LAST"`` and ``stopPriceLinkType="VALUE"`` are valid
  members of their enums.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config
    from swing.state import PickRecord

__all__ = [
    "ASSET_TYPE",
    "CHILD_DURATION",
    "DRAFT_KINDS",
    "OrderDraftError",
    "PARENT_DURATION",
    "STOP_LIMIT_SLIPPAGE",
    "draft_orders",
    "validate_order_draft",
]

#: The three payloads :func:`draft_orders` always returns, in a fixed order.
DRAFT_KINDS: tuple[str, ...] = ("oto_stop", "oto_stop_limit", "trailing_stop")

#: How far under the stop trigger the STOP_LIMIT child's limit price sits.
STOP_LIMIT_SLIPPAGE = 0.005

SESSION = "NORMAL"
PARENT_DURATION = "DAY"
CHILD_DURATION = "GOOD_TILL_CANCEL"
ASSET_TYPE = "EQUITY"
PARENT_STRATEGY = "TRIGGER"
CHILD_STRATEGY = "SINGLE"

_CHILD_ORDER_TYPES = ("STOP", "STOP_LIMIT", "TRAILING_STOP")

#: Schwab price strings: whole dollars and exactly two decimals.
_PRICE_RE = re.compile(r"^\d+\.\d{2}$")


class OrderDraftError(ValueError):
    """Raised when a pick cannot be turned into a sendable order.

    The message is always a complete sentence naming the symbol and the number
    that made the draft impossible.
    """


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


def _money(value: float, *, symbol: str, what: str) -> str:
    """Format a dollar price the way Schwab wants it: a two-decimal string."""
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderDraftError(
            f"Cannot draft an order for {symbol}: the {what} is {value!r}, which is not a price."
        ) from exc
    if not math.isfinite(price) or price <= 0:
        raise OrderDraftError(
            f"Cannot draft an order for {symbol}: the {what} is {price}, but a price has to be "
            f"a positive number of dollars."
        )
    return f"{price:.2f}"


def _leg(symbol: str, instruction: str, quantity: int) -> dict[str, Any]:
    return {
        "instruction": instruction,
        "quantity": quantity,
        "instrument": {"symbol": symbol, "assetType": ASSET_TYPE},
    }


def _parent(symbol: str, shares: int, entry: str, child: dict[str, Any]) -> dict[str, Any]:
    """A buy-limit TRIGGER parent carrying one protective child."""
    return {
        "orderType": "LIMIT",
        "session": SESSION,
        "duration": PARENT_DURATION,
        "orderStrategyType": PARENT_STRATEGY,
        "price": entry,
        "orderLegCollection": [_leg(symbol, "BUY", shares)],
        "childOrderStrategies": [child],
    }


def _stop_child(symbol: str, shares: int, stop: str) -> dict[str, Any]:
    return {
        "orderType": "STOP",
        "session": SESSION,
        "duration": CHILD_DURATION,
        "orderStrategyType": CHILD_STRATEGY,
        "stopPrice": stop,
        "orderLegCollection": [_leg(symbol, "SELL", shares)],
    }


def _stop_limit_child(symbol: str, shares: int, stop: float, stop_str: str) -> dict[str, Any]:
    limit = _money(
        round(stop * (1.0 - STOP_LIMIT_SLIPPAGE), 2), symbol=symbol, what="stop-limit price"
    )
    return {
        "orderType": "STOP_LIMIT",
        "session": SESSION,
        "duration": CHILD_DURATION,
        "orderStrategyType": CHILD_STRATEGY,
        "stopPrice": stop_str,
        "price": limit,
        "orderLegCollection": [_leg(symbol, "SELL", shares)],
    }


def _trailing_child(symbol: str, shares: int, offset: float) -> dict[str, Any]:
    return {
        "orderType": "TRAILING_STOP",
        "session": SESSION,
        "duration": CHILD_DURATION,
        "orderStrategyType": CHILD_STRATEGY,
        "stopPriceLinkBasis": "LAST",
        "stopPriceLinkType": "VALUE",
        "stopPriceOffset": offset,
        "orderLegCollection": [_leg(symbol, "SELL", shares)],
    }


def draft_orders(pick: PickRecord, cfg: Config) -> dict[str, dict[str, Any]]:
    """Build the three Schwab order payloads for one pick.

    Args:
        pick: a sized pick — ``shares`` must be at least 1, so watch-list
            entries are refused rather than silently drafted at zero quantity.
        cfg: the loaded configuration; only ``strategy.chandelier_mult`` is used,
            to set the trailing-stop offset.

    Returns:
        ``{"oto_stop": {...}, "oto_stop_limit": {...}, "trailing_stop": {...}}``
        where each value is a complete TRIGGER order ready for
        ``client.place_order``.

    Raises:
        OrderDraftError: when the pick cannot become a sendable order — zero
            shares, a stop at or above the entry, or a missing ATR.
    """
    symbol = str(pick.symbol).strip().upper()
    if not symbol:
        raise OrderDraftError("Cannot draft an order: the pick has no ticker symbol.")

    shares = int(pick.shares)
    if shares < 1:
        raise OrderDraftError(
            f"Cannot draft an order for {symbol}: the position sized to {shares} shares, so it "
            f"is a watch-list idea rather than a tradable pick."
        )

    entry_price = float(pick.entry)
    stop_price = float(pick.stop)
    entry = _money(entry_price, symbol=symbol, what="entry price")
    stop = _money(stop_price, symbol=symbol, what="stop price")
    if stop_price >= entry_price:
        raise OrderDraftError(
            f"Cannot draft an order for {symbol}: the stop (${stop_price:.2f}) is not below the "
            f"entry (${entry_price:.2f}), so the order would sell the moment it filled."
        )

    atr_value = float(pick.atr)
    if not math.isfinite(atr_value) or atr_value <= 0:
        raise OrderDraftError(
            f"Cannot draft a trailing stop for {symbol}: the ATR is {pick.atr!r}, so there is no "
            f"distance to trail by. Re-run the scan once the price history is complete."
        )
    offset = round(cfg.strategy.chandelier_mult * atr_value, 2)
    if offset <= 0:
        raise OrderDraftError(
            f"Cannot draft a trailing stop for {symbol}: the trailing distance rounds to "
            f"${offset:.2f}. The share price is too low for a {cfg.strategy.chandelier_mult}x "
            f"ATR trail."
        )

    return {
        "oto_stop": _parent(symbol, shares, entry, _stop_child(symbol, shares, stop)),
        "oto_stop_limit": _parent(
            symbol, shares, entry, _stop_limit_child(symbol, shares, stop_price, stop)
        ),
        "trailing_stop": _parent(symbol, shares, entry, _trailing_child(symbol, shares, offset)),
    }


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def _check_price_string(
    value: Any, where: str, field: str, problems: list[str], *, positive: bool = True
) -> None:
    if not isinstance(value, str):
        problems.append(
            f"{where}: {field} must be a string like '45.10' (Schwab rejects numbers here), "
            f"but it is {value!r}."
        )
        return
    if not _PRICE_RE.match(value):
        problems.append(
            f"{where}: {field} must be a price with exactly two decimals, but it is {value!r}."
        )
        return
    if positive and float(value) <= 0:
        problems.append(f"{where}: {field} must be greater than zero, but it is {value!r}.")


def _check_leg(leg: Any, where: str, instruction: str, problems: list[str]) -> int | None:
    """Validate one order leg and return its quantity when it is usable."""
    if not isinstance(leg, dict):
        problems.append(f"{where}: each entry of orderLegCollection must be an object.")
        return None
    if leg.get("instruction") != instruction:
        problems.append(
            f"{where}: the leg instruction must be {instruction!r}, but it is "
            f"{leg.get('instruction')!r}."
        )
    quantity = leg.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        problems.append(
            f"{where}: the leg quantity must be a whole number of shares greater than zero, "
            f"but it is {quantity!r}."
        )
        quantity = None
    instrument = leg.get("instrument")
    if not isinstance(instrument, dict):
        problems.append(f"{where}: the leg is missing its instrument object.")
        return quantity
    if instrument.get("assetType") != ASSET_TYPE:
        problems.append(
            f"{where}: instrument.assetType must be {ASSET_TYPE!r} (this system trades stocks "
            f"and ETFs only), but it is {instrument.get('assetType')!r}."
        )
    symbol = instrument.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        problems.append(f"{where}: instrument.symbol must be a ticker, but it is {symbol!r}.")
    return quantity


def _check_legs(order: Any, where: str, instruction: str, problems: list[str]) -> int | None:
    legs = order.get("orderLegCollection")
    if not isinstance(legs, list) or not legs:
        problems.append(f"{where}: orderLegCollection must be a list holding one leg.")
        return None
    if len(legs) != 1:
        problems.append(
            f"{where}: orderLegCollection holds {len(legs)} legs, but a single-name equity order "
            f"has exactly one."
        )
    return _check_leg(legs[0], where, instruction, problems)


def _check_child(child: Any, where: str, parent_quantity: int | None, problems: list[str]) -> None:
    if not isinstance(child, dict):
        problems.append(f"{where}: the child order strategy must be an object.")
        return
    order_type = child.get("orderType")
    if order_type not in _CHILD_ORDER_TYPES:
        problems.append(
            f"{where}: the protective child orderType must be one of "
            f"{', '.join(_CHILD_ORDER_TYPES)}, but it is {order_type!r}."
        )
    if child.get("session") != SESSION:
        problems.append(
            f"{where}: session must be {SESSION!r}, but it is {child.get('session')!r}."
        )
    if child.get("duration") != CHILD_DURATION:
        problems.append(
            f"{where}: a protective child must be {CHILD_DURATION!r} so it outlives the day it "
            f"was placed, but it is {child.get('duration')!r}."
        )
    if child.get("orderStrategyType") != CHILD_STRATEGY:
        problems.append(
            f"{where}: orderStrategyType must be {CHILD_STRATEGY!r}, but it is "
            f"{child.get('orderStrategyType')!r}."
        )

    quantity = _check_legs(child, where, "SELL", problems)
    if parent_quantity is not None and quantity is not None and quantity != parent_quantity:
        problems.append(
            f"{where}: the child sells {quantity} shares but the parent buys {parent_quantity}. "
            f"An unmatched exit leaves an unprotected position."
        )

    if order_type in ("STOP", "STOP_LIMIT"):
        _check_price_string(child.get("stopPrice"), where, "stopPrice", problems)
    if order_type == "STOP_LIMIT":
        _check_price_string(child.get("price"), where, "price", problems)
    if order_type == "TRAILING_STOP":
        if child.get("stopPriceLinkBasis") != "LAST":
            problems.append(
                f"{where}: stopPriceLinkBasis must be 'LAST', but it is "
                f"{child.get('stopPriceLinkBasis')!r}."
            )
        if child.get("stopPriceLinkType") != "VALUE":
            problems.append(
                f"{where}: stopPriceLinkType must be 'VALUE' (a dollar distance), but it is "
                f"{child.get('stopPriceLinkType')!r}."
            )
        offset = child.get("stopPriceOffset")
        if isinstance(offset, bool) or not isinstance(offset, int | float):
            problems.append(
                f"{where}: stopPriceOffset must be a number of dollars, but it is {offset!r}."
            )
        elif not math.isfinite(float(offset)) or float(offset) <= 0:
            problems.append(
                f"{where}: stopPriceOffset must be greater than zero, but it is {offset!r}."
            )


def validate_one_order(order: Any, where: str = "order") -> list[str]:
    """Structurally check a single TRIGGER order payload; empty list means valid."""
    problems: list[str] = []
    if not isinstance(order, dict):
        return [f"{where}: an order must be a JSON object, but it is a {type(order).__name__}."]

    required = (
        "orderType",
        "session",
        "duration",
        "orderStrategyType",
        "price",
        "orderLegCollection",
        "childOrderStrategies",
    )
    missing = [key for key in required if key not in order]
    if missing:
        problems.append(f"{where}: the order is missing {', '.join(missing)}.")

    if order.get("orderType") != "LIMIT":
        problems.append(
            f"{where}: the parent orderType must be 'LIMIT' — this system never sends market "
            f"orders — but it is {order.get('orderType')!r}."
        )
    if order.get("session") != SESSION:
        problems.append(
            f"{where}: session must be {SESSION!r}, but it is {order.get('session')!r}."
        )
    if order.get("duration") != PARENT_DURATION:
        problems.append(
            f"{where}: the parent duration must be {PARENT_DURATION!r} so a stale entry expires "
            f"tonight, but it is {order.get('duration')!r}."
        )
    if order.get("orderStrategyType") != PARENT_STRATEGY:
        problems.append(
            f"{where}: orderStrategyType must be {PARENT_STRATEGY!r} so the stop is attached to "
            f"the fill, but it is {order.get('orderStrategyType')!r}."
        )
    _check_price_string(order.get("price"), where, "price", problems)
    quantity = _check_legs(order, where, "BUY", problems)

    children = order.get("childOrderStrategies")
    if not isinstance(children, list) or not children:
        problems.append(
            f"{where}: childOrderStrategies must hold the protective exit. An entry without an "
            f"attached stop is exactly what this system exists to prevent."
        )
    else:
        if len(children) != 1:
            problems.append(
                f"{where}: childOrderStrategies holds {len(children)} children, but each draft "
                f"attaches exactly one exit."
            )
        for position, child in enumerate(children):
            _check_child(child, f"{where}.child[{position}]", quantity, problems)

    return problems


def validate_order_draft(draft: Any) -> list[str]:
    """Check a draft — the three-payload dict or a single order — structurally.

    Args:
        draft: what :func:`draft_orders` returned, or one order payload out of it.

    Returns:
        A list of plain-English problems. An empty list means the payload
        matches the documented Schwab TRIGGER/TRAILING_STOP shape.
    """
    if isinstance(draft, dict) and "orderStrategyType" in draft:
        return validate_one_order(draft)

    if not isinstance(draft, dict):
        return [
            f"An order draft must be a JSON object holding {', '.join(DRAFT_KINDS)}, but it is a "
            f"{type(draft).__name__}."
        ]

    problems: list[str] = []
    missing = [kind for kind in DRAFT_KINDS if kind not in draft]
    if missing:
        problems.append(f"The draft is missing the {', '.join(missing)} payload(s).")
    unexpected = sorted(set(draft) - set(DRAFT_KINDS))
    if unexpected:
        problems.append(
            f"The draft holds unexpected payload(s) {', '.join(unexpected)}; the only valid keys "
            f"are {', '.join(DRAFT_KINDS)}."
        )
    for kind in DRAFT_KINDS:
        if kind in draft:
            problems.extend(validate_one_order(draft[kind], kind))
    return problems
