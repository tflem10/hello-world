"""Tests for the yfinance provider — all of Yahoo is faked, none of it is reached.

Yahoo is an unsupported, best-effort source, so the tests here are mostly about
*misbehaviour*: a batch that silently omits a ticker, a sub-frame with no price
columns, a call that fails three times, fundamentals that exist for one symbol
and not the next.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from conftest import make_bars
from swing.config import Config
from swing.data.provider import Fundamentals
from swing.data.yf_provider import YFinanceProvider, _split_download

NOW = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
START = date(2020, 1, 2)
END = date(2020, 2, 12)
BARS = make_bars(30, start="2020-01-02")

FIELDS = ("Open", "High", "Low", "Close", "Volume")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def download_frame(
    frames: dict[str, pd.DataFrame],
    *,
    group_by: str = "ticker",
    tz: str | None = "America/New_York",
    index_name: str | None = "Date",
) -> pd.DataFrame:
    """Build a frame shaped like a real ``yfinance.download`` result."""
    data: dict[tuple[str, str], pd.Series] = {}
    for symbol, frame in frames.items():
        for field in FIELDS:
            key = (symbol, field) if group_by == "ticker" else (field, symbol)
            data[key] = frame[field.lower()]
    out = pd.DataFrame(data)
    out.columns = pd.MultiIndex.from_tuples(
        list(out.columns),
        names=["Ticker", "Price"] if group_by == "ticker" else ["Price", "Ticker"],
    )
    if tz is not None:
        out.index = out.index.tz_localize(tz)
    out.index = out.index.rename(index_name)
    return out


class RecordingDownload:
    """Stands in for ``yfinance.download``; records kwargs, returns a canned frame."""

    def __init__(self, result: Any = None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, tickers: Any, **kwargs: Any) -> Any:
        self.calls.append({"tickers": list(tickers), **kwargs})
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def count(self) -> int:
        return len(self.calls)


class FakeFastInfo:
    """Yahoo's FastInfo: attribute access *and* mapping access, both throwing."""

    def __init__(self, **values: Any) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __getitem__(self, key: str) -> Any:
        return self._values[key]


class FakeTicker:
    def __init__(
        self,
        symbol: str = "AAPL",
        *,
        fast: Any = None,
        history: pd.DataFrame | None = None,
        earnings: pd.DataFrame | None = None,
        calendar: Any = None,
        info: dict[str, Any] | None = None,
        statement: pd.DataFrame | None = None,
        explode: bool = False,
    ) -> None:
        self.symbol = symbol
        self.fast_info = fast
        self._history = history
        self._earnings = earnings
        self._calendar = calendar
        self._info = info
        self._statement = statement
        self._explode = explode

    def _boom(self) -> None:
        if self._explode:
            raise RuntimeError("Yahoo is having a day")

    def history(self, **kwargs: Any) -> pd.DataFrame:
        self._boom()
        if self._history is None:
            raise RuntimeError("no history")
        return self._history

    def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
        self._boom()
        return self._earnings

    def get_calendar(self) -> Any:
        self._boom()
        return self._calendar

    def get_info(self) -> dict[str, Any]:
        self._boom()
        if self._info is None:
            raise RuntimeError("no info")
        return self._info

    def get_income_stmt(self) -> pd.DataFrame | None:
        self._boom()
        return self._statement


class TickerFactory:
    """Hands out prepared tickers and counts how often each was asked for."""

    def __init__(self, tickers: dict[str, FakeTicker]) -> None:
        self.tickers = tickers
        self.calls: list[str] = []

    def __call__(self, symbol: str) -> FakeTicker:
        self.calls.append(symbol)
        return self.tickers.get(symbol, FakeTicker(symbol, explode=True))

    @property
    def count(self) -> int:
        return len(self.calls)


def build_provider(cfg: Config, **kwargs: Any) -> YFinanceProvider:
    """A provider that never waits between retries."""
    kwargs.setdefault("retry_backoff", 0.0)
    return YFinanceProvider(cfg, **kwargs)


# ---------------------------------------------------------------------------
# bars: splitting a bulk download
# ---------------------------------------------------------------------------


