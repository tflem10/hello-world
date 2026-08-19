"""FROZEN CONTRACT 8 — the local state journal and the kill switch.

Everything the system remembers between runs lives in one JSON file at
``cfg.paths.state_dir/journal.json``: the picks it drafted, the orders it sent,
and the status of each. It is deliberately boring — a single small file, atomic
writes (write to a temp file in the same directory, then ``os.replace``), and a
graceful recovery path when the file is missing or corrupt.

The kill switch is even simpler: the presence of a ``KILL`` file in the state
directory. A safety primitive should be checkable with ``ls``, and engageable
by anything (this code, a shell, a panicking human).
"""

from __future__ import annotations

import json
import logging
import os
import warnings
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "JOURNAL_FILENAME",
    "KILL_FILENAME",
    "OPEN_ORDER_STATUS",
    "STATUSES",
    "Journal",
    "PickRecord",
    "clear_kill",
    "engage_kill",
    "kill_active",
    "kill_path",
]

log = logging.getLogger(__name__)

JOURNAL_FILENAME = "journal.json"
KILL_FILENAME = "KILL"
JOURNAL_VERSION = 1

STATUSES: tuple[str, ...] = (
    "drafted",
    "confirmed",
    "invalidated",
    "ordered",
    "filled",
    "closed",
)
KINDS: tuple[str, ...] = ("pick", "watch")

#: The status a recorded order carries until something moves it on. An order
#: dict written before this field existed is treated as open, so old journals
#: keep working.
OPEN_ORDER_STATUS = "open"


def _order_status(order: dict[str, Any]) -> str:
    """The normalised status of a recorded order; missing means open."""
    return str(order.get("status", OPEN_ORDER_STATUS)).strip().lower()


@dataclass
class PickRecord:
    """One drafted trade idea, as written to the journal and the scan report.

    Mutable on purpose: :meth:`Journal.update_status` walks a pick through
    ``drafted -> confirmed -> ordered -> filled -> closed`` in place.
    """

    symbol: str
    date: str  # ISO calendar date, e.g. "2026-08-18"
    kind: str  # "pick" | "watch"
    entry: float
    stop: float
    shares: int
    risk_amount: float
    score: float
    atr: float
    earnings_date: str | None
    earnings_known: bool
    thesis: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PickRecord:
        """Build a record from JSON, ignoring unknown keys and filling gaps.

        Old journals must keep loading after the schema grows, so this is
        forgiving by design.
        """
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        defaults: dict[str, Any] = {
            "symbol": "",
            "date": "",
            "kind": "pick",
            "entry": 0.0,
            "stop": 0.0,
            "shares": 0,
            "risk_amount": 0.0,
            "score": 0.0,
            "atr": 0.0,
            "earnings_date": None,
            "earnings_known": False,
            "thesis": "",
            "status": "drafted",
        }
        return cls(**{**defaults, **kwargs})


def _as_iso(day: date | str) -> str:
    """Accept a ``date`` or an ISO string and always return an ISO string."""
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    return str(day)


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# kill switch
# --------------------------------------------------------------------------


def kill_path(cfg: Config) -> Path:
    """Return the path of the kill file for this configuration."""
    return Path(cfg.paths.state_dir).expanduser() / KILL_FILENAME


def kill_active(cfg: Config) -> bool:
    """True when the kill switch is engaged (the KILL file exists)."""
    return kill_path(cfg).exists()


def engage_kill(cfg: Config, *, reason: str = "") -> Path:
    """Engage the kill switch. Idempotent; returns the kill file path."""
    path = kill_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = reason.strip() or "engaged via swing kill"
    _atomic_write(path, f"{body}\n")
    log.warning("Kill switch ENGAGED: %s", path)
    return path


def clear_kill(cfg: Config) -> None:
    """Release the kill switch. Idempotent — no error if it was never engaged."""
    path = kill_path(cfg)
    try:
        path.unlink()
        log.warning("Kill switch cleared: %s", path)
    except FileNotFoundError:
        log.info("Kill switch was not engaged; nothing to clear (%s)", path)


# --------------------------------------------------------------------------
# atomic file writing
# --------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - only on a failed write
            tmp.unlink(missing_ok=True)


def _backup_corrupt(path: Path) -> Path:
    """Move a corrupt journal aside so nothing is silently destroyed."""
    target = path.with_name(f"{path.stem}.corrupt{path.suffix}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.stem}.corrupt.{counter}{path.suffix}")
        counter += 1
    os.replace(path, target)
    return target


# --------------------------------------------------------------------------
# the journal
# --------------------------------------------------------------------------


