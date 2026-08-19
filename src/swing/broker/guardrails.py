"""FROZEN CONTRACT 12 — the guardrails that stand between a pick and an order.

Every check in here is a **pure function**: it takes facts (a config, a quote
that somebody else fetched, a journal, a clock reading) and returns a
:class:`GuardrailResult`. Nothing in this module opens a socket, builds a
broker client, or reads the clock on its own. That is a deliberate design
choice with two payoffs:

* the tests can prove every single refusal without a network or schwab-py;
* the executor can run the same checks in dry-run mode, where the live inputs
  are simply absent and the affected checks report ``SKIP``.

The bar for a refusal is that it says, in one sentence, what stopped and what
the human should do about it. A guardrail that fires without explaining itself
is a bug, not a safety feature.

Two verdicts are softer than a refusal:

* ``warning`` — allowed, but something needs attention soon (a token entering
  its last day, an acknowledged reconciliation difference);
* ``skipped`` — not evaluated, because this is a dry run and the live input it
  needs was never fetched. A skipped check is never evidence of safety.

Failure is closed. When ``gate_passed`` cannot even find the backtest gate, it
refuses; an unknown answer is treated as a "no".
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from swing import state as _state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config
    from swing.state import Journal

__all__ = [
    "DEFAULT_DEDUPE_DAYS",
    "EQUITY_MISMATCH_TOLERANCE_PCT",
    "GuardrailResult",
    "MARKET_CLOSE",
    "MARKET_OPEN",
    "all_clear",
    "duplicate",
    "equity_mismatch",
    "gate_passed",
    "kill_switch",
    "limit_only",
    "new_exposure",
    "orders_today",
    "orders_placed_on",
    "quote_drift",
    "reconciliation",
    "refusals",
    "run_guardrails",
    "run_order_guardrails",
    "skipped",
    "stale_scan",
    "token_age",
    "trading_hours",
]

log = logging.getLogger(__name__)

#: Regular-session bounds, US equities, in Eastern time.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
MARKET_TZ = "America/New_York"

#: Config equity may differ from the broker's by this much before we refuse.
EQUITY_MISMATCH_TOLERANCE_PCT = 20.0

#: How many days must pass before the same symbol may be picked again.
DEFAULT_DEDUPE_DAYS = 5

#: Order types this system will never send, at any depth of an order structure.
_MARKET_ORDER_TYPES = frozenset({"MARKET", "MARKET_ON_CLOSE"})


@dataclass(frozen=True)
class GuardrailResult:
    """The verdict of a single guardrail.

    Attributes:
        ok: False means *do not place this order*. Nothing else overrides it.
        name: the guardrail's stable name, e.g. ``"quote_drift"``.
        reason: one plain-English sentence — for refusals, what to do next.
        warning: allowed, but the human should know. Never blocks.
        skipped: not evaluated (dry run without live data). Never blocks, and
            never counts as evidence that the check would have passed.
    """

    ok: bool
    name: str
    reason: str
    warning: bool = False
    skipped: bool = False

    @property
    def verdict(self) -> str:
        """A four-letter label for tables: PASS, WARN, SKIP or STOP."""
        if self.skipped:
            return "SKIP"
        if not self.ok:
            return "STOP"
        if self.warning:
            return "WARN"
        return "PASS"


def _ok(name: str, reason: str) -> GuardrailResult:
    return GuardrailResult(ok=True, name=name, reason=reason)


def _warn(name: str, reason: str) -> GuardrailResult:
    return GuardrailResult(ok=True, name=name, reason=reason, warning=True)


def _refuse(name: str, reason: str) -> GuardrailResult:
    return GuardrailResult(ok=False, name=name, reason=reason)


def skipped(name: str, reason: str) -> GuardrailResult:
    """Build a SKIP result — used by the executor for dry runs."""
    return GuardrailResult(ok=True, name=name, reason=reason, skipped=True)


def all_clear(results: Sequence[GuardrailResult]) -> bool:
    """True when no guardrail in ``results`` refused."""
    return all(r.ok for r in results)


def refusals(results: Sequence[GuardrailResult]) -> list[GuardrailResult]:
    """Just the guardrails that said no, in the order they were run."""
    return [r for r in results if not r.ok]


# --------------------------------------------------------------------------
# 1. kill switch
# --------------------------------------------------------------------------


def kill_switch(cfg: Config) -> GuardrailResult:
    """Refuse while the KILL file exists. The last line of defence."""
    if _state.kill_active(cfg):
        return _refuse(
            "kill_switch",
            f"The kill switch is engaged ({_state.kill_path(cfg)} exists), so no orders will be "
            f"sent: run `swing kill --off` when you are ready to trade again.",
        )
    return _ok("kill_switch", "The kill switch is off.")


# --------------------------------------------------------------------------
# 2. token age
# --------------------------------------------------------------------------


def token_age(cfg: Config, *, age_days: float | None) -> GuardrailResult:
    """Refuse on a token Schwab will no longer renew; warn on its last day.

    Args:
        cfg: the loaded configuration (used only for the token path in messages).
        age_days: the age of the token, from ``swing.broker.auth.token_age_days``.
            ``None`` means there is no token at all, which is also a refusal.
    """
    from swing.broker.auth import REAUTH_WARN_DAYS, REFRESH_TOKEN_LIFETIME_DAYS, token_path

    if age_days is None:
        return _refuse(
            "token_age",
            f"There is no Schwab token at {token_path(cfg)}, so no order can be sent: run "
            f"`swing auth` to log in.",
        )
    if age_days >= REFRESH_TOKEN_LIFETIME_DAYS:
        return _refuse(
            "token_age",
            f"The Schwab token is {age_days:.1f} days old and refresh tokens die after "
            f"{REFRESH_TOKEN_LIFETIME_DAYS:.0f} days, so it can no longer be renewed: run "
            f"`swing auth` to log in again.",
        )
    if age_days >= REAUTH_WARN_DAYS:
        return _warn(
            "token_age",
            f"The Schwab token is {age_days:.1f} days old — re-auth soon: run `swing auth` "
            f"before it hits {REFRESH_TOKEN_LIFETIME_DAYS:.0f} days and stops working.",
        )
    return _ok("token_age", f"The Schwab token is {age_days:.1f} days old.")


# --------------------------------------------------------------------------
# 3. trading hours
# --------------------------------------------------------------------------


def _to_eastern(moment: datetime) -> datetime:
    """Interpret ``moment`` in Eastern time; naive datetimes are taken as ET."""
    if moment.tzinfo is None:
        return moment
    try:
        from zoneinfo import ZoneInfo

        return moment.astimezone(ZoneInfo(MARKET_TZ))
    except Exception:  # pragma: no cover - only if tzdata is unavailable
        log.warning("Could not load the %s timezone; treating the clock as Eastern.", MARKET_TZ)
        return moment.replace(tzinfo=None)


def trading_hours(now: datetime) -> GuardrailResult:
    """Refuse outside 09:30–16:00 Eastern on a weekday.

    Holidays are not modelled: a market holiday simply means the order sits
    unfilled, whereas placing at 3am is a genuine mistake. Pass ``now``
    explicitly — this module never reads the clock itself.
    """
    eastern = _to_eastern(now)
    if eastern.weekday() >= 5:
        return _refuse(
            "trading_hours",
            f"{eastern:%A} is not a trading day, so nothing will be sent: run `swing execute` on "
            f"a weekday between {MARKET_OPEN:%H:%M} and {MARKET_CLOSE:%H:%M} Eastern.",
        )
    if not (MARKET_OPEN <= eastern.time() <= MARKET_CLOSE):
        return _refuse(
            "trading_hours",
            f"It is {eastern:%H:%M} Eastern, outside the {MARKET_OPEN:%H:%M}-"
            f"{MARKET_CLOSE:%H:%M} regular session, so nothing will be sent: run `swing execute` "
            f"during market hours.",
        )
    return _ok("trading_hours", f"It is {eastern:%H:%M} Eastern on a trading day.")


# --------------------------------------------------------------------------
# 4. backtest gate
# --------------------------------------------------------------------------


def gate_passed(cfg: Config) -> GuardrailResult:
    """Refuse unless the walk-forward backtest gate currently passes.

    Fails closed: if the gate module is missing, or raises, or answers with
    something unrecognisable, this refuses. The whole premise of the system is
    that no pick reaches the market without evidence behind it, so "I could not
    tell" has to mean "no".
    """
    try:
        from swing.backtest import gate
    except ImportError as exc:
        return _refuse(
            "gate_passed",
            f"The backtest gate is unavailable ({exc}), and picks are never traded without one: "
            f"run `swing backtest` to produce a walk-forward report the gate can read.",
        )
    checker = getattr(gate, "check", None)
    if not callable(checker):
        return _refuse(
            "gate_passed",
            "The backtest gate is unavailable (swing.backtest.gate defines no check()), and "
            "picks are never traded without one: reinstall with `uv sync` and run "
            "`swing backtest`.",
        )
    try:
        result = checker(cfg)
    except Exception as exc:
        return _refuse(
            "gate_passed",
            f"The backtest gate could not be evaluated ({exc}), so nothing will be sent: run "
            f"`swing backtest` and then `swing report` to see where it stands.",
        )
    if getattr(result, "passed", False):
        return _ok("gate_passed", "The walk-forward backtest gate passes.")
    reasons = [str(r) for r in (getattr(result, "reasons", None) or [])]
    detail = "; ".join(reasons) if reasons else "no reason was given"
    return _refuse(
        "gate_passed",
        f"The walk-forward backtest gate has not passed ({detail}), so no order will be sent: "
        f"run `swing report` to see the numbers, and only trade once the gate clears.",
    )


# --------------------------------------------------------------------------
# 5. quote drift
# --------------------------------------------------------------------------


def quote_drift(pick: Mapping[str, Any], quote: float | None, cfg: Config) -> GuardrailResult:
    """Refuse when the market has moved away from the price the scan planned on.

    Args:
        pick: the pick record from ``picks.json``; ``entry`` and ``atr`` are read.
        quote: the current price, already fetched by the caller. ``None`` means
            no quote could be had, which is itself a refusal.
        cfg: supplies ``execution.max_quote_drift_atr`` and ``max_quote_drift_pct``.
    """
    symbol = str(pick.get("symbol", "?")).upper()
    entry = float(pick.get("entry", 0.0) or 0.0)
    atr = float(pick.get("atr", 0.0) or 0.0)

    if quote is None:
        return _refuse(
            "quote_drift",
            f"No live quote for {symbol} could be fetched, so the scan's entry price cannot be "
            f"checked against the market: fix the data provider, or place this order by hand.",
        )
    if entry <= 0:
        return _refuse(
            "quote_drift",
            f"The scan recorded no entry price for {symbol}, so drift cannot be measured: re-run "
            f"`swing scan` and execute against a fresh report.",
        )

    drift = abs(quote - entry)
    drift_pct = drift / entry * 100.0
    atr_limit = cfg.execution.max_quote_drift_atr * atr
    pct_limit = cfg.execution.max_quote_drift_pct

    if atr > 0 and drift > atr_limit:
        return _refuse(
            "quote_drift",
            f"{symbol} is at {quote:.2f} but the scan planned an entry at {entry:.2f}, a move of "
            f"{drift:.2f} against a limit of {atr_limit:.2f} "
            f"({cfg.execution.max_quote_drift_atr:g} ATR): re-run `swing scan` so the stop and "
            f"size match today's price.",
        )
    if drift_pct > pct_limit:
        return _refuse(
            "quote_drift",
            f"{symbol} is at {quote:.2f} but the scan planned an entry at {entry:.2f}, "
            f"{drift_pct:.1f}% away against a limit of {pct_limit:g}%: re-run `swing scan` so the "
            f"stop and size match today's price.",
        )
    return _ok(
        "quote_drift",
        f"{symbol} is at {quote:.2f}, {drift_pct:.1f}% from the planned {entry:.2f}.",
    )


# --------------------------------------------------------------------------
# 6. orders per day
# --------------------------------------------------------------------------


def _order_day(order: Mapping[str, Any]) -> str:
    for key in ("date", "placed_at", "submitted_at", "entered_at"):
        value = order.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return ""


def orders_placed_on(journal: Journal, day: date) -> list[dict[str, Any]]:
    """Every order the journal recorded on ``day``."""
    target = day.isoformat()
    return [o for o in journal.orders if _order_day(o) == target]


def orders_today(journal: Journal, cfg: Config, *, today: date) -> GuardrailResult:
    """Refuse once the day's order budget is spent."""
    limit = int(cfg.execution.max_orders_per_day)
    already = len(orders_placed_on(journal, today))
    if limit <= 0:
        return _refuse(
            "orders_today",
            "execution.max_orders_per_day is 0, so no order may be sent today: raise it in the "
            "[execution] section of your config.toml if you meant to trade.",
        )
    if already >= limit:
        return _refuse(
            "orders_today",
            f"{already} order(s) have already been sent today and execution.max_orders_per_day "
            f"is {limit}, so nothing more will be sent: trade the rest tomorrow, or raise the "
            f"limit in the [execution] section of your config.toml.",
        )
    return _ok("orders_today", f"{already} of {limit} orders used today.")


