"""Guarded order execution — ``swing execute``, ``swing positions``, ``swing kill``.

The safe default is doing nothing. Running ``swing execute`` with no flags is a
**dry run**: it finds the latest scan report, prints the orders it *would* send
and the verdict of every guardrail, touches no network at all, needs no
schwab-py, and exits 0. Sending real money requires two switches thrown
independently — the ``--live`` flag on the command line *and*
``execution.enabled = true`` in config.toml — because one of them can be thrown
by a typo and two cannot.

Once live, the sequence is: build a client, read the real account, bring the
journal back in step with the broker's fills, fetch real quotes, run every
guardrail from :mod:`swing.broker.guardrails`, and place nothing at all unless
every single one passes. There is no partial mode where some orders go and
others are held back by a failing check: if the picture is wrong anywhere, the
whole run stops.

Every placement is journalled *before* it is sent, as a ``pending`` row, and
flipped to ``open`` with the broker's order id afterwards. That ordering is the
point: a crash, a full disk or a non-JSON 201 can leave a live order at Schwab,
and the one thing this program must never do is tell the operator an order was
not sent when it might have been.

The kill switch is deliberately cruder than everything else. It is a file, so
it can be engaged by this code, by a shell, or by a panicking human with a text
editor. It is checked before anything else happens *and* again immediately
before each individual order goes out, because the run pauses for confirmation
in between and a kill switch that only counts at startup is decoration.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swing import state as _state
from swing.broker import auth as _auth
from swing.broker import guardrails as _g
from swing.broker.guardrails import _as_float
from swing.state import Journal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "BrokerError",
    "FillSync",
    "PlannedOrder",
    "ScanBundle",
    "kill",
    "latest_scan_dir",
    "load_scan",
    "plan_orders",
    "print_positions",
    "run_execute",
    "sync_broker_orders",
]

log = logging.getLogger(__name__)

#: Which drafted variant to send when ``orders/<SYMBOL>.json`` holds several.
ORDER_VARIANT_PREFERENCE = ("oto_stop", "oto_stop_limit", "trailing_stop")

#: Order states that mean an order is no longer working at the broker.
_CLOSED_ORDER_STATES = frozenset(
    {"filled", "cancelled", "canceled", "rejected", "expired", "closed", "replaced"}
)

#: Schwab order statuses that end an order's life, mapped to the journal's own
#: vocabulary. Anything not in here (WORKING, QUEUED, PENDING_ACTIVATION,
#: ACCEPTED...) means the order is still live and the journal row stays as it is.
TERMINAL_BROKER_STATUSES: dict[str, str] = {
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "CANCELLED": "cancelled",
    "REJECTED": "rejected",
    "EXPIRED": "expired",
    "REPLACED": "replaced",
}

#: Broker position rows we care about. Cash sweeps (``MMDA1``, ``SWVXX``) come
#: back as CASH_EQUIVALENT and are not holdings; leaving them in made
#: reconciliation refuse on every funded account, forever (audit BUG-007).
TRADED_ASSET_TYPES = frozenset({"EQUITY", "ETF", "COLLECTIVE_INVESTMENT"})

_SCAN_DIR_PREFIX = "scan-"


class BrokerError(RuntimeError):
    """Raised when Schwab answers with something we cannot use.

    Like :class:`swing.broker.auth.AuthError`, the message is one printable
    sentence naming the problem and the fix.
    """


# --------------------------------------------------------------------------
# reading the scan report (FROZEN CONTRACTS 9 and 10, consumed as file formats)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanBundle:
    """One ``scan-YYYY-MM-DD/`` directory, loaded."""

    path: Path
    scan_date: _dt.date
    payload: dict[str, Any]
    picks: list[dict[str, Any]]
    orders: dict[str, dict[str, Any]]
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PlannedOrder:
    """A drafted order paired with the pick that produced it."""

    symbol: str
    variant: str
    quantity: int
    limit_price: float
    stop_price: float | None
    notional: float
    pick: dict[str, Any]
    order: dict[str, Any]


def _scan_date_of(path: Path) -> _dt.date | None:
    if not path.name.startswith(_SCAN_DIR_PREFIX):
        return None
    try:
        return _dt.date.fromisoformat(path.name[len(_SCAN_DIR_PREFIX) :])
    except ValueError:
        return None


def latest_scan_dir(cfg: Config) -> Path | None:
    """Return the most recent ``scan-YYYY-MM-DD`` directory, or ``None``.

    The resolution itself lives in :func:`swing.reports.latest_scan_dir`, shared
    with the alerts pipeline so the two can never disagree about which report is
    current (audit DEBT-001). This wrapper only supplies the configured
    directory — and asks for ``require_picks=False`` on purpose: the executor
    must act on the *newest* parseable scan whatever it contains. Skipping an
    empty report in favour of an older populated one would quietly re-execute
    yesterday's picks at today's prices, and ``stale_scan`` allows exactly that
    one-day gap. A report whose ``picks.json`` does not parse is invisible to
    both callers, so a half-written directory reads as "no scan yet" rather than
    as something to execute.
    """
    from swing.reports import latest_scan_dir as _latest

    return _latest(Path(cfg.paths.reports_dir).expanduser(), require_picks=False)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrokerError(
            f"{path} could not be read as JSON ({exc}), so this scan cannot be executed: re-run "
            f"`swing scan` to rebuild the report."
        ) from exc


def _resolve_scan_date(path: Path, payload: Mapping[str, Any]) -> _dt.date:
    """Agree the report's date between its directory name and its body, or refuse.

    The directory name is the authority — it is what chose this report and what
    the retention policy sorts on — and ``picks.json``'s ``asof`` is
    corroboration. They must match. Letting the body win meant one edited line
    in a text file could make a three-week-old report look like tonight's and
    walk straight past ``stale_scan``, while a malformed ``asof`` reverted to
    the directory silently, with nothing said (audit BUG-026).

    Raises:
        BrokerError: when the two disagree, when ``asof`` is unparseable, or
            when neither is available.
    """
    from_dir = _scan_date_of(path)
    raw = payload.get("asof")
    from_body: _dt.date | None = None
    if raw is not None:
        if not isinstance(raw, str):
            raise BrokerError(
                f"The scan report in {path} records `asof` as {raw!r}, which is not a date, so "
                f"its age cannot be judged and nothing will be executed: re-run `swing scan` to "
                f"rebuild the report."
            )
        try:
            from_body = _dt.date.fromisoformat(raw[:10])
        except ValueError as exc:
            raise BrokerError(
                f"The scan report in {path} records `asof` as {raw!r}, which is not a date in "
                f"YYYY-MM-DD form, so its age cannot be judged and nothing will be executed: "
                f"re-run `swing scan` to rebuild the report."
            ) from exc

    if from_dir is not None and from_body is not None and from_dir != from_body:
        raise BrokerError(
            f"The scan directory {path.name} says {from_dir.isoformat()} but the `asof` inside "
            f"its picks.json says {from_body.isoformat()}, and a report whose own two dates "
            f"disagree cannot be trusted to be fresh: re-run `swing scan` to rebuild it, or "
            f"delete the directory if you edited it by hand."
        )
    resolved = from_dir or from_body
    if resolved is None:
        raise BrokerError(
            f"The scan directory {path} has no date in its name and picks.json has no `asof`, "
            f"so its age cannot be judged: re-run `swing scan`."
        )
    return resolved


def load_scan(path: Path) -> ScanBundle:
    """Load ``picks.json`` and every ``orders/<SYMBOL>.json`` from a scan directory.

    Raises:
        BrokerError: when ``picks.json`` is missing or unreadable, or when the
            directory's date and the report's own ``asof`` disagree.
    """
    picks_file = path / "picks.json"
    if not picks_file.is_file():
        raise BrokerError(
            f"There is no picks.json in {path}, so there is nothing to execute: re-run "
            f"`swing scan` to rebuild the report."
        )
    payload = _read_json(picks_file)
    if not isinstance(payload, dict):
        raise BrokerError(
            f"{picks_file} does not contain a JSON object, so this scan cannot be executed: "
            f"re-run `swing scan` to rebuild the report."
        )

    scan_date = _resolve_scan_date(path, payload)

    problems: list[str] = []
    picks: list[dict[str, Any]] = []
    for raw in payload.get("picks", []) or []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        status = str(raw.get("status", "drafted")).lower()
        if status in {"invalidated", "closed", "ordered", "filled"}:
            problems.append(f"{symbol} is marked {status} in the scan report, so it is skipped.")
            continue
        picks.append({**raw, "symbol": symbol})

    orders: dict[str, dict[str, Any]] = {}
    orders_dir = path / "orders"
    if orders_dir.is_dir():
        for order_file in sorted(orders_dir.glob("*.json")):
            body = _read_json(order_file)
            if isinstance(body, dict):
                orders[order_file.stem.strip().upper()] = body
            else:
                problems.append(f"{order_file} is not a JSON object, so it is ignored.")

    return ScanBundle(
        path=path,
        scan_date=scan_date,
        payload=payload,
        picks=picks,
        orders=orders,
        problems=problems,
    )


def _select_variant(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pick which drafted order to send.

    ``orders/<SYMBOL>.json`` may hold a single Schwab order, or the whole
    ``draft_orders()`` dictionary of variants (Contract 10). Both are accepted;
    when there is a choice, the plain stop child wins over the stop-limit and
    trailing variants because it is the one that always fills.
    """
    if "orderType" in payload or "orderLegCollection" in payload:
        return "order", dict(payload)
    for name in ORDER_VARIANT_PREFERENCE:
        candidate = payload.get(name)
        if isinstance(candidate, dict):
            return name, dict(candidate)
    for name, candidate in payload.items():
        if isinstance(candidate, dict) and "orderType" in candidate:
            return str(name), dict(candidate)
    raise BrokerError(
        "A drafted order file holds no recognisable Schwab order (no orderType and none of "
        f"{', '.join(ORDER_VARIANT_PREFERENCE)}): re-run `swing scan` to redraft it."
    )