class Journal:
    """The append-mostly record of what the system decided and did.

    Load it with :meth:`load`; every mutating method persists immediately, so a
    crash between two calls can lose at most the call in flight.
    """

    def __init__(
        self,
        path: Path,
        picks: list[PickRecord] | None = None,
        orders: list[dict[str, Any]] | None = None,
    ) -> None:
        self.path = Path(path)
        self._picks: list[PickRecord] = list(picks or [])
        self._orders: list[dict[str, Any]] = list(orders or [])

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, cfg: Config) -> Journal:
        """Load the journal for ``cfg``, tolerating a missing or corrupt file.

        A missing file simply yields an empty journal. A corrupt file is moved
        aside to ``journal.corrupt.json`` and a warning is issued — losing the
        history is bad, but refusing to run because of it is worse.
        """
        path = Path(cfg.paths.state_dir).expanduser() / JOURNAL_FILENAME
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("journal root is not a JSON object")
            picks = [PickRecord.from_dict(p) for p in raw.get("picks", []) if isinstance(p, dict)]
            orders = [o for o in raw.get("orders", []) if isinstance(o, dict)]
        except (OSError, ValueError, TypeError) as exc:
            backup = _backup_corrupt(path)
            warnings.warn(
                f"The journal at {path} could not be read ({exc}). It has been moved to "
                f"{backup} and a fresh, empty journal will be used. Any earlier picks and "
                f"orders are still in the backup file if you need them.",
                UserWarning,
                stacklevel=2,
            )
            log.warning("Corrupt journal moved to %s", backup)
            return cls(path)
        return cls(path, picks, orders)

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        """Write the journal atomically. Called by every mutating method."""
        payload = {
            "version": JOURNAL_VERSION,
            "picks": [p.to_dict() for p in self._picks],
            "orders": self._orders,
        }
        _atomic_write(self.path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")

    # -- picks --------------------------------------------------------------

    @property
    def picks(self) -> list[PickRecord]:
        """All pick records, oldest first (a copy — mutate via the methods)."""
        return list(self._picks)

    def add_picks(self, picks: list[PickRecord]) -> None:
        """Append picks and persist immediately.

        A pick for the same symbol on the same day replaces the earlier one, so
        re-running a scan is safe.
        """
        for pick in picks:
            self._picks = [
                p for p in self._picks if not (p.symbol == pick.symbol and p.date == pick.date)
            ]
            self._picks.append(pick)
        self._picks.sort(key=lambda p: (p.date, p.symbol))
        self.save()

    def picks_for(self, day: date) -> list[PickRecord]:
        """Every pick drafted on ``day``."""
        target = _as_iso(day)
        return [p for p in self._picks if p.date == target]

    def recently_picked(self, symbol: str, within_days: int, *, asof: date | None = None) -> bool:
        """True if ``symbol`` was picked within the last ``within_days`` days.

        ``asof`` defaults to today; pass it explicitly to keep callers
        deterministic and testable.
        """
        if within_days <= 0:
            return False
        reference = asof or date.today()
        sym = symbol.strip().upper()
        for pick in self._picks:
            if pick.symbol.strip().upper() != sym:
                continue
            when = _parse_iso(pick.date)
            if when is None:
                continue
            delta = (reference - when).days
            if 0 <= delta < within_days:
                return True
        return False

    def update_status(self, symbol: str, day: date, status: str) -> None:
        """Move the pick for ``symbol`` on ``day`` to ``status`` and persist."""
        if status not in STATUSES:
            raise ValueError(
                f"{status!r} is not a valid pick status. Valid statuses are: {', '.join(STATUSES)}."
            )
        target = _as_iso(day)
        sym = symbol.strip().upper()
        found = False
        for pick in self._picks:
            if pick.symbol.strip().upper() == sym and pick.date == target:
                pick.status = status
                found = True
        if not found:
            raise KeyError(
                f"No pick for {symbol} dated {target} is in the journal, so its status cannot "
                f"be changed to {status!r}."
            )
        self.save()

    # -- orders -------------------------------------------------------------

    def record_order(self, order: dict[str, Any]) -> None:
        """Record a submitted (or drafted) order and persist.

        An order without a ``status`` is stored as ``"open"``, so every
        recorded order has a status that :meth:`update_order_status` can move.
        """
        if not isinstance(order, dict):
            raise TypeError("record_order expects a dict describing the order.")
        stored = dict(order)
        stored.setdefault("status", OPEN_ORDER_STATUS)
        self._orders.append(stored)
        self.save()

    def open_orders(self) -> list[dict[str, Any]]:
        """Orders still working at the broker — those whose status is ``"open"``.

        Orders recorded before the status field existed have no ``status`` key
        and are treated as open, which fails safe: the duplicate-suppression
        guardrail sees them and refuses rather than double-ordering.
        """
        return [dict(o) for o in self._orders if _order_status(o) == OPEN_ORDER_STATUS]

    def update_order_status(
        self, symbol: str, new_status: str, *, only_status: str | None = None
    ) -> int:
        """Move the recorded orders for ``symbol`` to ``new_status``; persist.

        Without this an order could never leave ``"open"``, so cancelling at the
        broker left the journal claiming the order was still working and the
        duplicate guardrail refused that symbol forever.

        Args:
            symbol: the ticker whose orders should change, matched
                case-insensitively.
            new_status: the status to write, e.g. ``"cancelled"``.
            only_status: when given, change only the orders currently in this
                status (an order with no status counts as ``"open"``).

        Returns:
            How many orders were changed. Zero when nothing matched — an
            unknown symbol is a no-op, not an error.
        """
        if not isinstance(new_status, str) or not new_status.strip():
            raise ValueError(
                "update_order_status needs a status to write, such as 'cancelled', "
                "but it was given an empty value."
            )
        target = symbol.strip().upper()
        wanted = only_status.strip().lower() if only_status is not None else None

        changed = 0
        for order in self._orders:
            if str(order.get("symbol", "")).strip().upper() != target:
                continue
            if wanted is not None and _order_status(order) != wanted:
                continue
            order["status"] = new_status
            changed += 1

        if changed:
            self.save()
        return changed

    @property
    def orders(self) -> list[dict[str, Any]]:
        """Every recorded order, oldest first (a copy)."""
        return [dict(o) for o in self._orders]

    # -- positions ----------------------------------------------------------

    def positions(self) -> list[dict[str, Any]]:
        """The local view of open positions, for reconciliation against the broker.

        A position is a pick whose status reached ``filled`` and has not since
        been marked ``closed``.
        """
        out: list[dict[str, Any]] = []
        for pick in self._picks:
            if pick.status != "filled":
                continue
            out.append(
                {
                    "symbol": pick.symbol,
                    "date": pick.date,
                    "shares": pick.shares,
                    "entry": pick.entry,
                    "stop": pick.stop,
                    "status": pick.status,
                }
            )
        return out
