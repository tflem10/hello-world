"""Tests for FROZEN CONTRACT 4 — swing.universe.

The universe comes from committed CSV snapshots, so these tests are exact:
they assert on the real files that ship with the package, offline.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import inspect
import re
from datetime import date
from pathlib import Path

import pytest

from swing import universe as universe_mod
from swing.config import Config, UniverseCfg
from swing.universe import (
    INDEX_SOURCES,
    MEMBERSHIP_SOURCES,
    UNKNOWN_EXCLUDE,
    UNKNOWN_INCLUDE,
    UNKNOWN_POLICIES,
    Instrument,
    MembershipInterval,
    UniverseError,
    asset_dir,
    load,
    load_csv,
    members_asof,
    membership,
    membership_coverage,
    membership_windows,
    symbols,
    to_schwab_symbol,
    to_yahoo_symbol,
)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,9}$")

#: Guard rails for the ETF snapshot, which is curated and has already grown
#: once (40 -> 137, for deeper inception history and broader exposure). These
#: two numbers are the only hard counts in this module: everything else derives
#: from :func:`etf_count`, so a future curation change updates one place. They
#: are wide enough not to churn, and tight enough that an emptied or truncated
#: file still fails loudly instead of quietly shrinking the universe.
MIN_ETFS = 100
MAX_ETFS = 500


def etf_count() -> int:
    """How many ETFs the committed snapshot actually holds, read from the file."""
    return len(load_csv("etfs"))


def _cfg(**universe_kwargs) -> Config:
    return Config(universe=UniverseCfg(**universe_kwargs))


# ---------------------------------------------------------------------------
# the committed snapshots
# ---------------------------------------------------------------------------


def test_every_snapshot_file_is_present() -> None:
    for stem in INDEX_SOURCES:
        assert (asset_dir() / f"{stem}.csv").is_file(), stem


@pytest.mark.parametrize("stem", sorted(INDEX_SOURCES))
def test_snapshots_have_a_symbol_name_header(stem: str) -> None:
    path = asset_dir() / f"{stem}.csv"
    with path.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == ["symbol", "name"]


@pytest.mark.parametrize("stem", sorted(INDEX_SOURCES))
def test_snapshot_symbols_are_yahoo_compatible(stem: str) -> None:
    rows = load_csv(stem)
    assert rows, stem
    for symbol, name in rows:
        assert SYMBOL_RE.match(symbol), f"{stem}: {symbol!r} is not a plausible Yahoo ticker"
        assert "." not in symbol, f"{stem}: {symbol!r} still uses a dot; Yahoo wants a dash"
        assert name.strip(), f"{stem}: {symbol} has no name"


@pytest.mark.parametrize("stem", sorted(INDEX_SOURCES))
def test_snapshots_have_no_duplicate_symbols(stem: str) -> None:
    found = [symbol for symbol, _ in load_csv(stem)]
    assert len(found) == len(set(found))


def test_index_snapshots_are_the_expected_size() -> None:
    assert len(load_csv("sp500")) > 480
    assert len(load_csv("sp400")) > 380
    assert len(load_csv("sp600")) > 570
    assert MIN_ETFS <= etf_count() <= MAX_ETFS


def test_the_etf_snapshot_is_neither_empty_nor_truncated() -> None:
    """Every data row in the file becomes an instrument, and there are plenty of them.

    The count is deliberately not pinned to a literal — the list is curated and
    will grow again — but silently losing it must not be a quiet event. Two
    independent things are checked: the parsed count against the file's own row
    count (so a parser that drops rows is caught), and the floor (so an emptied
    or half-written file is caught).
    """
    path = asset_dir() / "etfs.csv"
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows_in_file = [row for row in csv.reader(fh) if row and any(cell.strip() for cell in row)]
    data_rows = len(rows_in_file) - 1  # minus the header

    assert etf_count() == data_rows, "load_csv silently dropped rows from etfs.csv"
    assert data_rows >= MIN_ETFS, f"etfs.csv holds only {data_rows} rows — truncated or emptied?"


def test_share_classes_use_dashes() -> None:
    sp500 = dict(load_csv("sp500"))
    assert "BRK-B" in sp500
    assert "BF-B" in sp500
    assert "BRK.B" not in sp500


def test_etf_list_covers_index_sectors_and_the_usual_hedges() -> None:
    etfs = dict(load_csv("etfs"))
    broad = {"SPY", "QQQ", "IWM", "DIA", "MDY", "IJR", "RSP"}
    sectors = {"XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC"}
    other = {"GLD", "SLV", "TLT", "IEF", "HYG", "LQD", "XBI", "SMH", "XHB", "ITB", "KRE", "XME"}
    assert broad <= set(etfs)
    assert sectors <= set(etfs)
    assert other <= set(etfs)


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_returns_the_whole_universe_by_default() -> None:
    instruments = load(Config())

    assert len(instruments) > 1400
    assert all(isinstance(i, Instrument) for i in instruments)
    assert len({i.symbol for i in instruments}) == len(instruments)  # de-duped
    assert {i.kind for i in instruments} == {"stock", "etf"}
    assert {i.source for i in instruments} == {"sp500", "sp400", "sp600", "etf"}
    assert sum(1 for i in instruments if i.kind == "etf") == etf_count()


def test_instruments_are_frozen_value_objects() -> None:
    instrument = load(_cfg(sp500=False, sp400=False, sp600=False))[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        instrument.symbol = "NOPE"  # type: ignore[misc]


def test_toggles_select_only_the_requested_lists() -> None:
    etf_only = load(_cfg(sp500=False, sp400=False, sp600=False, etfs=True))
    assert len(etf_only) == etf_count()
    assert {i.source for i in etf_only} == {"etf"}
    assert {i.kind for i in etf_only} == {"etf"}

    large_only = load(_cfg(sp400=False, sp600=False, etfs=False))
    assert {i.source for i in large_only} == {"sp500"}
    assert len(large_only) == len(load_csv("sp500"))


def test_lists_are_concatenated_in_index_order() -> None:
    instruments = load(_cfg(etfs=False))
    sources = [i.source for i in instruments]
    assert sources[0] == "sp500"
    assert sources.index("sp400") > 0
    assert sources.index("sp600") > sources.index("sp400")
    # each source appears as one contiguous run
    for source in ("sp500", "sp400", "sp600"):
        first, last = sources.index(source), len(sources) - 1 - sources[::-1].index(source)
        assert sources[first : last + 1] == [source] * (last - first + 1)


def test_extra_symbols_are_appended_normalised_and_deduped() -> None:
    instruments = load(
        _cfg(
            sp500=False, sp400=False, sp600=False, etfs=True, extra_symbols=("brk.b", "spy", "gld")
        )
    )
    by_symbol = {i.symbol: i for i in instruments}

    # SPY and GLD are already ETFs, so they are not added twice: the whole ETF
    # list plus BRK-B, and nothing else.
    assert len(instruments) == etf_count() + 1
    assert by_symbol["SPY"].source == "etf"
    assert by_symbol["GLD"].source == "etf"

    extra = by_symbol["BRK-B"]
    assert extra.source == "extra"
    assert extra.kind == "stock"
    assert extra.name == "BRK-B"


def test_extra_symbol_that_is_a_known_etf_is_typed_as_an_etf() -> None:
    instruments = load(
        _cfg(sp500=False, sp400=False, sp600=False, etfs=False, extra_symbols=("qqq",))
    )
    assert instruments == [Instrument(symbol="QQQ", name="QQQ", kind="etf", source="extra")]


def test_extra_symbols_do_not_duplicate_index_members() -> None:
    instruments = load(_cfg(sp400=False, sp600=False, etfs=False, extra_symbols=("AAPL",)))
    assert sum(1 for i in instruments if i.symbol == "AAPL") == 1
    assert next(i for i in instruments if i.symbol == "AAPL").source == "sp500"


def test_symbols_helper_returns_plain_tickers() -> None:
    cfg = _cfg(sp500=False, sp400=False, sp600=False)
    assert symbols(cfg) == [i.symbol for i in load(cfg)]


def test_load_is_stable_across_calls() -> None:
    assert load(Config()) == load(Config())


# ---------------------------------------------------------------------------
# symbol normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BRK.B", "BRK-B"),
        ("brk.b", "BRK-B"),
        (" bf.b ", "BF-B"),
        ("AAPL", "AAPL"),
        ("aapl", "AAPL"),
        ("CWEN.A", "CWEN-A"),
        ("BRK B", "BRKB"),
    ],
)
def test_to_yahoo_symbol(raw: str, expected: str) -> None:
    assert to_yahoo_symbol(raw) == expected


@pytest.mark.parametrize(
    ("yahoo", "schwab"),
    [
        ("BRK-B", "BRK/B"),
        ("BF-B", "BF/B"),
        ("brk.b", "BRK/B"),  # normalised on the way through
        ("CWEN-A", "CWEN/A"),
        ("AAPL", "AAPL"),
        ("SPY", "SPY"),
        ("ABC-DE", "ABC-DE"),  # not a single-letter class: left alone
        ("", ""),
    ],
)
def test_to_schwab_symbol(yahoo: str, schwab: str) -> None:
    """Audit BUG-039: Yahoo-form class shares sent verbatim simply return nothing."""
    assert to_schwab_symbol(yahoo) == schwab


def test_every_dual_class_name_in_the_universe_translates() -> None:
    dashed = [sym for sym, _ in load_csv("sp500") if "-" in sym]
    assert dashed  # the snapshot really does contain some
    for symbol in dashed:
        assert to_schwab_symbol(symbol) == symbol.replace("-", "/")


def test_load_csv_reports_a_missing_snapshot_clearly() -> None:
    with pytest.raises(UniverseError, match="missing"):
        load_csv("sp999")


def test_asset_dir_is_inside_the_installed_package() -> None:
    directory = asset_dir()
    assert isinstance(directory, Path)
    assert directory.is_dir()
    assert directory.name == "universe"
    assert (directory / "sp500.csv").is_file()


# ---------------------------------------------------------------------------
# packaging and parsing details (audit DEBT-014, PERF-011)
# ---------------------------------------------------------------------------


def test_all_names_the_whole_public_surface() -> None:
    """Audit DEBT-014: `UniverseError` and `symbols` were missing from __all__."""
    exported = set(universe_mod.__all__)
    assert {"UniverseError", "symbols", "to_schwab_symbol", "to_yahoo_symbol"} <= exported
    for name in exported:
        assert hasattr(universe_mod, name), name


def test_a_byte_order_mark_does_not_hide_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit DEBT-014: a BOM landed inside the first header name.

    Under plain utf-8 the header parses as '﻿symbol', so the file is
    reported as having no 'symbol' column — a misleading error for a file that
    is perfectly fine, and the exact thing a spreadsheet round-trip produces.
    """
    snapshot = tmp_path / "bommed.csv"
    snapshot.write_text("symbol,name\nAAPL,Apple Inc.\n", encoding="utf-8-sig")
    assert snapshot.read_bytes().startswith(b"\xef\xbb\xbf")

    @contextlib.contextmanager
    def fake_snapshot_path(stem: str):
        yield snapshot

    monkeypatch.setattr(universe_mod, "_snapshot_path", fake_snapshot_path)
    universe_mod._read_snapshot.cache_clear()
    try:
        assert load_csv("bommed") == [("AAPL", "Apple Inc.")]
    finally:
        universe_mod._read_snapshot.cache_clear()


