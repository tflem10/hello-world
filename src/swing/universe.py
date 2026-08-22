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

Three things about that file decide whether the answer is honest:

* **Join-date coverage is uneven** — roughly 100% of current S&P 500 members
  carry a stated join date, 76% of the 400 and 57% of the 600. Any run that
  uses this data has to say so; :func:`membership_coverage` produces the
  counts for exactly that purpose.
* **A date that is not stated must not become "member since the dawn of
  time".** A blank cell, the literal ``unknown`` and a malformed date all mean
  *not stated*, and what happens then is an explicit choice made by the caller
  (:data:`UNKNOWN_POLICIES`), never a default that quietly reinstates the bias.
* **A date that is stated is not automatically a fact.** See below.

DATE PROVENANCE: EXACT, BOUNDED, UNSTATED
-----------------------------------------
Beside each date the file writes how it is known, in ``added_bound`` /
``removed_bound``:

``exact``
    A source names the day. Read it as the day.
``no_later_than``
    An *upper bound*, produced by diffing consecutive quarterly SEC holdings
    snapshots: the symbol had joined (or left) by this date, and nobody records
    when. The true date can be up to a snapshot interval earlier.

So a date cell is one of three things, not two — exact, bounded, or unstated —
and the middle one is the majority of the small-cap file: 893 of the 1,799
S&P 600 stints carry a bounded join date.

Reading a bound as a fact is not a rounding error, it is a *directional* one,
and the direction differs at the two ends of a stint:

* a bounded **join** read as exact puts the join too late, so days the symbol
  really was a member are scored as days it was not;
* a bounded **removal** read as exact puts the exit too late, so days it had
  already left are scored as membership.

Either way the boundary is an artefact of the snapshot cadence rather than a
market event, which is why a study that measures anything *at* the boundary
(before-versus-after membership, say) must know which stints rest on one.
:data:`BOUNDED_POLICIES` is the explicit choice — read a bound as the date
(:data:`BOUNDED_AS_EXACT`), or treat it as a date no source states
(:data:`BOUNDED_AS_UNKNOWN`, the default) so it inherits the same conservative
handling every other unstated date gets.

Inside a file that carries the columns, only those two tokens certify a date:
a blank provenance cell and a token nobody recognises both leave the date
standing as a *bound*, never as a fact. A file that has no bound column at all
is the one case read the other way — it predates the vocabulary, its dates are
the announced days its builder had, and the builder now refuses to write a file
that drops provenance (see ``scripts/build_membership.py``), so a missing
column means "old file", not "downgraded file". It is never silent about it:
the read logs a warning naming the file and the column.

:func:`membership_coverage` reports the composition — exact, bounded, undated,
per index — so no result can be read without knowing how much of it rests on
approximations.
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
    "BOUNDED_AS_EXACT",
    "BOUNDED_AS_UNKNOWN",
    "BOUNDED_POLICIES",
    "BOUND_EXACT",
    "BOUND_NO_LATER_THAN",
    "BOUND_TOKENS",
    "DATE_BOUNDED",
    "DATE_EXACT",
    "DATE_OPEN",
    "DATE_QUALITIES",
    "DATE_UNSTATED",
    "INDEX_SOURCES",
    "MEMBERSHIP_SOURCES",
    "MEMBERSHIP_UNKNOWN",
    "UNKNOWN_EXCLUDE",
    "UNKNOWN_INCLUDE",
    "UNKNOWN_POLICIES",
    "Instrument",
    "MembershipCoverage",
    "MembershipInterval",
    "StintCounts",
    "UniverseError",
    "Window",
    "asset_dir",
    "load",
    "load_csv",
    "members_asof",
    "membership",
    "membership_coverage",
    "membership_windows",
    "stint_counts",
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

#: ``added_bound``/``removed_bound`` token for "a source names this day".
BOUND_EXACT = "exact"

#: ``added_bound``/``removed_bound`` token for "it had happened by this day, and
#: nobody records when" — an upper bound from diffing quarterly SEC snapshots.
BOUND_NO_LATER_THAN = "no_later_than"

#: The whole provenance vocabulary a membership file may write. In a file that
#: carries the column, anything else — an empty cell, an unrecognised token —
#: is read as :data:`BOUND_NO_LATER_THAN`: the file could have certified the
#: date and did not. A file with no bound column at all is the separate case
#: handled in :func:`_read_date_cell`.
BOUND_TOKENS: tuple[str, ...] = (BOUND_EXACT, BOUND_NO_LATER_THAN)