# --------------------------------------------------------------------------
# 7. new exposure
# --------------------------------------------------------------------------


def new_exposure(
    cfg: Config,
    *,
    existing_notional: float,
    proposed_notional: float,
    equity: float,
) -> GuardrailResult:
    """Refuse when today's new commitments exceed the daily exposure budget.

    ``existing_notional`` is what has already been committed *today* (orders in
    the journal plus anything approved earlier in this run), not the whole
    portfolio: the knob is called ``max_new_exposure_pct`` because it caps how
    fast money goes to work, not how much is invested overall.
    """
    if equity <= 0:
        return _refuse(
            "new_exposure",
            "Account equity is zero or unknown, so the exposure limit cannot be applied: set "
            "account.equity in your config.toml to what the account is actually worth.",
        )
    budget = equity * cfg.execution.max_new_exposure_pct / 100.0
    total = existing_notional + proposed_notional
    if total > budget + 1e-9:
        return _refuse(
            "new_exposure",
            f"This order would commit ${total:,.2f} of new money today against a limit of "
            f"${budget:,.2f} ({cfg.execution.max_new_exposure_pct:g}% of ${equity:,.2f}): send "
            f"fewer orders today, or raise execution.max_new_exposure_pct in your config.toml.",
        )
    return _ok(
        "new_exposure",
        f"${total:,.2f} of ${budget:,.2f} new exposure used today.",
    )