def test_snapshots_are_read_through_importlib_resources() -> None:
    """`as_file` rather than `str(files(...))`, so a zipped install still works."""
    source = inspect.getsource(universe_mod)
    assert "resources.as_file" in source
    assert "Path(str(resources.files" not in source


def test_a_snapshot_is_parsed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit PERF-011: the CSVs were re-parsed on every call, etfs.csv twice per load()."""
    universe_mod._read_snapshot.cache_clear()
    universe_mod._etf_symbols.cache_clear()
    opened: list[str] = []

    real = universe_mod._snapshot_path

    @contextlib.contextmanager
    def counting(stem: str):
        opened.append(stem)
        with real(stem) as path:
            yield path

    monkeypatch.setattr(universe_mod, "_snapshot_path", counting)
    try:
        cfg = _cfg(sp400=False, sp600=False, extra_symbols=("qqq",))
        load(cfg)
        load(cfg)
        load_csv("sp500")
        assert sorted(opened) == ["etfs", "sp500"]
    finally:
        universe_mod._read_snapshot.cache_clear()
        universe_mod._etf_symbols.cache_clear()


def test_load_csv_hands_back_a_list_the_caller_may_mutate() -> None:
    """The cache is internal: mutating a result must not corrupt the next read."""
    rows = load_csv("etfs")
    length = len(rows)
    rows.append(("TAMPERED", "nope"))
    assert len(load_csv("etfs")) == length


# ---------------------------------------------------------------------------
# point-in-time membership: the committed files
# ---------------------------------------------------------------------------


def test_every_membership_file_is_present_and_parses() -> None:
    for source in MEMBERSHIP_SOURCES:
        assert (asset_dir() / f"{source}-membership.csv").is_file(), source
        stints = membership(source)
        assert stints, source
        assert all(SYMBOL_RE.match(stint.symbol) for stint in stints), source


def test_membership_files_are_a_superset_of_the_plain_snapshots() -> None:
    """Every current member has a row; departed companies have one too."""
    for source in MEMBERSHIP_SOURCES:
        current = {symbol for symbol, _ in load_csv(source)}
        recorded = {stint.symbol for stint in membership(source)}
        assert current <= recorded, f"{source}: {sorted(current - recorded)[:5]} have no stint"
        assert len(recorded) > len(current), source


def test_membership_intervals_are_frozen_value_objects() -> None:
    stint = membership("sp500")[0]
    assert isinstance(stint, MembershipInterval)
    assert dataclasses.is_dataclass(stint)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stint.added = date(1999, 1, 1)  # type: ignore[misc]


def test_membership_is_refused_for_a_source_that_has_no_file() -> None:
    """ETFs and extras were never added to or removed from an index."""
    with pytest.raises(UniverseError, match="no membership file"):
        membership("etfs")


def test_membership_hands_back_a_list_the_caller_may_mutate() -> None:
    stints = membership("sp600")
    length = len(stints)
    stints.clear()
    assert len(membership("sp600")) == length


def test_the_sp500_join_dates_are_complete_and_the_smaller_indices_are_not_asserted() -> None:
    """Coverage is uneven, and the run has to be able to say so.

    Only the S&P 500 claim is pinned: it is complete today and a data
    improvement can only keep it that way. The other two are asserted
    structurally, so a package that improves their coverage does not fail a
    test for getting better.
    """
    coverage = membership_coverage(Config())
    by_source = {source: (n, stated) for source, n, stated in coverage.by_source}
    members, stated = by_source["sp500"]
    assert stated == members > 480, "the S&P 500 lost join dates it used to have"
    for source in ("sp400", "sp600"):
        members, stated = by_source[source]
        assert 0 < stated <= members


def test_point_in_time_membership_really_does_shrink_the_early_universe() -> None:
    """The whole point, on the real files: 2010 was not 2026.

    The threshold is loose on purpose. The membership data belongs to another
    package and is being improved, and better coverage moves this number *up* —
    but hundreds of today's members demonstrably joined after 2010, so a
    materially smaller universe back then is a fact about the world rather than
    about the state of the CSVs. ETFs must survive it untouched.
    """
    cfg = Config()
    everything = load(cfg)
    back_then = members_asof(date(2010, 1, 4), cfg)
    assert len(back_then) < len(everything) * 0.9
    etfs = {i.symbol for i in everything if i.kind == "etf"}
    assert etfs <= {i.symbol for i in back_then}


# ---------------------------------------------------------------------------
# point-in-time membership: parsing and the unknown-date policy
# ---------------------------------------------------------------------------

#: A tiny universe with one of every case that matters, served in place of the
#: committed CSVs. ``GONE`` is in the membership file but not in the snapshot —
#: a company that left and is therefore not tradable at all today.
FAKE_SNAPSHOTS = {
    "sp500": "symbol,name\nALWAYS,Always In\nJOIN,Joined Midway\n",
    "sp400": "symbol,name\nMOVER,Moved Up\nTWICE,Two Stints\n",
    "sp600": "symbol,name\nNODATE,No Join Date\nODD,Odd Dates\n",
    "etfs": "symbol,name\nFAKEETF,A Fake ETF\n",
    "sp500-membership": (
        "symbol,name,added,removed\n"
        "ALWAYS,Always In,1990-01-02,\n"
        "JOIN,Joined Midway,2015-06-01,\n"
        "GONE,Departed Inc,2001-01-01,2016-11-01\n"
    ),
    "sp400-membership": (
        "symbol,name,added,removed\nMOVER,Moved Up,2018-02-01,\nTWICE,Two Stints,2013-01-01,\n"
    ),
    "sp600-membership": (
        "symbol,name,added,removed\n"
        "MOVER,Moved Up,2005-03-04,2012-07-08\n"
        "TWICE,Two Stints,2010-01-01,2014-12-31\n"
        "NODATE,No Join Date,,\n"
        "ODD,Odd Dates,unknown,unknown\n"
        "BADDATE,Bad Dates,not-a-date,2020-13-45\n"
    ),
}


@pytest.fixture
def fake_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Serve synthetic snapshots so boundaries can be asserted exactly.

    Returns a callable that installs a ``{stem: csv text}`` mapping; anything
    not installed falls through to the committed file. Both parse caches are
    cleared on the way in and on the way out, so no synthetic row can leak into
    another test.
    """
    real = universe_mod._snapshot_path
    served: dict[str, Path] = {}

    @contextlib.contextmanager
    def fake(stem: str):
        if stem in served:
            yield served[stem]
            return
        with real(stem) as path:
            yield path

    def clear() -> None:
        universe_mod._read_snapshot.cache_clear()
        universe_mod._read_membership.cache_clear()
        universe_mod._etf_symbols.cache_clear()

    def install(files: dict[str, str] = FAKE_SNAPSHOTS) -> Config:
        for stem, text in files.items():
            path = tmp_path / f"{stem}.csv"
            path.write_text(text, encoding="utf-8")
            served[stem] = path
        clear()
        return Config()

    monkeypatch.setattr(universe_mod, "_snapshot_path", fake)
    clear()
    yield install
    clear()


