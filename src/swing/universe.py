"""FROZEN CONTRACT 4 — the tradable universe.

The universe is read from CSV snapshots committed under
``src/swing/assets/universe/``. Snapshots rather than a live Wikipedia scrape,
for three reasons: scans must work offline, tests must never touch the network,
and a backtest that silently changes its universe between runs is not a
backtest.

Symbols are stored Yahoo-style (``BRK-B``, not ``BRK.B``) because yfinance is
the default data provider; :func:`to_schwab_symbol` translates on the way out
to Schwab.

The snapshots ship inside the package, so they are read through
``importlib.resources`` — ``files()`` for structure, ``as_file()`` for the
actual open, which is what keeps a zipped install working (audit DEBT-014).
They are also immutable for the life of the process, so each file is parsed
once and cached (audit PERF-011).

POINT-IN-TIME MEMBERSHIP
------------------------
``sp500.csv`` and friends are *today's* members, so applying them to all of
history lets a backtest trade a company during years when it was not in the
index — look-ahead, because index inclusion is itself an outcome of past
growth (``docs/survivorship.md`` §5.2 measures it: 10,569 of 24,096 nominal
member-years, 44%). Beside each index snapshot sits a
``<index>-membership.csv`` with one row per membership *stint*
(``symbol,name,added,removed``), and :func:`membership` /
:func:`members_asof` read it so a caller can ask who was actually a member on
a given day.

Two things about that file decide whether the answer is honest:

* **Join-date coverage is uneven** — roughly 100% of current S&P 500 members
  carry a stated join date, 76% of the 400 and 57% of the 600. Any run that
  uses this data has to say so; :func:`membership_coverage` produces the
  counts for exactly that purpose.
* **A date that is not stated must not become "member since the dawn of
  time".** A blank cell, the literal ``unknown`` and a malformed date all mean
  *not stated*, and what happens then is an explicit choice made by the caller
  (:data:`UNKNOWN_POLICIES`), never a default that quietly reinstates the bias.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "INDEX_SOURCES",
    "MEMBERSHIP_SOURCES",
    "MEMBERSHIP_UNKNOWN",
    "UNKNOWN_EXCLUDE",
    "UNKNOWN_INCLUDE",
    "UNKNOWN_POLICIES",
    "Instrument",
    "MembershipCoverage",
    "MembershipInterval",
    "UniverseError",
    "Window",
    "asset_dir",
    "load",
    "load_csv",
    "members_asof",
    "membership",
    "membership_coverage",
    "membership_windows",
    "symbols",
    "to_schwab_symbol",
    "to_yahoo_symbol",
]

log = logging.getLogger(__name__)

#: CSV stem -> (source label, instrument kind)
INDEX_SOURCES: dict[str, tuple[str, str]] = {
    "sp500": ("sp500", "stock"),
    "sp400": ("sp400", "stock"),
    "sp600": ("sp600", "stock"),
    "etfs": ("etf", "etf"),
}

#: Sources that have a ``<source>-membership.csv`` beside their snapshot.
#: Everything else — ETFs, ``extra_symbols`` — is not an index constituent at
#: all, so point-in-time membership has nothing to say about it and never
#: gates it.
MEMBERSHIP_SOURCES: tuple[str, ...] = ("sp500", "sp400", "sp600")

#: The literal a membership file writes where a date provably exists but no
#: source states it. Read exactly like a blank cell: not stated.
MEMBERSHIP_UNKNOWN = "unknown"

#: Drop a stint whose start or end no source states. The conservative reading:
#: a smaller honest universe beats a larger flattering one.
UNKNOWN_EXCLUDE = "exclude"

#: Stretch a stint whose start or end no source states as far as it could go —
#: a missing join date means "a member from the beginning of the data", a
#: missing removal date means "a member ever after". This is the *flattering*
#: reading and exists to measure what the other one costs.
UNKNOWN_INCLUDE = "include"

#: What a caller may do about a date no source states.
UNKNOWN_POLICIES: tuple[str, ...] = (UNKNOWN_EXCLUDE, UNKNOWN_INCLUDE)

#: A Yahoo-style share class: a root, a dash, and a single class letter.
_CLASS_SHARE_RE = re.compile(r"^([A-Z0-9]+)-([A-Z])$")


@dataclass(frozen=True)
class Instrument:
    """One tradable thing: a listed stock or an ETF."""

    symbol: str
    name: str
    kind: str  # "stock" | "etf"
    source: str  # "sp500" | "sp400" | "sp600" | "etf" | "extra"


class UniverseError(RuntimeError):
    """Raised when the committed universe snapshots are missing or unreadable."""


def to_yahoo_symbol(symbol: str) -> str:
    """Normalise a ticker to the form yfinance expects.

    Share classes are written with a dot by the exchanges and by Wikipedia
    (``BRK.B``) but with a dash by Yahoo (``BRK-B``).
    """
    return symbol.strip().upper().replace(".", "-").replace(" ", "")


def to_schwab_symbol(symbol: str) -> str:
    """Translate a Yahoo-style ticker to the form Schwab expects.

    Schwab writes share classes with a forward slash (``BRK/B``) where Yahoo
    uses a dash (``BRK-B``). Sent verbatim, a dual-class name simply returns no
    history — one warning per symbol, no aggregate, so it looks like the stock
    stopped trading (audit BUG-039).

    Only the exact single-letter class form is rewritten; everything else is
    passed through untouched, because a wrong guess here is indistinguishable
    from a delisting.

    Note:
        The Schwab symbology was verified against the schwab-py client and the
        published docs, not against a live quote — no credentials exist in this
        checkout. Confirm it with a real quote the first time Schwab is used;
        ``docs/schwab-setup.md`` carries the check.
    """
    normalised = to_yahoo_symbol(symbol)
    match = _CLASS_SHARE_RE.match(normalised)
    if match is None:
        return normalised
    return f"{match.group(1)}/{match.group(2)}"


def _universe_root() -> Traversable:
    """The packaged universe directory, as a resource rather than a filesystem path."""
    return resources.files("swing").joinpath("assets").joinpath("universe")


@contextmanager
def _snapshot_path(stem: str) -> Iterator[Path]:
    """Yield a real filesystem path for one snapshot, extracting it if need be.

    ``as_file`` is the whole point: in a normal checkout it hands back the file
    where it already is, and in a zipped install it materialises a temporary
    copy for the duration of the block. Stringifying the ``Traversable``
    instead — what this module used to do — produces a path that does not exist
    inside a zip.
    """
    resource = _universe_root().joinpath(f"{stem}.csv")
    if not resource.is_file():
        raise UniverseError(
            f"The universe snapshot {stem}.csv is missing (looked in {_universe_root()}). "
            f"Reinstall the package, or restore the file from git."
        )
    with resources.as_file(resource) as path:
        yield Path(path)


def asset_dir() -> Path:
    """Directory holding the committed universe CSVs.

    Convenience for diagnostics and tests. Reading a snapshot goes through
    :func:`load_csv`, which opens each file inside its own ``as_file`` block —
    under a zipped install the directory materialised here would be gone by the
    time this function returned.
    """
    with resources.as_file(_universe_root()) as path:
        return Path(path)


@cache
def _read_snapshot(stem: str) -> tuple[tuple[str, str], ...]:
    """Parse one snapshot once per process; the files never change under us."""
    # utf-8-sig, not utf-8: a byte-order mark on a file someone re-saved in
    # Excel would otherwise land inside the first header name and be reported
    # as a missing 'symbol' column (audit DEBT-014).
    with _snapshot_path(stem) as path, path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or "symbol" not in reader.fieldnames:
            raise UniverseError(
                f"{path} does not have the expected 'symbol,name' header row "
                f"(found {reader.fieldnames})."
            )
        rows: list[tuple[str, str]] = []
        for row in reader:
            symbol = to_yahoo_symbol(row.get("symbol") or "")
            name = (row.get("name") or "").strip()
            if not symbol:
                continue
            rows.append((symbol, name or symbol))
    return tuple(rows)


def load_csv(stem: str) -> list[tuple[str, str]]:
    """Read one universe CSV and return ``(symbol, name)`` pairs in file order.

    Raises:
        UniverseError: if the snapshot is missing or has the wrong header.
    """
    return list(_read_snapshot(stem))


@cache
def _etf_symbols() -> frozenset[str]:
    try:
        return frozenset(sym for sym, _ in _read_snapshot("etfs"))
    except UniverseError:  # pragma: no cover - only if the snapshot is missing
        return frozenset()


def load(cfg: Config) -> list[Instrument]:
    """Return the de-duplicated universe implied by ``cfg.universe``.

    Order is stable: S&P 500, then 400, then 600, then ETFs, then any
    ``extra_symbols``. The first appearance of a symbol wins, so a symbol that
    is in two indices is reported under the larger one.
    """
    wanted: list[str] = [
        stem
        for stem, enabled in (
            ("sp500", cfg.universe.sp500),
            ("sp400", cfg.universe.sp400),
            ("sp600", cfg.universe.sp600),
            ("etfs", cfg.universe.etfs),
        )
        if enabled
    ]

    instruments: list[Instrument] = []
    seen: set[str] = set()

    for stem in wanted:
        source, kind = INDEX_SOURCES[stem]
        for symbol, name in _read_snapshot(stem):
            if symbol in seen:
                continue
            seen.add(symbol)
            instruments.append(Instrument(symbol=symbol, name=name, kind=kind, source=source))

    if cfg.universe.extra_symbols:
        etfs = _etf_symbols()
        for raw in cfg.universe.extra_symbols:
            symbol = to_yahoo_symbol(raw)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            instruments.append(
                Instrument(
                    symbol=symbol,
                    name=symbol,
                    kind="etf" if symbol in etfs else "stock",
                    source="extra",
                )
            )

    log.info(
        "Universe: %d instruments from %s", len(instruments), ", ".join(wanted) or "extras only"
    )
    return instruments


def symbols(cfg: Config) -> list[str]:
    """Convenience: just the ticker strings of :func:`load`."""
    return [i.symbol for i in load(cfg)]


# ---------------------------------------------------------------------------
# point-in-time index membership
# ---------------------------------------------------------------------------

#: One eligible stretch of calendar: ``(first day, last day)``, both inclusive.
#: ``None`` at either end means "open" — before the data begins, or ever after.
Window = tuple[date | None, date | None]


@dataclass(frozen=True)
class MembershipInterval:
    """One stint: ``symbol`` was a member of ``source`` from ``added`` to ``removed``.

    Attributes:
        added: the day the stint began, or ``None`` when no source states it.
        removed: the day the stint ended, or ``None`` — which means one of two
            very different things, told apart by ``still_open``.
        still_open: the file says this stint has not ended. ``removed is None
            and not still_open`` is the other case: it ended, and no source
            says when.
    """

    symbol: str
    name: str
    source: str
    added: date | None
    removed: date | None
    still_open: bool

    @property
    def added_stated(self) -> bool:
        """Does a source state when this stint began?"""
        return self.added is not None

    @property
    def removed_stated(self) -> bool:
        """Does a source state how this stint ended — with a date, or not at all?"""
        return self.still_open or self.removed is not None

    def window(self, unknown: str) -> Window | None:
        """The eligible stretch this stint implies, or ``None`` for "no stretch".

        Both ends are inclusive: a symbol is a member **on** its join date and
        **on** its removal date. Entries are decided at a close and filled at
        the next open (Contract 11), so a signal generated on the removal date
        still fills the following morning — the same one-bar lag every other
        gate in the engine has, and it is the reason this boundary is stated
        here rather than left to a reader to infer.
        """
        _check_unknown(unknown)
        permissive = unknown == UNKNOWN_INCLUDE
        if self.added is None and not permissive:
            return None
        if not self.removed_stated and not permissive:
            return None
        return (self.added, self.removed)


@dataclass(frozen=True)
class MembershipCoverage:
    """How much of a universe point-in-time membership can honestly speak to.

    Every field is a count of *instruments*, not of stints. ``gated`` is the
    only population membership applies to; the arithmetic that matters is
    ``gated == stated_join + unknown_join + no_membership_row``.
    """

    #: Everything in the universe, gated or not.
    instruments: int
    #: Instruments an index membership file governs (S&P 500/400/600 stocks).
    gated: int
    #: ETFs and ``extra_symbols``: never index constituents, so never gated.
    ungated: int
    #: Gated instruments with at least one stint carrying a stated join date.
    stated_join: int
    #: Gated instruments that appear in a membership file, but with no stated
    #: join date anywhere. THE number to watch: under ``exclude`` these are
    #: dropped, under ``include`` they silently reinstate the whole bias.
    unknown_join: int
    #: Gated instruments with no row in any enabled membership file at all.
    no_membership_row: int
    #: Instruments with no eligible day at all under the chosen policy.
    excluded: int
    #: ``(source, gated, stated_join)`` per index, in :data:`MEMBERSHIP_SOURCES`
    #: order — this is where the uneven coverage becomes visible.
    by_source: tuple[tuple[str, int, int], ...]

    @property
    def coverage_pct(self) -> float:
        """Percent of gated instruments whose join date a source actually states."""
        return 100.0 * self.stated_join / self.gated if self.gated else 0.0


def _check_unknown(unknown: str) -> None:
    if unknown not in UNKNOWN_POLICIES:
        raise ValueError(
            f"The unknown-date policy must be one of {', '.join(UNKNOWN_POLICIES)}, but it is "
            f"{unknown!r}. '{UNKNOWN_EXCLUDE}' drops a stint whose join date no source states; "
            f"'{UNKNOWN_INCLUDE}' treats it as a member from the beginning of the data."
        )


#: How one date cell read: an ISO date, an empty cell, the ``unknown`` literal,
#: or something that is none of those. The last three all mean "no date", but
#: they are not the same fact and the caller distinguishes them.
_BLANK, _DATE, _UNSTATED, _MALFORMED = "blank", "date", "unstated", "malformed"


def _parse_membership_date(raw: str) -> tuple[date | None, str]:
    """Read one date cell as ``(date or None, which of the four it was)``.

    A malformed cell is a data bug rather than a documented gap, so it is
    counted and logged — but it is never allowed to crash a run, and it is
    never quietly promoted to a date.
    """
    text = raw.strip()
    if not text:
        return None, _BLANK
    if text.lower() == MEMBERSHIP_UNKNOWN:
        return None, _UNSTATED
    try:
        return date.fromisoformat(text), _DATE
    except ValueError:
        return None, _MALFORMED


@cache
def _read_membership(source: str) -> tuple[MembershipInterval, ...]:
    """Parse one membership file once per process; the files never change under us."""
    stem = f"{source}-membership"
    with _snapshot_path(stem) as path, path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        missing = [column for column in ("symbol", "added", "removed") if column not in fieldnames]
        if missing:
            raise UniverseError(
                f"{path} does not have the expected 'symbol,name,added,removed' header row: "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing "
                f"(found {fieldnames}). Rebuild it with scripts/build_membership.py, or restore "
                f"it from git."
            )
        intervals: list[MembershipInterval] = []
        malformed = 0
        for row in reader:
            symbol = to_yahoo_symbol(row.get("symbol") or "")
            if not symbol:
                continue
            added, added_state = _parse_membership_date(row.get("added") or "")
            removed, removed_state = _parse_membership_date(row.get("removed") or "")
            malformed += (added_state == _MALFORMED) + (removed_state == _MALFORMED)
            intervals.append(
                MembershipInterval(
                    symbol=symbol,
                    name=(row.get("name") or "").strip() or symbol,
                    source=source,
                    added=added,
                    removed=removed,
                    # A blank `removed` is the file saying "still a member".
                    # `unknown` and a malformed date are it saying "it ended and
                    # nobody records when", which is a different fact.
                    still_open=removed_state == _BLANK,
                )
            )
    if malformed:
        log.warning(
            "%s has %d date cell(s) that are neither blank, '%s', nor an ISO date; they are read "
            "as 'no date stated', which the unknown-date policy then decides what to do with.",
            f"{stem}.csv",
            malformed,
            MEMBERSHIP_UNKNOWN,
        )
    return tuple(intervals)


def membership(source: str) -> list[MembershipInterval]:
    """Read one index's membership stints, in file order.

    Args:
        source: one of :data:`MEMBERSHIP_SOURCES`.

    Returns:
        Every stint in the file, including symbols that have since left the
        index and are therefore absent from the plain snapshot. A symbol with
        two stints appears twice.

    Raises:
        UniverseError: if the file is missing or its header is not
            ``symbol,name,added,removed``.
    """
    if source not in MEMBERSHIP_SOURCES:
        raise UniverseError(
            f"There is no membership file for {source!r}. Point-in-time membership exists for "
            f"{', '.join(MEMBERSHIP_SOURCES)} only — ETFs and extra symbols are not index "
            f"constituents, so nothing was ever added to or removed from an index."
        )
    return list(_read_membership(source))


def _enabled_membership_sources(cfg: Config) -> tuple[str, ...]:
    """The membership files this config's universe is allowed to be eligible through."""
    return tuple(
        source
        for source, enabled in (
            ("sp500", cfg.universe.sp500),
            ("sp400", cfg.universe.sp400),
            ("sp600", cfg.universe.sp600),
        )
        if enabled
    )


