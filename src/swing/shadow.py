"""Forward paper trading — the only uncontaminated evidence left.

Eleven backtest experiments have now been run against the same 2013-2025
out-of-sample window. That window is therefore no longer out of sample in any
useful sense: the leading configuration was *chosen* after looking at it,
repeatedly, and selection contamination is not something a better metric can
undo. The one honest remedy is forward data that no configuration has seen.

So this module records, every trading day, what each competing configuration
**would** have done, and then scores those recorded decisions as real price
action arrives. No broker is involved and no money is at risk. Nothing here
ever writes to the real journal (``<state_dir>/journal.json``); shadow lives in
its own directory and its own files, and a test asserts that.

Three commands, one file each per tracked configuration
-------------------------------------------------------
``swing shadow run``
    For each tracked configuration, produce that configuration's picks for the
    day and append them to ``<state_dir>/shadow/<name>.json``. Idempotent per
    ``(config, date)``: re-running a day replaces that day's entry rather than
    duplicating it.

``swing shadow score``
    Walk every recorded position forward against the bars that have actually
    printed since, using the backtest engine's exit ladder, and mark the ones
    that are finished.

``swing shadow report``
    Put the tracked configurations side by side, under a header that says how
    little the numbers mean yet.

THE GATE IS DELIBERATELY IGNORED
--------------------------------
``swing scan`` is fail-closed: no gate, no picks. Shadow recording is the exact
opposite, and on purpose. These are hypothetical decisions whose entire job is
to *generate* the evidence the gate is asking for, and every configuration
currently fails that gate — a gate-respecting shadow harness would record
nothing, forever. What shadow will not do is hide it: :func:`run` asks the gate
for its verdict anyway and writes it into every day's record, so no reader can
mistake a shadow position for a validated one.

WHY THIS IS COMPARABLE TO THE BACKTEST
---------------------------------------
Recording reuses :func:`swing.alerts.pipeline._scan` — the live scanner's own
candidate pipeline — rather than a reimplementation of it, so a shadow pick is
by construction the same pick ``swing scan --force`` would print. Scoring
reuses :func:`swing.strategy.rules.initial_stop` /
:func:`~swing.strategy.rules.chandelier_stop`, the engine's
:class:`~swing.backtest.costs.CostModel`, and the engine's own
``_close_position`` booking, and applies the exit ladder in the order Contract
11 documents. Signals decided at a close fill at the next open, exactly as the
engine does.

Two differences from the engine are unavoidable and are stated here rather than
buried:

1. **Sizing happens at the signal close, not at the fill.** The live scanner
   sizes off the closing price, the engine sizes off the fill price it is about
   to pay. Shadow is a record of a *live* decision, so it keeps the live
   behaviour and inherits the live discrepancy.
2. **``hold_days`` is counted on the symbol's own bars**, not on the master
   calendar of every symbol in the universe. For a liquid name these are the
   same number; for a halted one shadow will read one or two days shorter.

Positions are sized against ``backtest.initial_equity``, not
``account.equity``, for the same reason the backtest is (see
``swing.backtest.runner``): on a real $100 account whole-share rounding rejects
almost every entry, and a shadow journal of zero-share decisions would compare
nothing to nothing.

SCORING IS A REPLAY, NOT AN INCREMENT
--------------------------------------
:func:`score` recomputes every position from its entry each time it runs. The
stored outcome is a cache, never a source of truth, so a rerun cannot drift, a
revised bar cannot leave a stale exit behind, and a test can check one position
against synthetic bars without constructing any history of prior scoring runs.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.state import PickRecord, atomic_write_text, file_lock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "MEANINGFUL_TRADE_COUNT",
    "SHADOW_DIRNAME",
    "SHADOW_VERSION",
    "TRADES_PER_YEAR",
    "Outcome",
    "ShadowDay",
    "ShadowError",
    "ShadowJournal",
    "ShadowPick",
    "TrackedConfig",
    "report",
    "run",
    "score",
    "shadow_config_dir",
    "shadow_dir",
    "tracked_configs",
]

log = logging.getLogger(__name__)

#: Schema version of a shadow journal document.
SHADOW_VERSION = 1

#: Sub-directory of ``paths.state_dir`` that holds the shadow journals.
SHADOW_DIRNAME = "shadow"

#: Roughly how many closed trades a profit-factor estimate needs before it says
#: anything at all. The honest number is "a few hundred"; this is the low end of
#: it, and the report quotes it as a floor rather than as a target.
MEANINGFUL_TRADE_COUNT = 300

#: Closed trades this strategy produces in a year, order of magnitude. Used only
#: to translate :data:`MEANINGFUL_TRADE_COUNT` into the years a reader actually
#: has to wait.
TRADES_PER_YEAR = 50

#: How many weekdays may pass with no record before :func:`report` says the
#: series has stalled. One is normal — tonight's scan has not run yet when the
#: report is read in the morning — so the first weekday of silence means
#: nothing. Two or more means yesterday is missing too, which is either a market
#: holiday or a scheduler that stopped, and the reader needs to know which.
STALE_WEEKDAYS = 2

#: Outcome statuses.
STATUS_PENDING = "pending"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
STATUS_LAPSED = "lapsed"

#: Sections a tracked shadow config may NOT set. ``config.toml`` is gitignored
#: because it holds real credentials; ``config/shadow/*.toml`` is committed, so
#: a tracked file that carried these would leak them. They are taken from the
#: host configuration at load time instead.
FORBIDDEN_SECTIONS = ("schwab", "alerts", "execution")


class ShadowError(RuntimeError):
    """Raised when the shadow harness cannot run.

    The message is a complete plain-English sentence naming what is wrong and
    what to do about it.
    """


# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackedConfig:
    """One configuration under forward observation.

    Attributes:
        name: the journal name, taken from the TOML file's stem.
        path: where the tracked TOML lives.
        cfg: the configuration to scan with — the tracked file's strategy
            settings on top of the host configuration's paths, data and
            credentials, rebased onto ``backtest.initial_equity``.
    """

    name: str
    path: Path
    cfg: Config


@dataclass(frozen=True)
class Outcome:
    """What actually happened to a recorded position, as of the last scoring run.

    Every field is derived: :func:`score` recomputes the whole object from the
    bars each time, so nothing in here is ever carried forward by hand.
    """

    status: str = STATUS_PENDING
    scored_asof: str = ""
    note: str = ""
    entry_date: str = ""
    entry_price: float = 0.0
    entry_cost: float = 0.0
    shares: int = 0
    stop: float = 0.0
    last_date: str = ""
    last_close: float = 0.0
    hold_days: int = 0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    exit_cost: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    unrealized_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> Outcome:
        if not isinstance(raw, dict):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass(frozen=True)
class ShadowPick:
    """One hypothetical decision, exactly as the scanner made it.

    The recorded half is immutable history — what this configuration decided at
    the close of ``signal_date``, and nothing else. ``outcome`` is the derived
    half and is replaced wholesale by every :func:`score` run.

    Note that ``initial_stop`` here and ``outcome.stop`` are two different
    numbers on purpose. This one is the scanner's, rounded to the cent, because
    that is the price a human would actually have written on the order ticket.
    Scoring uses the unrounded ``rules.initial_stop`` the engine uses, because
    the whole point of the exercise is that shadow results and backtest results
    can be read side by side. The gap is fractions of a cent and it is recorded
    rather than reconciled away.
    """

    symbol: str
    signal_date: str
    kind: str  # "pick" (would have been bought) | "watch" (sized to zero)
    shares: int
    signal_close: float
    initial_stop: float  # the scanner's, rounded to the cent — see above
    atr: float
    score: float
    thesis: str = ""
    outcome: Outcome = field(default_factory=Outcome)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["outcome"] = self.outcome.to_dict()
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShadowPick:
        known = set(cls.__dataclass_fields__) - {"outcome"}
        kwargs = {k: v for k, v in raw.items() if k in known}
        return cls(**kwargs, outcome=Outcome.from_dict(raw.get("outcome")))

    @classmethod
    def from_pick_record(cls, record: PickRecord) -> ShadowPick:
        """Build a shadow pick from the scanner's own :class:`PickRecord`."""
        return cls(
            symbol=record.symbol,
            signal_date=record.date,
            kind=record.kind,
            shares=int(record.shares),
            signal_close=float(record.entry),
            initial_stop=float(record.stop),
            atr=float(record.atr),
            score=float(record.score),
            thesis=record.thesis,
        )

    def as_pick_record(self, *, status: str) -> PickRecord:
        """Project back to a :class:`PickRecord` for the scanner's dedupe view."""
        return PickRecord(
            symbol=self.symbol,
            date=self.signal_date,
            kind=self.kind,
            entry=self.signal_close,
            stop=self.initial_stop,
            shares=self.shares,
            risk_amount=0.0,
            score=self.score,
            atr=self.atr,
            earnings_date=None,
            earnings_known=False,
            thesis=self.thesis,
            status=status,
        )