#: A source names this day and a source certifies it.
DATE_EXACT = "exact"

#: A source names this day as an upper bound only: the event happened on or
#: before it. A third state — neither a known date nor no date at all.
DATE_BOUNDED = "bounded"

#: No source states this date: a blank ``added``, the literal ``unknown``, or a
#: cell that is not a date.
DATE_UNSTATED = "unstated"

#: ``removed`` only: the file says the stint has not ended. Not a missing date —
#: a stated fact about today, and the reason a blank ``removed`` and an
#: ``unknown`` ``removed`` are different things.
DATE_OPEN = "open"

#: How one date cell can be known. ``added`` is never :data:`DATE_OPEN`.
DATE_QUALITIES: tuple[str, ...] = (DATE_EXACT, DATE_BOUNDED, DATE_UNSTATED, DATE_OPEN)

#: Read a bounded date as though it were the exact day. What every run before
#: this vocabulary existed did, kept so those runs stay reproducible — but it
#: states as a fact something no source states, so it is not the default.
BOUNDED_AS_EXACT = "exact"

#: Read a bounded date as a date no source states, so it inherits the
#: :data:`UNKNOWN_POLICIES` handling: dropped under ``exclude``, stretched to
#: the limit under ``include``. The default, because a bound is an
#: approximation and an approximation must not masquerade as a measurement.
BOUNDED_AS_UNKNOWN = "unknown"

