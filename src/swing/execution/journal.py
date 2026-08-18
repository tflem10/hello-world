"""Append-only trade journal.

Every order this system drafts, places, rejects or fills gets one JSON line in
``~/.swing/journal.jsonl``. Append-only is the point: the journal is the record
that survives a crashed process, a container restart, or a config edit, and it
is what duplicate-suppression and reconciliation are checked against.

The journal is also how the nightly scan knows what you already hold. If you
place orders by hand (v1), record them with :func:`record_entry` — otherwise
the scan will happily suggest a fifth position while you hold four, and the
sheet will say so.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("swing.journal")

EVENT_DRAFTED = "drafted"
EVENT_PLACED = "placed"
EVENT_REJECTED = "rejected"
EVENT_ENTRY = "entry"
EVENT_EXIT = "exit"
EVENT_STOP_MOVED = "stop_moved"
EVENT_NOTE = "note"
EVENT_KILL = "kill"


@dataclass
class OpenPosition:
    symbol: str
    shares: int
    entry_price: float
    entry_date: str
    stop: float
    trail_offset: float = 0.0
    order_id: str | None = None
    note: str = ""

    @property
    def cost_basis(self) -> float:
        return self.shares * self.entry_price

    def days_held(self, as_of: date | None = None) -> int:
        try:
            entered = date.fromisoformat(self.entry_date[:10])
        except (TypeError, ValueError):
            return 0
        return ((as_of or date.today()) - entered).days


def journal_path(cfg: Config) -> Path:
    return cfg.expand_path(cfg.execution.journal_path)


def record(cfg: Config, event_type: str, **fields: Any) -> dict:
    """Append one event. Never raises — losing a run to a full disk is worse
    than losing a log line, and the caller has already done the real work."""
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "type": event_type,
        **fields,
    }
    path = journal_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        log.error("could not write to the journal at %s: %s", path, exc)
    return payload


def read_events(cfg: Config) -> list[dict]:
    path = journal_path(cfg)
    if not path.exists():
        return []
    events = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("journal line %d is not valid JSON; skipping", line_no)
    return events


def record_entry(
    cfg: Config,
    symbol: str,
    shares: int,
    price: float,
    stop: float,
    trail_offset: float = 0.0,
    order_id: str | None = None,
    note: str = "",
) -> dict:
    return record(
        cfg, EVENT_ENTRY, symbol=symbol.upper(), shares=int(shares), price=float(price),
        stop=float(stop), trail_offset=float(trail_offset), order_id=order_id, note=note,
    )


def record_exit(
    cfg: Config, symbol: str, shares: int, price: float, reason: str = "", order_id=None
) -> dict:
    return record(
        cfg, EVENT_EXIT, symbol=symbol.upper(), shares=int(shares), price=float(price),
        reason=reason, order_id=order_id,
    )


def open_positions(cfg: Config) -> dict[str, OpenPosition]:
    """Replay the journal into current holdings.

    Partial exits reduce the share count; a full exit removes the position. A
    second entry in the same symbol averages into the existing one, which is
    what a broker would show.
    """
    positions: dict[str, OpenPosition] = {}
    for event in read_events(cfg):
        kind = event.get("type")
        symbol = str(event.get("symbol", "")).upper()
        if not symbol:
            continue

        if kind == EVENT_ENTRY:
            shares = int(event.get("shares", 0))
            price = float(event.get("price", 0.0))
            if shares < 1 or price <= 0:
                continue
            existing = positions.get(symbol)
            if existing is None:
                positions[symbol] = OpenPosition(
                    symbol=symbol,
                    shares=shares,
                    entry_price=price,
                    entry_date=str(event.get("ts", ""))[:10],
                    stop=float(event.get("stop", 0.0)),
                    trail_offset=float(event.get("trail_offset", 0.0)),
                    order_id=event.get("order_id"),
                    note=str(event.get("note", "")),
                )
            else:
                total = existing.shares + shares
                existing.entry_price = (
                    existing.cost_basis + shares * price
                ) / total
                existing.shares = total
                existing.stop = float(event.get("stop", existing.stop))

        elif kind == EVENT_EXIT:
            existing = positions.get(symbol)
            if existing is None:
                continue
            existing.shares -= int(event.get("shares", existing.shares))
            if existing.shares <= 0:
                positions.pop(symbol, None)

        elif kind == EVENT_STOP_MOVED:
            existing = positions.get(symbol)
            if existing is not None:
                existing.stop = float(event.get("stop", existing.stop))
                existing.trail_offset = float(
                    event.get("trail_offset", existing.trail_offset)
                )

    return positions


def placed_today(cfg: Config, when: date | None = None) -> list[dict]:
    """Orders already transmitted today — the input to the daily-count guardrail
    and to duplicate suppression."""
    when = when or date.today()
    stamp = when.isoformat()
    return [
        e for e in read_events(cfg)
        if e.get("type") == EVENT_PLACED and str(e.get("ts", "")).startswith(stamp)
    ]


def print_positions(cfg: Config) -> int:
    positions = open_positions(cfg)
    path = journal_path(cfg)
    if not positions:
        print(f"no open positions recorded in {path}")
        print(
            "\nIf you placed orders by hand, record them so the scanner knows:\n"
            "  python -c \"from swing.config import load_config; "
            "from swing.execution.journal import record_entry; "
            "record_entry(load_config(), 'AAPL', 10, 190.50, 182.00)\""
        )
        return 0

    print(f"open positions (from {path}):")
    print(f"  {'symbol':<8}{'shares':>8}{'entry':>10}{'stop':>10}{'risk':>10}  since")
    total_risk = 0.0
    for pos in sorted(positions.values(), key=lambda p: p.symbol):
        risk = max(pos.entry_price - pos.stop, 0.0) * pos.shares
        total_risk += risk
        print(
            f"  {pos.symbol:<8}{pos.shares:>8}{pos.entry_price:>10.2f}"
            f"{pos.stop:>10.2f}{risk:>10.2f}  {pos.entry_date}"
        )
    equity = float(cfg.account.equity)
    print(
        f"\n  {len(positions)} position(s), ${total_risk:,.2f} at risk "
        f"({total_risk / equity:.1%} of ${equity:,.2f} equity) if every stop fills"
    )
    return 0