def test_split_download_handles_the_group_by_ticker_layout() -> None:
    frame = download_frame({"AAPL": BARS, "MSFT": BARS}, group_by="ticker")
    parts = _split_download(frame, ["AAPL", "MSFT"])
    assert sorted(parts) == ["AAPL", "MSFT"]
    assert list(parts["AAPL"].columns) == list(FIELDS)


def test_split_download_handles_the_group_by_column_layout() -> None:
    frame = download_frame({"AAPL": BARS, "MSFT": BARS}, group_by="column")
    parts = _split_download(frame, ["AAPL", "MSFT"])
    assert sorted(parts) == ["AAPL", "MSFT"]
    assert list(parts["MSFT"].columns) == list(FIELDS)


def test_split_download_is_not_fooled_by_a_ticker_named_open() -> None:
    """OPEN is a real ticker; the field level must still be told apart."""
    frame = download_frame({"OPEN": BARS, "MSFT": BARS}, group_by="column")
    parts = _split_download(frame, ["OPEN", "MSFT"])
    assert sorted(parts) == ["MSFT", "OPEN"]
    assert list(parts["OPEN"].columns) == list(FIELDS)


def test_split_download_skips_a_symbol_yahoo_left_out() -> None:
    frame = download_frame({"AAPL": BARS}, group_by="ticker")
    assert sorted(_split_download(frame, ["AAPL", "GONE"])) == ["AAPL"]


def test_split_download_accepts_a_flat_single_symbol_frame() -> None:
    flat = BARS.rename(columns=str.title)
    assert list(_split_download(flat, ["AAPL"])) == ["AAPL"]


# ---------------------------------------------------------------------------
# bars: end to end through the cache
# ---------------------------------------------------------------------------


def test_daily_bars_normalizes_a_yahoo_frame_to_the_contract(test_cfg: Config) -> None:
    download = RecordingDownload(download_frame({"AAPL": BARS, "MSFT": BARS}))
    provider = build_provider(test_cfg, download=download)

    out = provider.daily_bars(["aapl", "MSFT"], START, END)

    assert sorted(out) == ["AAPL", "MSFT"]
    bars = out["AAPL"]
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert bars.index.tz is None, "tz-aware stamps must be stripped"
    assert bars.index.name is None, "a stray index name breaks fixture comparisons"
    assert bars.index.is_monotonic_increasing
    assert all(dtype == "float64" for dtype in bars.dtypes)
    assert bars["close"].to_list() == pytest.approx(BARS["close"].to_list())


def test_daily_bars_asks_yahoo_for_an_exclusive_end_and_adjusted_prices(
    test_cfg: Config,
) -> None:
    download = RecordingDownload(download_frame({"AAPL": BARS}))
    build_provider(test_cfg, download=download).daily_bars(["AAPL"], START, END)

    call = download.calls[0]
    assert call["start"] == START.isoformat()
    assert call["end"] == (END + timedelta(days=1)).isoformat(), "yfinance end is exclusive"
    assert call["auto_adjust"] is True
    assert call["progress"] is False
    assert call["threads"] is True


def test_daily_bars_batches_large_universes(test_cfg: Config) -> None:
    download = RecordingDownload(download_frame({"AAPL": BARS, "MSFT": BARS}))
    provider = build_provider(test_cfg, download=download, batch_size=1)

    provider.daily_bars(["AAPL", "MSFT"], START, END)

    assert download.count == 2
    assert [call["tickers"] for call in download.calls] == [["AAPL"], ["MSFT"]]


def test_daily_bars_serves_a_warm_cache_without_calling_yahoo(test_cfg: Config) -> None:
    download = RecordingDownload(download_frame({"AAPL": BARS}))
    provider = build_provider(test_cfg, download=download)

    provider.daily_bars(["AAPL"], START, END)
    before = download.count
    again = provider.daily_bars(["AAPL"], START, END)

    assert download.count == before
    assert len(again["AAPL"]) == len(BARS)


