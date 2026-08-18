"""v2 auto-execution.

Three independent things must all be true before a single order is
transmitted:

1. ``[execution] enabled = true`` in the config file
2. ``--live`` on the command line
3. every guardrail in :mod:`swing.execution.guardrails` passes

and unless ``autopilot = true``, you also type ``yes`` per order.

Default behaviour with no flags at all is a **dry run**: it prints the exact
JSON that would be sent and transmits nothing. That is also the tool for
debugging a rejected order — run it, read the payload, compare to the docs.

Every attempt, success and rejection is written to the journal before and after
the API call, so a process killed mid-flight leaves evidence rather than
ambiguity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import Config
from ..logging_setup import get_logger
from ..orders import OrderValidationError, describe_order, validate_order
from ..picks import PickSheet, latest_sheet_path, load_sheet
from .guardrails import (
    check_order,
    choose_order_variant,
    kill_switch_engaged,
    preflight,
)
from .journal import (
    EVENT_DRAFTED,
    EVENT_PLACED,
    EVENT_REJECTED,
    record,
    record_entry,
)

log = get_logger("swing.execute")


@dataclass
class ExecutionOutcome:
    symbol: str
    action: str          # placed | skipped | rejected | dry_run
    detail: str = ""
    order_id: str | None = None

    def line(self) -> str:
        return f"  {self.symbol:<8} {self.action:<9} {self.detail}"


def run_execute(
    cfg: Config,
    live: bool = False,
    sheet_path: Path | None = None,
    assume_yes: bool = False,
    client=None,
    quotes: dict | None = None,
    account_equity: float | None = None,
    broker_positions: dict[str, int] | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()

    # The kill switch is checked before anything else, including reading the
    # config's execution block, so that it works even when the rest is broken.
    if kill_switch_engaged(cfg):
        print(
            f"KILL SWITCH ENGAGED ({cfg.expand_path(cfg.execution.kill_file)}).\n"
            "No orders will be placed. Release with `swing kill --release`."
        )
        return 6

    path = Path(sheet_path) if sheet_path else _newest_sheet(cfg)
    if path is None or not path.exists():
        print("no pick sheet found. Run `swing scan` (and `swing confirm`) first.")
        return 4
    sheet = load_sheet(path)
    print(f"sheet: {path}")

    tradable = [p for p in sheet.picks if p.tradable and p.shares >= 1]
    if not tradable:
        print("nothing tradable on this sheet.")
        return 0

    # -- live account state -----------------------------------------------
    if live and client is None:
        from ..auth import SchwabNotConfigured, get_client

        try:
            client = get_client(cfg, interactive=False)
        except SchwabNotConfigured as exc:
            print(f"cannot execute: {exc}")
            return 2

    if client is not None:
        if account_equity is None:
            account_equity = _fetch_equity(cfg, client)
        if broker_positions is None:
            broker_positions = _fetch_positions(cfg, client)
        if quotes is None:
            quotes = _fetch_quotes(cfg, client, [p.symbol for p in tradable])

    # -- pre-flight --------------------------------------------------------
    report = preflight(
        cfg, live=live, sheet=sheet, account_equity=account_equity,
        broker_positions=broker_positions, now=now,
    )
    print()
    print(report.describe())

    hard_blockers = [g for g in report.blockers if g.name != "--live flag"]
    if hard_blockers:
        print("\nrefusing to proceed. Fix the BLOCK lines above.")
        return 5

    if not live:
        print("\n--- DRY RUN: nothing will be transmitted ---")

    # -- per order ---------------------------------------------------------
    outcomes: list[ExecutionOutcome] = []
    exposure = 0.0
    placed = 0
    autopilot = bool(cfg.execution.get("autopilot", False)) or assume_yes

    for pick in tradable:
        quote_price = _quote_price(quotes, pick.symbol) if quotes is not None else None
        if quote_price is None and not live:
            # In a dry run there may be no quote source at all; fall back to
            # the sheet's own reference so the run still demonstrates the flow.
            quote_price = pick.confirm_price or pick.close

        order_report = check_order(
            cfg, pick, quote_price, exposure, sheet.equity, placed, now=now
        )
        chosen = choose_order_variant(cfg, pick)

        print(f"\n{pick.symbol}:")
        print(order_report.describe())

        if not order_report.passed:
            reasons = "; ".join(g.detail for g in order_report.blockers)
            outcomes.append(ExecutionOutcome(pick.symbol, "skipped", reasons))
            record(cfg, EVENT_REJECTED, symbol=pick.symbol, reason=reasons, stage="guardrail")
            continue

        if chosen is None:
            outcomes.append(
                ExecutionOutcome(pick.symbol, "skipped", "no usable order variant")
            )
            continue

        variant, order = chosen
        try:
            validate_order(order, path=f"{pick.symbol}.{variant}")
        except OrderValidationError as exc:
            outcomes.append(ExecutionOutcome(pick.symbol, "rejected", str(exc)))
            record(cfg, EVENT_REJECTED, symbol=pick.symbol, reason=str(exc),
                   stage="validation")
            continue

        print(f"  order: {describe_order(order)}")
        record(cfg, EVENT_DRAFTED, symbol=pick.symbol, variant=variant, order=order)

        if not live:
            print(json.dumps(order, indent=2, sort_keys=True))
            outcomes.append(
                ExecutionOutcome(pick.symbol, "dry_run", f"{variant}, not transmitted")
            )
            exposure += pick.notional
            continue

        if not autopilot and not _confirm(pick, order):
            outcomes.append(ExecutionOutcome(pick.symbol, "skipped", "declined at the prompt"))
            continue

        outcome = _place(cfg, client, pick, order, variant)
        outcomes.append(outcome)
        if outcome.action == "placed":
            placed += 1
            exposure += pick.notional

    # -- summary -----------------------------------------------------------
    print("\nsummary:")
    for outcome in outcomes:
        print(outcome.line())
    if not live:
        print(
            "\nNothing was transmitted. To place these for real:\n"
            "  1. set [execution] enabled = true in config.toml\n"
            "  2. swing execute --live"
        )
    return 0


# ---------------------------------------------------------------------------
def _newest_sheet(cfg: Config) -> Path | None:
    """Prefer the confirmed sheet: it has the pre-open re-quote applied."""
    latest = latest_sheet_path(cfg)
    if latest is None:
        return None
    confirmed = latest.parent / "picks-confirmed.json"
    return confirmed if confirmed.exists() else latest


def _confirm(pick, order) -> bool:
    print(f"\n  place this order for {pick.symbol}?")
    print(f"    {describe_order(order)}")
    print(f"    risking ${pick.risk_dollars:,.2f} if the stop fills")
    try:
        answer = input("  type 'yes' to place: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  aborted")
        return False
    return answer == "yes"


def _place(cfg: Config, client, pick, order: dict, variant: str) -> ExecutionOutcome:
    account_hash = str(cfg.schwab.get("account_hash", "") or "")
    if not account_hash:
        detail = "[schwab] account_hash is empty — run `swing auth --check`"
        record(cfg, EVENT_REJECTED, symbol=pick.symbol, reason=detail, stage="config")
        return ExecutionOutcome(pick.symbol, "rejected", detail)

    # Journal the attempt BEFORE the call: a process killed between the request
    # and the response must leave evidence that something may be resting at the
    # broker, rather than silently nothing.
    record(cfg, EVENT_PLACED, symbol=pick.symbol, variant=variant, shares=pick.shares,
           limit=order.get("price"), stop=pick.stop, status="submitting")

    try:
        response = client.place_order(account_hash, order)
        response.raise_for_status()
    except Exception as exc:
        detail = str(exc)[:300]
        record(cfg, EVENT_REJECTED, symbol=pick.symbol, reason=detail, stage="api")
        return ExecutionOutcome(pick.symbol, "rejected", detail)

    order_id = _extract_order_id(response)
    record(cfg, EVENT_PLACED, symbol=pick.symbol, variant=variant, shares=pick.shares,
           limit=order.get("price"), stop=pick.stop, status="accepted", order_id=order_id)
    # The entry is journalled optimistically so slot counting stays right. A
    # buy LIMIT that never fills leaves a phantom position, which the next
    # reconciliation catches and blocks on — noisy, but the safe direction.
    record_entry(
        cfg, pick.symbol, pick.shares, float(order.get("price", pick.close)),
        pick.stop, pick.trail_offset, order_id=order_id,
        note=f"placed via {variant}; unfilled limits show up in reconciliation",
    )
    return ExecutionOutcome(pick.symbol, "placed", f"{variant}, id {order_id}", order_id)


def _extract_order_id(response) -> str | None:
    """Schwab returns the new order id in the Location header, not the body."""
    location = ""
    try:
        location = response.headers.get("Location", "") or ""
    except Exception:
        return None
    return location.rstrip("/").split("/")[-1] if location else None


def _fetch_equity(cfg: Config, client) -> float | None:
    account_hash = str(cfg.schwab.get("account_hash", "") or "")
    if not account_hash:
        return None
    try:
        response = client.get_account(account_hash)
        response.raise_for_status()
        payload = response.json()
        balances = (payload.get("securitiesAccount") or {}).get("currentBalances") or {}
        for key in ("liquidationValue", "equity", "cashBalance"):
            if balances.get(key) is not None:
                return float(balances[key])
    except Exception as exc:
        log.warning("could not read the account balance: %s", exc)
    return None


def _fetch_positions(cfg: Config, client) -> dict[str, int] | None:
    account_hash = str(cfg.schwab.get("account_hash", "") or "")
    if not account_hash:
        return None
    try:
        response = client.get_account(account_hash, fields=client.Account.Fields.POSITIONS)
        response.raise_for_status()
        payload = response.json()
        positions = (payload.get("securitiesAccount") or {}).get("positions") or []
        out: dict[str, int] = {}
        for position in positions:
            symbol = (position.get("instrument") or {}).get("symbol")
            quantity = float(position.get("longQuantity", 0) or 0)
            if symbol and quantity:
                out[str(symbol).upper()] = int(quantity)
        return out
    except Exception as exc:
        log.warning("could not read broker positions: %s", exc)
        return None


def _fetch_quotes(cfg: Config, client, symbols: list[str]) -> dict | None:
    """Live quotes only — deliberately no yfinance fallback.

    The data layer falls back to free data when Schwab is unavailable, which is
    right for producing a pick sheet. It is wrong here: substituting a delayed
    print for a live quote would let the drift guardrail wave through an order
    against a price that is fifteen minutes stale. If the quote call fails we
    return None, no quote reaches check_order(), and every order is blocked.
    """
    try:
        from ..data.schwab_provider import live_quotes

        return live_quotes(client, symbols)
    except Exception as exc:
        log.warning(
            "live quotes unavailable (%s); every order will be blocked by the "
            "quote guardrail rather than placed against a stale price", exc
        )
        return None


def _quote_price(quotes: dict, symbol: str) -> float | None:
    quote = (quotes or {}).get(symbol.upper()) or (quotes or {}).get(symbol)
    if quote is None:
        return None
    return float(getattr(quote, "price", quote))


def summarise_sheet(sheet: PickSheet) -> str:
    tradable = [p for p in sheet.picks if p.tradable and p.shares >= 1]
    total = sum(p.notional for p in tradable)
    risk = sum(p.risk_dollars for p in tradable)
    return (
        f"{len(tradable)} order(s), ${total:,.2f} deployed, "
        f"${risk:,.2f} at risk ({risk / sheet.equity:.2%} of equity)"
        if sheet.equity
        else f"{len(tradable)} order(s)"
    )