def test_a_membership_file_without_date_columns_is_refused_in_plain_english(
    fake_snapshots,
) -> None:
    fake_snapshots({"sp500-membership": "symbol,name\nAAPL,Apple\n"})
    with pytest.raises(UniverseError) as excinfo:
        membership("sp500")
    message = str(excinfo.value)
    assert "added, removed are missing" in message
    assert "build_membership.py" in message


def test_an_unknown_join_date_is_read_as_not_stated_never_as_a_date(fake_snapshots) -> None:
    fake_snapshots()
    odd = next(s for s in membership("sp600") if s.symbol == "ODD")
    assert odd.added is None
    assert not odd.added_stated
    assert odd.removed is None
    assert not odd.removed_stated, "'unknown' must not be read as 'still a member'"


def test_a_malformed_date_does_not_crash_and_does_not_become_a_date(fake_snapshots) -> None:
    """A junk cell is a data bug; it must not be a crash, and must not be a guess."""
    fake_snapshots()
    bad = next(s for s in membership("sp600") if s.symbol == "BADDATE")
    assert bad.added is None and bad.removed is None
    assert not bad.added_stated and not bad.removed_stated


def test_a_blank_removed_cell_means_still_a_member(fake_snapshots) -> None:
    fake_snapshots()
    always = next(s for s in membership("sp500") if s.symbol == "ALWAYS")
    gone = next(s for s in membership("sp500") if s.symbol == "GONE")
    assert always.still_open and always.removed is None and always.removed_stated
    assert not gone.still_open and gone.removed == date(2016, 11, 1)