# --------------------------------------------------------------------------
# 8. limit orders only
# --------------------------------------------------------------------------


def _order_types(node: Any) -> list[str]:
    """Every ``orderType`` value anywhere in an order structure."""
    found: list[str] = []
    if isinstance(node, Mapping):
        value = node.get("orderType")
        if isinstance(value, str):
            found.append(value.strip().upper())
        for child in node.values():
            found.extend(_order_types(child))
    elif isinstance(node, list | tuple):
        for child in node:
            found.extend(_order_types(child))
    return found


def limit_only(order: Mapping[str, Any]) -> GuardrailResult:
    """Refuse anything that is not a LIMIT parent, and any MARKET leg at all.

    A market order on a thin small-cap is how a swing trade turns into a
    donation, so this system never sends one — not as the parent, not as a
    child, not nested three levels down in a TRIGGER structure.
    """
    types = _order_types(order)
    if not types:
        return _refuse(
            "limit_only",
            "This order has no orderType field at all, so it cannot be confirmed as a limit "
            "order: re-run `swing scan` to redraft it.",
        )
    market = sorted({t for t in types if t in _MARKET_ORDER_TYPES})
    if market:
        return _refuse(
            "limit_only",
            f"This order contains a {', '.join(market)} leg and this system only ever sends "
            f"limit orders: re-run `swing scan` to redraft it, or place it by hand in the Schwab "
            f"app if you really want a market fill.",
        )
    parent = types[0]
    if parent != "LIMIT":
        return _refuse(
            "limit_only",
            f"The parent order is a {parent} order, but entries must be LIMIT so the fill price "
            f"is the one the scan sized the position on: re-run `swing scan` to redraft it.",
        )
    return _ok("limit_only", f"Parent is a LIMIT order ({len(types)} leg(s) checked).")