def _price_key(value: Any) -> str | None:
    """A price as Schwab writes it — two decimals — or ``None`` if it is not one.

    Drafted orders carry prices as strings (``"100.00"``) because that is what
    the Schwab API wants; picks carry them as floats. Comparing them as text at
    cent resolution is what makes the payload/plan check exact without tripping
    over ``100.0`` versus ``"100.00"``.
    """
    number = _as_float(value)
    return None if number is None else f"{number:.2f}"


def _leg_symbol(leg: Mapping[str, Any]) -> str:
    instrument = leg.get("instrument")
    if isinstance(instrument, Mapping):
        return str(instrument.get("symbol", "")).strip().upper()
    return ""


def _quantity_of(leg: Mapping[str, Any]) -> int | None:
    """The leg's share count, only when it is a genuine whole number."""
    value = leg.get("quantity")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    number = _as_float(value)
    if number is None or number != int(number):
        return None
    return int(number)


def _payload_disagreements(
    symbol: str, pick: Mapping[str, Any], order: Mapping[str, Any]
) -> list[str]:
    """Everything about the wire payload that the approved plan does not say.

    The guardrails validate a *model* of the order — symbol, side, size, price —
    built from the pick. What actually goes on the wire is the JSON file. If
    those two ever drift apart (a stale directory, a half-written file, a hand
    edit) the eleven checks upstream will have blessed a trade nobody looked at,
    and the old ``or``-chained fallbacks made that invisible: a payload buying
    zero shares displayed, prompted and journalled as the pick's 50 (BUG-009).

    Returns:
        One plain-English sentence per disagreement; empty means the payload
        encodes exactly the trade the plan describes.
    """
    problems: list[str] = []
    leg = _first_leg(order)
    if not leg:
        return [
            f"{symbol}: the drafted order has no order leg, so there is nothing to check it "
            f"against: re-run `swing scan` to redraft it."
        ]

    wire_symbol = _leg_symbol(leg)
    if wire_symbol != symbol:
        problems.append(
            f"{symbol}: the drafted order buys {wire_symbol or 'an unnamed instrument'}, not "
            f"{symbol}, so it will not be sent: re-run `swing scan` to redraft it."
        )

    instruction = str(leg.get("instruction", "")).strip().upper()
    if instruction != "BUY":
        problems.append(
            f"{symbol}: the drafted order's instruction is {instruction or 'missing'}, but this "
            f"system only ever opens long positions with BUY: re-run `swing scan` to redraft it."
        )

    wire_quantity = _quantity_of(leg)
    pick_quantity = _quantity_of({"quantity": pick.get("shares")})
    if wire_quantity is None or pick_quantity is None or wire_quantity != pick_quantity:
        problems.append(
            f"{symbol}: the drafted order buys {leg.get('quantity')!r} shares but the scan sized "
            f"the position at {pick.get('shares')!r}, so it will not be sent: re-run `swing scan` "
            f"to redraft it."
        )

    wire_price = _price_key(order.get("price"))
    pick_price = _price_key(pick.get("entry"))
    if wire_price is None or pick_price is None or wire_price != pick_price:
        problems.append(
            f"{symbol}: the drafted order's limit price is {order.get('price')!r} but the scan "
            f"planned an entry at {pick.get('entry')!r}, so it will not be sent: re-run "
            f"`swing scan` to redraft it."
        )

    kind, wire_stop = _child_protection(order)
    if kind == "none":
        problems.append(
            f"{symbol}: the drafted order has no protective stop child, so a fill would sit there "
            f"with nothing limiting the loss, and it will not be sent: re-run `swing scan` to "
            f"redraft it."
        )
    elif kind == "stop":
        pick_stop = _price_key(pick.get("stop"))
        if pick_stop is None or _price_key(wire_stop) != pick_stop:
            problems.append(
                f"{symbol}: the drafted order's stop is {wire_stop!r} but the scan sized the "
                f"position against a stop of {pick.get('stop')!r}, so it will not be sent: the "
                f"risk on the wire is not the risk that was approved — re-run `swing scan` to "
                f"redraft it."
            )
    return problems