def test_a_symbol_is_a_member_on_its_join_date_and_not_the_day_before(fake_snapshots) -> None:
    cfg = fake_snapshots()
    assert "JOIN" not in {i.symbol for i in members_asof(date(2015, 5, 31), cfg)}
    assert "JOIN" in {i.symbol for i in members_asof(date(2015, 6, 1), cfg)}


def test_a_symbol_is_a_member_on_its_removal_date_and_not_the_day_after(fake_snapshots) -> None:
    cfg = fake_snapshots()
    windows = membership_windows(cfg)
    assert windows["MOVER"][0] == (date(2005, 3, 4), date(2012, 7, 8))
    assert "MOVER" in {i.symbol for i in members_asof(date(2012, 7, 8), cfg)}
    assert "MOVER" not in {i.symbol for i in members_asof(date(2012, 7, 9), cfg)}


def test_a_symbol_that_joins_mid_backtest_is_absent_before_and_present_after(
    fake_snapshots,
) -> None:
    cfg = fake_snapshots()
    for day, expected in (
        (date(2010, 1, 4), False),
        (date(2015, 5, 29), False),
        (date(2015, 6, 1), True),
        (date(2026, 1, 2), True),
    ):
        present = "JOIN" in {i.symbol for i in members_asof(day, cfg)}
        assert present is expected, day


