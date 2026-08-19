"""FROZEN CONTRACT 8 — the local state journal and the kill switch.

Everything the system remembers between runs lives in one JSON file at
``cfg.paths.state_dir/journal.json``: the picks it drafted, the orders it sent,
and the status of each. It is deliberately boring — a single small file, atomic
writes (write to a temp file in the same directory, then ``os.replace``), and a
graceful recovery path when the file is missing or corrupt.

Concurrency (audit BUG-001). Three processes overlap by design: the evening
launchd scan, the morning confirm, and whatever the operator types. An atomic
write is atomic for *readers* but last-writer-wins between *writers*, so the
scan — which holds its journal object across minutes of network fetch — used to
erase an order recorded by ``swing execute`` in the meantime. Every mutating
method therefore takes an exclusive advisory lock (:func:`file_lock`), re-reads
the file, applies its own change to *that* state, and saves. Reads stay
lock-free: ``os.replace`` guarantees a reader never sees half a file.

Retention (audit LEAK-001). :meth:`Journal.save` moves picks that never became
trades (kind ``watch``, or still ``drafted``) and finished orders out to
``journal.archive.json`` once they are :data:`ARCHIVE_AFTER_DAYS` old, so the
live file — which every mutation rewrites whole — stays small.

The kill switch is even simpler: the presence of a ``KILL`` file in the state
directory. A safety primitive should be checkable with ``ls``, and engageable
by anything (this code, a shell, a panicking human).
"""

from __future__ import annotations

import errno
import fcntl
import json
import logging
import os
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "ARCHIVE_AFTER_DAYS",
    "JOURNAL_ARCHIVE_FILENAME",
    "JOURNAL_FILENAME",
    "KILL_FILENAME",
    "KINDS",
    "LOCK_TIMEOUT_SECONDS",
    "OPEN_ORDER_STATUS",
    "STATUSES",
    "TERMINAL_ORDER_STATUSES",
    "Journal",
    "PickRecord",
    "atomic_write_text",
    "clear_kill",
    "engage_kill",
    "file_lock",
    "kill_active",
    "kill_path",
]

log = logging.getLogger(__name__)

JOURNAL_FILENAME = "journal.json"
JOURNAL_ARCHIVE_FILENAME = "journal.archive.json"
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

#: Order statuses that mean "this order will never do anything again". Only
#: these are eligible for archiving; anything unrecognised (including
#: ``"pending"`` and ``"unknown"``) is kept, which fails safe.
TERMINAL_ORDER_STATUSES: tuple[str, ...] = (
    "cancelled",
    "canceled",
    "closed",
    "expired",
    "filled",
    "rejected",
    "replaced",
)

#: How long a writer waits for the lock before giving up with a sentence.
LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_SUFFIX = ".lock"
_LOCK_POLL_SECONDS = 0.02

#: Picks that never became trades, and finished orders, are archived once they
#: are this old. See :meth:`Journal.save` for what "old" is measured against.
ARCHIVE_AFTER_DAYS = 90

#: Leftover ``.name.tmpPID`` files older than this are swept on the next write.
#: Only a SIGKILL between write and rename can leave one behind (LEAK-002).
STALE_TMP_AGE_SECONDS = 24 * 60 * 60.0

_T = TypeVar("_T")


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
    atomic_write_text(path, f"{body}\n")
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
# cross-process locking
# --------------------------------------------------------------------------


