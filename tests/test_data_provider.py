"""Tests for FROZEN CONTRACT 3 — the provider protocol, bars format and factory.

Everything downstream (indicators, rules, backtest) assumes bars come out of
this layer in exactly one shape, so the normalisation rules are pinned here
rather than trusted. No test in this file touches a network: the socket block
in ``conftest`` would fail them if they did.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from conftest import build_config, make_bars
from swing.data import (
    BAR_COLUMNS,
    DataProvider,
    Fundamentals,
    Quote,
    SchwabProvider,
    YFinanceProvider,
    empty_bars,
    get_provider,
    normalize_bars,
)
from swing.data.provider import (
    RetryPolicy,
    as_date,
    as_utc,
    choose_header_level,
    chunked,
    clean_symbols,
    coerce_float,
    is_contract_shaped,
    map_concurrent,
    with_retry,
)

# ---------------------------------------------------------------------------
# value objects
# ---------------------------------------------------------------------------


def test_quote_is_a_frozen_dataclass_with_the_contract_fields() -> None:
    quote = Quote(symbol="AAPL", price=123.45, asof=datetime(2026, 8, 18, 20, tzinfo=UTC))
    assert [f.name for f in dataclasses.fields(quote)] == ["symbol", "price", "asof"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        quote.price = 1.0  # type: ignore[misc]


def test_fundamentals_is_a_frozen_dataclass_and_allows_unknown_growth() -> None:
    """The additive ``basis`` field labels which period the growth compares.

    Audit BUG-037: quarterly and annual figures used to be mixed with nothing
    on the record saying which one a given number was.
    """
    fundamentals = Fundamentals(symbol="AAPL", eps_growth=None, revenue_growth=0.12)
    assert [f.name for f in dataclasses.fields(fundamentals)] == [
        "symbol",
        "eps_growth",
        "revenue_growth",
        "basis",
    ]
    assert fundamentals.eps_growth is None
    assert fundamentals.basis is None, "the new field defaults, so old call sites still work"
    with pytest.raises(dataclasses.FrozenInstanceError):
        fundamentals.symbol = "MSFT"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# the protocol itself
# ---------------------------------------------------------------------------


class _Complete:
    def daily_bars(self, symbols: Any, start: date, end: date) -> dict[str, pd.DataFrame]:
        return {}

    def latest_quotes(self, symbols: Any) -> dict[str, Quote]:
        return {}

    def earnings_dates(self, symbols: Any) -> dict[str, date | None]:
        return {}

    def earnings_history(self, symbols: Any, start: date, end: date) -> dict[str, tuple[date, ...]]:
        return {}

    def fundamentals(self, symbols: Any) -> dict[str, Fundamentals]:
        return {}


class _Partial:
    def daily_bars(self, symbols: Any, start: date, end: date) -> dict[str, pd.DataFrame]:
        return {}


class _WithoutHistory:
    """Everything Contract 3 asked for before amendment A12, and nothing more."""

    def daily_bars(self, symbols: Any, start: date, end: date) -> dict[str, pd.DataFrame]:
        return {}

    def latest_quotes(self, symbols: Any) -> dict[str, Quote]:
        return {}

    def earnings_dates(self, symbols: Any) -> dict[str, date | None]:
        return {}

    def fundamentals(self, symbols: Any) -> dict[str, Fundamentals]:
        return {}


def test_data_provider_is_runtime_checkable() -> None:
    assert isinstance(_Complete(), DataProvider)
    assert not isinstance(_Partial(), DataProvider)
    assert not isinstance(object(), DataProvider)


def test_the_protocol_now_requires_earnings_history() -> None:
    """Amendment A12: a provider without it cannot answer a backtest's blackout."""
    assert not isinstance(_WithoutHistory(), DataProvider)


def test_both_shipped_providers_satisfy_the_protocol(test_cfg: Any) -> None:
    assert isinstance(YFinanceProvider(test_cfg), DataProvider)
    assert isinstance(SchwabProvider(test_cfg, client_factory=lambda: object()), DataProvider)


# ---------------------------------------------------------------------------
# normalize_bars — the definition of "a bars frame"
# ---------------------------------------------------------------------------


