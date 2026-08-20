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
import math
import os
import sys
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

# One attempt to transmit an order writes an EVENT_PLACED with STATUS_SUBMITTING
# before the API call and, if the broker takes it, a second one with
# STATUS_ACCEPTED after. Two events, one order: anything counting orders has to
# know that, which is why the writer (``executor._place``) and the counter
# (``guardrails.today_orders_placed``) share these names rather than repeating
# the strings.
STATUS_SUBMITTING = "submitting"
STATUS_ACCEPTED = "accepted"


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


# One ``swing execute`` re-reads the journal twice per order (duplicate
# suppression, open positions) plus a few times for the account-level checks.
# The file is append-only, so its (mtime_ns, size) identifies its contents: an
# append changes the size and a rewrite changes the mtime, which means
# :func:`record` invalidates this cache without having to know it exists.
_EVENT_CACHE: dict[Path, tuple[tuple[int, int], list[dict]]] = {}


def read_events(cfg: Config) -> list[dict]:
    path = journal_path(cfg)
    try:
        stat = path.stat()
    except OSError:
        _EVENT_CACHE.pop(path, None)
        return []

    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _EVENT_CACHE.get(path)
    if cached is None or cached[0] != stamp:
        cached = (stamp, _parse_events(path))
        _EVENT_CACHE[path] = cached
    # A fresh list every call, as an uncached read gave: the cached parse must
    # not be reachable through something a caller is free to append to.
    return list(cached[1])


def _parse_events(path: Path) -> list[dict]:
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
    """The raw EVENT_PLACED lines for one day.

    Lines, not orders: one attempt writes up to two of them (see
    STATUS_SUBMITTING). Duplicate suppression only asks whether a symbol
    appears here at all, so it can read this directly; anything that *counts*
    must go through :func:`guardrails.today_orders_placed`.
    """
    when = when or date.today()
    stamp = when.isoformat()
    return [
        e for e in read_events(cfg)
        if e.get("type") == EVENT_PLACED and str(e.get("ts", "")).startswith(stamp)
    ]


POSITION_HEADER = f"  {'symbol':<8}{'shares':>8}{'entry':>10}{'stop':>10}{'risk':>10}  since"


def position_risk(pos: OpenPosition) -> float:
    """Dollars lost if the stop fills. Never negative: a stop above the entry
    is a locked-in gain, not negative risk."""
    return max(pos.entry_price - pos.stop, 0.0) * pos.shares


def position_line(pos: OpenPosition) -> str:
    """One position as a row under :data:`POSITION_HEADER`."""
    return (
        f"  {pos.symbol:<8}{pos.shares:>8}{pos.entry_price:>10.2f}"
        f"{pos.stop:>10.2f}{position_risk(pos):>10.2f}  {pos.entry_date}"
    )


def print_positions(cfg: Config) -> int:
    positions = open_positions(cfg)
    path = journal_path(cfg)
    if not positions:
        print(f"no open positions recorded in {path}")
        print(
            "\nIf you placed orders by hand, record them so the scanner knows:\n"
            "  swing journal add AAPL 10 190.50 182.00"
        )
        return 0

    print(f"open positions (from {path}):")
    print(POSITION_HEADER)
    total_risk = 0.0
    for pos in sorted(positions.values(), key=lambda p: p.symbol):
        total_risk += position_risk(pos)
        print(position_line(pos))
    equity = float(cfg.account.equity)
    print(
        f"\n  {len(positions)} position(s), ${total_risk:,.2f} at risk "
        f"({total_risk / equity:.1%} of ${equity:,.2f} equity) if every stop fills"
    )
    return 0


# ---------------------------------------------------------------------------
# ``swing journal add|exit|stop|show``
#
# Recording a hand-placed trade used to mean an inline ``python -c`` one-liner,
# which is exactly the kind of friction that ends with an unrecorded position
# and a scanner that thinks you are flat. These four functions are the whole
# CLI surface; ``commands.cmd_journal`` only unpacks argparse into them.
#
# Every one of them validates *before* it writes. The journal is append-only,
# so a bad line cannot be taken back — it can only be argued with by a later
# event. Refusing up front is the only real correction mechanism there is.
# ---------------------------------------------------------------------------


