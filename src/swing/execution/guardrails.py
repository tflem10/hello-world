"""Guardrails: every reason to refuse to place an order.

This module exists because the failure mode of automated execution is not
"places a slightly worse trade". It is "places forty trades at 3am into a
halted stock because a config edit had an extra zero". Each check below is one
such story, and each one is a hard block, not a warning.

The checks, and what each is actually defending against:

``kill switch``
    ``swing kill`` (or touching ``~/.swing/KILL``) stops everything, without
    needing the network, the config, or the API. First thing checked.
``master switch and --live``
    Two independent affirmations — ``execution.enabled = true`` in the config
    *and* ``--live`` on the command line. Either alone does nothing. Nobody
    auto-executes by fat-fingering one flag.
``token freshness``
    An expired Schwab token means no orders. Data falls back to yfinance;
    order placement does not fall back to anything.
``trading hours``
    Do not transmit at 03:00. Schwab would reject it, but a rejected order at
    3am is a page you did not need.
``sheet freshness``
    Refuse to act on a pick sheet from last Tuesday. Prices have moved.
``equity drift``
    If the broker's balance disagrees with ``account.equity`` by more than
    ``stale_equity_tolerance_pct``, every share count on the sheet was computed
    from a stale number. Stop and re-scan.
``quote drift``
    If the price has moved more than 1 ATR (or 3%) since the scan, the trade
    you are about to place is not the trade that was analysed.
``duplicate suppression``
    The journal is checked for the same symbol placed today. Re-running
    ``swing execute`` after a timeout must not double the position.
``daily order cap``
    ``max_orders_per_day``. A bug that generates orders in a loop stops at 3.
``new-exposure cap``
    ``max_new_exposure_pct`` of equity deployed in one day, total.
``reconciliation``
    Live broker positions are diffed against the journal before anything is
    placed. If they disagree, the journal is wrong and so is every slot
    calculation built on it.
``limit orders only``
    Market orders are never transmitted, at any point in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("swing.guardrails")


@dataclass
class Guard:
    name: str
    passed: bool
    detail: str = ""

    def line(self) -> str:
        return f"  [{'ok  ' if self.passed else 'BLOCK'}] {self.name:<20} {self.detail}"


@dataclass
class GuardReport:
    guards: list[Guard] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> Guard:
        guard = Guard(name, passed, detail)
        self.guards.append(guard)
        return guard

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.guards)

    @property
    def blockers(self) -> list[Guard]:
        return [g for g in self.guards if not g.passed]

    def describe(self) -> str:
        head = "guardrails: PASS" if self.passed else "guardrails: BLOCKED"
        return "\n".join([head, *(g.line() for g in self.guards)])


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------
def kill_file(cfg: Config) -> Path:
    return cfg.expand_path(cfg.execution.kill_file)


def kill_switch_engaged(cfg: Config) -> bool:
    return kill_file(cfg).exists()


def set_kill_switch(cfg: Config) -> int:
    path = kill_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"engaged {datetime.now().isoformat(timespec='seconds')}\n"
        "No orders will be placed while this file exists.\n"
        "Release with: swing kill --release\n"
    )
    from .journal import EVENT_KILL, record

    record(cfg, EVENT_KILL, action="engaged")
    print(f"KILL SWITCH ENGAGED — {path}")
    print("No orders will be placed. Release with `swing kill --release`.")
    print(
        "\nThis does NOT cancel orders already resting at Schwab. "
        "Cancel those in thinkorswim or the Schwab app."
    )
    return 0


def release_kill_switch(cfg: Config) -> int:
    path = kill_file(cfg)
    if not path.exists():
        print(f"kill switch is not engaged ({path} does not exist)")
        return 0
    path.unlink()
    from .journal import EVENT_KILL, record

    record(cfg, EVENT_KILL, action="released")
    print(f"kill switch released ({path} removed)")
    return 0


# ---------------------------------------------------------------------------
# pre-flight
# ---------------------------------------------------------------------------
def preflight(
    cfg: Config,
    live: bool,
    sheet,
    account_equity: float | None = None,
    broker_positions: dict[str, int] | None = None,
    now: datetime | None = None,
) -> GuardReport:
    """Account-level checks, run once before any order is considered."""
    report = GuardReport()
    now = now or datetime.now()

    engaged = kill_switch_engaged(cfg)
    report.add(
        "kill switch", not engaged,
        f"engaged at {kill_file(cfg)}" if engaged else "clear",
    )

    enabled = bool(cfg.execution.get("enabled", False))
    report.add(
        "execution.enabled", enabled,
        "set [execution] enabled = true to arm the executor" if not enabled else "armed",
    )
    report.add(
        "--live flag", live,
        "dry run — nothing will be transmitted" if not live else "live",
    )

    if str(cfg.data.provider).lower() == "schwab" or enabled:
        from ..auth import token_status

        status = token_status(cfg, now=now)
        if not status.exists:
            report.add("schwab token", False, "no token — run `swing auth`")
        elif status.expired:
            report.add(
                "schwab token", False,
                f"expired {abs(status.days_left):.1f} days ago — run `swing auth`",
            )
        else:
            report.add("schwab token", True, f"{status.days_left:.1f} day(s) left")

    if bool(cfg.execution.get("trading_hours_only", True)):
        from ..data.schwab_provider import market_is_open

        # market_is_open() converts to US/Eastern; a naive `now` is read as
        # local time, which is what a machine-local scheduler produces.
        is_open = market_is_open(now)
        report.add(
            "trading hours", is_open,
            "the market is closed" if not is_open else "market open",
        )

    from ..confirm import sheet_is_for_today

    fresh = sheet_is_for_today(sheet, now.date())
    report.add(
        "sheet freshness", fresh,
        f"sheet is dated {sheet.as_of}" + ("" if fresh else " — re-run `swing scan`"),
    )

    if sheet.config_hash != cfg.hash:
        report.add(
            "config match", False,
            f"the sheet was produced under config {sheet.config_hash}, but the "
            f"current config hashes to {cfg.hash}. Re-run `swing scan`.",
        )
    else:
        report.add("config match", True, cfg.hash)

    if not sheet.gate_passed:
        report.add(
            "backtest gate", False,
            "the sheet was produced with the gate BLOCKED (--force). "
            "Automated execution of an unvalidated strategy is refused.",
        )
    else:
        report.add("backtest gate", True, "sheet passed the gate")

    if account_equity is not None:
        configured = float(cfg.account.equity)
        tolerance = float(cfg.account.get("stale_equity_tolerance_pct", 0.20))
        drift = abs(account_equity - configured) / configured if configured else 1.0
        ok = drift <= tolerance
        report.add(
            "equity match", ok,
            f"config ${configured:,.2f} vs broker ${account_equity:,.2f} "
            f"({drift:.1%} apart, limit {tolerance:.0%})"
            + ("" if ok else " — every share count was sized off a stale number"),
        )

    if broker_positions is not None:
        report.add(*_reconcile(cfg, broker_positions))

    from .journal import placed_today

    already = placed_today(cfg, now.date())
    cap = int(cfg.execution.get("max_orders_per_day", 3))
    report.add(
        "daily order cap", len(already) < cap,
        f"{len(already)} placed today, cap {cap}",
    )

    return report


def _reconcile(cfg: Config, broker_positions: dict[str, int]) -> tuple[str, bool, str]:
    """Diff live broker positions against the journal.

    A disagreement means the journal is wrong, which means the free-slot count,
    the available cash and the duplicate check are all wrong too. Better to
    stop and have the human sort it out.
    """
    from .journal import open_positions

    journal = {s: p.shares for s, p in open_positions(cfg).items()}
    broker = {s.upper(): int(q) for s, q in broker_positions.items() if int(q) != 0}

    only_broker = {s: q for s, q in broker.items() if journal.get(s) != q}
    only_journal = {s: q for s, q in journal.items() if broker.get(s) != q}

    if not only_broker and not only_journal:
        return ("reconciliation", True, f"{len(broker)} position(s) agree")

    bits = []
    if only_broker:
        bits.append(
            "broker has " + ", ".join(f"{s}x{q}" for s, q in sorted(only_broker.items()))
        )
    if only_journal:
        bits.append(
            "journal has " + ", ".join(f"{s}x{q}" for s, q in sorted(only_journal.items()))
        )
    return (
        "reconciliation",
        False,
        "; ".join(bits) + " — fix the journal before executing",
    )


# ---------------------------------------------------------------------------
# per-order
# ---------------------------------------------------------------------------
def check_order(
    cfg: Config,
    pick,
    quote_price: float | None,
    new_exposure_so_far: float,
    equity: float,
    orders_placed_today: int,
    now: datetime | None = None,
) -> GuardReport:
    """Checks for one specific order, given everything already committed today."""
    report = GuardReport()
    now = now or datetime.now()

    report.add(
        "shares", pick.shares >= 1,
        f"{pick.shares} share(s)" + ("" if pick.shares >= 1 else " — nothing to buy"),
    )
    report.add(
        "has orders", bool(pick.orders),
        "drafted" if pick.orders else "no drafted order attached",
    )
    report.add(
        "status", pick.tradable,
        pick.status + ("" if pick.tradable else " — not a tradable pick"),
    )

    from .journal import EVENT_PLACED, read_events

    today = now.date().isoformat()
    duplicate = any(
        e.get("type") == EVENT_PLACED
        and str(e.get("symbol", "")).upper() == pick.symbol.upper()
        and str(e.get("ts", "")).startswith(today)
        for e in read_events(cfg)
    )
    report.add(
        "duplicate", not duplicate,
        f"{pick.symbol} was already placed today" if duplicate else "not placed today",
    )

    from .journal import open_positions

    held = pick.symbol.upper() in open_positions(cfg)
    report.add(
        "not already held", not held,
        f"{pick.symbol} is already an open position" if held else "flat",
    )

    cap = int(cfg.execution.get("max_orders_per_day", 3))
    report.add(
        "daily order cap", orders_placed_today < cap,
        f"{orders_placed_today}/{cap} placed today",
    )

    exposure_cap = float(cfg.execution.get("max_new_exposure_pct", 1.0)) * equity
    would_be = new_exposure_so_far + pick.notional
    report.add(
        "new exposure cap", would_be <= exposure_cap,
        f"${would_be:,.2f} of ${exposure_cap:,.2f} after this order",
    )

    if quote_price is None:
        report.add("quote", False, "no live quote — refusing to place blind")
    else:
        reference = pick.confirm_price or pick.close
        drift = quote_price - reference
        drift_pct = drift / reference if reference else 1.0
        drift_atr = drift / pick.atr if pick.atr else 0.0
        max_atr = float(cfg.execution.get("max_quote_drift_atr", 1.0))
        max_pct = float(cfg.execution.get("max_quote_drift_pct", 0.03))
        ok = abs(drift_atr) <= max_atr and abs(drift_pct) <= max_pct
        report.add(
            "quote drift", ok,
            f"{quote_price:.2f} vs {reference:.2f} "
            f"({drift_pct:+.2%}, {drift_atr:+.2f} ATR; limits {max_pct:.0%} / {max_atr:g} ATR)",
        )

    for name, order in (pick.orders or {}).items():
        if order.get("orderType") == "MARKET":
            report.add("no market orders", False, f"{name} is a MARKET order")
            break
    else:
        report.add("no market orders", True, "limit only")

    return report


def choose_order_variant(cfg: Config, pick) -> tuple[str, dict] | None:
    """Pick the drafted variant named by ``execution.child_stop_type``."""
    mapping = {
        "stop": "bracket_stop",
        "stop_limit": "bracket_stop_limit",
        "trailing_stop": "bracket_trailing",
    }
    wanted = str(cfg.execution.get("child_stop_type", "stop"))
    key = mapping.get(wanted)
    if key is None:
        log.error(
            "unknown execution.child_stop_type %r (expected one of %s)",
            wanted, ", ".join(mapping),
        )
        return None
    order = (pick.orders or {}).get(key)
    if order is None:
        log.error("%s has no %s order drafted", pick.symbol, key)
        return None
    return key, order


def today_orders_placed(cfg: Config, when: date | None = None) -> int:
    from .journal import placed_today

    return len(placed_today(cfg, when))
