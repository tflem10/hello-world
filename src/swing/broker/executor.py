"""Guarded order execution — ``swing execute``, ``swing positions``, ``swing kill``.

The safe default is doing nothing. Running ``swing execute`` with no flags is a
**dry run**: it finds the latest scan report, prints the orders it *would* send
and the verdict of every guardrail, touches no network, needs no schwab-py, and
exits 0. Sending real money requires two switches thrown independently — the
``--live`` flag on the command line *and* ``execution.enabled = true`` in
config.toml — because one of them can be thrown by a typo and two cannot.

Once live, the sequence is: build a client, read the real account, fetch real
quotes, run every guardrail from :mod:`swing.broker.guardrails`, and place
nothing at all unless every single one passes. There is no partial mode where
some orders go and others are held back by a failing check: if the picture is
wrong anywhere, the whole run stops.

The kill switch is deliberately cruder than everything else. It is a file, so
it can be engaged by this code, by a shell, or by a panicking human with a text
editor, and it is checked before anything else happens.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swing import state as _state
from swing.broker import auth as _auth
from swing.broker import guardrails as _g
from swing.state import Journal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "BrokerError",
    "PlannedOrder",
    "ScanBundle",
    "kill",
    "latest_scan_dir",
    "load_scan",
    "plan_orders",
    "print_positions",
    "run_execute",
]

log = logging.getLogger(__name__)

#: Which drafted variant to send when ``orders/<SYMBOL>.json`` holds several.
ORDER_VARIANT_PREFERENCE = ("oto_stop", "oto_stop_limit", "trailing_stop")

#: Order states that mean an order is no longer working at the broker.
_CLOSED_ORDER_STATES = frozenset(
    {"filled", "cancelled", "canceled", "rejected", "expired", "closed", "replaced"}
)

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
    """Return the most recent ``scan-YYYY-MM-DD`` directory, or ``None``."""
    reports = Path(cfg.paths.reports_dir).expanduser()
    if not reports.is_dir():
        return None
    dated = [
        (day, path)
        for path in reports.iterdir()
        if path.is_dir() and (day := _scan_date_of(path)) is not None
    ]
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrokerError(
            f"{path} could not be read as JSON ({exc}), so this scan cannot be executed: re-run "
            f"`swing scan` to rebuild the report."
        ) from exc


def load_scan(path: Path) -> ScanBundle:
    """Load ``picks.json`` and every ``orders/<SYMBOL>.json`` from a scan directory.

    Raises:
        BrokerError: when ``picks.json`` is missing or unreadable.
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

    scan_date = _scan_date_of(path)
    asof = payload.get("asof")
    if isinstance(asof, str):
        with contextlib.suppress(ValueError):
            scan_date = _dt.date.fromisoformat(asof[:10])
    if scan_date is None:
        raise BrokerError(
            f"The scan directory {path} has no date in its name and picks.json has no `asof`, "
            f"so its age cannot be judged: re-run `swing scan`."
        )

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


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_leg(order: Mapping[str, Any]) -> Mapping[str, Any]:
    legs = order.get("orderLegCollection")
    if isinstance(legs, list) and legs and isinstance(legs[0], Mapping):
        return legs[0]
    return {}


def _child_stop(order: Mapping[str, Any]) -> float | None:
    children = order.get("childOrderStrategies")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, Mapping):
                stop = _as_float(child.get("stopPrice"))
                if stop is not None:
                    return stop
    return None