class JournalInputError(ValueError):
    """An argument the journal refuses to record. Carries the user-facing text."""


def _fail(exc: Exception) -> int:
    """One line on stderr, exit code 2, nothing written to the journal."""
    print(f"error: {exc}", file=sys.stderr)
    return 2


def _parse_symbol(raw: Any) -> str:
    symbol = str(raw or "").strip().upper()
    if not symbol:
        raise JournalInputError("symbol is required")
    return symbol


def _parse_int(raw: Any, label: str) -> int:
    """A whole number >= 1. Fractional shares are not a thing this system
    sizes, and ``int("10.5")`` raising is the point, not a nuisance."""
    text = str(raw).strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise JournalInputError(f"{label} must be a whole number, got {text!r}") from None
    if value < 1:
        raise JournalInputError(f"{label} must be at least 1, got {value}")
    return value


def _parse_amount(raw: Any, label: str, allow_zero: bool = False) -> float:
    text = str(raw).strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise JournalInputError(f"{label} must be a number, got {text!r}") from None
    if not math.isfinite(value):
        raise JournalInputError(f"{label} must be a finite number, got {text!r}")
    if value < 0 or (value == 0 and not allow_zero):
        floor = "0 or more" if allow_zero else "greater than 0"
        raise JournalInputError(f"{label} must be {floor}, got {value:g}")
    return value


def _require_open(cfg: Config, symbol: str) -> OpenPosition:
    position = open_positions(cfg).get(symbol)
    if position is None:
        raise JournalInputError(
            f"{symbol} is not an open position — `swing positions` lists what the journal holds"
        )
    return position


def _refuse_lower_stop(symbol: str, old_stop: float, new_stop: float) -> None:
    """The one refusal both stop-changing paths share.

    ``journal stop`` and a second ``journal add`` into an open symbol make the
    *same* state change — the replay in :func:`open_positions` takes the newest
    recorded stop either way — so they have to be guarded the same way. Anything
    less makes ``add`` a backdoor around trailing discipline.
    """
    raise JournalInputError(
        f"refusing to lower the {symbol} stop from {old_stop:.2f} to {new_stop:.2f}: "
        "stops only ratchet up. Pass --force if you are fixing a typo."
    )


def journal_add(
    cfg: Config,
    symbol: Any,
    shares: Any,
    price: Any,
    stop: Any,
    trail: Any = 0.0,
    order_id: str | None = None,
    note: str = "",
    force: bool = False,
) -> int:
    """Record a manual entry and print the resulting position.

    Adding into a symbol you already hold averages the cost basis and adopts the
    new stop, so it can lower a stop just as ``journal stop`` can; it is refused
    the same way unless ``force`` is set.
    """
    try:
        sym = _parse_symbol(symbol)
        shares_n = _parse_int(shares, "shares")
        price_f = _parse_amount(price, "price")
        stop_f = _parse_amount(stop, "stop")
        trail_f = _parse_amount(0.0 if trail is None else trail, "trail", allow_zero=True)
        if stop_f >= price_f:
            raise JournalInputError(
                f"stop {stop_f:.2f} must be below the entry price {price_f:.2f} — "
                "a long stop above the entry is nonsense"
            )
        existing = open_positions(cfg).get(sym)
        if existing is not None and stop_f < existing.stop and not force:
            _refuse_lower_stop(sym, existing.stop, stop_f)
    except JournalInputError as exc:
        return _fail(exc)

    record_entry(
        cfg, sym, shares_n, price_f, stop_f,
        trail_offset=trail_f, order_id=order_id, note=note or "",
    )
    position = open_positions(cfg).get(sym)
    if position is None:  # pragma: no cover - only reachable if the write failed
        print(f"error: recorded {sym} but it is not in {journal_path(cfg)}", file=sys.stderr)
        return 1
    print(f"recorded entry in {journal_path(cfg)}:")
    print(POSITION_HEADER)
    print(position_line(position))
    return 0


