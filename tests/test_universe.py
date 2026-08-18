"""Universe assembly and the liquidity screen."""

from __future__ import annotations

from swing.config import Config, load_config
from swing.data.cache import BarCache
from swing.data.universe import (
    UNIVERSE_DIR,
    build_universe,
    describe_universe,
    filter_by_liquidity,
    read_symbol_file,
)

from .conftest import make_bars


def test_shipped_seed_files_parse_and_are_non_trivial():
    for name in ("sp500.csv", "sp400.csv", "sp600.csv", "etfs.csv"):
        rows = read_symbol_file(UNIVERSE_DIR / name)
        assert len(rows) > 50, name
        assert all(r.symbol == r.symbol.upper() for r in rows)
        assert all(r.kind in ("stock", "etf") for r in rows)


def test_etf_file_is_all_etfs():
    rows = read_symbol_file(UNIVERSE_DIR / "etfs.csv")
    assert all(r.is_etf for r in rows)


def test_comment_lines_are_skipped(tmp_path):
    p = tmp_path / "u.csv"
    p.write_text("# a comment\n\nsymbol,kind,name\nAAA,stock,Alpha\n# trailing note\n")
    rows = read_symbol_file(p)
    assert [r.symbol for r in rows] == ["AAA"]


def test_union_dedupes_and_excludes():
    cfg = load_config().as_dict()
    cfg["universe"].update(
        {"sp500": True, "sp400": False, "sp600": False, "etfs": True,
         "extra_symbols": ["AAPL", "ZZZZ"], "exclude_symbols": ["SPY"]}
    )
    uni = build_universe(Config(cfg))
    symbols = [s.symbol for s in uni]
    assert len(symbols) == len(set(symbols))
    assert "SPY" not in symbols
    assert "ZZZZ" in symbols
    assert symbols.count("AAPL") == 1


def test_max_symbols_caps_the_universe():
    cfg = load_config().as_dict()
    cfg["universe"]["max_symbols"] = 7
    assert len(build_universe(Config(cfg))) == 7


def test_liquidity_filter_drops_cheap_and_thin(base_config):
    cache = BarCache(base_config.expand_path(base_config.data.cache_dir))
    # RICH: $50 and 2M shares -> $100M/day. Passes.
    cache.write("RICH", make_bars([50.0] * 30, volume=2_000_000))
    # CHEAP: $2 -> below min_price of $5.
    cache.write("CHEAP", make_bars([2.0] * 30, volume=50_000_000))
    # THIN: $50 but 1k shares -> $50k/day, below the $5M floor.
    cache.write("THIN", make_bars([50.0] * 30, volume=1_000))

    from swing.data.universe import Symbol

    syms = [Symbol(s, "stock") for s in ("RICH", "CHEAP", "THIN")]
    kept = {s.symbol for s in filter_by_liquidity(base_config, syms)}
    assert kept == {"RICH"}


def test_liquidity_filter_keeps_symbols_with_no_cached_data(base_config):
    from swing.data.universe import Symbol

    kept = filter_by_liquidity(base_config, [Symbol("UNKNOWN", "stock")])
    assert [s.symbol for s in kept] == ["UNKNOWN"]


def test_describe_is_readable():
    text = describe_universe(build_universe(load_config()), limit=5)
    assert "universe:" in text and "first 5" in text