def _first_leg(order: Mapping[str, Any]) -> Mapping[str, Any]:
    legs = order.get("orderLegCollection")
    if isinstance(legs, list) and legs and isinstance(legs[0], Mapping):
        return legs[0]
    return {}


def _child_protection(order: Mapping[str, Any]) -> tuple[str, float | None]:
    """What the drafted order's protective child actually promises.

    Three answers, because the drafts come in three shapes (Contract 10):

    * ``("stop", price)`` — a STOP or STOP_LIMIT child with an absolute
      ``stopPrice``. That price is checkable against the pick, and is.
    * ``("trailing", None)`` — a TRAILING_STOP child, which carries a
      ``stopPriceOffset`` from the last price and no absolute level at all.
      There is nothing to compare it with, so nothing is claimed about it: the
      plan's ``stop_price`` stays ``None`` and the table prints "-" rather than
      a number the order does not encode.
    * ``("none", None)`` — no protective child. An entry with nothing behind it
      is never sent.
    """
    children = order.get("childOrderStrategies")
    if not isinstance(children, list):
        return ("none", None)
    trailing = False
    for child in children:
        if not isinstance(child, Mapping):
            continue
        stop = _as_float(child.get("stopPrice"))
        if stop is not None:
            return ("stop", stop)
        if _as_float(child.get("stopPriceOffset")) is not None:
            trailing = True
    return ("trailing", None) if trailing else ("none", None)


def _journal_terminal_status(
    journal: Journal | None, symbol: str, scan_date: _dt.date
) -> str | None:
    """The journal's terminal verdict on this pick, if it has one.

    ``swing confirm`` writes ``invalidated`` into the journal, and a placement
    writes ``ordered``; the report on disk may still say ``drafted`` because it
    was written before either happened. The journal is the authority on what has
    already been decided, so a rerun cannot resurrect a pick the morning check
    threw out (frozen contract A7).
    """
    if journal is None:
        return None
    stamp = scan_date.isoformat()
    for pick in journal.picks:
        if pick.symbol.strip().upper() != symbol or str(pick.date)[:10] != stamp:
            continue
        status = pick.status.strip().lower()
        if status in {"invalidated", "ordered", "filled", "closed"}:
            return status
    return None


def plan_orders(
    bundle: ScanBundle, *, journal: Journal | None = None
) -> tuple[list[PlannedOrder], list[str]]:
    """Pair every actionable pick with its drafted order.

    Args:
        bundle: the loaded scan directory.
        journal: the local journal, when one is available. Picks it has already
            marked terminal are dropped even if the report still calls them
            drafted (frozen contract A7).

    Returns:
        ``(plans, problems)`` — picks with no usable order file, a payload that
        does not match the plan, or a symbol already planned in this same run
        are reported in ``problems`` rather than silently dropped or sent.
    """
    plans: list[PlannedOrder] = []
    problems = list(bundle.problems)
    seen: set[str] = set()
    for pick in bundle.picks:
        symbol = str(pick["symbol"]).upper()
        if symbol in seen:
            # Two rows for one ticker would place two identical live orders: the
            # journal-based `duplicate` guardrail runs before either is
            # journalled, so it cannot see the first one (audit BUG-025).
            problems.append(
                f"{symbol} appears more than once in this scan report, and only the first entry "
                f"is used: re-run `swing scan` to rebuild it, and check the report before "
                f"executing."
            )
            continue
        seen.add(symbol)

        decided = _journal_terminal_status(journal, symbol, bundle.scan_date)
        if decided is not None:
            problems.append(
                f"{symbol} is already {decided} in the journal, so it is skipped: the journal is "
                f"the record of what has actually been decided, whatever the report still says."
            )
            continue

        payload = bundle.orders.get(symbol)
        if payload is None:
            problems.append(
                f"{symbol} was picked but {bundle.path / 'orders' / (symbol + '.json')} does not "
                f"exist, so it is skipped: re-run `swing scan` to redraft it."
            )
            continue
        try:
            variant, order = _select_variant(payload)
        except BrokerError as exc:
            problems.append(f"{symbol}: {exc}")
            continue

        mismatches = _payload_disagreements(symbol, pick, order)
        if mismatches:
            problems.extend(mismatches)
            continue

        quantity = _quantity_of(_first_leg(order)) or 0
        limit = _as_float(order.get("price")) or 0.0
        # Whatever the order itself encodes, and nothing else. Falling back to
        # the pick's stop here let the confirmation prompt and the journal show
        # a protective level the wire payload did not carry (audit BUG-009).
        _kind, stop = _child_protection(order)
        if quantity <= 0:
            problems.append(
                f"{symbol} has a quantity of {quantity}, so there is nothing to buy: the account "
                f"is probably too small for this price — see the watch list instead."
            )
            continue
        plans.append(
            PlannedOrder(
                symbol=symbol,
                variant=variant,
                quantity=quantity,
                limit_price=limit,
                stop_price=stop,
                notional=quantity * limit,
                pick=dict(pick),
                order=order,
            )
        )
    return plans, problems


# --------------------------------------------------------------------------
# talking to Schwab
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountSnapshot:
    """What the broker says about the account right now."""

    hash_value: str
    masked_number: str
    equity: float | None
    positions: list[dict[str, Any]]

    @property
    def symbols(self) -> list[str]:
        return [str(p.get("symbol", "")).upper() for p in self.positions]


def _body(response: Any) -> Any:
    """Return the JSON body of a schwab-py response (or a plain object in tests)."""
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise BrokerError(
            f"Schwab answered with HTTP {status}. If that is a 401 the token has expired — run "
            f"`swing auth` to log in again."
        )
    getter = getattr(response, "json", None)
    if callable(getter):
        return getter()
    return response