def journal_exit(cfg: Config, symbol: Any, shares: Any, price: Any, reason: str = "") -> int:
    """Record a full or partial exit and print what is left."""
    try:
        sym = _parse_symbol(symbol)
        shares_n = _parse_int(shares, "shares")
        price_f = _parse_amount(price, "price")
        position = _require_open(cfg, sym)
        if shares_n > position.shares:
            raise JournalInputError(
                f"cannot exit {shares_n} shares of {sym}: only {position.shares} held"
            )
    except JournalInputError as exc:
        return _fail(exc)

    entry_price = position.entry_price
    record_exit(cfg, sym, shares_n, price_f, reason=reason or "")
    pnl = (price_f - entry_price) * shares_n
    remaining = open_positions(cfg).get(sym)
    tail = (
        "position closed"
        if remaining is None
        else f"{remaining.shares} shares remaining"
    )
    print(
        f"{sym}: sold {shares_n} @ {price_f:.2f} vs entry {entry_price:.2f} "
        f"({pnl:+,.2f}) — {tail}"
    )
    return 0


def journal_stop(cfg: Config, symbol: Any, new_stop: Any, force: bool = False) -> int:
    """Ratchet a stop up (or, with ``force``, correct one that was typed wrong)."""
    try:
        sym = _parse_symbol(symbol)
        stop_f = _parse_amount(new_stop, "stop")
        position = _require_open(cfg, sym)
        if stop_f < position.stop and not force:
            _refuse_lower_stop(sym, position.stop, stop_f)
    except JournalInputError as exc:
        return _fail(exc)

    old_stop = position.stop
    record(cfg, EVENT_STOP_MOVED, symbol=sym, stop=stop_f, prev_stop=old_stop, forced=bool(force))
    print(f"{sym} stop {old_stop:.2f} -> {stop_f:.2f}")
    return 0


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def describe_event(event: dict) -> str:
    """One journal event as one line: when, what, which symbol, the numbers."""
    ts = str(event.get("ts", ""))[:19]
    kind = str(event.get("type", "?"))
    symbol = str(event.get("symbol", "") or "-").upper()
    return f"  {ts:<19}  {kind:<11}{symbol:<8}{_event_detail(event, kind)}".rstrip()


def _event_detail(event: dict, kind: str) -> str:
    shares = _as_int(event.get("shares"))
    price = _as_float(event.get("price"))
    stop = _as_float(event.get("stop"))
    prev_stop = _as_float(event.get("prev_stop"))
    bits: list[str] = []

    if kind == EVENT_STOP_MOVED:
        if stop is not None and prev_stop is not None:
            bits.append(f"stop {prev_stop:.2f} -> {stop:.2f}")
        elif stop is not None:
            bits.append(f"stop {stop:.2f}")
    else:
        if shares is not None and price is not None:
            bits.append(f"{shares} @ {price:.2f}")
        elif shares is not None:
            bits.append(f"{shares} sh")
        elif price is not None:
            bits.append(f"@ {price:.2f}")
        if stop is not None:
            bits.append(f"stop {stop:.2f}")

    for key in ("reason", "note", "order_id"):
        value = event.get(key)
        if value:
            bits.append(f"{key}={value}")
    return "  ".join(bits)


def journal_show(cfg: Config, limit: Any = 20) -> int:
    """Print the tail of the event log. Corrupt lines are already dropped by
    :func:`read_events`, so a half-written line never hides the rest."""
    try:
        limit_n = _parse_int(limit, "--limit")
    except JournalInputError as exc:
        return _fail(exc)

    path = journal_path(cfg)
    events = read_events(cfg)
    if not events:
        print(f"the journal at {path} is empty")
        print("record your first trade with: swing journal add AAPL 10 190.50 182.00")
        return 0

    tail = events[-limit_n:]
    print(f"last {len(tail)} of {len(events)} event(s) in {path}:")
    for event in tail:
        print(describe_event(event))
    return 0