# --------------------------------------------------------------------------
# 9. duplicate suppression
# --------------------------------------------------------------------------


def duplicate(
    journal: Journal,
    symbol: str,
    *,
    asof: date,
    within_days: int = DEFAULT_DEDUPE_DAYS,
) -> GuardrailResult:
    """Refuse a symbol we already hold, already have working, or just traded.

    ``asof`` is the execution day. The "recently picked" test deliberately looks
    at the days *before* ``asof``, because today's own pick is in the journal by
    the time the executor runs and must not block itself.
    """
    sym = symbol.strip().upper()

    for order in journal.open_orders():
        if str(order.get("symbol", "")).strip().upper() == sym:
            return _refuse(
                "duplicate",
                f"An order for {sym} is already working at the broker, so a second one will not "
                f"be sent: check `swing positions`, and cancel the old order in the Schwab app "
                f"if it is stale.",
            )

    for position in journal.positions():
        if str(position.get("symbol", "")).strip().upper() == sym:
            return _refuse(
                "duplicate",
                f"{sym} is already an open position in the journal, so it will not be bought "
                f"again: close it first, or run `swing positions` to reconcile.",
            )

    if within_days > 0 and journal.recently_picked(sym, within_days, asof=asof - timedelta(days=1)):
        return _refuse(
            "duplicate",
            f"{sym} was already picked within the last {within_days} days, so it will not be "
            f"re-entered so soon: wait for the cooling-off window to pass, or place it by hand "
            f"if you have a reason.",
        )
    return _ok("duplicate", f"{sym} is not held, working, or recently picked.")