def test_a_symbol_with_no_stated_join_date_is_excluded_by_default(fake_snapshots) -> None:
    """The crux: an unknown join date must NOT mean 'member since the dawn of time'."""
    cfg = fake_snapshots()
    windows = membership_windows(cfg)
    assert windows["NODATE"] == ()
    assert windows["ODD"] == ()
    for day in (date(1995, 1, 3), date(2010, 1, 4), date(2026, 1, 2)):
        assert "NODATE" not in {i.symbol for i in members_asof(day, cfg)}


def test_the_include_policy_backdates_an_unknown_join_to_the_dawn_of_time(
    fake_snapshots,
) -> None:
    """The flattering reading, available on purpose so its cost can be measured."""
    cfg = fake_snapshots()
    windows = membership_windows(cfg, unknown=UNKNOWN_INCLUDE)
    assert windows["NODATE"] == ((None, None),)
    assert "NODATE" in {i.symbol for i in members_asof(date(1995, 1, 3), cfg, unknown="include")}


def test_the_two_policies_disagree_only_about_symbols_with_unstated_dates(
    fake_snapshots,
) -> None:
    cfg = fake_snapshots()
    strict = membership_windows(cfg, unknown=UNKNOWN_EXCLUDE)
    loose = membership_windows(cfg, unknown=UNKNOWN_INCLUDE)
    assert {s for s in strict if strict[s] != loose[s]} == {"NODATE", "ODD"}