@dataclass(frozen=True)
class ShadowDay:
    """The record of one day's observation for one configuration.

    A day with no picks is still a day: it is the difference between "this
    configuration found nothing" and "nobody looked", and a comparison that
    cannot tell those apart is worthless.
    """

    date: str
    recorded_at: str
    gate_passed: bool
    gate_reasons: tuple[str, ...] = ()
    regime_ok: bool = False
    n_picks: int = 0
    watch: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("gate_reasons", "watch", "notes"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ShadowDay:
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in raw.items() if k in known}
        for key in ("gate_reasons", "watch", "notes"):
            if key in kwargs:
                kwargs[key] = tuple(kwargs[key] or ())
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# where things live
# ---------------------------------------------------------------------------


def shadow_dir(cfg: Config) -> Path:
    """The directory holding the shadow journals, beside the real journal."""
    return Path(cfg.paths.state_dir).expanduser() / SHADOW_DIRNAME


def shadow_config_dir(explicit: Path | None = None) -> Path:
    """Locate ``config/shadow`` — the committed directory of tracked configurations.

    Searched in order: an explicit path, ``./config/shadow`` and then the
    checkout the running package was imported from. A packaged install with no
    checkout has nowhere sensible to look, so the first candidate is returned
    and :func:`tracked_configs` explains the absence.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    candidates = [Path.cwd() / "config" / SHADOW_DIRNAME]
    here = Path(__file__).resolve()
    candidates.extend(parent / "config" / SHADOW_DIRNAME for parent in here.parents[:4])
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


# ---------------------------------------------------------------------------
# tracked configurations
# ---------------------------------------------------------------------------


def _merge_with_host(tracked: Config, host: Config) -> Config:
    """Give a tracked configuration the host's plumbing and its own strategy.

    A tracked file describes *rules*: ``[strategy]``, ``[regime]``,
    ``[backtest]``, ``[gates]``, ``[universe]``, ``[account]``. Everything to do
    with this machine — where the cache is, where state goes, which data
    provider, which timezone — comes from the real configuration, so all tracked
    configurations share one cache and one state directory and none of them can
    be made to write somewhere unexpected by editing a committed file.

    Account equity is then rebased onto ``backtest.initial_equity``, exactly as
    ``swing.backtest.runner`` does, so shadow sizes the way the backtest sizes
    and the two sets of numbers can be read side by side.
    """
    merged = replace(
        tracked,
        data=host.data,
        paths=host.paths,
        schedule=host.schedule,
        alerts=host.alerts,
        schwab=host.schwab,
        execution=host.execution,
    )
    return replace(
        merged, account=replace(merged.account, equity=float(merged.backtest.initial_equity))
    )


def _load_tracked_file(path: Path) -> Config:
    """Read one tracked TOML, refusing any section that would carry a secret."""
    from swing.config import ConfigError, load_config

    forbidden = _sections_present(path) & set(FORBIDDEN_SECTIONS)
    if forbidden:
        raise ShadowError(
            f"The tracked shadow configuration {path} sets "
            f"{', '.join('[' + s + ']' for s in sorted(forbidden))}, which it must not. Files in "
            f"config/shadow are committed to git, and those sections hold credentials. Shadow "
            f"takes them from your real config.toml — delete them from this file."
        )
    try:
        return load_config(path)
    except ConfigError as exc:
        raise ShadowError(
            f"The tracked shadow configuration {path} could not be loaded: {exc}"
        ) from exc


def _sections_present(path: Path) -> set[str]:
    """The top-level TOML tables in ``path``; an unreadable file has none."""
    import tomllib

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    return {key for key, value in data.items() if isinstance(value, dict)}


def tracked_configs(
    cfg: Config, *, names: Sequence[str] | None = None, directory: Path | None = None
) -> list[TrackedConfig]:
    """Load every configuration under forward observation, sorted by name.

    Args:
        cfg: the host configuration, which supplies paths, data and timezone.
        names: only load these journal names. ``None`` loads all of them.
        directory: override the search for ``config/shadow``.

    Raises:
        ShadowError: when the directory is missing or empty, when a named
            configuration is not there, or when a file cannot be read.
    """
    root = shadow_config_dir(directory)
    if not root.is_dir():
        raise ShadowError(
            f"There is no tracked-configuration directory at {root}, so there is nothing to "
            f"shadow. Create it and put one .toml file in it per configuration you want to "
            f"track (see docs/paper-trading.md)."
        )
    files = sorted(p for p in root.glob("*.toml") if p.is_file())
    if not files:
        raise ShadowError(
            f"{root} holds no .toml files, so there is nothing to shadow. Put one file in it per "
            f"configuration you want to track (see docs/paper-trading.md)."
        )
    if names:
        wanted = [n.strip() for n in names if n.strip()]
        available = {p.stem: p for p in files}
        missing = [n for n in wanted if n not in available]
        if missing:
            raise ShadowError(
                f"No tracked configuration named {', '.join(missing)} in {root}. The ones that "
                f"are there are: {', '.join(sorted(available))}."
            )
        files = [available[n] for n in wanted]
    return [
        TrackedConfig(
            name=path.stem, path=path, cfg=_merge_with_host(_load_tracked_file(path), cfg)
        )
        for path in files
    ]


# ---------------------------------------------------------------------------
# the journal
# ---------------------------------------------------------------------------


class ShadowJournal:
    """One configuration's forward record, stored as JSON beside the real journal.

    This is a completely separate file from ``journal.json`` and shares nothing
    with it: no shadow code path constructs a :class:`swing.state.Journal`
    pointed at the real journal, and no shadow code path calls a mutating
    journal method. Writes are atomic (:func:`swing.state.atomic_write_text`)
    and serialised across processes (:func:`swing.state.file_lock`). Neither is
    reentrant, so every method here takes the lock at most once and never calls
    another locking method from inside the block.
    """

    def __init__(self, path: Path, name: str) -> None:
        self.path = Path(path)
        self.name = name
        self.days: list[ShadowDay] = []
        self.picks: list[ShadowPick] = []

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, cfg: Config, name: str) -> ShadowJournal:
        """Read this configuration's journal; a missing file is simply an empty one.

        Reads take no lock: writes land via ``os.replace``, so a reader sees one
        whole document or the other, never a torn one.

        Raises:
            ShadowError: when the file exists but is not readable JSON. Unlike
                the real journal, nothing here is safety state, so refusing is
                better than silently resetting a record of forward evidence
                that cannot be regenerated.
        """
        path = shadow_dir(cfg) / f"{name}.json"
        journal = cls(path, name)
        if not path.exists():
            return journal
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ShadowError(
                f"The shadow journal at {path} could not be read ({exc}). It holds forward "
                f"evidence that cannot be regenerated, so nothing has been changed — move the "
                f"file aside by hand if you are sure you want to start over."
            ) from exc
        journal._adopt(raw)
        return journal

    def _adopt(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            raise ShadowError(
                f"The shadow journal at {self.path} is valid JSON but not a shadow journal "
                f"(it is a {type(raw).__name__}, not an object)."
            )
        self.days = [ShadowDay.from_dict(d) for d in raw.get("days", []) if isinstance(d, dict)]
        self.picks = [ShadowPick.from_dict(p) for p in raw.get("picks", []) if isinstance(p, dict)]
        self.days.sort(key=lambda d: d.date)
        self.picks.sort(key=lambda p: (p.signal_date, p.symbol))

    def _reload(self) -> None:
        """Adopt whatever is on disk right now. Call only while holding the lock."""
        if not self.path.exists():
            self.days = []
            self.picks = []
            return
        self._adopt(json.loads(self.path.read_text(encoding="utf-8")))

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SHADOW_VERSION,
            "name": self.name,
            "days": [d.to_dict() for d in self.days],
            "picks": [p.to_dict() for p in self.picks],
        }

    def _write(self) -> None:
        """Serialise to disk. Call only while holding the lock."""
        atomic_write_text(self.path, json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")

    # -- mutation -----------------------------------------------------------

    def record_day(self, day: ShadowDay, picks: Sequence[ShadowPick]) -> None:
        """Write one day's observation, replacing any earlier record of that day.

        Idempotent per date, which is the whole point: a scan that is re-run
        because the first attempt half-failed must correct the record rather
        than double-count it. The day entry and every pick carrying that
        ``signal_date`` are removed together, so the two halves can never
        disagree about what happened on a given day.
        """
        with file_lock(self.path):
            self._reload()
            self.days = [d for d in self.days if d.date != day.date]
            self.picks = [p for p in self.picks if p.signal_date != day.date]
            self.days.append(day)
            self.picks.extend(picks)
            self.days.sort(key=lambda d: d.date)
            self.picks.sort(key=lambda p: (p.signal_date, p.symbol))
            self._write()

    def record_outcomes(self, outcomes: dict[tuple[str, str], Outcome]) -> int:
        """Apply scored outcomes, keyed by ``(signal_date, symbol)``. Returns how many landed.

        Re-reads under the lock and applies the outcomes to *that* state, so a
        concurrent ``shadow run`` recording tonight's picks cannot be erased by
        a scoring pass that loaded the file minutes ago.
        """
        if not outcomes:
            return 0
        with file_lock(self.path):
            self._reload()
            applied = 0
            updated: list[ShadowPick] = []
            for pick in self.picks:
                outcome = outcomes.get((pick.signal_date, pick.symbol))
                if outcome is None:
                    updated.append(pick)
                    continue
                updated.append(replace(pick, outcome=outcome))
                applied += 1
            self.picks = updated
            self._write()
        return applied

    # -- views --------------------------------------------------------------

    def open_picks(self) -> list[ShadowPick]:
        """Picks that are not finished: never filled yet, or filled and still running."""
        return [
            p
            for p in self.picks
            if p.kind == "pick" and p.outcome.status in (STATUS_PENDING, STATUS_OPEN)
        ]

    def closed_picks(self) -> list[ShadowPick]:
        """Picks that reached an exit."""
        return [p for p in self.picks if p.outcome.status == STATUS_CLOSED]

    def scanner_view(self, *, exclude_date: str | None = None) -> Any:
        """A :class:`swing.state.Journal` the scanner can dedupe and count slots against.

        Built in memory and pointed at a path that is never written, so the
        scanner gets exactly the semantics it expects — the real
        ``recently_picked`` window and the real ``positions()`` rule — while the
        actual ``journal.json`` is not opened, let alone touched. Nothing calls
        a mutating method on it.

        ``exclude_date`` drops the picks already recorded FOR the day being
        scanned, and it is what makes re-recording a day reproduce the decision
        instead of cannibalising it. Without it the second run of a day sees the
        first run's four picks as four occupied slots and records nothing, so
        the answer alternates between "four picks" and "none" with each rerun —
        storage that overwrites cleanly but a decision that does not.

        This is the same boundary :meth:`swing.state.Journal.recently_picked`
        already draws for dedupe (audit BUG-010: the window is ``0 < delta``, so
        a record dated ``asof`` itself never blocks tonight's rerun). That fix
        never reached ``positions()`` because in the live system a pick drafted
        tonight is not ``filled`` tonight; in shadow it is, because shadow has
        to assume its own hypothetical fills. So the boundary has to be drawn
        here instead.
        """
        from swing.state import Journal

        records = [
            pick.as_pick_record(
                status=(
                    "filled"
                    if pick.kind == "pick" and pick.outcome.status in (STATUS_PENDING, STATUS_OPEN)
                    else "closed"
                    if pick.kind == "pick"
                    else "drafted"
                )
            )
            for pick in self.picks
            if exclude_date is None or pick.signal_date != exclude_date
        ]
        return Journal(self.path.with_suffix(".view.json"), records)


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def _today(cfg: Config) -> date:
    """Today's date in the configured market timezone. Nothing below reads the clock."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(cfg.schedule.timezone)).date()


def _now_iso(cfg: Config) -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(cfg.schedule.timezone)).isoformat(timespec="seconds")