def test_empty_bars_has_the_contract_shape() -> None:
    frame = empty_bars()
    assert list(frame.columns) == list(BAR_COLUMNS)
    assert frame.index.name is None
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert all(dtype == "float64" for dtype in frame.dtypes)


def test_normalize_lowercases_columns_and_drops_extras() -> None:
    raw = make_bars(5).rename(
        columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    )
    raw["Dividends"] = 0.0
    out = normalize_bars(raw)
    assert list(out.columns) == list(BAR_COLUMNS)


def test_normalize_strips_the_timezone_without_shifting_the_date() -> None:
    raw = make_bars(3)
    raw.index = raw.index.tz_localize("America/New_York")
    out = normalize_bars(raw)
    assert out.index.tz is None
    assert list(out.index.date) == list(make_bars(3).index.date)


def test_normalize_clears_the_index_name_for_fixture_parity() -> None:
    raw = make_bars(3)
    raw.index = raw.index.rename("Date")
    assert normalize_bars(raw).index.name is None


def test_normalize_sorts_dedupes_and_drops_all_nan_rows() -> None:
    base = make_bars(4)
    scrambled = pd.concat([base.iloc[[3]], base.iloc[[0, 1]], base.iloc[[1]]])
    scrambled.loc[pd.Timestamp("2020-01-10")] = [float("nan")] * 5
    out = normalize_bars(scrambled)
    assert out.index.is_monotonic_increasing
    assert out.index.is_unique
    assert pd.Timestamp("2020-01-10") not in out.index
    assert len(out) == 3


def test_normalize_drops_a_row_whose_open_is_not_a_number() -> None:
    """Contract 3 amendment A3 / audit BUG-005: a partial bar is not a bar.

    ``dropna(how="all")`` admitted this row, and a NaN open three layers down
    became a NaN fill price, a NaN equity curve and a fabricated drawdown.
    """
    base = make_bars(4)
    damaged = base.copy()
    damaged.loc[damaged.index[2], "open"] = float("nan")

    out = normalize_bars(damaged)

    assert len(out) == 3
    assert base.index[2] not in out.index
    assert out.index.to_list() == [base.index[0], base.index[1], base.index[3]]


def test_normalize_drops_a_row_whose_close_is_infinite() -> None:
    """Amendment A3 covers infinities too — ``notna`` alone lets them through."""
    base = make_bars(3)
    damaged = base.copy()
    damaged.loc[damaged.index[1], "close"] = float("inf")
    assert len(normalize_bars(damaged)) == 2


def test_normalize_keeps_a_complete_row_and_zeroes_its_missing_volume() -> None:
    """Amendment A3: volume is not a price, so an unknown one is 0.0, not a drop."""
    base = make_bars(3)
    partial = base.copy()
    partial.loc[partial.index[1], "volume"] = float("nan")

    out = normalize_bars(partial)

    assert len(out) == 3, "the bar itself is complete, so it stays"
    assert out.loc[base.index[1], "volume"] == 0.0
    assert out.loc[base.index[1], "close"] == pytest.approx(base.loc[base.index[1], "close"])


def test_normalize_leaves_an_already_normalized_frame_untouched() -> None:
    """Audit PERF-006: the warm-cache path must not re-copy 1,500 clean frames."""
    base = make_bars(5)
    assert is_contract_shaped(base)
    assert normalize_bars(base) is base


def test_the_fast_path_refuses_anything_that_is_not_quite_the_contract() -> None:
    base = make_bars(3)
    assert not is_contract_shaped(base.rename(columns={"open": "Open"}))
    assert not is_contract_shaped(base.astype({"volume": "int64"}))
    assert not is_contract_shaped(base.iloc[::-1])
    named = base.copy()
    named.index = named.index.rename("Date")
    assert not is_contract_shaped(named)
    damaged = base.copy()
    damaged.loc[damaged.index[0], "high"] = float("nan")
    assert not is_contract_shaped(damaged)


def test_normalize_is_not_fooled_by_a_single_ticker_literally_named_open() -> None:
    """Audit BUG-048: additive field scoring picked the *ticker* level here.

    Both levels scored a perfect 1.0 for looking like price fields, the ticker
    level came first, and the frame collapsed to one column plus a "missing the
    high, low, close column" error.
    """
    base = make_bars(3)
    raw = base.copy()
    raw.columns = pd.MultiIndex.from_product([["OPEN"], ["Open", "High", "Low", "Close", "Volume"]])

    out = normalize_bars(raw)

    assert list(out.columns) == list(BAR_COLUMNS)
    assert out["close"].to_list() == pytest.approx(base["close"].to_list())


