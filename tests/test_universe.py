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
from pathlib import Path

import pytest

from swing import universe as universe_mod
from swing.config import Config, UniverseCfg
from swing.universe import (
    INDEX_SOURCES,
    Instrument,
    UniverseError,
    asset_dir,
    load,
    load_csv,
    symbols,
    to_schwab_symbol,
    to_yahoo_symbol,
)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,9}$")


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
    assert len(load_csv("etfs")) == 40


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
    assert sum(1 for i in instruments if i.kind == "etf") == 40


def test_instruments_are_frozen_value_objects() -> None:
    instrument = load(_cfg(sp500=False, sp400=False, sp600=False))[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        instrument.symbol = "NOPE"  # type: ignore[misc]


def test_toggles_select_only_the_requested_lists() -> None:
    etf_only = load(_cfg(sp500=False, sp400=False, sp600=False, etfs=True))
    assert len(etf_only) == 40
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

    # SPY and GLD are already ETFs, so they are not added twice
    assert len(instruments) == 41
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