def test_one_unreadable_symbol_does_not_lose_the_rest(test_cfg: Config) -> None:
    good = download_frame({"AAPL": BARS})
    broken = pd.DataFrame(
        {("BAD", "Close"): BARS["close"]},
        index=good.index,
    )
    broken.columns = pd.MultiIndex.from_tuples(list(broken.columns), names=["Ticker", "Price"])
    frame = pd.concat([good, broken], axis=1)
    download = RecordingDownload(frame)

    out = build_provider(test_cfg, download=download).daily_bars(["AAPL", "BAD"], START, END)

    assert sorted(out) == ["AAPL"], "a symbol with no OHLC is logged and skipped, not raised"


def test_a_failing_download_is_retried_then_gives_up_quietly(test_cfg: Config) -> None:
    download = RecordingDownload(error=RuntimeError("connection reset"))
    provider = build_provider(test_cfg, download=download, retries=3)

    assert provider.daily_bars(["AAPL"], START, END) == {}
    assert download.count == 3, "bounded retries, then the scan carries on"


def test_a_nonsense_download_result_is_ignored(test_cfg: Config) -> None:
    provider = build_provider(test_cfg, download=RecordingDownload("not a frame"))
    assert provider.daily_bars(["AAPL"], START, END) == {}


def test_provider_construction_rejects_impossible_settings(test_cfg: Config) -> None:
    with pytest.raises(ValueError, match="batch_size"):
        YFinanceProvider(test_cfg, batch_size=0)
    with pytest.raises(ValueError, match="retries"):
        YFinanceProvider(test_cfg, retries=0)


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------


def test_latest_quotes_reads_fast_info(test_cfg: Config) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(fast=FakeFastInfo(last_price=191.25))})
    provider = build_provider(test_cfg, ticker_factory=factory)

    quotes = provider.latest_quotes(["aapl"], now=NOW)

    assert quotes["AAPL"].price == 191.25
    assert quotes["AAPL"].symbol == "AAPL"
    assert quotes["AAPL"].asof == NOW


def test_a_naive_injected_clock_still_produces_a_timezone_aware_quote(test_cfg: Config) -> None:
    """Contract 3 promises ``Quote.asof`` is aware UTC, whatever the caller passes."""
    factory = TickerFactory({"AAPL": FakeTicker(fast=FakeFastInfo(last_price=191.25))})
    naive = datetime(2026, 8, 18, 21, 0)

    quote = build_provider(test_cfg, ticker_factory=factory).latest_quotes(["AAPL"], now=naive)[
        "AAPL"
    ]

    assert quote.asof.tzinfo is not None
    assert quote.asof == NOW


def test_a_naive_injected_clock_leaves_the_ttl_logic_intact(test_cfg: Config) -> None:
    ticker = FakeTicker(earnings=earnings_frame(["2026-09-01"]))
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory)
    naive = datetime(2026, 8, 18, 21, 0)

    first = provider.earnings_dates(["AAPL"], now=naive)
    after_first = factory.count
    cached = provider.earnings_dates(["AAPL"], now=naive + timedelta(days=2))
    after_cached = factory.count
    refetched = provider.earnings_dates(["AAPL"], now=naive + timedelta(days=4))

    assert first == cached == refetched == {"AAPL": date(2026, 9, 1)}
    assert after_cached == after_first, "a naive clock must not confuse the TTL"
    assert factory.count > after_cached
    assert provider.fundamentals(["AAPL"], now=naive) is not None


def test_latest_quotes_accepts_mapping_style_fast_info(test_cfg: Config) -> None:
    class MappingOnly:
        def __init__(self, values: dict[str, Any]) -> None:
            self._values = values

        def __getitem__(self, key: str) -> Any:
            return self._values[key]

    factory = TickerFactory({"AAPL": FakeTicker(fast=MappingOnly({"lastPrice": 55.5}))})
    quotes = build_provider(test_cfg, ticker_factory=factory).latest_quotes(["AAPL"], now=NOW)
    assert quotes["AAPL"].price == 55.5


def test_latest_quotes_falls_back_to_the_last_close(test_cfg: Config) -> None:
    history = pd.DataFrame(
        {"Close": [10.0, 11.5]}, index=pd.DatetimeIndex(["2026-08-17", "2026-08-18"])
    )
    factory = TickerFactory({"AAPL": FakeTicker(fast=FakeFastInfo(), history=history)})
    quotes = build_provider(test_cfg, ticker_factory=factory).latest_quotes(["AAPL"], now=NOW)
    assert quotes["AAPL"].price == 11.5