def test_the_header_heuristic_answers_both_questions_from_one_frame() -> None:
    columns = pd.MultiIndex.from_product([["OPEN", "MSFT"], ["Open", "High", "Low", "Close"]])
    assert choose_header_level(columns) == 1, "the price-field level"
    assert choose_header_level(columns, tickers={"OPEN", "MSFT"}) == 0, "the ticker level"


def test_normalize_keeps_the_last_row_when_a_date_repeats() -> None:
    base = make_bars(2)
    dupe = base.iloc[[1]].copy()
    dupe.loc[:, "close"] = 999.0
    out = normalize_bars(pd.concat([base, dupe]))
    assert out.loc[base.index[1], "close"] == 999.0


def test_normalize_casts_integer_columns_to_float() -> None:
    raw = make_bars(3)
    raw = raw.astype({"volume": "int64"})
    out = normalize_bars(raw)
    assert out["volume"].dtype == "float64"


def test_normalize_flattens_a_single_symbol_multiindex_header() -> None:
    base = make_bars(3)
    raw = base.copy()
    raw.columns = pd.MultiIndex.from_product([["AAPL"], ["Open", "High", "Low", "Close", "Volume"]])
    out = normalize_bars(raw)
    assert list(out.columns) == list(BAR_COLUMNS)
    assert out["close"].to_list() == pytest.approx(base["close"].to_list())


def test_normalize_falls_back_to_adj_close_when_close_is_absent() -> None:
    raw = make_bars(3).rename(columns={"close": "Adj Close"})
    assert "close" in normalize_bars(raw).columns


def test_normalize_fills_a_missing_volume_column_with_zero() -> None:
    raw = make_bars(3).drop(columns=["volume"])
    assert normalize_bars(raw)["volume"].eq(0.0).all()


def test_normalize_explains_itself_when_prices_are_missing() -> None:
    with pytest.raises(ValueError, match="missing the open, high, low"):
        normalize_bars(pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex(["2020-01-02"])))


def test_normalize_rejects_values_that_are_not_numbers() -> None:
    raw = make_bars(2)
    raw = raw.astype({"close": "object"})
    raw.loc[raw.index[0], "close"] = "not a price"
    with pytest.raises(ValueError, match="not numbers"):
        normalize_bars(raw)


def test_normalize_accepts_none_and_an_empty_frame() -> None:
    assert normalize_bars(None).empty
    assert normalize_bars(pd.DataFrame()).empty


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def test_clean_symbols_uppercases_dedupes_and_keeps_order() -> None:
    assert clean_symbols([" aapl ", "MSFT", "aapl", "", "brk-b"]) == ["AAPL", "MSFT", "BRK-B"]


def test_chunked_splits_without_losing_anything() -> None:
    assert list(chunked(list("abcde"), 2)) == [["a", "b"], ["c", "d"], ["e"]]
    assert list(chunked([], 2)) == []
    with pytest.raises(ValueError, match="at least 1"):
        list(chunked(["a"], 0))


def test_as_date_accepts_the_usual_suspects() -> None:
    assert as_date(date(2026, 8, 18)) == date(2026, 8, 18)
    assert as_date(datetime(2026, 8, 18, 17, 30)) == date(2026, 8, 18)
    assert as_date(pd.Timestamp("2026-08-18")) == date(2026, 8, 18)
    assert as_date("2026-08-18") == date(2026, 8, 18)
    with pytest.raises(TypeError):
        as_date(17)  # type: ignore[arg-type]


def test_as_utc_reads_a_naive_stamp_as_utc_and_leaves_aware_ones_alone() -> None:
    naive = datetime(2026, 8, 18, 17, 30)
    assert as_utc(naive) == datetime(2026, 8, 18, 17, 30, tzinfo=UTC)
    aware = datetime(2026, 8, 18, 17, 30, tzinfo=ZoneInfo("America/New_York"))
    assert as_utc(aware) is aware