#: What a caller may do about a date a source states only as a bound.
BOUNDED_POLICIES: tuple[str, ...] = (BOUNDED_AS_EXACT, BOUNDED_AS_UNKNOWN)

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
        added_quality: how ``added`` is known — :data:`DATE_EXACT`,
            :data:`DATE_BOUNDED` or :data:`DATE_UNSTATED`. A bounded date is
            still a date, so ``added`` is set; what the bound says is that the
            true day is that one *or earlier*.
        removed_quality: the same for ``removed``, plus :data:`DATE_OPEN` for
            "the file says this stint has not ended".
    """

    symbol: str
    name: str
    source: str
    added: date | None
    removed: date | None
    added_quality: str
    removed_quality: str

    @property
    def still_open(self) -> bool:
        """Does the file say this stint has not ended?

        ``removed is None and not still_open`` is the other case: it ended, and
        no source says when.
        """
        return self.removed_quality == DATE_OPEN

    @property
    def added_stated(self) -> bool:
        """Does a source state when this stint began, exactly or as a bound?"""
        return self.added_quality in (DATE_EXACT, DATE_BOUNDED)

    @property
    def removed_stated(self) -> bool:
        """Does a source state how this stint ended — with a date, or not at all?"""
        return self.removed_quality in (DATE_EXACT, DATE_BOUNDED, DATE_OPEN)

    @property
    def dates_bounded(self) -> bool:
        """Does either end rest on a bound rather than on a stated day?"""
        return DATE_BOUNDED in (self.added_quality, self.removed_quality)

    @property
    def quality(self) -> str:
        """The stint's date quality: the weaker of its two ends.

        :data:`DATE_UNSTATED` if either end is missing, else
        :data:`DATE_BOUNDED` if either end is an upper bound, else
        :data:`DATE_EXACT` — an open removal is a stated fact, not a gap, so it
        does not weaken the stint.
        """
        if DATE_UNSTATED in (self.added_quality, self.removed_quality):
            return DATE_UNSTATED
        return DATE_BOUNDED if self.dates_bounded else DATE_EXACT

    def window(self, unknown: str, bounded: str = BOUNDED_AS_UNKNOWN) -> Window | None:
        """The eligible stretch this stint implies, or ``None`` for "no stretch".

        Both ends are inclusive: a symbol is a member **on** its join date and
        **on** its removal date. Entries are decided at a close and filled at
        the next open (Contract 11), so a signal generated on the removal date
        still fills the following morning — the same one-bar lag every other
        gate in the engine has, and it is the reason this boundary is stated
        here rather than left to a reader to infer.

        Args:
            unknown: one of :data:`UNKNOWN_POLICIES` — what to do about an end
                no source states.
            bounded: one of :data:`BOUNDED_POLICIES` — whether an end a source
                states only as an upper bound counts as stated. Under
                :data:`BOUNDED_AS_UNKNOWN` (the default) it does not, so the
                bound is *erased* rather than believed: under ``include`` the
                end opens out to the limit, and under ``exclude`` the stint
                contributes nothing at all.
        """
        _check_unknown(unknown)
        _check_bounded(bounded)
        keep_bounds = bounded == BOUNDED_AS_EXACT
        permissive = unknown == UNKNOWN_INCLUDE
        added_known = self.added_quality == DATE_EXACT or (
            self.added_quality == DATE_BOUNDED and keep_bounds
        )
        removed_known = self.removed_quality in (DATE_EXACT, DATE_OPEN) or (
            self.removed_quality == DATE_BOUNDED and keep_bounds
        )
        if not (added_known and removed_known) and not permissive:
            return None
        return (
            self.added if added_known else None,
            self.removed if removed_known else None,
        )


@dataclass(frozen=True)
class StintCounts:
    """The date quality of one membership file's stints, all of them.

    Counted over the whole file rather than over the instruments a run traded,
    because it describes the *evidence* the gate is built from: a file that is
    half upper bounds is half upper bounds whichever slice of it a run uses.

    ``stints == exact + bounded + undated`` by construction: each stint is
    classified by :attr:`MembershipInterval.quality`, its weaker end.
    """

    source: str
    #: Rows in the file.
    stints: int
    #: Both ends stated as fact (an open removal counts: the file states it).
    exact: int
    #: No end unstated, but at least one is an upper bound — the stint's
    #: boundary is an artefact of the snapshot cadence, not a market event.
    bounded: int
    #: At least one end no source states at all.
    undated: int


@dataclass(frozen=True)
class MembershipCoverage:
    """How much of a universe point-in-time membership can honestly speak to.

    Most fields count *instruments*, not stints. ``gated`` is the only
    population membership applies to; the arithmetic that matters is
    ``gated == stated_join + unknown_join + no_membership_row``.

    ``by_source_stints`` and the ``stints*`` properties derived from it are the
    exception: they count rows of the enabled membership files, so a reader can
    see how much of the whole answer rests on approximate dates rather than
    stated ones.
    """

    #: Everything in the universe, gated or not.
    instruments: int
    #: Instruments an index membership file governs (S&P 500/400/600 stocks).
    gated: int
    #: ETFs and ``extra_symbols``: never index constituents, so never gated.
    ungated: int
    #: Gated instruments with at least one stint whose join date this run is
    #: willing to treat as stated. Under :data:`BOUNDED_AS_UNKNOWN` that means
    #: an exact date; under :data:`BOUNDED_AS_EXACT` a bound counts too, so
    #: this number moves with the policy — deliberately, because it is a
    #: statement about this run and not about the file.
    stated_join: int
    #: Gated instruments that appear in a membership file, but with no such
    #: join date anywhere. THE number to watch: under ``exclude`` these are
    #: dropped, under ``include`` they silently reinstate the whole bias.
    unknown_join: int
    #: Gated instruments whose join date is known only as an upper bound — no
    #: exact join anywhere, at least one bounded one. A breakdown, not a fourth
    #: term of the sum above: these sit inside ``unknown_join`` under
    #: :data:`BOUNDED_AS_UNKNOWN` and inside ``stated_join`` under
    #: :data:`BOUNDED_AS_EXACT`, which is exactly the size of that choice.
    bounded_join: int
    #: Gated instruments with no row in any enabled membership file at all.
    no_membership_row: int
    #: Instruments with no eligible day at all under the chosen policies.
    excluded: int
    #: ``(source, gated, stated_join)`` per index, in :data:`MEMBERSHIP_SOURCES`
    #: order — this is where the uneven coverage becomes visible.
    by_source: tuple[tuple[str, int, int], ...]
    #: Date quality per enabled membership file, in :data:`MEMBERSHIP_SOURCES`
    #: order. Rows of the file, not instruments of this run.
    by_source_stints: tuple[StintCounts, ...]
    #: Which bounded-date policy produced the counts above.
    bounded_policy: str

    @property
    def coverage_pct(self) -> float:
        """Percent of gated instruments whose join date a source actually states."""
        return 100.0 * self.stated_join / self.gated if self.gated else 0.0

    @property
    def stints(self) -> int:
        """Stints in the enabled membership files."""
        return sum(counts.stints for counts in self.by_source_stints)

    @property
    def stints_exact(self) -> int:
        """Stints whose two ends are both stated as fact."""
        return sum(counts.exact for counts in self.by_source_stints)

    @property
    def stints_bounded(self) -> int:
        """Stints resting on at least one upper bound and no missing date."""
        return sum(counts.bounded for counts in self.by_source_stints)

    @property
    def stints_undated(self) -> int:
        """Stints with at least one end no source states."""
        return sum(counts.undated for counts in self.by_source_stints)

    @property
    def approximate_pct(self) -> float:
        """Percent of stints that rest on a bound or a missing date.

        The single number that says how much of any point-in-time answer is an
        approximation. It is a property of the files, so it does not move with
        the policy — only what the run *does* about it moves.
        """
        total = self.stints
        return 100.0 * (self.stints_bounded + self.stints_undated) / total if total else 0.0


def _check_unknown(unknown: str) -> None:
    if unknown not in UNKNOWN_POLICIES:
        raise ValueError(
            f"The unknown-date policy must be one of {', '.join(UNKNOWN_POLICIES)}, but it is "
            f"{unknown!r}. '{UNKNOWN_EXCLUDE}' drops a stint whose join date no source states; "
            f"'{UNKNOWN_INCLUDE}' treats it as a member from the beginning of the data."
        )


def _check_bounded(bounded: str) -> None:
    if bounded not in BOUNDED_POLICIES:
        raise ValueError(
            f"The bounded-date policy must be one of {', '.join(BOUNDED_POLICIES)}, but it is "
            f"{bounded!r}. '{BOUNDED_AS_EXACT}' reads 'no later than 2015-06-30' as "
            f"'2015-06-30'; '{BOUNDED_AS_UNKNOWN}' reads it as a date no source states, so the "
            f"unknown-date policy decides what happens to it."
        )


#: Anomalies one date cell can carry. Neither stops a run, both are counted and
#: logged, and both take the same safe path: the date is never treated as a
#: fact it is not.
_MALFORMED, _UNRECOGNISED_BOUND = "malformed", "unrecognised-bound"


def _read_date_cell(
    raw_date: str, raw_bound: str, *, blank_means: str, certified: bool
) -> tuple[date | None, str, str]:
    """Read one date cell and its provenance cell.

    Returns ``(date or None, quality, anomaly)``. ``blank_means`` is what an
    empty date cell says in this column: :data:`DATE_UNSTATED` for ``added``
    (nobody records the join) and :data:`DATE_OPEN` for ``removed`` (the stint
    has not ended) — the same cell, two entirely different facts.

    A malformed date is a data bug rather than a documented gap, so it is
    counted and logged — but it is never allowed to crash a run, and it is
    never quietly promoted to a date.

    ``certified`` says whether this column has a bound column beside it at all.
    Where one exists, only the tokens it defines certify a date: a blank cell
    and a token nobody recognises both leave the date standing as a bound,
    because a file that can say "exact" and does not say it has not said it.
    Where no bound column exists the file predates the vocabulary entirely — it
    is read as it always was, on the strength of the builder refusing to write
    a file that drops provenance, and the caller says so out loud.
    """
    text = raw_date.strip()
    if not text:
        return None, blank_means, ""
    if text.lower() == MEMBERSHIP_UNKNOWN:
        return None, DATE_UNSTATED, ""
    try:
        value = date.fromisoformat(text)
    except ValueError:
        return None, DATE_UNSTATED, _MALFORMED
    if not certified:
        return value, DATE_EXACT, ""
    token = raw_bound.strip().lower()
    if token == BOUND_EXACT:
        return value, DATE_EXACT, ""
    if token == BOUND_NO_LATER_THAN:
        return value, DATE_BOUNDED, ""
    return value, DATE_BOUNDED, "" if not token else _UNRECOGNISED_BOUND


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
        # The provenance columns arrived after the files did, one per date
        # column. A file without them still parses; what it cannot do is pass
        # unnoticed, because nothing in it distinguishes a day a source named
        # from a day inferred by diffing snapshots.
        no_provenance = [c for c in ("added_bound", "removed_bound") if c not in fieldnames]
        intervals: list[MembershipInterval] = []
        malformed = 0
        unrecognised = 0
        for row in reader:
            symbol = to_yahoo_symbol(row.get("symbol") or "")
            if not symbol:
                continue
            # A blank `added` is "nobody records the join"; a blank `removed` is
            # the file saying "still a member". `unknown` and a malformed date
            # in either column are "it happened and nobody records when".
            added, added_quality, added_note = _read_date_cell(
                row.get("added") or "",
                row.get("added_bound") or "",
                blank_means=DATE_UNSTATED,
                certified="added_bound" not in no_provenance,
            )
            removed, removed_quality, removed_note = _read_date_cell(
                row.get("removed") or "",
                row.get("removed_bound") or "",
                blank_means=DATE_OPEN,
                certified="removed_bound" not in no_provenance,
            )
            for note in (added_note, removed_note):
                malformed += note == _MALFORMED
                unrecognised += note == _UNRECOGNISED_BOUND
            intervals.append(
                MembershipInterval(
                    symbol=symbol,
                    name=(row.get("name") or "").strip() or symbol,
                    source=source,
                    added=added,
                    removed=removed,
                    added_quality=added_quality,
                    removed_quality=removed_quality,
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
    if unrecognised:
        log.warning(
            "%s has %d date(s) whose %s/%s cell is neither '%s' nor '%s'. They are read as upper "
            "bounds rather than as exact days: nothing in the file certifies them, and a date "
            "nothing certifies must not be treated as a fact.",
            f"{stem}.csv",
            unrecognised,
            "added_bound",
            "removed_bound",
            BOUND_EXACT,
            BOUND_NO_LATER_THAN,
        )
    if no_provenance:
        log.warning(
            "%s has no %s column, so nothing in it says which of those dates a source named and "
            "which were inferred by diffing quarterly snapshots. They are read as exact days, "
            "which is what the file meant before the column existed — but nothing here can "
            "confirm it, so treat any point-in-time result from this file as unverified and "
            "rebuild it with scripts/build_membership.py.",
            f"{stem}.csv",
            " or ".join(no_provenance),
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


def _resolve_bounded(cfg: Config, bounded: str | None) -> str:
    """The bounded-date policy in force: the argument, or the config's setting.

    Every entry point takes ``bounded=None`` and lands here, so a caller that
    knows nothing about bounds — including one written before they existed —
    still gets the policy the user configured rather than a hard-coded guess.
    """
    policy = cfg.universe.membership_bounded if bounded is None else bounded
    _check_bounded(policy)
    return policy


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
    bounded: str | None = None,
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
        bounded: one of :data:`BOUNDED_POLICIES` — what to do about a stint
            whose start or end a source states only as an upper bound.
            ``None``, the default, means ``cfg.universe.membership_bounded``.

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
    policy = _resolve_bounded(cfg, bounded)
    sources = _enabled_membership_sources(cfg)
    by_symbol = _intervals_by_symbol(sources)

    windows: dict[str, tuple[Window, ...]] = {}
    for instrument in load(cfg) if instruments is None else instruments:
        if instrument.source not in MEMBERSHIP_SOURCES:
            windows[instrument.symbol] = ((None, None),)
            continue
        stints = by_symbol.get(instrument.symbol, [])
        usable = [w for w in (stint.window(unknown, policy) for stint in stints) if w is not None]
        windows[instrument.symbol] = _merge_windows(usable) if usable else ()
    return windows


def _in_windows(windows: tuple[Window, ...], day: date) -> bool:
    """Is ``day`` inside any window? Both ends inclusive; ``None`` is open."""
    return any(
        (start is None or day >= start) and (end is None or day <= end) for start, end in windows
    )


def members_asof(
    day: date,
    cfg: Config,
    *,
    unknown: str = UNKNOWN_EXCLUDE,
    bounded: str | None = None,
) -> list[Instrument]:
    """The instruments of :func:`load` that were index members on ``day``.

    Args:
        day: the date to ask about.
        cfg: the configuration, as for :func:`load`.
        unknown: one of :data:`UNKNOWN_POLICIES`; defaults to the conservative
            ``exclude``, so a symbol whose join date no source states is **not**
            treated as a member for all of history.
        bounded: one of :data:`BOUNDED_POLICIES`; ``None`` means
            ``cfg.universe.membership_bounded``, which defaults to reading a
            date stated only as an upper bound as a date no source states.

    Returns:
        A subset of ``load(cfg)`` in the same order. ETFs and extra symbols are
        always present: they are not index constituents and membership has
        nothing to say about them.

    Note:
        This rebuilds every symbol's windows on each call. Asking about one day
        is what it is for; asking about a whole calendar should take
        :func:`membership_windows` once and test days against the result.
    """
    windows = membership_windows(cfg, unknown=unknown, bounded=bounded)
    return [i for i in load(cfg) if _in_windows(windows.get(i.symbol, ()), day)]


def stint_counts(source: str) -> StintCounts:
    """The date quality of one membership file, counted over every row in it.

    The file's own composition, independent of any run: how many stints rest on
    two stated dates, how many on at least one upper bound, and how many on a
    date nobody records.
    """
    stints = _read_membership(source)
    tally = {DATE_EXACT: 0, DATE_BOUNDED: 0, DATE_UNSTATED: 0}
    for stint in stints:
        tally[stint.quality] += 1
    return StintCounts(
        source=source,
        stints=len(stints),
        exact=tally[DATE_EXACT],
        bounded=tally[DATE_BOUNDED],
        undated=tally[DATE_UNSTATED],
    )


def membership_coverage(
    cfg: Config,
    *,
    instruments: Iterable[Instrument] | None = None,
    unknown: str = UNKNOWN_EXCLUDE,
    bounded: str | None = None,
) -> MembershipCoverage:
    """Count what point-in-time membership does and does not know about this universe.

    This is the number a run has to publish next to its results. Join-date
    coverage is uneven across the three indices and much of it is approximate,
    so "point-in-time membership was applied" on its own is not a statement
    anyone can check.

    Two populations are counted and the difference matters. The instrument
    counts describe *this run's* symbols under *this run's* policies; the
    ``by_source_stints`` composition describes the files, and does not move
    when a policy does. A summary line covering both is logged at INFO, because
    a result that does not travel with its date quality is a result nobody can
    weigh.
    """
    _check_unknown(unknown)
    policy = _resolve_bounded(cfg, bounded)
    sources = _enabled_membership_sources(cfg)
    by_symbol = _intervals_by_symbol(sources)
    instruments = list(load(cfg) if instruments is None else instruments)

    gated = stated = unknown_join = bounded_join = no_row = excluded = 0
    per_source: dict[str, list[int]] = {source: [0, 0] for source in MEMBERSHIP_SOURCES}

    for instrument in instruments:
        if instrument.source not in MEMBERSHIP_SOURCES:
            continue
        gated += 1
        counts = per_source[instrument.source]
        counts[0] += 1
        stints = by_symbol.get(instrument.symbol, [])
        # "Stated" means stated the way this run reads the file: an exact date
        # always, a bound only when the policy says a bound is good enough.
        has_join = any(
            stint.added_quality == DATE_EXACT
            or (stint.added_quality == DATE_BOUNDED and policy == BOUNDED_AS_EXACT)
            for stint in stints
        )
        if not stints:
            no_row += 1
        elif has_join:
            stated += 1
            counts[1] += 1
        else:
            unknown_join += 1
        qualities = {stint.added_quality for stint in stints}
        if DATE_BOUNDED in qualities and DATE_EXACT not in qualities:
            bounded_join += 1
        usable = [w for w in (stint.window(unknown, policy) for stint in stints) if w is not None]
        if not usable:
            excluded += 1

    coverage = MembershipCoverage(
        instruments=len(instruments),
        gated=gated,
        ungated=len(instruments) - gated,
        stated_join=stated,
        unknown_join=unknown_join,
        bounded_join=bounded_join,
        no_membership_row=no_row,
        excluded=excluded,
        by_source=tuple(
            (source, per_source[source][0], per_source[source][1])
            for source in MEMBERSHIP_SOURCES
            if source in sources
        ),
        by_source_stints=tuple(
            stint_counts(source) for source in MEMBERSHIP_SOURCES if source in sources
        ),
        bounded_policy=policy,
    )
    _log_composition(coverage, unknown)
    return coverage


def _log_composition(coverage: MembershipCoverage, unknown: str) -> None:
    """Say out loud how much of this answer rests on approximate dates.

    Two lines, because they are two different facts: what the files know, and
    what this run does about it. The first does not move when a policy does.
    """
    log.info(
        "Membership date quality: %d stints — %d exact, %d resting on an upper bound, %d with a "
        "date no source states (%.1f%% approximate). Per index: %s.",
        coverage.stints,
        coverage.stints_exact,
        coverage.stints_bounded,
        coverage.stints_undated,
        coverage.approximate_pct,
        "; ".join(
            f"{c.source} {c.stints} ({c.exact} exact, {c.bounded} bounded, {c.undated} undated)"
            for c in coverage.by_source_stints
        )
        or "no index enabled",
    )
    log.info(
        "Membership policy: bounded dates read as '%s', unstated dates as '%s'. Of %d gated "
        "symbols, %d have a join date this run will use and %d do not; %d rest on a bounded join "
        "date, which is the size of the bounded-date choice; %d have no eligible day at all.",
        coverage.bounded_policy,
        unknown,
        coverage.gated,
        coverage.stated_join,
        coverage.unknown_join,
        coverage.bounded_join,
        coverage.excluded,
    )