def test_latest_quotes_skips_a_symbol_it_cannot_price(test_cfg: Config) -> None:
    factory = TickerFactory(
        {
            "AAPL": FakeTicker(fast=FakeFastInfo(last_price=100.0)),
            "DEAD": FakeTicker(explode=True),
        }
    )
    quotes = build_provider(test_cfg, ticker_factory=factory, retries=1).latest_quotes(
        ["AAPL", "DEAD"], now=NOW
    )
    assert sorted(quotes) == ["AAPL"]


def test_latest_quotes_ignores_a_zero_or_missing_price(test_cfg: Config) -> None:
    factory = TickerFactory(
        {"AAPL": FakeTicker(fast=FakeFastInfo(last_price=0.0, previousClose=None))}
    )
    assert (
        build_provider(test_cfg, ticker_factory=factory, retries=1).latest_quotes(["AAPL"], now=NOW)
        == {}
    )


# ---------------------------------------------------------------------------
# earnings dates
# ---------------------------------------------------------------------------


def earnings_frame(days: list[str], *, tz: str | None = "America/New_York") -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(day) for day in days])
    if tz is not None:
        index = index.tz_localize(tz)
    return pd.DataFrame({"EPS Estimate": [1.0] * len(days)}, index=index)


def test_earnings_dates_returns_the_next_upcoming_date(test_cfg: Config) -> None:
    factory = TickerFactory(
        {"AAPL": FakeTicker(earnings=earnings_frame(["2026-05-01", "2026-11-02", "2026-09-01"]))}
    )
    out = build_provider(test_cfg, ticker_factory=factory).earnings_dates(["AAPL"], now=NOW)
    assert out == {"AAPL": date(2026, 9, 1)}, "past reports are ignored, the nearest future wins"


def test_earnings_dates_falls_back_to_the_calendar(test_cfg: Config) -> None:
    factory = TickerFactory(
        {"AAPL": FakeTicker(earnings=None, calendar={"Earnings Date": [date(2026, 10, 5)]})}
    )
    out = build_provider(test_cfg, ticker_factory=factory).earnings_dates(["AAPL"], now=NOW)
    assert out == {"AAPL": date(2026, 10, 5)}


def test_earnings_dates_is_none_when_yahoo_knows_nothing(test_cfg: Config) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(earnings=None, calendar=None)})
    out = build_provider(test_cfg, ticker_factory=factory, retries=1).earnings_dates(
        ["AAPL"], now=NOW
    )
    assert out == {"AAPL": None}


def test_earnings_dates_are_cached_until_the_ttl_expires(test_cfg: Config) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(earnings=earnings_frame(["2026-09-01"]))})
    provider = build_provider(test_cfg, ticker_factory=factory)

    first = provider.earnings_dates(["AAPL"], now=NOW)
    after_first = factory.count
    cached = provider.earnings_dates(["AAPL"], now=NOW + timedelta(days=2))
    after_cached = factory.count
    refetched = provider.earnings_dates(["AAPL"], now=NOW + timedelta(days=4))

    assert first == cached == refetched == {"AAPL": date(2026, 9, 1)}
    assert after_cached == after_first, "inside the 3-day TTL nothing is re-asked"
    assert factory.count > after_cached, "past the TTL the calendar is checked again"


def test_earnings_cache_survives_a_new_provider_instance(test_cfg: Config) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(earnings=earnings_frame(["2026-09-01"]))})
    build_provider(test_cfg, ticker_factory=factory).earnings_dates(["AAPL"], now=NOW)
    before = factory.count

    fresh = build_provider(test_cfg, ticker_factory=factory)
    assert fresh.earnings_dates(["AAPL"], now=NOW) == {"AAPL": date(2026, 9, 1)}
    assert factory.count == before, "the TTL cache is on disk, not in memory"