def _account_block(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    inner = payload.get("securitiesAccount")
    if isinstance(inner, Mapping):
        return dict(inner)
    return dict(payload)


def _extract_equity(payload: Any) -> float | None:
    """Find the account's liquidation value in a Schwab account payload."""
    account = _account_block(payload)
    blocks = [
        account.get("currentBalances"),
        account.get("aggregatedBalance"),
        payload.get("aggregatedBalance") if isinstance(payload, Mapping) else None,
        account.get("initialBalances"),
        account.get("projectedBalances"),
    ]
    keys = (
        "liquidationValue",
        "currentLiquidationValue",
        "equity",
        "accountValue",
        "totalValue",
    )
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        for key in keys:
            value = _as_float(block.get(key))
            if value is not None:
                return value
    return None


def _extract_positions(payload: Any) -> list[dict[str, Any]]:
    """The account's real holdings, cash sweeps and closed lines removed.

    Schwab reports the settlement fund (``MMDA1``, ``SWVXX``) as a position like
    any other, and keeps zero-quantity rows around after a close. Both used to
    reach ``reconciliation``, where they could never match a journal that only
    knows about stock — so every live run on a funded account refused
    (audit BUG-007). A row whose ``assetType`` is missing entirely is *kept*:
    an unrecognised holding should make the reconciliation stop and be looked
    at, never disappear.
    """
    account = _account_block(payload)
    rows = account.get("positions")
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        instrument = row.get("instrument")
        symbol = ""
        asset_type = ""
        if isinstance(instrument, Mapping):
            symbol = str(instrument.get("symbol", "")).strip().upper()
            asset_type = str(instrument.get("assetType", "")).strip().upper()
        if not symbol:
            continue
        if asset_type and asset_type not in TRADED_ASSET_TYPES:
            log.debug(
                "Ignoring the %s position in %s: it is not a traded holding.", asset_type, symbol
            )
            continue
        quantity = (_as_float(row.get("longQuantity")) or 0.0) - (
            _as_float(row.get("shortQuantity")) or 0.0
        )
        if abs(quantity) < 1e-9:
            continue
        out.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "market_value": _as_float(row.get("marketValue")) or 0.0,
            }
        )
    return out


def fetch_account(cfg: Config, client: Any) -> AccountSnapshot:
    """Read the configured account's hash, equity and positions from Schwab."""
    try:
        masked, hash_value = _auth.account_hash(cfg, client)
    except _auth.AuthError as exc:
        raise BrokerError(str(exc)) from exc
    if not hash_value:
        raise BrokerError(
            "Schwab returned an account without a hash value, so orders cannot be addressed to "
            "it: run `swing auth --check` and check the account list."
        )
    payload = _body(client.get_account(hash_value, fields="positions"))
    return AccountSnapshot(
        hash_value=hash_value,
        masked_number=masked,
        equity=_extract_equity(payload),
        positions=_extract_positions(payload),
    )


@dataclass(frozen=True)
class FillSync:
    """What one pass of :func:`sync_broker_orders` found and changed."""

    filled: tuple[str, ...] = ()
    closed: tuple[str, ...] = ()
    working_symbols: tuple[str, ...] = ()

    @property
    def changed(self) -> int:
        return len(self.filled) + len(self.closed)

    def describe(self) -> str:
        """One line for the operator, or an empty string when nothing moved."""
        parts: list[str] = []
        if self.filled:
            parts.append(f"filled: {', '.join(self.filled)}")
        if self.closed:
            parts.append(f"no longer working: {', '.join(self.closed)}")
        return "; ".join(parts)