@contextmanager
def file_lock(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive advisory lock covering ``path`` for the duration of the block.

    The lock is ``fcntl.flock(LOCK_EX)`` on a ``<path>.lock`` sidecar rather
    than on ``path`` itself, because the files this protects are replaced by
    ``os.replace`` — a lock held on the old inode would protect nothing.

    Args:
        path: the file being protected. It does not have to exist; only its
            parent directory is created.
        timeout: how long to wait for another holder, in seconds. ``0`` tries
            once and gives up.

    Yields:
        Nothing. The lock is released (and the descriptor closed) on exit,
        including when the body raises.

    Raises:
        TimeoutError: when the lock could not be taken within ``timeout``. The
            message is a plain-English sentence naming the file and the wait.

    Notes:
        macOS semantics, since that is the only platform this ships on: BSD
        ``flock`` is *advisory* (a process that never calls it can still write
        the file), it is attached to the open file description rather than the
        process, and it is released automatically when the descriptor is closed
        or the process dies — including on SIGKILL, so a crash cannot wedge the
        system. It is therefore **not reentrant**: taking the lock again from
        the same process on a second descriptor would wait for the first, so
        nothing inside a ``file_lock`` block may take it again. Waiting is
        implemented as ``LOCK_NB`` plus a short sleep rather than a blocking
        ``flock``, so the timeout is real (BSD flock has no timed variant) and
        Ctrl-C is still responsive. Advisory locks work on APFS/HFS+ locally;
        on a network share (NFS/SMB) they may be a no-op, which is one more
        reason ``~/.swing`` should stay on the local disk.
    """
    target = Path(path)
    lock_path = target.with_name(target.name + _LOCK_SUFFIX)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout))

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Another swing process has been holding the lock on {target} for more "
                        f"than {timeout:g} seconds, so this change was not made. Wait for the "
                        f"other run (a scan can take minutes) and try again; if nothing else is "
                        f"running, check for a stuck swing process. The lock file {lock_path} is "
                        f"empty and safe to leave in place."
                    ) from exc
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# atomic file writing
# --------------------------------------------------------------------------


def _sweep_stale_tmp(directory: Path, *, older_than: float = STALE_TMP_AGE_SECONDS) -> int:
    """Delete orphaned temp files in ``directory``; return how many went (LEAK-002).

    Only a SIGKILL between the write and the rename can leave one behind, and
    a live writer's temp file is seconds old, so an age floor of a day cannot
    tread on a concurrent write.
    """
    cutoff = time.time() - older_than
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:  # pragma: no cover - unreadable directory; the write will report it
        return 0
    for entry in entries:
        if not entry.name.startswith(".") or ".tmp" not in entry.name:
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:  # pragma: no cover - another process got there first
            continue
    if removed:
        log.info("Swept %d stale temporary file(s) from %s", removed, directory)
    return removed


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + rename).

    Readers therefore see either the whole previous file or the whole new one,
    never a half-written document. Serialising *writers* is a separate job —
    see :func:`file_lock`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_tmp(path.parent)
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


def _backup_corrupt(path: Path) -> Path | None:
    """Move a corrupt journal aside so nothing is silently destroyed.

    The name carries the pid and a timestamp because two processes can hit the
    same corrupt file at the same moment (audit BUG-024): they must not fight
    over one backup name, and the loser must not die on the ``FileNotFoundError``
    it gets for a file the winner has already moved.

    Returns:
        Where the file was moved, or ``None`` if it had already gone.
    """
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    prefix = f"{path.stem}.corrupt.{os.getpid()}-{stamp}"
    target = path.with_name(f"{prefix}{path.suffix}")
    counter = 1
    while target.exists():  # pragma: no cover - same pid, same microsecond
        target = path.with_name(f"{prefix}.{counter}{path.suffix}")
        counter += 1
    try:
        os.replace(path, target)
    except FileNotFoundError:
        log.warning("The corrupt journal at %s was moved aside by another process first", path)
        return None
    return target


# --------------------------------------------------------------------------
# the journal
# --------------------------------------------------------------------------


def _read_journal(path: Path) -> tuple[list[PickRecord], list[dict[str, Any]]]:
    """Parse the journal file, or raise if it cannot be understood."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("journal root is not a JSON object")
    picks = [PickRecord.from_dict(p) for p in raw.get("picks", []) if isinstance(p, dict)]
    orders = [dict(o) for o in raw.get("orders", []) if isinstance(o, dict)]
    return picks, orders


def _append_archive(
    path: Path, picks: Sequence[PickRecord], orders: Sequence[dict[str, Any]]
) -> bool:
    """Append retired records to the archive file; True when they are safely stored.

    A ``False`` return means the caller must keep the records in the live
    journal: losing history quietly would be worse than a journal that stays
    a little too big.
    """
    existing_picks: list[dict[str, Any]] = []
    existing_orders: list[dict[str, Any]] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("archive root is not a JSON object")
            existing_picks = [p for p in raw.get("picks", []) if isinstance(p, dict)]
            existing_orders = [o for o in raw.get("orders", []) if isinstance(o, dict)]
        except (OSError, ValueError, TypeError) as exc:
            log.warning(
                "The journal archive at %s could not be read (%s), so nothing was archived this "
                "time and the older records stay in the live journal.",
                path,
                exc,
            )
            return False
    payload = {
        "version": JOURNAL_VERSION,
        "picks": [*existing_picks, *(p.to_dict() for p in picks)],
        "orders": [*existing_orders, *(dict(o) for o in orders)],
    }
    try:
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    except OSError as exc:  # pragma: no cover - full disk / permissions
        log.warning(
            "The journal archive at %s could not be written (%s); nothing archived", path, exc
        )
        return False
    return True


class Journal:
    """The append-mostly record of what the system decided and did.

    Load it with :meth:`load`; every mutating method re-reads the file under an
    exclusive lock, applies its change to that state and persists, so a crash
    between two calls can lose at most the call in flight and a concurrent
    process cannot lose anything at all.
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
        #: True when :meth:`load` had to reset a corrupt file. Callers surface
        #: this loudly: an empty journal means full slots, full cash and a
        #: blinded duplicate guardrail while real positions may be open.
        self.recovered: bool = False

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, cfg: Config) -> Journal:
        """Load the journal for ``cfg``, tolerating a missing or corrupt file.

        A missing file simply yields an empty journal. A corrupt file is moved
        aside to ``journal.corrupt.<pid>-<timestamp>.json``, a warning is
        issued and ``recovered`` is set — losing the history is bad, but
        refusing to run because of it is worse, and the caller has to be able
        to say so out loud.

        Reading takes no lock: writes land via ``os.replace``, so a reader sees
        one whole document or the other, never a torn one.
        """
        path = Path(cfg.paths.state_dir).expanduser() / JOURNAL_FILENAME
        if not path.exists():
            return cls(path)
        try:
            picks, orders = _read_journal(path)
        except (OSError, ValueError, TypeError) as exc:
            backup = _backup_corrupt(path)
            whereabouts = (
                f"It has been moved to {backup}"
                if backup is not None
                else "Another swing process moved it aside at the same moment"
            )
            warnings.warn(
                f"The journal at {path} could not be read ({exc}). {whereabouts} and a fresh, "
                f"empty journal will be used. Any earlier picks and orders are still in the "
                f"backup file if you need them. Until this run finishes, swing believes it holds "
                f"no positions and has sent no orders — check the broker before trading.",
                UserWarning,
                stacklevel=2,
            )
            log.warning("Corrupt journal moved to %s", backup)
            recovered = cls(path)
            recovered.recovered = True
            return recovered
        return cls(path, picks, orders)

    # -- persistence --------------------------------------------------------

    @property
    def archive_path(self) -> Path:
        """Where retired records go. Beside the journal, never rewritten in place."""
        return self.path.with_name(JOURNAL_ARCHIVE_FILENAME)

    def save(self, *, asof: date | None = None) -> None:
        """Write the journal atomically, archiving anything too old to matter.

        Args:
            asof: the date retention is measured from. The default is the
                newest date the journal itself carries rather than the wall
                clock: a journal that is still being written measures roughly
                today anyway, and one that has gone quiet stops archiving
                instead of quietly emptying itself — which also keeps the
                policy deterministic and testable.
        """
        with file_lock(self.path):
            self._save_locked(asof=asof)

    def _save_locked(self, *, asof: date | None = None) -> None:
        """Archive and write. The caller must already hold :func:`file_lock`."""
        self._archive_old(asof=asof)
        payload = {
            "version": JOURNAL_VERSION,
            "picks": [p.to_dict() for p in self._picks],
            "orders": self._orders,
        }
        atomic_write_text(
            self.path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
        )

    def _retention_reference(self) -> date:
        """The newest date any record carries, or today when there are none."""
        dates = [d for d in (_parse_iso(p.date) for p in self._picks) if d is not None]
        dates += [
            d for d in (_parse_iso(str(o.get("date", ""))) for o in self._orders) if d is not None
        ]
        return max(dates) if dates else date.today()

    def _archive_old(self, *, asof: date | None = None) -> None:
        """Move stale watch/drafted picks and finished orders to the archive (LEAK-001)."""
        cutoff = (asof or self._retention_reference()) - timedelta(days=ARCHIVE_AFTER_DAYS)

        def _stale(when: date | None) -> bool:
            return when is not None and when < cutoff

        retire_picks = [
            p
            for p in self._picks
            if _stale(_parse_iso(p.date))
            and (p.kind.strip().lower() == "watch" or p.status.strip().lower() == "drafted")
        ]
        retire_orders = [
            o
            for o in self._orders
            if _order_status(o) in TERMINAL_ORDER_STATUSES
            and _stale(_parse_iso(str(o.get("date", ""))))
        ]
        if not retire_picks and not retire_orders:
            return
        if not _append_archive(self.archive_path, retire_picks, retire_orders):
            return

        retired_picks = {id(p) for p in retire_picks}
        retired_orders = {id(o) for o in retire_orders}
        self._picks = [p for p in self._picks if id(p) not in retired_picks]
        self._orders = [o for o in self._orders if id(o) not in retired_orders]
        log.info(
            "Archived %d pick(s) and %d order(s) older than %d days to %s",
            len(retire_picks),
            len(retire_orders),
            ARCHIVE_AFTER_DAYS,
            self.archive_path,
        )

    def _refresh_from_disk(self) -> None:
        """Adopt whatever is on disk right now. Call only under :func:`file_lock`."""
        if not self.path.exists():
            self._picks = []
            self._orders = []
            return
        try:
            picks, orders = _read_journal(self.path)
        except (OSError, ValueError, TypeError) as exc:
            log.warning(
                "The journal at %s could not be re-read before this change (%s), so the "
                "in-memory copy is being used instead.",
                self.path,
                exc,
            )
            return
        self._picks = picks
        self._orders = orders

    def _mutate(
        self, change: Callable[[], _T], *, save_if: Callable[[_T], bool] | None = None
    ) -> _T:
        """Apply ``change`` to the newest state on disk, then persist — under the lock.

        This is the whole of the BUG-001 fix: an object loaded minutes ago
        contributes its own change and nothing else, so two processes writing
        different things both keep them.
        """
        with file_lock(self.path):
            self._refresh_from_disk()
            result = change()
            if save_if is None or save_if(result):
                self._save_locked()
        return result

    # -- picks --------------------------------------------------------------

    @property
    def picks(self) -> list[PickRecord]:
        """All pick records, oldest first (a copy — mutate via the methods)."""
        return list(self._picks)

    def add_picks(self, picks: list[PickRecord]) -> None:
        """Append picks and persist immediately, merging with whatever is on disk.

        A pick for the same symbol on the same day replaces the earlier one, so
        re-running a scan is safe.
        """
        incoming = list(picks)

        def _apply() -> None:
            for pick in incoming:
                self._picks = [
                    p for p in self._picks if not (p.symbol == pick.symbol and p.date == pick.date)
                ]
                self._picks.append(pick)
            self._picks.sort(key=lambda p: (p.date, p.symbol))

        self._mutate(_apply)

    def picks_for(self, day: date) -> list[PickRecord]:
        """Every pick drafted on ``day``."""
        target = _as_iso(day)
        return [p for p in self._picks if p.date == target]

    def recently_picked(
        self,
        symbol: str,
        within_days: int,
        *,
        asof: date | None = None,
        kinds: tuple[str, ...] = ("pick",),
    ) -> bool:
        """True if ``symbol`` was picked in the ``within_days`` *before* ``asof``.

        Two deliberate boundaries, both audit fixes:

        * The window is ``0 < delta < within_days``. A record dated ``asof``
          itself never blocks, so re-running tonight's scan does not dedupe
          away the picks the first run just journalled (BUG-010).
        * Only records whose ``kind`` is in ``kinds`` count. The default is
          picks only: dedupe means "we already committed capital here", and a
          watch-list line commits nothing — letting it suppress the symbol for
          a week emptied the watch list of the only user it exists for
          (BUG-011). Pass ``kinds=("pick", "watch")`` to count both, or an
          empty tuple to count nothing.

        Args:
            symbol: ticker, matched case-insensitively.
            within_days: window length in calendar days; ``0`` disables it.
            asof: the day being scanned. Defaults to today; pass it explicitly
                to keep callers deterministic and testable.
            kinds: which record kinds count as "picked".
        """
        if within_days <= 0 or not kinds:
            return False
        wanted = {k.strip().lower() for k in kinds}
        reference = asof or date.today()
        sym = symbol.strip().upper()
        for pick in self._picks:
            if pick.symbol.strip().upper() != sym:
                continue
            if pick.kind.strip().lower() not in wanted:
                continue
            when = _parse_iso(pick.date)
            if when is None:
                continue
            if 0 < (reference - when).days < within_days:
                return True
        return False

    def update_status(self, symbol: str, day: date, status: str) -> None:
        """Move the pick for ``symbol`` on ``day`` to ``status`` and persist.

        Raises:
            ValueError: if ``status`` is not one of :data:`STATUSES`.
            KeyError: if no such pick is in the journal.
        """
        _check_status(status)
        target = _as_iso(day)
        sym = symbol.strip().upper()

        def _apply() -> None:
            found = False
            for pick in self._picks:
                if pick.symbol.strip().upper() == sym and pick.date == target:
                    pick.status = status
                    found = True
            if not found:
                raise KeyError(
                    f"No pick for {symbol} dated {target} is in the journal, so its status "
                    f"cannot be changed to {status!r}."
                )

        self._mutate(_apply)

    def update_statuses(self, changes: Sequence[tuple[str, date | str, str]]) -> int:
        """Apply several pick status changes in one locked read-merge-write.

        Confirming a report used to rewrite the whole journal once per pick
        (LEAK-001); fill-sync would do the same per order. This does it once.

        Args:
            changes: ``(symbol, date, status)`` triples. A triple naming a pick
                the journal does not have is ignored rather than raising —
                broker fills can arrive for orders older than the live journal.

        Returns:
            How many pick records actually changed status. Nothing is written
            when that is zero.

        Raises:
            ValueError: if any status is not one of :data:`STATUSES`. Nothing
                is applied in that case.
        """
        wanted: list[tuple[str, str, str]] = []
        for symbol, day, status in changes:
            _check_status(status)
            wanted.append((str(symbol).strip().upper(), _as_iso(day), status))

        def _apply() -> int:
            changed = 0
            for pick in self._picks:
                key = (pick.symbol.strip().upper(), pick.date)
                for sym, target, status in wanted:
                    if key == (sym, target) and pick.status != status:
                        pick.status = status
                        changed += 1
            return changed

        return self._mutate(_apply, save_if=bool)

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

        def _apply() -> None:
            self._orders.append(stored)

        self._mutate(_apply)

    def annotate_order(self, client_ref: str, /, **fields: Any) -> int:
        """Fill in details on an already-recorded order, found by its client reference.

        The placement flow journals a ``"pending"`` row *before* the network
        call and comes back afterwards to write the broker's order id and flip
        the status (audit BUG-002). The row is found by the correlation id the
        caller minted and stored under ``"client_ref"`` — an identity that
        exists locally before the broker has said anything at all, which is the
        whole point of recording first.

        Args:
            client_ref: the correlation id to match, compared exactly against
                each order's ``"client_ref"`` value. Positional-only, so that
                ``client_ref=`` unambiguously means "a field of that name" —
                which is refused, see below.
            **fields: what to write, e.g. ``order_id="123", status="open"``.

        Returns:
            How many orders carry that reference. Zero means no such order —
            not an error, and nothing is written.

        Raises:
            ValueError: if ``client_ref`` is empty or not a string, or if
                ``fields`` tries to change ``client_ref`` itself. The reference
                is an identity: rewriting it would orphan the row the next
                lookup needs.
        """
        if not isinstance(client_ref, str) or not client_ref.strip():
            raise ValueError(
                "annotate_order needs the client reference of the order to update, such as "
                f"'swing-20260818-AAPL-1', but it was given {client_ref!r}."
            )
        if "client_ref" in fields:
            raise ValueError(
                "annotate_order cannot change an order's client_ref: it is the identity the "
                "order was recorded under, and rewriting it would leave the order unfindable. "
                "Pass the reference as the first argument — annotate_order('ref-1', "
                "status='open') — and record a new order if you really need a new reference."
            )

        def _apply() -> int:
            matched = 0
            for order in self._orders:
                if order.get("client_ref") != client_ref:
                    continue
                order.update(fields)
                matched += 1
            return matched

        return self._mutate(_apply, save_if=lambda matched: bool(matched and fields))

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
            unknown symbol is a no-op, not an error, and writes nothing.
        """
        if not isinstance(new_status, str) or not new_status.strip():
            raise ValueError(
                "update_order_status needs a status to write, such as 'cancelled', "
                "but it was given an empty value."
            )
        target = symbol.strip().upper()
        wanted = only_status.strip().lower() if only_status is not None else None

        def _apply() -> int:
            changed = 0
            for order in self._orders:
                if str(order.get("symbol", "")).strip().upper() != target:
                    continue
                if wanted is not None and _order_status(order) != wanted:
                    continue
                order["status"] = new_status
                changed += 1
            return changed

        return self._mutate(_apply, save_if=bool)

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


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise ValueError(
            f"{status!r} is not a valid pick status. Valid statuses are: {', '.join(STATUSES)}."
        )