def test_a_stale_earnings_entry_is_refetched(test_cfg: Config) -> None:
    ticker = FakeTicker(earnings=earnings_frame(["2026-09-01"]))
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory)
    provider.earnings_dates(["AAPL"], now=NOW)
    before = factory.count

    ticker._earnings = earnings_frame(["2026-09-08"])
    later = provider.earnings_dates(["AAPL"], now=NOW + timedelta(days=4))

    assert later == {"AAPL": date(2026, 9, 8)}
    assert factory.count > before


# ---------------------------------------------------------------------------
# fundamentals
# ---------------------------------------------------------------------------


def income_statement(eps: list[float], revenue: list[float]) -> pd.DataFrame:
    """Two reporting periods, newest column first, as yfinance returns them."""
    columns = pd.DatetimeIndex(["2025-12-31", "2024-12-31"])
    return pd.DataFrame([eps, revenue], index=["Diluted EPS", "Total Revenue"], columns=columns)


def test_fundamentals_prefer_the_company_profile(test_cfg: Config) -> None:
    info = {"earningsQuarterlyGrowth": 0.18, "revenueGrowth": 0.09}
    factory = TickerFactory({"AAPL": FakeTicker(info=info)})

    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)

    assert out["AAPL"] == Fundamentals(symbol="AAPL", eps_growth=0.18, revenue_growth=0.09)


def test_fundamentals_fall_back_to_the_income_statement(test_cfg: Config) -> None:
    factory = TickerFactory(
        {"AAPL": FakeTicker(info={}, statement=income_statement([6.0, 5.0], [110.0, 100.0]))}
    )

    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)

    assert out["AAPL"].eps_growth == pytest.approx(0.2)
    assert out["AAPL"].revenue_growth == pytest.approx(0.1)


def test_fundamentals_can_be_half_known(test_cfg: Config) -> None:
    """Yahoo often has revenue growth and nothing else — that is not a failure."""
    factory = TickerFactory({"AAPL": FakeTicker(info={"revenueGrowth": 0.05}, statement=None)})
    out = build_provider(test_cfg, ticker_factory=factory, retries=1).fundamentals(
        ["AAPL"], now=NOW
    )
    assert out["AAPL"].revenue_growth == 0.05
    assert out["AAPL"].eps_growth is None


def test_fundamentals_are_none_when_yahoo_has_nothing(test_cfg: Config) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(explode=True)})
    out = build_provider(test_cfg, ticker_factory=factory, retries=1).fundamentals(
        ["AAPL"], now=NOW
    )
    assert out["AAPL"] == Fundamentals(symbol="AAPL", eps_growth=None, revenue_growth=None)


def test_fundamentals_reject_junk_values(test_cfg: Config) -> None:
    info = {"earningsQuarterlyGrowth": "N/A", "revenueGrowth": float("nan")}
    factory = TickerFactory({"AAPL": FakeTicker(info=info, statement=None)})
    out = build_provider(test_cfg, ticker_factory=factory, retries=1).fundamentals(
        ["AAPL"], now=NOW
    )
    assert out["AAPL"].eps_growth is None
    assert out["AAPL"].revenue_growth is None


def test_fundamentals_survive_a_json_round_trip(test_cfg: Config) -> None:
    info = {"earningsQuarterlyGrowth": 0.18, "revenueGrowth": 0.09}
    factory = TickerFactory({"AAPL": FakeTicker(info=info)})
    provider = build_provider(test_cfg, ticker_factory=factory)

    provider.fundamentals(["AAPL"], now=NOW)
    before = factory.count
    cached = provider.fundamentals(["AAPL"], now=NOW + timedelta(days=6))

    assert factory.count == before, "fundamentals are cached for a week"
    assert cached["AAPL"] == Fundamentals(symbol="AAPL", eps_growth=0.18, revenue_growth=0.09)


def test_fundamentals_are_refetched_after_a_week(test_cfg: Config) -> None:
    ticker = FakeTicker(info={"earningsQuarterlyGrowth": 0.18, "revenueGrowth": 0.09})
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory)
    provider.fundamentals(["AAPL"], now=NOW)

    ticker._info = {"earningsQuarterlyGrowth": 0.25, "revenueGrowth": 0.11}
    later = provider.fundamentals(["AAPL"], now=NOW + timedelta(days=8))

    assert later["AAPL"].eps_growth == 0.25