def _order_rows(payload: Any) -> list[Mapping[str, Any]]:
    """The list of orders in a Schwab ``get_orders_for_account`` response."""
    rows: Any = payload
    if isinstance(payload, Mapping):
        rows = payload.get("orders", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _broker_order_id(row: Mapping[str, Any]) -> str:
    value = row.get("orderId", row.get("order_id"))
    return "" if value in (None, "") else str(value)


def fetch_broker_orders(
    cfg: Config, client: Any, *, account_hash: str = ""
) -> list[Mapping[str, Any]]:
    """Read the account's recent orders from Schwab.

    Raises:
        BrokerError: when the account cannot be identified. Transport failures
            are the caller's to handle — they mean "we do not know what is
            working", which is never something to shrug off in a live run.
    """
    if not account_hash:
        try:
            _masked, account_hash = _auth.account_hash(cfg, client)
        except _auth.AuthError as exc:
            raise BrokerError(str(exc)) from exc
    return _order_rows(_body(client.get_orders_for_account(account_hash)))


def sync_broker_orders(journal: Journal, rows: Sequence[Mapping[str, Any]]) -> FillSync:
    """Move journal orders (and their picks) to where the broker says they are.

    This is the half of the order state machine that was missing. Nothing in the
    system ever wrote ``filled``, so ``Journal.positions()`` was structurally
    empty, ``swing positions`` always printed "(none)", and reconciliation could
    never pass on an account that actually held something (audit BUG-007). It
    also un-blinds the duplicate guardrail after a cancel-in-the-app.

    Orders are matched by the broker's order id, and the journal row is found by
    the correlation id it was recorded under, so two orders for one symbol never
    tread on each other. A row the broker does not mention is left alone: "not
    in this window" is not the same as "gone".

    Args:
        journal: the journal to bring up to date, mutated in place.
        rows: order records straight from Schwab.

    Returns:
        A :class:`FillSync` describing what moved, plus the symbols the broker
        still shows as working — which reconciliation compares against.
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    working: set[str] = set()
    for row in rows:
        status = str(row.get("status", "")).strip().upper()
        order_id = _broker_order_id(row)
        if order_id:
            by_id[order_id] = row
        if status.lower() not in _CLOSED_ORDER_STATES:
            symbol = _order_symbol(row)
            if symbol:
                working.add(symbol)

    filled: list[str] = []
    closed: list[str] = []
    pick_changes: list[tuple[str, str, str]] = []
    for order in journal.orders:
        status = str(order.get("status", _state.OPEN_ORDER_STATUS)).strip().lower()
        if status not in _g.WORKING_ORDER_STATUSES:
            continue
        order_id = _broker_order_id(order)
        row = by_id.get(order_id) if order_id else None
        if row is None:
            continue
        new_status = TERMINAL_BROKER_STATUSES.get(str(row.get("status", "")).strip().upper())
        if new_status is None:
            continue
        symbol = str(order.get("symbol", "")).strip().upper()
        _move_order(journal, order, new_status)
        if new_status == "filled":
            filled.append(symbol)
            day = str(order.get("scan_date") or order.get("date") or "")[:10]
            if day:
                pick_changes.append((symbol, day, "filled"))
        else:
            closed.append(f"{symbol} ({new_status})")

    if pick_changes:
        journal.update_statuses(pick_changes)
    return FillSync(
        filled=tuple(sorted(filled)),
        closed=tuple(sorted(closed)),
        working_symbols=tuple(sorted(working)),
    )


def _move_order(journal: Journal, order: Mapping[str, Any], new_status: str) -> None:
    """Write ``new_status`` onto exactly this order row."""
    client_ref = order.get("client_ref")
    if isinstance(client_ref, str) and client_ref.strip():
        journal.annotate_order(client_ref, status=new_status)
        return
    # Orders recorded before correlation ids existed can only be found by
    # symbol; the status filter keeps it from touching anything already settled.
    symbol = str(order.get("symbol", "")).strip().upper()
    current = str(order.get("status", _state.OPEN_ORDER_STATUS)).strip().lower()
    if symbol:
        journal.update_order_status(symbol, new_status, only_status=current)


def _quotes_from_provider(cfg: Config, symbols: Sequence[str]) -> dict[str, float]:
    from swing.data import get_provider

    quotes = get_provider(cfg).latest_quotes(list(symbols))
    out: dict[str, float] = {}
    for symbol, quote in (quotes or {}).items():
        price = _as_float(getattr(quote, "price", None))
        if price is not None:
            out[str(symbol).upper()] = price
    return out


def fetch_quotes(
    cfg: Config,
    symbols: Sequence[str],
    *,
    client: Any = None,
    problems: list[str] | None = None,
) -> dict[str, float]:
    """Best-effort current prices: the configured provider first, Schwab second.

    Never raises. An empty dict means "no quote was available", which the
    ``quote_drift`` guardrail turns into a refusal when live and a SKIP in a
    dry run.

    Args:
        cfg: the loaded configuration.
        symbols: tickers to price.
        client: a broker client to fall back on, when there is one.
        problems: an optional list the reasons are appended to. Quote failures
            used to be logged at INFO and vanish, leaving ``quote_drift`` to
            say "fix the data provider" without ever saying what it said
            (audit DEBT-004); handing the text back lets the refusal quote it.
    """
    wanted = [s.upper() for s in symbols]
    if not wanted:
        return {}
    try:
        prices = _quotes_from_provider(cfg, wanted)
    except Exception as exc:
        log.warning("Quotes from the configured data provider were unavailable: %s", exc)
        if problems is not None:
            problems.append(f"the {cfg.data.provider} provider said {exc}")
        prices = {}
    missing = [s for s in wanted if s not in prices]
    if missing and client is not None:
        try:
            payload = _body(client.get_quotes(missing))
            for symbol in missing:
                price = _auth.quote_price(payload, symbol)
                if price is not None:
                    prices[symbol] = price
        except Exception as exc:
            log.warning("Quotes from Schwab were unavailable: %s", exc)
            if problems is not None:
                problems.append(f"Schwab said {exc}")
    return prices


def _order_id_of(response: Any) -> str | None:
    """Dig the new order's id out of a ``place_order`` response."""
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if callable(getter):
        location = getter("Location") or getter("location")
        if isinstance(location, str) and location:
            tail = location.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                return tail
    body = getattr(response, "json", None)
    payload = body() if callable(body) else response
    if isinstance(payload, Mapping):
        for key in ("orderId", "order_id", "id"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


# --------------------------------------------------------------------------
# printing
# --------------------------------------------------------------------------


def _print_order_table(plans: Sequence[PlannedOrder]) -> None:
    if not plans:
        print("  (no orders drafted)")
        return
    print(f"  {'SYMBOL':<8}{'QTY':>6}{'LIMIT':>11}{'STOP':>11}{'NOTIONAL':>13}  DRAFT")
    for plan in plans:
        stop = f"{plan.stop_price:.2f}" if plan.stop_price is not None else "-"
        print(
            f"  {plan.symbol:<8}{plan.quantity:>6}{plan.limit_price:>11.2f}{stop:>11}"
            f"{plan.notional:>13,.2f}  {plan.variant}"
        )
    total = sum(p.notional for p in plans)
    print(f"  {'total':<8}{'':>6}{'':>11}{'':>11}{total:>13,.2f}")


def _print_results(title: str, results: Sequence[_g.GuardrailResult]) -> None:
    print(f"{title}:")
    for result in results:
        print(f"  {result.verdict:<5} {result.name:<16} {result.reason}")


def _print_positions_table(rows: Sequence[Mapping[str, Any]], *, quantity_key: str) -> None:
    if not rows:
        print("  (none)")
        return
    for row in rows:
        quantity = _as_float(row.get(quantity_key)) or 0.0
        extra = ""
        entry = _as_float(row.get("entry"))
        if entry is not None:
            extra = f"  entry {entry:.2f}"
        value = _as_float(row.get("market_value"))
        if value is not None:
            extra = f"  value {value:,.2f}"
        print(f"  {str(row.get('symbol', '?')):<8}{quantity:>10.0f}{extra}")


# --------------------------------------------------------------------------
# `swing execute`
# --------------------------------------------------------------------------


def _now_eastern() -> _dt.datetime:
    from zoneinfo import ZoneInfo

    return _dt.datetime.now(ZoneInfo(_g.MARKET_TZ))


def _confirm(question: str) -> bool:
    """Ask the human. Anything that is not an explicit yes means no."""
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _committed_today(journal: Journal, today: _dt.date) -> float:
    total = 0.0
    for order in _g.orders_placed_on(journal, today):
        total += _as_float(order.get("notional")) or 0.0
    return total


def run_execute(
    cfg: Config,
    *,
    live: bool = False,
    assume_yes: bool = False,
    now: _dt.datetime | None = None,
) -> None:
    """Execute the latest scan — dry run by default (FROZEN CONTRACT 2).

    Args:
        cfg: the loaded configuration.
        live: send real orders. Also requires ``execution.enabled`` in config.
        assume_yes: skip the per-order confirmation prompt.
        now: override the clock. A test seam — the CLI never passes it. It must
            carry a timezone: ``auth`` reads a naive datetime as local time and
            ``guardrails`` reads it as Eastern, so a naive value near midnight
            makes "today" mean two different days in one run (audit DEBT-008).

    Raises:
        SystemExit: 2 when the two live switches disagree, the clock is
            ambiguous, or the broker cannot be reached; 1 when a guardrail
            refuses. A dry run never raises.
    """
    # One clock for the whole run. The guardrails that can change while a human
    # is thinking are re-read from it before each placement; everything else
    # uses the single Eastern reading below, so "today" cannot mean two
    # different days in one run (audit DEBT-008).
    clock: Callable[[], _dt.datetime] = _now_eastern
    if now is not None:
        if now.tzinfo is None:
            print(
                "The clock passed to `swing execute` has no timezone, so 'today' would mean one "
                "day to the token check and another to the market-hours check. Nothing was sent: "
                "pass a timezone-aware datetime (the CLI always does)."
            )
            raise SystemExit(2)
        frozen = now

        def clock() -> _dt.datetime:
            return frozen

    moment = _g.to_eastern(clock())
    today = moment.date()

    if live and not cfg.execution.enabled:
        print(
            "`swing execute --live` needs both switches on: the --live flag is set but "
            "execution.enabled is false in your config.toml, so nothing was sent. Set "
            "execution.enabled = true in the [execution] section once you have watched enough "
            "dry runs to trust it."
        )
        raise SystemExit(2)

    scan_dir = latest_scan_dir(cfg)
    if scan_dir is None:
        print(
            f"No scan report was found in {Path(cfg.paths.reports_dir).expanduser()}, so there "
            f"is nothing to execute: run `swing scan` first."
        )
        if live:
            raise SystemExit(1)
        return

    try:
        bundle = load_scan(scan_dir)
    except BrokerError as exc:
        # A dry run reports problems; it never becomes a failure itself.
        print(str(exc))
        if live:
            raise SystemExit(1) from exc
        return

    journal = Journal.load(cfg)
    if journal.recovered:
        print(
            "WARNING: the journal could not be read and a fresh, empty one is in use, so swing "
            "believes it holds no positions and has sent no orders today. Check the Schwab app "
            "before letting anything go out."
        )

    mode = "LIVE" if live else "DRY RUN"
    print(f"swing execute — {mode}")
    print(f"Scan report : {bundle.path} (asof {bundle.scan_date.isoformat()})")
    print(f"Clock       : {moment:%Y-%m-%d %H:%M %Z}")

    client: Any = None
    try:
        snapshot: AccountSnapshot | None = None
        working_orders: list[str] | None = None
        if live:
            try:
                client = _auth.get_client(cfg)
            except _auth.AuthError as exc:
                print(f"{exc} Nothing was sent.")
                raise SystemExit(2) from exc
            try:
                snapshot = fetch_account(cfg, client)
            except BrokerError as exc:
                print(f"{exc} Nothing was sent.")
                raise SystemExit(2) from exc
            except Exception as exc:
                print(
                    f"Schwab could not be reached to read the account ({exc}), so nothing "
                    f"was sent: check your connection and `swing auth --check`."
                )
                raise SystemExit(2) from exc
            print(f"Account     : {snapshot.masked_number}, equity {snapshot.equity or 0:,.2f}")

            # Before anything is judged, find out what happened to the orders from
            # last time. Without this the journal never learns a fill, so it holds
            # no positions, and reconciliation refuses every run (audit BUG-007).
            try:
                rows = fetch_broker_orders(cfg, client, account_hash=snapshot.hash_value)
            except Exception as exc:
                print(
                    f"Schwab could not be asked which orders are working ({exc}), so there "
                    f"is no way to tell what last night's orders did and nothing was sent: "
                    f"check your "
                    f"connection and `swing auth --check`."
                )
                raise SystemExit(2) from exc
            sync = sync_broker_orders(journal, rows)
            working_orders = list(sync.working_symbols)
            if sync.changed:
                print(f"Broker sync : {sync.describe()}.")

        plans, problems = plan_orders(bundle, journal=journal)
        print("Orders drafted:")
        _print_order_table(plans)
        for problem in problems:
            print(f"  note: {problem}")
        print()

        # A dry run fetches nothing at all: the promise at the top of this module is
        # that it touches no network, and calling the data provider broke it —
        # invisibly, because the suite's socket block turned the stall into a SKIP
        # (audit DEBT-003). Live runs are unchanged.
        quote_problems: list[str] = []
        quotes = (
            fetch_quotes(cfg, [p.symbol for p in plans], client=client, problems=quote_problems)
            if live
            else {}
        )
        quote_error = "; ".join(quote_problems) or None

        account_results = _g.run_guardrails(
            cfg,
            now=moment,
            journal=journal,
            scan_date=bundle.scan_date,
            token_age_days=_auth.token_age_days(cfg, now=moment),
            live_symbols=snapshot.symbols if snapshot is not None else None,
            live_order_symbols=working_orders,
            live_equity=snapshot.equity if snapshot is not None else None,
            dry_run=not live,
        )
        _print_results("Account checks", account_results)

        equity = cfg.account.equity
        if snapshot is not None and snapshot.equity:
            equity = snapshot.equity
        running = _committed_today(journal, today)

        per_order: list[tuple[PlannedOrder, list[_g.GuardrailResult]]] = []
        for plan in plans:
            results = _g.run_order_guardrails(
                cfg,
                pick=plan.pick,
                order=plan.order,
                quote=quotes.get(plan.symbol),
                journal=journal,
                asof=today,
                existing_notional=running,
                proposed_notional=plan.notional,
                equity=equity,
                scan_date=bundle.scan_date,
                quote_error=quote_error,
                dry_run=not live,
            )
            per_order.append((plan, results))
            if _g.all_clear(results):
                running += plan.notional

        for plan, results in per_order:
            print()
            _print_results(f"{plan.symbol} checks", results)

        blocked = not _g.all_clear(account_results) or any(
            not _g.all_clear(results) for _, results in per_order
        )

        if not live:
            print()
            print(
                "DRY RUN — nothing was sent and no broker connection was made. Add --live (and set "
                "execution.enabled = true) to send these orders for real."
            )
            return

        if blocked:
            stopped = [r.name for r in _g.refusals(account_results)]
            stopped += [
                f"{plan.symbol}/{r.name}"
                for plan, results in per_order
                for r in _g.refusals(results)
            ]
            print()
            print(
                f"Refusing to send anything: {len(stopped)} guardrail(s) said no "
                f"({', '.join(stopped)}). Fix what they describe above and run "
                f"`swing execute` again."
            )
            raise SystemExit(1)

        if not plans:
            print()
            print("Every check passed, but the scan drafted no orders, so nothing was sent.")
            return

        _place_orders(
            cfg,
            client=client,
            snapshot=snapshot,
            plans=[plan for plan, _ in per_order],
            journal=journal,
            moment=moment,
            clock=clock,
            scan_date=bundle.scan_date,
            assume_yes=assume_yes,
        )
    finally:
        # The httpx connection pool behind a schwab-py client is not closed
        # by garbage collection in any timely way (audit LEAK-004).
        if client is not None:
            _auth.close_client(client)


def _client_ref(symbol: str, moment: _dt.datetime) -> str:
    """A correlation id minted before the order exists anywhere else.

    The journal row has to be written *before* the network call, which means it
    needs an identity that does not depend on Schwab having answered. This is
    it; the broker's own order id is written onto the same row afterwards.
    """
    return f"swing-{moment:%Y%m%dT%H%M%S}-{symbol}-{uuid.uuid4().hex[:8]}"


def _order_may_be_live(symbol: str, order_id: str | None, detail: str) -> None:
    """Say, loudly, that an order might exist at Schwab that we cannot vouch for."""
    where = f"order {order_id}" if order_id else "the order id is unknown"
    message = (
        f"WARNING: the {symbol} order was accepted by Schwab but something afterwards failed "
        f"({detail}) — {where}. THE ORDER MAY BE LIVE. Check the Schwab app now, and cancel it "
        f"there if you did not want it; do not assume it was not sent."
    )
    log.warning("%s", message)
    print(message)


def _stop_before_placing(cfg: Config, now: _dt.datetime) -> str | None:
    """Re-check the two guardrails that can change while a human is thinking.

    Everything else was settled before the run started, but the kill switch is a
    file a panicking human can create from another terminal and the clock keeps
    moving through the confirmation prompts. Checking them once at startup meant
    ``swing kill`` could not stop orders that were already queued, and a run
    begun at 15:55 could place after the close (audit BUG-008).

    Returns:
        A sentence explaining why the run must stop, or ``None`` to carry on.
    """
    for result in (_g.kill_switch(cfg), _g.trading_hours(now)):
        if not result.ok:
            return result.reason
    return None


def _place_orders(
    cfg: Config,
    *,
    client: Any,
    snapshot: AccountSnapshot | None,
    plans: Sequence[PlannedOrder],
    journal: Journal,
    moment: _dt.datetime,
    clock: Callable[[], _dt.datetime],
    scan_date: _dt.date,
    assume_yes: bool,
) -> None:
    """Send the approved orders one at a time, journalling each as it goes.

    The order of operations inside the loop is the safety property: re-check,
    journal as ``pending``, send, then flip to ``open`` with the broker's id.
    Every rearrangement of those four steps has a failure mode where a live
    order exists that the journal has never heard of (audit BUG-002).
    """
    today = moment.date()
    account_hash = snapshot.hash_value if snapshot is not None else ""
    budget = _g.remaining_order_budget(journal, cfg, today=today)
    placed = 0

    print()
    for plan in plans:
        if placed >= budget:
            print(
                f"Stopping at {placed} order(s): execution.max_orders_per_day is "
                f"{cfg.execution.max_orders_per_day} and the rest of today's budget is spent. "
                f"The remaining picks were not sent."
            )
            break

        if not (cfg.execution.autopilot or assume_yes):
            question = (
                f"Send {plan.quantity} {plan.symbol} at limit {plan.limit_price:.2f} "
                f"(${plan.notional:,.2f})? [y/N] "
            )
            if not _confirm(question):
                print(f"Skipped {plan.symbol} — you did not confirm it.")
                continue

        stop = _stop_before_placing(cfg, clock())
        if stop is not None:
            print(f"Stopping before the {plan.symbol} order: {stop}")
            print("The remaining orders were not sent.")
            break

        ref = _client_ref(plan.symbol, moment)
        try:
            journal.record_order(
                {
                    "symbol": plan.symbol,
                    "date": today.isoformat(),
                    "placed_at": moment.isoformat(),
                    "status": "pending",
                    "order_id": None,
                    "client_ref": ref,
                    "quantity": plan.quantity,
                    "limit": plan.limit_price,
                    "stop": plan.stop_price,
                    "notional": plan.notional,
                    "variant": plan.variant,
                    "scan_date": scan_date.isoformat(),
                    "order": plan.order,
                }
            )
        except Exception as exc:
            # Nothing has been sent yet, and an order that cannot be recorded is
            # an order nothing can watch afterwards. Refuse it.
            log.warning("Could not journal the %s order before sending it: %s", plan.symbol, exc)
            print(
                f"The {plan.symbol} order could not be written to the journal ({exc}), so it was "
                f"NOT sent — an order nothing can record is an order nothing can track. The "
                f"remaining orders are being held back: fix the journal at {journal.path} and "
                f"run `swing execute --live` again."
            )
            raise SystemExit(1) from exc

        try:
            response = client.place_order(account_hash, plan.order)
        except Exception as exc:
            # It is not certain this was refused — a timeout can follow an order
            # Schwab already booked — so the row stays "unknown", which counts
            # as working everywhere it matters, rather than being deleted.
            with contextlib.suppress(Exception):
                journal.annotate_order(ref, status="unknown", error=str(exc))
            print(
                f"Schwab did not accept the {plan.symbol} order ({exc}). It has been journalled "
                f"as unknown rather than sent, because a failed call can still leave a live "
                f"order: check the Schwab app. The remaining orders are being held back — fix "
                f"what Schwab complained about, then run `swing execute --live` again."
            )
            raise SystemExit(1) from exc

        placed += 1
        # From here the order is at the broker. Nothing below may claim otherwise.
        try:
            order_id = _order_id_of(response)
        except Exception as exc:
            order_id = None
            _order_may_be_live(plan.symbol, None, f"its id could not be read: {exc}")

        try:
            journal.annotate_order(ref, status="open", order_id=order_id)
        except Exception as exc:
            _order_may_be_live(plan.symbol, order_id, f"the journal could not be written: {exc}")
        try:
            journal.update_status(plan.symbol, scan_date, "ordered")
        except KeyError as exc:
            log.info("Could not mark %s as ordered in the journal: %s", plan.symbol, exc)
        except Exception as exc:
            _order_may_be_live(plan.symbol, order_id, f"the journal could not be written: {exc}")
        print(
            f"Sent {plan.quantity} {plan.symbol} at limit {plan.limit_price:.2f} "
            f"(order {order_id or 'id unknown'})."
        )

    print(
        f"{placed} order(s) sent. Watch them in the Schwab app; `swing positions` shows both views."
    )


# --------------------------------------------------------------------------
# `swing positions`
# --------------------------------------------------------------------------


def _print_journal_views(journal: Journal) -> list[dict[str, Any]]:
    """Print what the journal thinks it holds and has working; return the positions."""
    local = journal.positions()
    print("Journal positions:")
    _print_positions_table(local, quantity_key="shares")

    working = [
        o
        for o in journal.orders
        if str(o.get("status", "open")).strip().lower() in _g.WORKING_ORDER_STATUSES
    ]
    if working:
        print("Working orders (journal):")
        for order in working:
            status = str(order.get("status", "open")).strip().lower()
            print(
                f"  {str(order.get('symbol', '?')):<8}"
                f"{_as_float(order.get('quantity')) or 0:>10.0f}"
                f"  limit {_as_float(order.get('limit')) or 0:.2f}"
                f"  id {order.get('order_id') or '-'}"
                f"  {status}"
            )
    return local


def print_positions(cfg: Config) -> None:
    """Print the journal's positions and, when reachable, the broker's.

    When Schwab is reachable this also syncs fills first, so the journal view is
    the current one rather than a snapshot frozen at the last placement
    (audit BUG-007).
    """
    journal = Journal.load(cfg)

    try:
        client = _auth.get_client(cfg)
    except _auth.AuthError as exc:
        _print_journal_views(journal)
        print()
        print(f"Broker positions: not available. {exc}")
        return

    with _auth.closing_client(client):
        try:
            snapshot = fetch_account(cfg, client)
        except Exception as exc:
            _print_journal_views(journal)
            print()
            print(
                f"Broker positions: not available ({exc}). The journal view above is all there is "
                f"until Schwab answers again."
            )
            return

        working_orders: list[str] | None = None
        try:
            rows = fetch_broker_orders(cfg, client, account_hash=snapshot.hash_value)
        except Exception as exc:
            print(
                f"Note: Schwab would not list working orders ({exc}), so fills could not be "
                f"synced and only positions are compared below."
            )
        else:
            sync = sync_broker_orders(journal, rows)
            working_orders = list(sync.working_symbols)
            if sync.changed:
                print(f"Broker sync: {sync.describe()}.")

        local = _print_journal_views(journal)
        print()
        print(f"Broker positions ({snapshot.masked_number}, equity {snapshot.equity or 0:,.2f}):")
        _print_positions_table(snapshot.positions, quantity_key="quantity")

        result = _g.reconciliation(
            live_symbols=snapshot.symbols,
            journal_symbols=[p.get("symbol", "") for p in local],
            live_order_symbols=working_orders,
            journal_order_symbols=_g.working_order_symbols(journal),
        )
    print()
    print(f"Reconciliation: {result.verdict} {result.reason}")


# --------------------------------------------------------------------------
# `swing kill`
# --------------------------------------------------------------------------


def _order_symbol(row: Mapping[str, Any]) -> str:
    """The ticker an order refers to, taken from its first leg."""
    legs = row.get("orderLegCollection")
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            instrument = leg.get("instrument")
            if isinstance(instrument, Mapping):
                symbol = str(instrument.get("symbol", "")).strip().upper()
                if symbol:
                    return symbol
    return ""


def _cancel_open_orders(
    cfg: Config, client: Any
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Cancel every working order at the broker.

    Returns ``(cancelled, failed)``, each a list of ``{"order_id", "symbol"}``
    records (``failed`` also carries ``"error"``). The symbol travels with the
    result so the journal can be brought back in step with reality afterwards.
    """
    masked, hash_value = _auth.account_hash(cfg, client)
    del masked
    rows = fetch_broker_orders(cfg, client, account_hash=hash_value)

    cancelled: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        if str(row.get("status", "")).strip().lower() in _CLOSED_ORDER_STATES:
            continue
        order_id = row.get("orderId", row.get("order_id"))
        if order_id in (None, ""):
            continue
        symbol = _order_symbol(row)
        try:
            client.cancel_order(order_id, hash_value)
        except Exception as exc:
            log.warning("Could not cancel Schwab order %s: %s", order_id, exc)
            failed.append({"order_id": str(order_id), "symbol": symbol, "error": str(exc)})
        else:
            cancelled.append({"order_id": str(order_id), "symbol": symbol})
    return cancelled, failed


def _mark_cancelled_in_journal(
    cfg: Config, cancelled: Sequence[Mapping[str, str]], failed: Sequence[Mapping[str, str]]
) -> int:
    """Flip the journal's orders to ``cancelled`` for symbols the broker cancelled.

    Without this the journal would keep claiming those orders were working, and
    the ``duplicate`` guardrail would refuse the symbol forever. A symbol whose
    cancellation *failed* keeps its ``open`` status on purpose: when we are not
    sure an order is really gone, the pessimistic view is the safe one.

    Returns how many journal orders were changed.
    """
    unsure = {str(o.get("symbol", "")).upper() for o in failed if o.get("symbol")}
    symbols = {str(o.get("symbol", "")).upper() for o in cancelled if o.get("symbol")} - unsure
    if not symbols:
        return 0
    journal = Journal.load(cfg)
    changed = 0
    for symbol in sorted(symbols):
        changed += journal.update_order_status(symbol, "cancelled", only_status="open")
    return changed


def kill(cfg: Config, *, off: bool = False) -> None:
    """Engage or release the kill switch, cancelling working orders on the way in.

    ``swing kill`` in the CLI already engages the file before calling this, so
    engaging again here must be harmless — and it is, because
    :func:`swing.state.engage_kill` is idempotent. Cancellation is strictly
    best effort: the kill switch itself is a local file and must never depend
    on Schwab being reachable.
    """
    if off:
        _state.clear_kill(cfg)
        print(
            f"Kill switch is off ({_state.kill_path(cfg)} does not exist). Execution is allowed "
            f"again — dry runs first."
        )
        return

    already = _state.kill_active(cfg)
    path = _state.engage_kill(cfg, reason="cli")
    if already:
        print(f"Kill switch confirmed engaged ({path}). Checking for working orders to cancel.")
    else:
        print(f"Kill switch engaged ({path}). No orders will be sent until `swing kill --off`.")

    try:
        client = _auth.get_client(cfg)
    except _auth.AuthError as exc:
        print(
            f"No broker connection, so no working orders could be cancelled ({exc}). Cancel "
            f"anything open in the Schwab app yourself."
        )
        return

    with _auth.closing_client(client):
        try:
            cancelled, failed = _cancel_open_orders(cfg, client)
        except Exception as exc:
            print(
                f"Schwab could not be asked to cancel working orders ({exc}). The kill switch is "
                f"set, but cancel anything open in the Schwab app yourself."
            )
            return

    if cancelled:
        ids = ", ".join(o["order_id"] for o in cancelled)
        print(f"Cancelled {len(cancelled)} working order(s): {ids}.")
    else:
        print("There were no working orders at Schwab to cancel.")
    if failed:
        detail = "; ".join(f"{o['order_id']} ({o['error']})" for o in failed)
        print(
            f"{len(failed)} order(s) could not be cancelled ({detail}). Cancel those in the "
            f"Schwab app yourself."
        )

    changed = _mark_cancelled_in_journal(cfg, cancelled, failed)
    if changed:
        print(
            f"Marked {changed} journal order(s) as cancelled, so those symbols can be picked "
            f"again once the kill switch is released."
        )