def test_the_retry_policy_comes_from_the_config(tmp_path: Path) -> None:
    """Audit DEBT-013: retries used to be inline literals in two providers."""
    cfg = build_config(tmp_path, data={"retries": 7, "retry_backoff": 1.5, "download_batch": 25})
    policy = RetryPolicy.from_config(cfg)
    assert (policy.retries, policy.backoff, policy.batch) == (7, 1.5, 25)

    override = RetryPolicy.from_config(cfg, retries=2, backoff=0.0, batch=10)
    assert (override.retries, override.backoff, override.batch) == (2, 0.0, 10)


def test_the_retry_policy_refuses_impossible_settings() -> None:
    with pytest.raises(ValueError, match="retries"):
        RetryPolicy(retries=0)
    with pytest.raises(ValueError, match="retry_backoff"):
        RetryPolicy(backoff=-1.0)
    with pytest.raises(ValueError, match="batch_size"):
        RetryPolicy(batch=0)


def test_with_retry_gives_up_after_the_configured_attempts() -> None:
    attempts: list[int] = []

    def boom() -> None:
        attempts.append(1)
        raise RuntimeError("connection reset")

    assert with_retry("a thing", boom, policy=RetryPolicy(retries=3, backoff=0.0)) is None
    assert len(attempts) == 3


def test_map_concurrent_keeps_the_input_order_and_survives_one_failure() -> None:
    seen: list[str] = []
    lock = threading.Lock()

    def call(symbol: str) -> str:
        with lock:
            seen.append(symbol)
        if symbol == "BAD":
            raise RuntimeError("vendor said no")
        return symbol.lower()

    out = map_concurrent(["AAPL", "BAD", "MSFT", "AAPL"], call, workers=4)

    assert list(out) == ["AAPL", "BAD", "MSFT"], "deduped, in the caller's order"
    assert out["BAD"] is None, "one bad symbol must not sink the batch"
    assert out["MSFT"] == "msft"
    assert sorted(seen) == ["AAPL", "BAD", "MSFT"]


def test_map_concurrent_runs_inline_when_asked_for_one_worker() -> None:
    threads: set[int] = set()
    map_concurrent(["A", "B"], lambda _s: threads.add(threading.get_ident()) or None, workers=1)
    assert threads == {threading.get_ident()}


def test_coerce_float_rejects_junk() -> None:
    assert coerce_float("12.5") == 12.5
    assert coerce_float(3) == 3.0
    assert coerce_float(None) is None
    assert coerce_float("N/A") is None
    assert coerce_float(float("nan")) is None
    assert coerce_float(float("inf")) is None
    assert coerce_float(True) is None


# ---------------------------------------------------------------------------
# get_provider
# ---------------------------------------------------------------------------


def test_get_provider_defaults_to_yfinance(test_cfg: Any) -> None:
    assert isinstance(get_provider(test_cfg), YFinanceProvider)


def test_get_provider_returns_schwab_when_configured(cfg_factory: Any) -> None:
    cfg = cfg_factory(data={"provider": "schwab"})
    built: list[str] = []

    def factory() -> object:
        built.append("client")
        return object()

    provider = get_provider(cfg, client_factory=factory)
    assert isinstance(provider, SchwabProvider)
    assert built == ["client"], "the client is built once, at selection time"


def test_get_provider_falls_back_to_yfinance_when_schwab_cannot_connect(cfg_factory: Any) -> None:
    cfg = cfg_factory(data={"provider": "schwab"})

    def broken_factory() -> object:
        raise RuntimeError("token expired")

    with pytest.warns(UserWarning, match="falling back to free Yahoo data"):
        provider = get_provider(cfg, client_factory=broken_factory)
    assert isinstance(provider, YFinanceProvider)


def test_get_provider_keeps_the_two_caches_apart(cfg_factory: Any) -> None:
    """Schwab candles are not dividend-adjusted, so they get their own directory."""
    cfg = cfg_factory(data={"provider": "schwab"})
    schwab = get_provider(cfg, client_factory=lambda: object())
    yahoo = get_provider(cfg_factory())
    assert isinstance(schwab, SchwabProvider)
    assert isinstance(yahoo, YFinanceProvider)
    assert schwab.cache.root != yahoo.cache.root
    assert Path(cfg.data.cache_dir) in schwab.cache.root.parents