def test_etfs_and_extra_symbols_are_never_gated_by_membership(fake_snapshots) -> None:
    """An ETF is not an index constituent, so membership has nothing to say about it."""
    fake_snapshots()
    cfg = Config(universe=UniverseCfg(extra_symbols=("ZZZZ",)))
    windows = membership_windows(cfg)
    assert windows["FAKEETF"] == ((None, None),)
    assert windows["ZZZZ"] == ((None, None),)
    for day in (date(1995, 1, 3), date(2026, 1, 2)):
        present = {i.symbol for i in members_asof(day, cfg)}
        assert {"FAKEETF", "ZZZZ"} <= present


def test_stints_in_different_indices_are_unioned_not_replaced(fake_snapshots) -> None:
    """MOVER was in the 600 until 2012 and is in the 400 since 2018; both count."""
    cfg = fake_snapshots()
    assert membership_windows(cfg)["MOVER"] == (
        (date(2005, 3, 4), date(2012, 7, 8)),
        (date(2018, 2, 1), None),
    )
    assert "MOVER" not in {i.symbol for i in members_asof(date(2015, 1, 2), cfg)}


def test_overlapping_stints_collapse_into_one_window(fake_snapshots) -> None:
    """TWICE is in the 600 from 2010-2014 and the 400 from 2013; that is one stretch."""
    cfg = fake_snapshots()
    assert membership_windows(cfg)["TWICE"] == ((date(2010, 1, 1), None),)