# --------------------------------------------------------------------------
# 10. reconciliation
# --------------------------------------------------------------------------


def _symbol_set(symbols: Collection[str] | None) -> set[str]:
    return {str(s).strip().upper() for s in (symbols or []) if str(s).strip()}


def reconciliation(
    *,
    live_symbols: Collection[str] | None,
    journal_symbols: Collection[str] | None,
    acknowledged: bool = False,
) -> GuardrailResult:
    """Refuse when the broker and the journal disagree about what is held.

    If the two views differ, one of them is wrong, and placing orders against a
    wrong view of the portfolio is how position limits get quietly broken. Pass
    ``acknowledged=True`` to downgrade a known, understood difference to a
    warning.
    """
    if live_symbols is None:
        return _refuse(
            "reconciliation",
            "Live positions could not be read from Schwab, so the journal cannot be reconciled "
            "against the account: check `swing auth --check` before trading.",
        )
    live = _symbol_set(live_symbols)
    local = _symbol_set(journal_symbols)
    only_broker = sorted(live - local)
    only_local = sorted(local - live)
    if not only_broker and not only_local:
        return _ok("reconciliation", f"Broker and journal agree on {len(live)} position(s).")

    parts: list[str] = []
    if only_broker:
        parts.append(f"held at Schwab but not in the journal: {', '.join(only_broker)}")
    if only_local:
        parts.append(f"in the journal but not at Schwab: {', '.join(only_local)}")
    detail = "; ".join(parts)
    if acknowledged:
        return _warn(
            "reconciliation",
            f"Positions differ ({detail}) but the difference was acknowledged, so execution "
            f"continues — reconcile it before the next run.",
        )
    return _refuse(
        "reconciliation",
        f"The broker and the journal disagree about open positions ({detail}), so nothing will "
        f"be sent: run `swing positions` and settle the difference before executing.",
    )


# --------------------------------------------------------------------------
# 11. equity mismatch
# --------------------------------------------------------------------------


def equity_mismatch(
    *,
    config_equity: float,
    live_equity: float | None,
    tolerance_pct: float = EQUITY_MISMATCH_TOLERANCE_PCT,
) -> GuardrailResult:
    """Refuse when config equity and the real account balance have diverged.

    Position sizes are computed from ``account.equity``. If that number is
    stale, every size in today's report is wrong, so the sizing has to be
    redone before anything is sent.
    """
    if live_equity is None:
        return _refuse(
            "equity_mismatch",
            "The account balance could not be read from Schwab, so the equity used for position "
            "sizing cannot be verified: run `swing auth --check` before trading.",
        )
    if config_equity <= 0:
        return _refuse(
            "equity_mismatch",
            "account.equity in your config.toml is zero or negative, so no position can be "
            "sized: set it to what the account is actually worth.",
        )
    diff_pct = abs(config_equity - live_equity) / config_equity * 100.0
    if diff_pct > tolerance_pct:
        return _refuse(
            "equity_mismatch",
            f"config.toml says the account holds ${config_equity:,.2f} but Schwab reports "
            f"${live_equity:,.2f}, a {diff_pct:.0f}% difference (limit {tolerance_pct:g}%), and "
            f"every position size was computed from the config figure: update account.equity and "
            f"re-run `swing scan`.",
        )
    return _ok(
        "equity_mismatch",
        f"Config equity ${config_equity:,.2f} is within {diff_pct:.0f}% of Schwab's "
        f"${live_equity:,.2f}.",
    )


# --------------------------------------------------------------------------
# 12. stale scan
# --------------------------------------------------------------------------