def _intervals_by_symbol(sources: Iterable[str]) -> dict[str, list[MembershipInterval]]:
    """Every stint from ``sources``, collected per symbol.

    Collected across indices on purpose. A company that was in the S&P 500
    until 2016 and is in the S&P 400 today was an index constituent throughout
    both stints, and the snapshot only knows about the second one — reading its
    eligibility from its *current* index alone would delete a decade of
    legitimate membership.
    """
    collected: dict[str, list[MembershipInterval]] = {}
    for source in sources:
        for interval in _read_membership(source):
            collected.setdefault(interval.symbol, []).append(interval)
    return collected


def _merge_windows(windows: Iterable[Window]) -> tuple[Window, ...]:
    """Sort and merge overlapping stints into disjoint windows, earliest first."""
    spans = sorted((start or date.min, end or date.max) for start, end in windows)
    merged: list[tuple[date, date]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(
        (None if start == date.min else start, None if end == date.max else end)
        for start, end in merged
    )


def membership_windows(
    cfg: Config,
    *,
    instruments: Iterable[Instrument] | None = None,
    unknown: str = UNKNOWN_EXCLUDE,
) -> dict[str, tuple[Window, ...]]:
    """The days each instrument was an index member.

    Args:
        cfg: the configuration; its ``[universe]`` toggles decide which
            membership files a symbol may be eligible through.
        instruments: the instruments to answer for. Defaults to the whole
            ``load(cfg)`` universe; a backtest passes the slice it is actually
            trading, so an ETF-only run reports on ETFs and nothing else.
        unknown: one of :data:`UNKNOWN_POLICIES` — what to do about a stint
            whose start or end no source states.

    Returns:
        ``{symbol: ((first_day, last_day), ...)}``, one entry per instrument,
        windows disjoint and in date order. Both ends are inclusive and either
        may be ``None`` for "open". Two values carry meaning of their own:

        * ``((None, None),)`` — never gated. An ETF is not an index constituent
          and an ``extra_symbols`` entry was asked for by name, so index
          membership does not restrict either of them. (A departed company
          named in ``extra_symbols`` is therefore tradable throughout: the user
          said to trade it, and second-guessing an explicit instruction with
          index data would be the surprising behaviour.)
        * ``()`` — no eligible day whatsoever. Under ``exclude`` this is what a
          symbol with no stated join date gets, and it is the point of the
          policy: it is dropped rather than back-dated to the dawn of time.
    """
    _check_unknown(unknown)
    sources = _enabled_membership_sources(cfg)
    by_symbol = _intervals_by_symbol(sources)

    windows: dict[str, tuple[Window, ...]] = {}
    for instrument in load(cfg) if instruments is None else instruments:
        if instrument.source not in MEMBERSHIP_SOURCES:
            windows[instrument.symbol] = ((None, None),)
            continue
        stints = by_symbol.get(instrument.symbol, [])
        usable = [w for w in (stint.window(unknown) for stint in stints) if w is not None]
        windows[instrument.symbol] = _merge_windows(usable) if usable else ()
    return windows


def _in_windows(windows: tuple[Window, ...], day: date) -> bool:
    """Is ``day`` inside any window? Both ends inclusive; ``None`` is open."""
    return any(
        (start is None or day >= start) and (end is None or day <= end) for start, end in windows
    )


def members_asof(day: date, cfg: Config, *, unknown: str = UNKNOWN_EXCLUDE) -> list[Instrument]:
    """The instruments of :func:`load` that were index members on ``day``.

    Args:
        day: the date to ask about.
        cfg: the configuration, as for :func:`load`.
        unknown: one of :data:`UNKNOWN_POLICIES`; defaults to the conservative
            ``exclude``, so a symbol whose join date no source states is **not**
            treated as a member for all of history.

    Returns:
        A subset of ``load(cfg)`` in the same order. ETFs and extra symbols are
        always present: they are not index constituents and membership has
        nothing to say about them.

    Note:
        This rebuilds every symbol's windows on each call. Asking about one day
        is what it is for; asking about a whole calendar should take
        :func:`membership_windows` once and test days against the result.
    """
    windows = membership_windows(cfg, unknown=unknown)
    return [i for i in load(cfg) if _in_windows(windows.get(i.symbol, ()), day)]


def membership_coverage(
    cfg: Config,
    *,
    instruments: Iterable[Instrument] | None = None,
    unknown: str = UNKNOWN_EXCLUDE,
) -> MembershipCoverage:
    """Count what point-in-time membership does and does not know about this universe.

    This is the number a run has to publish next to its results. Join-date
    coverage is uneven across the three indices, so "point-in-time membership
    was applied" on its own is not a statement anyone can check.
    """
    _check_unknown(unknown)
    sources = _enabled_membership_sources(cfg)
    by_symbol = _intervals_by_symbol(sources)
    instruments = list(load(cfg) if instruments is None else instruments)

    gated = stated = unknown_join = no_row = excluded = 0
    per_source: dict[str, list[int]] = {source: [0, 0] for source in MEMBERSHIP_SOURCES}

    for instrument in instruments:
        if instrument.source not in MEMBERSHIP_SOURCES:
            continue
        gated += 1
        counts = per_source[instrument.source]
        counts[0] += 1
        stints = by_symbol.get(instrument.symbol, [])
        has_join = any(stint.added_stated for stint in stints)
        if not stints:
            no_row += 1
        elif has_join:
            stated += 1
            counts[1] += 1
        else:
            unknown_join += 1
        usable = [w for w in (stint.window(unknown) for stint in stints) if w is not None]
        if not usable:
            excluded += 1

    return MembershipCoverage(
        instruments=len(instruments),
        gated=gated,
        ungated=len(instruments) - gated,
        stated_join=stated,
        unknown_join=unknown_join,
        no_membership_row=no_row,
        excluded=excluded,
        by_source=tuple(
            (source, per_source[source][0], per_source[source][1])
            for source in MEMBERSHIP_SOURCES
            if source in sources
        ),
    )