def run(
    cfg: Config,
    *,
    asof: date | None = None,
    dry_run: bool = False,
    names: Sequence[str] | None = None,
    directory: Path | None = None,
    emit: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Record what every tracked configuration would do on ``asof``.

    The trading gate is asked for its verdict and written into the record, but
    it does **not** suppress anything — see the module docstring. Positions are
    hypothetical; no order is drafted, no notification is sent, and the real
    journal is not opened.

    Args:
        cfg: the host configuration (paths, data provider, timezone).
        asof: pretend today is this date. Defaults to today in
            ``cfg.schedule.timezone``.
        dry_run: do all the work and print what would be recorded, but write
            nothing to disk.
        names: only run these tracked configurations.
        directory: override the search for ``config/shadow``.
        emit: where progress lines go.

    Returns:
        One summary dict per tracked configuration, in name order.

    Raises:
        ShadowError: when there is nothing to track, when a tracked file cannot
            be read, or when the strategy layer is not installed. A failure
            inside one configuration's scan is *recorded* rather than raised —
            a bad night for one config must not lose the record for the others.
    """
    say = emit or (lambda _message: None)
    day = asof or _today(cfg)
    tracked = tracked_configs(cfg, names=names, directory=directory)
    gate = _gate_status(cfg)
    if not gate["passed"]:
        say(
            "The trading gate is NOT passing. Shadow records anyway — that is what it is for — "
            "and every entry below is stamped with the failure."
        )

    summaries: list[dict[str, Any]] = []
    for entry in tracked:
        say(f"[{entry.name}] scanning {day.isoformat()} ...")
        journal = ShadowJournal.load(cfg, entry.name)
        record, picks, watch = _observe(entry, journal, day, gate, cfg)
        if dry_run:
            say(f"[{entry.name}] dry run: nothing written to {journal.path}")
        else:
            journal.record_day(record, picks)
        summaries.append(
            {
                "name": entry.name,
                "date": record.date,
                "picks": [p.symbol for p in picks],
                "watch": list(watch),
                "regime_ok": record.regime_ok,
                "gate_passed": record.gate_passed,
                "error": record.error,
                "path": str(journal.path),
                "dry_run": bool(dry_run),
            }
        )
        detail = ", ".join(f"{p.symbol} x{p.shares}" for p in picks) or "nothing"
        say(f"[{entry.name}] {len(picks)} pick(s): {detail}")
        if record.error:
            say(f"[{entry.name}] recorded a failure: {record.error}")
    return summaries


def _gate_status(cfg: Config) -> dict[str, Any]:
    """The trading gate's verdict, for the record only — it suppresses nothing here.

    Delegated to the scanner's own gate reader so shadow can never disagree
    with ``swing scan`` about whether the gate is passing. It is asked once, of
    the HOST configuration, because the gate reads the latest backtest report
    and shadow does not run a backtest per tracked configuration: the honest
    statement to stamp on a day is "the system's gate was failing when this was
    recorded", not eleven hypothetical per-arm verdicts nobody computed.
    """
    from swing.alerts import pipeline

    return pipeline._gate_status(cfg)


def _observe(
    entry: TrackedConfig,
    journal: ShadowJournal,
    day: date,
    gate: dict[str, Any],
    host: Config,
) -> tuple[ShadowDay, list[ShadowPick], list[str]]:
    """Run one configuration's scan for one day and turn it into a day record.

    The candidate pipeline is the live scanner's ``_scan``, called directly with
    a shadow journal view in place of the real journal. That is deliberate and
    load-bearing: reimplementing selection here would let the two drift, and a
    shadow harness that picks different names from the scanner measures nothing
    anybody cares about. It is also why the gate does not appear in this
    function at all — ``_scan`` never consults it; ``run_scan`` does, which is
    exactly the "force" behaviour shadow wants.
    """
    from swing.alerts import pipeline

    try:
        deps = pipeline._load_deps()
    except pipeline.ScanError as exc:
        raise ShadowError(str(exc)) from exc

    provider = deps.get_provider(entry.cfg)
    try:
        regime_ok, picks, watch, notes = pipeline._scan(
            deps,
            entry.cfg,
            provider,
            # Exclude this day's own earlier record, so a rerun re-decides the
            # day rather than colliding with itself. See ``scanner_view``.
            journal.scanner_view(exclude_date=day.isoformat()),
            day,
        )
    except Exception as exc:  # noqa: BLE001 - one config's bad night is data, not a crash
        log.warning("Shadow scan for %s failed on %s: %s", entry.name, day, exc)
        return (
            ShadowDay(
                date=day.isoformat(),
                recorded_at=_now_iso(host),
                gate_passed=bool(gate["passed"]),
                gate_reasons=tuple(gate["reasons"]),
                error=(
                    f"The scan for this configuration failed on {day.isoformat()} ({exc}), so no "
                    f"decision was recorded for this day. The day itself is recorded so the "
                    f"comparison cannot silently lose it."
                ),
            ),
            [],
            [],
        )

    shadow_picks = [ShadowPick.from_pick_record(p) for p in picks]
    watch_symbols = [p.symbol for p in watch]
    record = ShadowDay(
        date=day.isoformat(),
        recorded_at=_now_iso(host),
        gate_passed=bool(gate["passed"]),
        gate_reasons=tuple(gate["reasons"]),
        regime_ok=bool(regime_ok),
        n_picks=len(shadow_picks),
        watch=tuple(watch_symbols),
        notes=tuple(notes),
    )
    return record, shadow_picks, watch_symbols


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def score(
    cfg: Config,
    *,
    asof: date | None = None,
    names: Sequence[str] | None = None,
    directory: Path | None = None,
    emit: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Walk every recorded position forward against the bars that have printed since.

    Every unfinished position is replayed from its signal bar using the exit
    ladder of :mod:`swing.backtest.engine` — gap through the stop at the open,
    then the time stop, then an intraday touch of the stop — with the chandelier
    ratchet applied at each close. Positions that have not yet met an exit stay
    open with their current effective stop and unrealised P&L recorded.

    Scoring is a full replay, not an increment: rerunning it produces the same
    answer, and a revised bar cannot leave a stale exit behind.

    Args:
        cfg: the host configuration.
        asof: score against bars up to and including this date. Defaults to
            today in ``cfg.schedule.timezone``. Bars after it are ignored, so a
            scoring run is reproducible.
        names: only score these tracked configurations.
        directory: override the search for ``config/shadow``.
        emit: where progress lines go.

    Returns:
        One summary dict per tracked configuration, in name order.
    """
    say = emit or (lambda _message: None)
    day = asof or _today(cfg)
    tracked = tracked_configs(cfg, names=names, directory=directory)

    summaries: list[dict[str, Any]] = []
    for entry in tracked:
        journal = ShadowJournal.load(cfg, entry.name)
        pending = journal.open_picks()
        if not pending:
            say(f"[{entry.name}] nothing open to score.")
            summaries.append({"name": entry.name, "scored": 0, "closed": 0, "open": 0, "lapsed": 0})
            continue

        bars = _bars_for(entry.cfg, pending, asof=day)
        outcomes: dict[tuple[str, str], Outcome] = {}
        for pick in pending:
            outcomes[(pick.signal_date, pick.symbol)] = _score_pick(
                pick, bars.get(pick.symbol), entry.cfg, asof=day
            )
        journal.record_outcomes(outcomes)

        tally = {STATUS_CLOSED: 0, STATUS_OPEN: 0, STATUS_PENDING: 0, STATUS_LAPSED: 0}
        for outcome in outcomes.values():
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        say(
            f"[{entry.name}] scored {len(outcomes)} position(s) through {day.isoformat()}: "
            f"{tally[STATUS_CLOSED]} closed, {tally[STATUS_OPEN]} still open, "
            f"{tally[STATUS_PENDING]} awaiting a fill, {tally[STATUS_LAPSED]} lapsed."
        )
        summaries.append(
            {
                "name": entry.name,
                "scored": len(outcomes),
                "closed": tally[STATUS_CLOSED],
                "open": tally[STATUS_OPEN],
                "pending": tally[STATUS_PENDING],
                "lapsed": tally[STATUS_LAPSED],
            }
        )
    return summaries


def _bars_for(cfg: Config, picks: Sequence[ShadowPick], *, asof: date) -> dict[str, pd.DataFrame]:
    """Fetch enough history for every symbol in ``picks`` to warm its indicators up.

    The window starts :data:`swing.alerts.pipeline.SCAN_LOOKBACK_DAYS` before the
    earliest signal, which is what the scanner asks for and comfortably more
    than the 22-bar chandelier and 14-bar ATR need.
    """
    from swing.alerts.pipeline import SCAN_LOOKBACK_DAYS
    from swing.data import get_provider

    symbols = sorted({p.symbol for p in picks})
    if not symbols:
        return {}
    signal_days = [d for d in (_parse_iso(p.signal_date) for p in picks) if d is not None]
    earliest = min(signal_days) if signal_days else asof
    start = max(cfg.data.start_date, earliest - timedelta(days=SCAN_LOOKBACK_DAYS))
    try:
        return get_provider(cfg).daily_bars(symbols, start, asof) or {}
    except Exception as exc:  # noqa: BLE001 - a vendor outage postpones scoring, it does not fail it
        log.warning("Shadow scoring could not fetch bars (%s); nothing was scored.", exc)
        return {}


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> bool:
    """True for a real number. Mirrors ``swing.backtest.engine._finite``."""
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _score_pick(pick: ShadowPick, bars: pd.DataFrame | None, cfg: Config, *, asof: date) -> Outcome:
    """Replay one recorded position forward and report where it stands.

    This is the function that has to agree with the backtest engine, so it
    borrows the engine's own pieces rather than restating them: the stop levels
    come from :mod:`swing.strategy.rules`, the ATR from the engine's own
    accessor, the frictions from :class:`~swing.backtest.costs.CostModel`, and
    the trade is booked by the engine's ``_close_position``. What is written out
    here is the *order* of the ladder, which Contract 11 fixes as: gap through
    the stop at the open, then a time stop armed at yesterday's close, then an
    intraday touch of a resting stop.

    The one rule the engine has that shadow deliberately does not is
    ``end_of_data``: a backtest knows its data has ended, whereas a position
    whose last bar is yesterday is simply still open. Shadow never marks a
    position out just because the future has not happened yet.
    """
    from swing.backtest.costs import CostModel
    from swing.backtest.engine import (
        EXIT_CHANDELIER,
        EXIT_STOP,
        EXIT_TIME,
        _atr_series,
        _close_position,
        _OpenPosition,
    )
    from swing.strategy import rules

    scored_asof = asof.isoformat()
    signal_day = _parse_iso(pick.signal_date)
    if signal_day is None:
        return Outcome(
            status=STATUS_LAPSED,
            scored_asof=scored_asof,
            note=f"The recorded signal date {pick.signal_date!r} is not a date, so this position "
            f"cannot be scored.",
        )
    if pick.shares < 1:
        return Outcome(
            status=STATUS_LAPSED,
            scored_asof=scored_asof,
            note="This candidate sized to zero shares, so there is no position to score.",
        )
    if bars is None or len(bars) == 0:
        return Outcome(
            status=STATUS_PENDING,
            scored_asof=scored_asof,
            note=f"No price history came back for {pick.symbol}, so nothing could be scored yet.",
        )

    # Truncating at ``asof`` is safe for every series below: ATR, the 22-bar
    # chandelier maximum and the initial stop are all backward-looking, so
    # removing LATER bars cannot change an earlier value. Removing earlier ones
    # could, which is why the caller fetches a 600-day run-up rather than just
    # the bars since the signal.
    frame = bars.loc[bars.index <= pd.Timestamp(asof)]
    index = pd.DatetimeIndex(frame.index)
    signal_row = int(index.searchsorted(pd.Timestamp(signal_day), side="right")) - 1
    if signal_row < 0:
        return Outcome(
            status=STATUS_PENDING,
            scored_asof=scored_asof,
            note=f"{pick.symbol} has no bar at or before the signal date {pick.signal_date} yet.",
        )
    entry_row = signal_row + 1
    if entry_row >= len(frame):
        return Outcome(
            status=STATUS_PENDING,
            scored_asof=scored_asof,
            note=(
                f"The signal was made at the close of {pick.signal_date} and fills at the next "
                f"open, which has not printed yet."
            ),
        )

    open_ = frame["open"].astype(float).to_numpy()
    low_ = frame["low"].astype(float).to_numpy()
    close = frame["close"].astype(float).to_numpy()
    atr = _atr_series(frame, cfg.strategy.atr_window).astype(float).to_numpy()
    init_stops = rules.initial_stop(frame, cfg).astype(float).to_numpy()
    chandelier = rules.chandelier_stop(frame, cfg).astype(float).to_numpy()
    costs = CostModel.from_config(cfg)

    raw_fill = float(open_[entry_row])
    if not _finite(raw_fill) or raw_fill <= 0.0:
        return Outcome(
            status=STATUS_LAPSED,
            scored_asof=scored_asof,
            note=(
                f"{pick.symbol} printed no usable open on "
                f"{index[entry_row].date().isoformat()}, so the order lapsed unfilled — exactly "
                f"as it would in the backtest."
            ),
        )
    per_share = costs.per_share(raw_fill, atr[signal_row])
    stop0 = float(init_stops[signal_row])
    if not _finite(stop0) or stop0 < 0.0 or stop0 >= raw_fill + per_share:
        return Outcome(
            status=STATUS_LAPSED,
            scored_asof=scored_asof,
            note=(
                "The initial stop was not below the cost-inclusive entry, so this was not a "
                "trade the engine would have taken either."
            ),
        )

    position = _OpenPosition(
        symbol=pick.symbol,
        symbol_index=0,
        entry_day=entry_row,
        entry_date=index[entry_row],
        entry_price=raw_fill,
        shares=int(pick.shares),
        entry_cost=int(pick.shares) * per_share,
        initial_stop=stop0,
        stop=stop0,
        last_row=entry_row,
        last_close=raw_fill,
        last_day=entry_row,
    )
    time_stop_days = int(cfg.strategy.time_stop_days)

    def _close_of_day(row: int) -> None:
        """Ratchet, mark and arm — step 3 of the engine's day."""
        value = chandelier[row]
        if _finite(value):
            position.stop = max(position.stop, float(value), position.initial_stop)
        if _finite(close[row]):
            position.last_row = row
            position.last_close = float(close[row])
            position.last_day = row
        if row - position.entry_day >= time_stop_days:
            position.time_exit_pending = True

    # The fill day itself is never an exit day: the engine runs exits before
    # entries, so a position opened this morning is first tested tomorrow.
    _close_of_day(entry_row)

    trade_rows: list[dict[str, Any]] = []
    for row in range(entry_row + 1, len(frame)):
        open_px = open_[row]
        low_px = low_[row]
        stop = position.stop
        stop_reason = EXIT_CHANDELIER if stop > position.initial_stop + 1e-12 else EXIT_STOP

        exit_price: float | None = None
        reason = ""
        if _finite(open_px) and open_px <= stop:
            exit_price, reason = float(open_px), stop_reason  # (a) gapped through overnight
        elif position.time_exit_pending and _finite(open_px):
            exit_price, reason = float(open_px), EXIT_TIME  # (b) time stop, fills at the open
        elif _finite(low_px) and low_px <= stop:
            exit_price, reason = float(stop), stop_reason  # (c) resting stop touched intraday

        if exit_price is None:
            _close_of_day(row)
            continue

        _close_position(
            position=position,
            exit_day=row,
            exit_date=index[row],
            exit_price=exit_price,
            reason=reason,
            atr_for_exit=atr[row - 1],
            costs=costs,
            cash=0.0,
            trade_rows=trade_rows,
        )
        booked = trade_rows[0]
        return Outcome(
            status=STATUS_CLOSED,
            scored_asof=scored_asof,
            entry_date=index[entry_row].date().isoformat(),
            entry_price=round(float(position.entry_price), 4),
            entry_cost=round(float(position.entry_cost), 4),
            shares=int(position.shares),
            stop=round(float(position.stop), 4),
            exit_date=index[row].date().isoformat(),
            exit_price=round(float(booked["exit_price"]), 4),
            exit_reason=str(booked["exit_reason"]),
            exit_cost=round(float(booked["exit_cost"]), 4),
            pnl=round(float(booked["pnl"]), 4),
            pnl_pct=round(float(booked["pnl_pct"]), 4),
            hold_days=int(booked["hold_days"]),
            last_date=index[row].date().isoformat(),
            last_close=round(float(exit_price), 4),
        )

    unrealized = (
        position.shares * (position.last_close - position.entry_price) - position.entry_cost
    )
    return Outcome(
        status=STATUS_OPEN,
        scored_asof=scored_asof,
        entry_date=index[entry_row].date().isoformat(),
        entry_price=round(float(position.entry_price), 4),
        entry_cost=round(float(position.entry_cost), 4),
        shares=int(position.shares),
        stop=round(float(position.stop), 4),
        last_date=index[position.last_day].date().isoformat(),
        last_close=round(float(position.last_close), 4),
        hold_days=max(position.last_day - position.entry_day, 0),
        unrealized_pnl=round(float(unrealized), 4),
        note=(
            "Still open. The exit price above is not a result — it is where the position stands "
            "with the future unwritten."
        ),
    )


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Stats:
    """One configuration's forward record, reduced to numbers."""

    name: str
    days_tracked: int
    first_day: str
    last_day: str
    n_open: int
    n_pending: int
    n_closed: int
    n_lapsed: int
    realised_pnl: float
    unrealised_pnl: float
    wins: int
    losses: int
    gross_win: float
    gross_loss: float
    avg_hold: float
    gate_passing_days: int
    by_reason: tuple[tuple[str, int], ...]
    #: Every date this arm has a record for, ascending. The gap check needs the
    #: whole set, not just its ends.
    recorded_days: tuple[str, ...] = ()
    #: Days whose record carries a recorded failure — the arm was observed, but
    #: its scan did not produce a decision.
    error_days: int = 0
    #: The most recent such failure, for the header to quote.
    last_error: str = ""

    @property
    def win_rate(self) -> float | None:
        return (self.wins / self.n_closed * 100.0) if self.n_closed else None

    @property
    def profit_factor(self) -> float | None:
        """Gross wins over gross losses; ``None`` when it cannot be computed.

        Undefined with no losses (division by zero) and meaningless with no
        trades. Reporting "inf" as a score is how a two-trade sample gets
        mistaken for a strategy.
        """
        if not self.n_closed or self.gross_loss <= 0.0:
            return None
        return self.gross_win / self.gross_loss


def _stats_for(journal: ShadowJournal) -> _Stats:
    closed = journal.closed_picks()
    wins = [p for p in closed if p.outcome.pnl > 0]
    losses = [p for p in closed if p.outcome.pnl <= 0]
    open_picks = [p for p in journal.picks if p.outcome.status == STATUS_OPEN]
    pending = [p for p in journal.picks if p.kind == "pick" and p.outcome.status == STATUS_PENDING]
    lapsed = [p for p in journal.picks if p.outcome.status == STATUS_LAPSED]
    reasons: dict[str, int] = {}
    for pick in closed:
        reasons[pick.outcome.exit_reason] = reasons.get(pick.outcome.exit_reason, 0) + 1
    days = [d.date for d in journal.days]
    failed = [d for d in journal.days if d.error]
    return _Stats(
        name=journal.name,
        days_tracked=len(journal.days),
        first_day=min(days) if days else "",
        last_day=max(days) if days else "",
        n_open=len(open_picks),
        n_pending=len(pending),
        n_closed=len(closed),
        n_lapsed=len(lapsed),
        realised_pnl=sum(p.outcome.pnl for p in closed),
        unrealised_pnl=sum(p.outcome.unrealized_pnl for p in open_picks),
        wins=len(wins),
        losses=len(losses),
        gross_win=sum(p.outcome.pnl for p in wins),
        gross_loss=abs(sum(p.outcome.pnl for p in losses)),
        avg_hold=(sum(p.outcome.hold_days for p in closed) / len(closed)) if closed else 0.0,
        gate_passing_days=sum(1 for d in journal.days if d.gate_passed),
        by_reason=tuple(sorted(reasons.items())),
        recorded_days=tuple(sorted(days)),
        error_days=len(failed),
        last_error=failed[-1].error if failed else "",
    )


def _money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _maybe(value: float | None, fmt: str) -> str:
    return "n/a" if value is None else format(value, fmt)


def _weekdays_between(start: date, end: date) -> list[date]:
    """Every Monday-to-Friday date in ``[start, end]``, ascending.

    A weekday is the best available stand-in for a trading day here: the real
    calendar would need a holiday feed, and getting that wrong in the *quiet*
    direction — a checker that under-counts missing days — would defeat the
    point of checking. So this over-counts by the handful of market holidays a
    year, and the wording it feeds says so out loud.
    """
    if end < start:
        return []
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _gaps_for(stat: _Stats, asof: date) -> tuple[int, int]:
    """``(weekdays since the last record, weekday holes inside the series)``."""
    recorded = {d for d in (_parse_iso(value) for value in stat.recorded_days) if d is not None}
    if not recorded:
        return 0, 0
    first, last = min(recorded), max(recorded)
    stale = len(_weekdays_between(last + timedelta(days=1), asof))
    holes = len([d for d in _weekdays_between(first, last) if d not in recorded])
    return stale, holes


def _gap_lines(cfg: Config, stats: Sequence[_Stats], asof: date) -> list[str]:
    """The block that has to be read before the numbers: is this still running?

    A forward experiment dies quietly. Nothing raises, nothing pages, the file
    simply stops growing — and because the report happily prints whatever is in
    the journal, three weeks of silence look exactly like three weeks of a flat
    market. This is the one check a human actually notices, so it goes at the
    very top, above even the small-sample banner, and it names the log to read
    and the command to backfill with.

    Returns an empty list when every arm is current, which is the normal case.
    """
    logs = Path(cfg.paths.state_dir).expanduser() / "logs"
    findings: list[str] = []
    stalled = False
    for stat in stats:
        stale, holes = _gaps_for(stat, asof)
        parts: list[str] = []
        if stale >= STALE_WEEKDAYS:
            stalled = True
            parts.append(f"last record {stat.last_day}, {stale} weekday(s) ago")
        if holes:
            parts.append(f"{holes} weekday(s) inside the series have no record")
        if stat.error_days:
            said = stat.last_error if len(stat.last_error) <= 90 else stat.last_error[:87] + "..."
            parts.append(f"{stat.error_days} day(s) recorded a failure, most recently: {said}")
        if parts:
            # One fact per line: these are read at a glance, and a wrapped
            # semicolon-joined sentence is not read at all.
            findings.append(f"  {stat.name}:")
            findings.extend(f"    - {part}" for part in parts)

    if not findings:
        return []

    lines = [
        "#" * 78,
        "  SHADOW MAY HAVE STOPPED RECORDING — READ THIS BEFORE THE NUMBERS BELOW",
        "#" * 78,
        "",
        *findings,
        "",
    ]
    if stalled:
        lines += [
            "  Two or three weekdays of silence is usually a market holiday. Longer than",
            "  that is a scheduler that is no longer running, and every day it stays broken",
            "  is forward evidence that cannot be recovered later — the whole point of this",
            "  record is that it accrues in real time.",
            "",
        ]
    lines += [
        f"  Check:     tail -n 40 {logs / 'shadow.err.log'}",
        "             swing schedule status",
        "  Backfill:  swing shadow run --asof YYYY-MM-DD   (cached bars only, one day)",
        "",
        "#" * 78,
        "",
    ]
    return lines


def _warning_header(stats: Sequence[_Stats]) -> list[str]:
    """The header that has to be read before the table under it.

    It is not decoration. Everything below it is a handful of trades, and a
    handful of trades will always show a winner; saying so once, loudly, at the
    top is the only thing standing between this report and the exact
    selection-on-noise mistake it exists to correct.
    """
    total_closed = sum(s.n_closed for s in stats)
    shortfall = max(MEANINGFUL_TRADE_COUNT - total_closed, 0)
    years = shortfall / TRADES_PER_YEAR
    per_config = ", ".join(f"{s.name} {s.n_closed}" for s in stats) or "none"
    return [
        "=" * 78,
        "SHADOW PAPER TRADING — SAMPLE FAR TOO SMALL TO MEAN ANYTHING YET",
        "=" * 78,
        "",
        "  These configurations were compared repeatedly against the SAME 2013-2025",
        "  backtest window, so whichever one leads there was chosen after looking at",
        "  that data. This forward record is the only evidence that is not",
        "  contaminated by that choice — and there is almost none of it yet.",
        "",
        f"  Closed trades so far: {per_config}  (total {total_closed}).",
        f"  A profit factor does not start to mean anything until roughly"
        f" {MEANINGFUL_TRADE_COUNT} closed",
        f"  trades. At about {TRADES_PER_YEAR} trades a year that is another"
        f" {years:.0f} year(s) of tracking.",
        "",
        "  Until then the columns below differ by NOISE. Whichever configuration is",
        "  ahead in this table is not the better configuration; it is the luckier one.",
        "  DO NOT pick a winner from this report, and do not change the shipping",
        "  config because of it.",
        "",
        "=" * 78,
        "",
    ]


def report(
    cfg: Config,
    *,
    names: Sequence[str] | None = None,
    directory: Path | None = None,
    asof: date | None = None,
) -> str:
    """Build the side-by-side comparison of every tracked configuration.

    Args:
        cfg: the host configuration.
        names: only these tracked configurations.
        directory: override the search for ``config/shadow``.
        asof: the day the record is judged current against, for the gap check.
            Defaults to today in ``cfg.schedule.timezone``.

    Returns:
        The report as plain text. It always opens with the small-sample
        warning, at every sample size — there is no threshold above which the
        header is dropped, because the threshold at which a reader stops
        needing it is not one this function can know — and, when the series has
        stopped accumulating, with a louder warning above that one.
    """
    tracked = tracked_configs(cfg, names=names, directory=directory)
    journals = [ShadowJournal.load(cfg, entry.name) for entry in tracked]
    stats = [_stats_for(journal) for journal in journals]

    lines = _gap_lines(cfg, stats, asof or _today(cfg))
    lines += _warning_header(stats)
    labels = [s.name for s in stats]
    width = max([12, *(len(label) for label in labels)]) + 2

    def row(label: str, values: Iterable[str]) -> str:
        return f"  {label:<26}" + "".join(f"{value:>{width}}" for value in values)

    lines.append(row("", labels))
    lines.append("  " + "-" * (26 + width * len(labels)))
    lines.append(row("days tracked", [str(s.days_tracked) for s in stats]))
    lines.append(row("first day", [s.first_day or "-" for s in stats]))
    lines.append(row("last day", [s.last_day or "-" for s in stats]))
    lines.append(row("days gate was passing", [str(s.gate_passing_days) for s in stats]))
    lines.append("")
    lines.append(row("positions open", [str(s.n_open) for s in stats]))
    lines.append(row("awaiting fill", [str(s.n_pending) for s in stats]))
    lines.append(row("positions closed", [str(s.n_closed) for s in stats]))
    lines.append(row("lapsed / unfilled", [str(s.n_lapsed) for s in stats]))
    lines.append("")
    lines.append(row("realised P&L", [_money(s.realised_pnl) for s in stats]))
    lines.append(row("unrealised P&L", [_money(s.unrealised_pnl) for s in stats]))
    lines.append(row("wins / losses", [f"{s.wins}/{s.losses}" for s in stats]))
    lines.append(row("win rate", [_maybe(s.win_rate, ".1f") for s in stats]))
    lines.append(row("profit factor", [_maybe(s.profit_factor, ".2f") for s in stats]))
    lines.append(row("average hold (days)", [f"{s.avg_hold:.1f}" for s in stats]))
    lines.append("")

    reasons = sorted({reason for s in stats for reason, _ in s.by_reason})
    if reasons:
        lines.append("  exits by reason")
        for reason in reasons:
            counts = [str(dict(s.by_reason).get(reason, 0)) for s in stats]
            lines.append(row(f"  {reason}", counts))
        lines.append("")

    if not any(s.n_closed for s in stats):
        lines.append(
            "  Nothing has closed yet, so every number above that depends on a closed trade is"
        )
        lines.append("  either zero or n/a. That is the honest state of this experiment.")
        lines.append("")
    lines.append(f"  Journals: {shadow_dir(cfg)}")
    lines.append("")
    return "\n".join(lines)
