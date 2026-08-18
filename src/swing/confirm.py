"""Pre-open confirmation.

Last night's picks were computed from last night's close. By 09:00 ET the
world may have moved: an earnings miss, an upgrade, a market-wide gap. This
step re-quotes every pick and re-classifies it:

``confirmed``
    Price is close to the reference. The drafted order stands as written.
``adjusted``
    Price moved, but less than the invalidation threshold. Shares are re-sized
    against the new price and the stop is re-derived, so the dollar risk stays
    where you put it rather than drifting with the gap.
``invalidated``
    Price gapped more than ``max_quote_drift_atr`` ATR (or
    ``max_quote_drift_pct``) past the reference. A breakout that has already
    gapped 1 ATR is a different trade with a much worse entry, and chasing it
    is how a 2% risk budget quietly becomes 5%.

The updated sheet is written back alongside the original, and a short push goes
out with the confirmed/adjusted/cancelled counts.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .config import Config
from .data.provider import Quote, get_provider
from .logging_setup import get_logger
from .orders import OrderValidationError, draft_orders, validate_order
from .picks import (
    STATUS_ADJUSTED,
    STATUS_CONFIRMED,
    STATUS_INVALIDATED,
    PickSheet,
    latest_sheet_path,
    load_sheet,
    now_stamp,
)
from .strategy import rules
from .strategy.sizing import size_position

log = get_logger("swing.confirm")


def run_confirm(
    cfg: Config,
    dry_run: bool = False,
    sheet_path: Path | None = None,
    quotes: dict[str, Quote] | None = None,
) -> int:
    path = Path(sheet_path) if sheet_path else latest_sheet_path(cfg)
    if path is None or not path.exists():
        print("no pick sheet found. Run `swing scan` first.")
        return 4

    sheet = load_sheet(path)
    if not sheet.picks:
        print(f"{path}: no tradable picks to confirm.")
        return 0

    symbols = [p.symbol for p in sheet.picks]
    if quotes is None:
        try:
            quotes = get_provider(cfg).quotes(symbols)
        except Exception as exc:
            print(f"could not fetch quotes ({exc}). The overnight sheet stands unchanged.")
            log.error("quote fetch failed: %s", exc)
            return 5

    missing = [s for s in symbols if s not in quotes]
    if missing:
        sheet.warnings.append(
            f"no quote for {', '.join(missing)} — left at last night's numbers; "
            "check these by hand before placing"
        )

    counts = {STATUS_CONFIRMED: 0, STATUS_ADJUSTED: 0, STATUS_INVALIDATED: 0}
    stale_quotes = False

    for pick in sheet.picks:
        quote = quotes.get(pick.symbol)
        if quote is None:
            continue
        stale_quotes = stale_quotes or quote.stale
        _apply_quote(cfg, sheet, pick, quote)
        counts[pick.status] = counts.get(pick.status, 0) + 1

    if stale_quotes:
        sheet.warnings.append(
            "quotes came from a delayed feed (yfinance is ~15 minutes behind). "
            "Treat the drift check as approximate until the Schwab provider is live."
        )

    sheet.generated_at = now_stamp()
    out = path.parent
    (out / "picks-confirmed.json").write_text(sheet.to_json())
    (out / "picks-confirmed.html").write_text(sheet.to_html())
    (out / "picks-confirmed.txt").write_text(sheet.to_text())
    _rewrite_orders(out, sheet)

    summary = (
        f"{counts.get(STATUS_CONFIRMED, 0)} confirmed, "
        f"{counts.get(STATUS_ADJUSTED, 0)} adjusted, "
        f"{counts.get(STATUS_INVALIDATED, 0)} cancelled"
    )
    print(f"pre-open confirm — {summary}\n")
    for pick in sheet.picks:
        print(pick.block(sheet.equity))
        print()
    print(f"sheet: {out / 'picks-confirmed.html'}")

    if dry_run:
        print("\n--dry-run: no alerts were sent.")
        return 0

    from .alerts.dispatch import deliver

    results = deliver(
        cfg,
        title=f"swing pre-open {sheet.as_of}: {summary}",
        text=sheet.to_text(),
        html=sheet.to_html(),
    )
    print()
    for result in results:
        print(result.line())
    return 0


def _apply_quote(cfg: Config, sheet: PickSheet, pick, quote: Quote) -> None:
    """Re-classify one pick against a fresh quote, re-sizing where appropriate."""
    s = cfg.strategy
    price = float(quote.price)
    pick.confirm_price = price

    reference = pick.close
    drift = price - reference
    drift_pct = drift / reference if reference else 0.0
    drift_atr = drift / pick.atr if pick.atr else 0.0

    max_atr = float(cfg.execution.get("max_quote_drift_atr", 1.0))
    max_pct = float(cfg.execution.get("max_quote_drift_pct", 0.03))

    if abs(drift_atr) > max_atr or abs(drift_pct) > max_pct:
        pick.status = STATUS_INVALIDATED
        pick.shares = 0
        pick.orders = {}
        direction = "gapped up" if drift > 0 else "gapped down"
        pick.confirm_note = (
            f"CANCELLED — {price:.2f} {direction} {drift_pct:+.1%} "
            f"({drift_atr:+.2f} ATR) from the {reference:.2f} reference; "
            f"limit is {max_atr:g} ATR / {max_pct:.0%}"
        )
        return

    if abs(drift_pct) < 0.002:
        pick.status = STATUS_CONFIRMED
        pick.confirm_note = f"confirmed at {price:.2f} ({drift_pct:+.2%})"
        return

    # Re-size against the new price so the dollar risk stays put.
    stop = rules.initial_stop(price, pick.atr, s)
    size = size_position(
        entry=price,
        stop=stop,
        equity=sheet.equity,
        risk_pct=float(cfg.account.risk_pct),
        max_position_pct=float(cfg.account.max_position_pct),
        available_cash=sheet.available_cash,
    )
    if not size.affordable:
        pick.status = STATUS_INVALIDATED
        pick.shares = 0
        pick.orders = {}
        pick.confirm_note = (
            f"CANCELLED — at {price:.2f} the position no longer sizes: "
            f"{size.notes[0] if size.notes else size.limit.value}"
        )
        return

    old_shares = pick.shares
    pick.status = STATUS_ADJUSTED
    pick.shares = size.shares
    pick.stop = round(stop, 2)
    pick.stop_limit_price = round(rules.stop_limit_price(stop, s), 2)
    pick.notional = round(size.notional, 2)
    pick.risk_dollars = round(size.risk_dollars, 2)
    pick.risk_pct = size.risk_pct(sheet.equity)
    pick.equity_pct = size.equity_pct(sheet.equity)
    pick.sizing_limit = size.limit.value
    pick.confirm_note = (
        f"adjusted to {price:.2f} ({drift_pct:+.2%}): "
        f"{old_shares} -> {size.shares} shares, stop {pick.stop:.2f}"
    )

    try:
        pick.orders = draft_orders(
            symbol=pick.symbol,
            shares=size.shares,
            reference_price=price,
            stop=pick.stop,
            trail_offset=pick.trail_offset,
            stop_limit_offset_pct=float(s.exit.stop_limit_offset_pct),
            limit_slippage_pct=float(cfg.execution.get("limit_slippage_pct", 0.003)),
        )
        for name, order in pick.orders.items():
            validate_order(order, path=f"{pick.symbol}.{name}")
    except OrderValidationError as exc:
        pick.status = STATUS_INVALIDATED
        pick.shares = 0
        pick.orders = {}
        pick.confirm_note = f"CANCELLED — re-drafted order failed validation: {exc}"


def _rewrite_orders(out_dir: Path, sheet: PickSheet) -> None:
    """Replace the orders directory so a cancelled pick cannot be placed by mistake."""
    orders_dir = out_dir / "orders"
    orders_dir.mkdir(exist_ok=True)
    for existing in orders_dir.glob("*.json"):
        existing.unlink()
    for pick in sheet.picks:
        for name, payload in (pick.orders or {}).items():
            (orders_dir / f"{pick.symbol}-{name}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True)
            )


def sheet_is_for_today(sheet: PickSheet, today: date | None = None) -> bool:
    """Is this sheet fresh enough to act on? Used by the executor's staleness guard."""
    today = today or date.today()
    try:
        as_of = date.fromisoformat(sheet.as_of)
    except (TypeError, ValueError):
        return False
    return (today - as_of).days <= 4