def _trading_days_between(start: date, end: date) -> int:
    """Weekdays strictly after ``start``, up to and including ``end``."""
    if end <= start:
        return 0
    days = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def stale_scan(*, scan_date: date, today: date) -> GuardrailResult:
    """Refuse a scan report older than one trading day.

    A scan run on Friday evening may be executed on Monday; a scan from last
    week may not, because its stops and sizes were computed against prices that
    no longer exist. Market holidays are not modelled, which errs on the side
    of refusing.
    """
    if scan_date > today:
        return _refuse(
            "stale_scan",
            f"The scan report is dated {scan_date.isoformat()}, which is in the future compared "
            f"with {today.isoformat()}: check your system clock, then re-run `swing scan`.",
        )
    elapsed = _trading_days_between(scan_date, today)
    if elapsed > 1:
        return _refuse(
            "stale_scan",
            f"The latest scan is from {scan_date.isoformat()}, {elapsed} trading days ago, and "
            f"its entries and stops were sized against prices that have moved: run `swing scan` "
            f"again before executing.",
        )
    return _ok(
        "stale_scan",
        f"The scan from {scan_date.isoformat()} is {elapsed} trading day(s) old.",
    )


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------


def run_guardrails(
    cfg: Config,
    *,
    now: datetime,
    journal: Journal,
    scan_date: date,
    token_age_days: float | None,
    live_symbols: Collection[str] | None = None,
    live_equity: float | None = None,
    acknowledged: bool = False,
    dry_run: bool = False,
) -> list[GuardrailResult]:
    """Run every account-level guardrail, in the order a human would check them.

    Args:
        cfg: the loaded configuration.
        now: the current moment (Eastern, or tz-aware). Never read from a clock here.
        journal: the local journal, for today's order count.
        scan_date: the date of the scan report being executed.
        token_age_days: from ``swing.broker.auth.token_age_days``.
        live_symbols: symbols the broker says are held; ``None`` when unknown.
        live_equity: the broker's account value; ``None`` when unknown.
        acknowledged: downgrade a known reconciliation difference to a warning.
        dry_run: when True, checks that need live data report SKIP instead of
            refusing, because in a dry run nobody fetched that data.

    Returns:
        One :class:`GuardrailResult` per check. Ask :func:`all_clear` whether
        anything may be sent.
    """
    results = [
        kill_switch(cfg),
        token_age(cfg, age_days=token_age_days),
        trading_hours(now),
        gate_passed(cfg),
        stale_scan(scan_date=scan_date, today=_to_eastern(now).date()),
        orders_today(journal, cfg, today=_to_eastern(now).date()),
    ]

    if dry_run and live_symbols is None:
        results.append(
            skipped(
                "reconciliation",
                "Not checked in a dry run: comparing Schwab's positions with the journal needs a "
                "live connection.",
            )
        )
    else:
        results.append(
            reconciliation(
                live_symbols=live_symbols,
                journal_symbols=[p.get("symbol", "") for p in journal.positions()],
                acknowledged=acknowledged,
            )
        )

    if dry_run and live_equity is None:
        results.append(
            skipped(
                "equity_mismatch",
                "Not checked in a dry run: comparing config equity with the account balance needs "
                "a live connection.",
            )
        )
    else:
        results.append(equity_mismatch(config_equity=cfg.account.equity, live_equity=live_equity))
    return results


def run_order_guardrails(
    cfg: Config,
    *,
    pick: Mapping[str, Any],
    order: Mapping[str, Any],
    quote: float | None,
    journal: Journal,
    asof: date,
    existing_notional: float,
    proposed_notional: float,
    equity: float,
    dedupe_days: int = DEFAULT_DEDUPE_DAYS,
    dry_run: bool = False,
) -> list[GuardrailResult]:
    """Run every per-order guardrail for one drafted order.

    In a dry run without a quote, ``quote_drift`` reports SKIP rather than
    refusing: the dry run must work with no network at all, and pretending we
    checked would be worse than saying we did not.
    """
    symbol = str(pick.get("symbol", "?")).upper()
    results = [limit_only(order)]

    if dry_run and quote is None:
        results.append(
            skipped(
                "quote_drift",
                f"Not checked in a dry run: comparing {symbol} against the scan's entry price "
                f"needs a live quote.",
            )
        )
    else:
        results.append(quote_drift(pick, quote, cfg))

    results.append(duplicate(journal, symbol, asof=asof, within_days=dedupe_days))
    results.append(
        new_exposure(
            cfg,
            existing_notional=existing_notional,
            proposed_notional=proposed_notional,
            equity=equity,
        )
    )
    return results