def test_a_disabled_index_cannot_confer_membership(fake_snapshots) -> None:
    """Turning the 600 off must not leave MOVER eligible through its 600 stint."""
    fake_snapshots()
    cfg = Config(universe=UniverseCfg(sp600=False))
    assert membership_windows(cfg)["MOVER"] == ((date(2018, 2, 1), None),)


def test_members_asof_is_a_subset_of_load_in_the_same_order(fake_snapshots) -> None:
    cfg = fake_snapshots()
    everything = [i.symbol for i in load(cfg)]
    members = [i.symbol for i in members_asof(date(2019, 6, 3), cfg)]
    assert set(members) <= set(everything)
    assert members == [s for s in everything if s in set(members)]


def test_coverage_counts_add_up_and_name_the_excluded(fake_snapshots) -> None:
    cfg = fake_snapshots()
    coverage = membership_coverage(cfg)
    assert coverage.instruments == 7  # 2 + 2 + 2 stocks + 1 ETF
    assert coverage.gated == 6
    assert coverage.ungated == 1
    assert coverage.gated == (
        coverage.stated_join + coverage.unknown_join + coverage.no_membership_row
    )
    assert coverage.unknown_join == 2  # NODATE, ODD
    assert coverage.no_membership_row == 0
    assert coverage.excluded == 2
    assert coverage.coverage_pct == pytest.approx(100 * 4 / 6)


def test_the_include_policy_excludes_nobody(fake_snapshots) -> None:
    cfg = fake_snapshots()
    assert membership_coverage(cfg, unknown=UNKNOWN_INCLUDE).excluded == 0


def test_coverage_can_be_asked_about_one_slice_of_the_universe(fake_snapshots) -> None:
    """A backtest reports on the symbols it traded, not on the whole file."""
    cfg = fake_snapshots()
    etfs_only = [i for i in load(cfg) if i.kind == "etf"]
    coverage = membership_coverage(cfg, instruments=etfs_only)
    assert (coverage.instruments, coverage.gated, coverage.ungated) == (1, 0, 1)
    assert coverage.coverage_pct == 0.0


def test_a_symbol_missing_from_every_membership_file_is_counted_and_excluded(
    fake_snapshots,
) -> None:
    """A snapshot symbol with no stint is a coverage hole, not a free pass."""
    cfg = fake_snapshots(
        {**FAKE_SNAPSHOTS, "sp500": "symbol,name\nALWAYS,Always In\nORPHAN,No Stint At All\n"}
    )
    assert membership_windows(cfg)["ORPHAN"] == ()
    coverage = membership_coverage(cfg)
    assert coverage.no_membership_row == 1
    assert "ORPHAN" not in {i.symbol for i in members_asof(date(2020, 1, 2), cfg)}


@pytest.mark.parametrize("policy", ["", "maybe", "EXCLUDE", None])
def test_an_unknown_date_policy_that_is_not_one_of_the_two_is_refused(policy) -> None:
    with pytest.raises(ValueError, match="unknown-date policy must be one of"):
        membership_windows(Config(), unknown=policy)


def test_the_two_policies_are_the_only_two() -> None:
    assert UNKNOWN_POLICIES == (UNKNOWN_EXCLUDE, UNKNOWN_INCLUDE) == ("exclude", "include")


def test_a_membership_file_is_parsed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same PERF-011 reasoning as the plain snapshots: the files never change."""
    universe_mod._read_membership.cache_clear()
    opened: list[str] = []
    real = universe_mod._snapshot_path

    @contextlib.contextmanager
    def counting(stem: str):
        opened.append(stem)
        with real(stem) as path:
            yield path

    monkeypatch.setattr(universe_mod, "_snapshot_path", counting)
    try:
        membership("sp500")
        membership("sp500")
        membership("sp500")
        assert opened == ["sp500-membership"]
    finally:
        universe_mod._read_membership.cache_clear()