def plan_orders(bundle: ScanBundle) -> tuple[list[PlannedOrder], list[str]]:
    """Pair every actionable pick with its drafted order.

    Returns:
        ``(plans, problems)`` — picks with no usable order file are reported in
        ``problems`` rather than silently dropped.
    """
    plans: list[PlannedOrder] = []
    problems = list(bundle.problems)
    for pick in bundle.picks:
        symbol = str(pick["symbol"]).upper()
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

        leg = _first_leg(order)
        quantity = int(_as_float(leg.get("quantity")) or _as_float(pick.get("shares")) or 0)
        limit = _as_float(order.get("price")) or _as_float(pick.get("entry")) or 0.0
        stop = _child_stop(order) or _as_float(pick.get("stop"))
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
        if isinstance(instrument, Mapping):
            symbol = str(instrument.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        quantity = (_as_float(row.get("longQuantity")) or 0.0) - (
            _as_float(row.get("shortQuantity")) or 0.0
        )
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


def _quotes_from_provider(cfg: Config, symbols: Sequence[str]) -> dict[str, float]:
    from swing.data import get_provider

    quotes = get_provider(cfg).latest_quotes(list(symbols))
    out: dict[str, float] = {}
    for symbol, quote in (quotes or {}).items():
        price = _as_float(getattr(quote, "price", None))
        if price is not None:
            out[str(symbol).upper()] = price
    return out


def fetch_quotes(cfg: Config, symbols: Sequence[str], *, client: Any = None) -> dict[str, float]:
    """Best-effort current prices: the configured provider first, Schwab second.

    Never raises. An empty dict means "no quote was available", which the
    ``quote_drift`` guardrail turns into a refusal when live and a SKIP in a
    dry run.
    """
    wanted = [s.upper() for s in symbols]
    if not wanted:
        return {}
    try:
        prices = _quotes_from_provider(cfg, wanted)
    except Exception as exc:
        log.info("Quotes from the configured data provider were unavailable: %s", exc)
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
            log.info("Quotes from Schwab were unavailable: %s", exc)
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
        now: override the clock. A test seam — the CLI never passes it.

    Raises:
        SystemExit: 2 when the two live switches disagree or the broker cannot
            be reached; 1 when a guardrail refuses. A dry run never raises.
    """
    moment = now or _now_eastern()
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
    plans, problems = plan_orders(bundle)

    mode = "LIVE" if live else "DRY RUN"
    print(f"swing execute — {mode}")
    print(f"Scan report : {bundle.path} (asof {bundle.scan_date.isoformat()})")
    print(f"Clock       : {moment:%Y-%m-%d %H:%M %Z}")
    print("Orders drafted:")
    _print_order_table(plans)
    for problem in problems:
        print(f"  note: {problem}")
    print()

    client: Any = None
    snapshot: AccountSnapshot | None = None
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
                f"Schwab could not be reached to read the account ({exc}), so nothing was sent: "
                f"check your connection and `swing auth --check`."
            )
            raise SystemExit(2) from exc
        print(f"Account     : {snapshot.masked_number}, equity {snapshot.equity or 0:,.2f}")

    quotes = fetch_quotes(cfg, [p.symbol for p in plans], client=client)

    account_results = _g.run_guardrails(
        cfg,
        now=moment,
        journal=journal,
        scan_date=bundle.scan_date,
        token_age_days=_auth.token_age_days(cfg, now=moment),
        live_symbols=snapshot.symbols if snapshot is not None else None,
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
            f"{plan.symbol}/{r.name}" for plan, results in per_order for r in _g.refusals(results)
        ]
        print()
        print(
            f"Refusing to send anything: {len(stopped)} guardrail(s) said no "
            f"({', '.join(stopped)}). Fix what they describe above and run `swing execute` again."
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
        scan_date=bundle.scan_date,
        assume_yes=assume_yes,
    )


def _place_orders(
    cfg: Config,
    *,
    client: Any,
    snapshot: AccountSnapshot | None,
    plans: Sequence[PlannedOrder],
    journal: Journal,
    moment: _dt.datetime,
    scan_date: _dt.date,
    assume_yes: bool,
) -> None:
    """Send the approved orders one at a time, journalling each as it goes."""
    today = moment.date()
    account_hash = snapshot.hash_value if snapshot is not None else ""
    budget = int(cfg.execution.max_orders_per_day) - len(_g.orders_placed_on(journal, today))
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

        try:
            response = client.place_order(account_hash, plan.order)
            order_id = _order_id_of(response)
        except Exception as exc:
            print(
                f"Schwab rejected the {plan.symbol} order ({exc}), so it was not sent. The "
                f"remaining orders are being held back — check the Schwab app, then run "
                f"`swing execute --live` again."
            )
            raise SystemExit(1) from exc

        placed += 1
        journal.record_order(
            {
                "symbol": plan.symbol,
                "date": today.isoformat(),
                "placed_at": moment.isoformat(),
                "status": "open",
                "order_id": order_id,
                "quantity": plan.quantity,
                "limit": plan.limit_price,
                "stop": plan.stop_price,
                "notional": plan.notional,
                "variant": plan.variant,
                "scan_date": scan_date.isoformat(),
                "order": plan.order,
            }
        )
        try:
            journal.update_status(plan.symbol, scan_date, "ordered")
        except (KeyError, ValueError) as exc:
            log.info("Could not mark %s as ordered in the journal: %s", plan.symbol, exc)
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


def print_positions(cfg: Config) -> None:
    """Print the journal's positions and, when reachable, the broker's."""
    journal = Journal.load(cfg)
    local = journal.positions()
    print("Journal positions:")
    _print_positions_table(local, quantity_key="shares")

    open_orders = journal.open_orders()
    if open_orders:
        print("Working orders (journal):")
        for order in open_orders:
            print(
                f"  {str(order.get('symbol', '?')):<8}"
                f"{_as_float(order.get('quantity')) or 0:>10.0f}"
                f"  limit {_as_float(order.get('limit')) or 0:.2f}"
                f"  id {order.get('order_id', '-')}"
            )

    print()
    try:
        client = _auth.get_client(cfg)
    except _auth.AuthError as exc:
        print(f"Broker positions: not available. {exc}")
        return
    try:
        snapshot = fetch_account(cfg, client)
    except Exception as exc:
        print(
            f"Broker positions: not available ({exc}). The journal view above is all there is "
            f"until Schwab answers again."
        )
        return

    print(f"Broker positions ({snapshot.masked_number}, equity {snapshot.equity or 0:,.2f}):")
    _print_positions_table(snapshot.positions, quantity_key="quantity")

    result = _g.reconciliation(
        live_symbols=snapshot.symbols,
        journal_symbols=[p.get("symbol", "") for p in local],
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
    payload = _body(client.get_orders_for_account(hash_value))
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, Mapping):
        candidate = payload.get("orders")
        rows = candidate if isinstance(candidate, list) else []

    cancelled: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
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

    try:
        cancelled, failed = _cancel_open_orders(cfg, client)
    except Exception as exc:
        print(
            f"Schwab could not be asked to cancel working orders ({exc}). The kill switch is set, "
            f"but cancel anything open in the Schwab app yourself."
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
