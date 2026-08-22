"""Tests for the yfinance provider — all of Yahoo is faked, none of it is reached.

Yahoo is an unsupported, best-effort source, so the tests here are mostly about
*misbehaviour*: a batch that silently omits a ticker, a sub-frame with no price
columns, a call that fails three times, fundamentals that exist for one symbol
and not the next.

Every test injects both seams (``download`` and ``ticker_factory``) whenever the
path under test can reach either one. yfinance 1.6 talks through ``curl_cffi``,
which does not go anywhere near ``socket.socket.connect`` — so the suite's
socket block would *not* catch a leak here.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from conftest import build_config, make_bars
from swing.config import Config
from swing.data.cache import TtlJsonCache, earnings_coverage, earnings_fingerprint
from swing.data.provider import Fundamentals
from swing.data.yf_provider import (
    EARNINGS_HISTORY_TTL,
    EARNINGS_TTL,
    YFinanceProvider,
    _split_download,
    settled_history_ttl,
)

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


class RecordingSleep:
    """Stands in for ``time.sleep``: records the waits, performs none of them."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def build_provider(cfg: Config, **kwargs: Any) -> YFinanceProvider:
    """A provider that never waits between retries and never dials out.

    ``download`` defaults to a downloader that refuses: the quote path falls
    back to a bulk download for anything ``fast_info`` could not price, and a
    test that forgets to say what Yahoo should answer must fail rather than
    quietly reach the real endpoint.

    ``sleep`` defaults to a recorder for the same reason ``retry_backoff``
    defaults to zero: the earnings sweep paces itself between chunks, and a
    suite that actually waited would take minutes.
    """
    kwargs.setdefault("retry_backoff", 0.0)
    kwargs.setdefault("sleep", RecordingSleep())
    kwargs.setdefault("download", RecordingDownload(error=AssertionError("no download injected")))
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


def test_a_whole_batch_collapsing_to_one_table_is_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Audit BUG-015: 200 symbols evaporating used to leave one DEBUG line.

    Downstream that is indistinguishable from a bear market — a missing SPY is
    reported to the user as "the market regime gate is OFF".
    """
    flat = BARS.rename(columns=str.title)

    with caplog.at_level("WARNING", logger="swing.data.yf_provider"):
        assert _split_download(flat, ["AAPL", "MSFT", "NVDA"]) == {}

    assert "single unlabelled table for a batch of 3 symbols" in caplog.text


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


def test_the_network_knobs_come_from_the_config(tmp_path: Any) -> None:
    """Audit DEBT-013: a rate-limited user had to edit the source to back off."""
    cfg = build_config(tmp_path, data={"retries": 2, "retry_backoff": 0.001, "download_batch": 10})
    download = RecordingDownload(error=RuntimeError("rate limited"))
    provider = YFinanceProvider(cfg, download=download)

    assert provider.batch_size == 10
    assert provider.daily_bars(["AAPL"], START, END) == {}
    assert download.count == 2, "two attempts, because the config said two"


def test_the_download_batch_size_comes_from_the_config(tmp_path: Any) -> None:
    cfg = build_config(tmp_path, data={"download_batch": 10, "retry_backoff": 0.001})
    symbols = [f"S{index}" for index in range(12)]
    download = RecordingDownload(download_frame(dict.fromkeys(symbols, BARS)))

    YFinanceProvider(cfg, download=download).daily_bars(symbols, START, END)

    assert [len(call["tickers"]) for call in download.calls] == [10, 2]


def test_daily_bars_accepts_an_injected_clock(test_cfg: Config) -> None:
    """Audit DEBT-014: ``daily_bars`` was the one call with no ``now=`` seam."""
    download = RecordingDownload(download_frame({"AAPL": BARS}))
    provider = build_provider(test_cfg, download=download)

    provider.daily_bars(["AAPL"], START, END, now=NOW)

    assert provider.cache.read_meta("AAPL").fetched_at == NOW


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


def test_latest_quotes_falls_back_to_one_bulk_download_for_the_misses(
    test_cfg: Config,
) -> None:
    """Audit PERF-002: the fallback used to be a 5-day history call *per miss*.

    Two unpriced symbols now cost one download between them, and the symbol
    ``fast_info`` could price never waits for it.
    """
    recent = make_bars(3, start="2026-08-14")
    download = RecordingDownload(download_frame({"MISS1": recent, "MISS2": recent}))
    factory = TickerFactory(
        {
            "AAPL": FakeTicker(fast=FakeFastInfo(last_price=191.25)),
            "MISS1": FakeTicker(fast=FakeFastInfo()),
            "MISS2": FakeTicker(fast=FakeFastInfo()),
        }
    )
    provider = build_provider(test_cfg, download=download, ticker_factory=factory, retries=1)

    quotes = provider.latest_quotes(["AAPL", "MISS1", "MISS2"], now=NOW)

    assert download.count == 1, "one call for both misses"
    assert download.calls[0]["tickers"] == ["MISS1", "MISS2"], "and only for the misses"
    assert quotes["AAPL"].price == 191.25, "the fresher intraday price still wins"
    assert quotes["MISS1"].price == pytest.approx(recent["close"].iloc[-1])
    assert quotes["MISS2"].price == pytest.approx(recent["close"].iloc[-1])


def test_the_quote_fallback_reads_the_flat_frame_yahoo_sends_for_one_symbol(
    test_cfg: Config,
) -> None:
    """A single-ticker download has no MultiIndex header — the common miss."""
    recent = make_bars(3, start="2026-08-14").rename(columns=str.title)
    download = RecordingDownload(recent)
    factory = TickerFactory({"AAPL": FakeTicker(fast=FakeFastInfo())})
    provider = build_provider(test_cfg, download=download, ticker_factory=factory, retries=1)

    quotes = provider.latest_quotes(["AAPL"], now=NOW)

    assert download.calls[0]["period"] == "5d"
    assert quotes["AAPL"].price == pytest.approx(recent["Close"].iloc[-1])


def test_latest_quotes_needs_no_download_when_every_symbol_is_priced(
    test_cfg: Config,
) -> None:
    download = RecordingDownload(error=AssertionError("must not be called"))
    factory = TickerFactory({"AAPL": FakeTicker(fast=FakeFastInfo(last_price=191.25))})
    provider = build_provider(test_cfg, download=download, ticker_factory=factory)

    assert provider.latest_quotes(["AAPL"], now=NOW)["AAPL"].price == 191.25
    assert download.count == 0


def test_latest_quotes_skips_a_symbol_it_cannot_price(test_cfg: Config) -> None:
    download = RecordingDownload(download_frame({"AAPL": BARS}))
    factory = TickerFactory(
        {
            "AAPL": FakeTicker(fast=FakeFastInfo(last_price=100.0)),
            "DEAD": FakeTicker(explode=True),
        }
    )
    quotes = build_provider(
        test_cfg, download=download, ticker_factory=factory, retries=1
    ).latest_quotes(["AAPL", "DEAD"], now=NOW)
    assert sorted(quotes) == ["AAPL"]


def test_latest_quotes_ignores_a_zero_or_missing_price(test_cfg: Config) -> None:
    download = RecordingDownload(error=RuntimeError("Yahoo has nothing either"))
    factory = TickerFactory(
        {"AAPL": FakeTicker(fast=FakeFastInfo(last_price=0.0, previousClose=None))}
    )
    provider = build_provider(test_cfg, download=download, ticker_factory=factory, retries=1)
    assert provider.latest_quotes(["AAPL"], now=NOW) == {}


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


def test_a_cold_earnings_walk_is_persisted_in_chunks(test_cfg: Config) -> None:
    """Audit PERF-003: nothing was written until all 1,500 symbols had answered.

    A Ctrl-C at symbol 1,400 threw away 1,400 downloads. Chunks are persisted
    as they complete, so an interrupted run resumes almost where it stopped.
    """
    symbols = [f"S{index}" for index in range(5)]
    factory = TickerFactory(
        {symbol: FakeTicker(earnings=earnings_frame(["2026-09-01"])) for symbol in symbols}
    )
    provider = build_provider(test_cfg, ticker_factory=factory, chunk_size=2)
    cache_file = test_cfg.data.cache_dir / "earnings.json"

    class StopAfterTwoChunks(FakeTicker):
        def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
            if len(json.loads(cache_file.read_text())["entries"]) >= 4:
                raise KeyboardInterrupt("the user gave up")
            return earnings_frame(["2026-09-01"])

    factory.tickers["S4"] = StopAfterTwoChunks()

    with pytest.raises(KeyboardInterrupt):
        provider.earnings_dates(symbols, now=NOW)

    saved = json.loads(cache_file.read_text())["entries"]
    assert sorted(saved) == ["S0", "S1", "S2", "S3"], "two chunks survive the interrupt"


def test_a_cold_walk_asks_for_several_symbols_at_once(test_cfg: Config) -> None:
    """Audit PERF-003: the serial dict comprehension is now a bounded pool."""
    symbols = ["S0", "S1", "S2", "S3"]
    barrier = threading.Barrier(len(symbols), timeout=3)

    class Blocking(FakeTicker):
        def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
            barrier.wait()  # only clears if every call is in flight together
            return earnings_frame(["2026-09-01"])

    factory = TickerFactory({symbol: Blocking() for symbol in symbols})
    provider = build_provider(test_cfg, ticker_factory=factory, workers=4)

    out = provider.earnings_dates(symbols, now=NOW)

    assert out == dict.fromkeys(symbols, date(2026, 9, 1))


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


def test_an_unknown_earnings_date_is_re_asked_the_same_day(test_cfg: Config) -> None:
    """Audit BUG-051: "Yahoo has no date" cached three days against a 10-day blackout.

    A date published in the meantime could not be seen until the TTL expired,
    so an entry the blackout exists to block sailed through.
    """
    ticker = FakeTicker(earnings=None, calendar=None)
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory, retries=1)

    assert provider.earnings_dates(["AAPL"], now=NOW) == {"AAPL": None}
    assert provider.earnings_dates(["AAPL"], now=NOW + timedelta(hours=6)) == {"AAPL": None}
    before = factory.count

    ticker._earnings = earnings_frame(["2026-08-24"])
    later = provider.earnings_dates(["AAPL"], now=NOW + timedelta(hours=13))

    assert later == {"AAPL": date(2026, 8, 24)}
    assert factory.count > before


# ---------------------------------------------------------------------------
# the live scanner's upcoming-date path (audit COVER-1, BUG-051)
# ---------------------------------------------------------------------------


def test_a_healthy_upcoming_sweep_behaves_exactly_as_it_always_did(test_cfg: Config) -> None:
    """The whole observable surface of a normal sweep, pinned.

    This is the live scanner's path, so the three-outcome rewrite underneath it
    has to be invisible whenever Yahoo answers normally: same dates, same
    number of vendor calls, same cache contents, and no pacing beyond the
    ordinary gap between chunks.
    """
    days = ["2026-09-01", "2026-11-02", "2026-05-01"]  # one past, two ahead of NOW
    tickers = {f"S{i}": FakeTicker(earnings=earnings_frame(days)) for i in range(12)}
    factory = TickerFactory(tickers)
    sleep = RecordingSleep()
    provider = build_provider(test_cfg, ticker_factory=factory, sleep=sleep, chunk_size=5)
    symbols = list(tickers)

    out = provider.earnings_dates(symbols, now=NOW)

    assert out == dict.fromkeys(symbols, date(2026, 9, 1)), "the soonest date still in the future"
    assert factory.count == 12, "one call per symbol, as before"
    assert sleep.waits == [2.0, 2.0], "three chunks, two ordinary gaps"

    stored = TtlJsonCache(test_cfg.data.cache_dir / "earnings.json", timedelta(days=3)).read_all()
    assert len(stored) == 12
    assert {e["value"] for e in stored.values()} == {"2026-09-01"}

    # ...and it is still a warm cache on the next call.
    assert provider.earnings_dates(symbols, now=NOW + timedelta(days=2)) == out
    assert factory.count == 12


def test_a_symbol_yahoo_refuses_is_not_cached_as_having_no_earnings_date(
    test_cfg: Config,
) -> None:
    """Audit COVER-1 on the live path — the dangerous half.

    A rate-limited reply used to arrive as "no upcoming earnings date", get
    written down as one for twelve hours, and let the scanner enter a position
    the blackout existed to prevent. It must leave no trace instead.
    """
    tickers: dict[str, FakeTicker] = {
        f"S{i}": FakeTicker(earnings=earnings_frame(["2026-09-01"])) for i in range(11)
    }
    tickers["BROKEN"] = FakeTicker(explode=True)
    factory = TickerFactory(tickers)
    provider = build_provider(test_cfg, ticker_factory=factory, retries=1)

    out = provider.earnings_dates(list(tickers), now=NOW)

    assert "BROKEN" not in out, "unknown is not a date, and not a None either"
    stored = TtlJsonCache(test_cfg.data.cache_dir / "earnings.json", timedelta(days=3)).read_all()
    assert "BROKEN" not in stored, "a refusal must not be written down as a fact"
    assert len(stored) == 11, "its healthy neighbours are cached as usual"


def test_the_calendar_still_rescues_a_symbol_whose_earnings_table_refused(
    test_cfg: Config,
) -> None:
    """One source failing must not lose the other; that fallback predates this."""

    class TableRefuses(FakeTicker):
        def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
            raise RuntimeError("rate limited")

    ticker = TableRefuses(calendar={"Earnings Date": [date(2026, 10, 5)]})
    provider = build_provider(test_cfg, ticker_factory=TickerFactory({"AAPL": ticker}), retries=1)

    assert provider.earnings_dates(["AAPL"], now=NOW) == {"AAPL": date(2026, 10, 5)}


def test_only_past_dates_is_a_real_answer_and_keeps_the_short_miss_ttl(
    test_cfg: Config,
) -> None:
    """Audit BUG-051 semantics intact: "no upcoming date" is still cached, briefly.

    The vendor answered; it simply has nothing ahead of today. That is a fact
    about the company, so it is cached — but only for twelve hours, because it
    is the one fact that can turn into a date inside the blackout window.
    """
    ticker = FakeTicker(earnings=earnings_frame(["2020-02-01", "2021-05-02"]))
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory)

    assert provider.earnings_dates(["AAPL"], now=NOW) == {"AAPL": None}
    stored = TtlJsonCache(test_cfg.data.cache_dir / "earnings.json", timedelta(days=3)).read_all()
    assert stored["AAPL"]["value"] is None, "cached, unlike a refusal"

    before = factory.count
    provider.earnings_dates(["AAPL"], now=NOW + timedelta(hours=6))
    assert factory.count == before, "still fresh at six hours"

    ticker._earnings = earnings_frame(["2026-08-24"])
    later = provider.earnings_dates(["AAPL"], now=NOW + timedelta(hours=13))
    assert later == {"AAPL": date(2026, 8, 24)}, "and stale at thirteen"


def test_a_throttled_upcoming_batch_caches_nothing(test_cfg: Config) -> None:
    """The history guard, reused rather than reinvented, on the live path."""

    class Refuses(FakeTicker):
        def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
            raise RuntimeError("rate limited")

        def get_calendar(self) -> Any:
            raise RuntimeError("rate limited")

    tickers = {f"S{i}": Refuses() for i in range(20)}
    provider = build_provider(
        test_cfg, ticker_factory=TickerFactory(tickers), retries=1, history_retries=0
    )

    out = provider.earnings_dates(list(tickers), now=NOW)

    assert out == {}, "nothing is known, and nothing is claimed"
    stored = TtlJsonCache(test_cfg.data.cache_dir / "earnings.json", timedelta(days=3)).read_all()
    assert stored == {}


def test_a_morning_confirm_over_a_few_positions_is_never_read_as_throttling(
    test_cfg: Config,
) -> None:
    """The live case that must not change: four held names, all answered "none".

    Below the minimum batch the guard stays out of the way, so a confirm run
    over a handful of positions caches its answers exactly as it always has.
    """
    tickers = {s: FakeTicker(earnings=None, calendar=None) for s in ("AAPL", "MSFT", "KO", "PG")}
    provider = build_provider(test_cfg, ticker_factory=TickerFactory(tickers))

    out = provider.earnings_dates(list(tickers), now=NOW)

    assert out == dict.fromkeys(tickers), "all four genuinely have no upcoming date"
    stored = TtlJsonCache(test_cfg.data.cache_dir / "earnings.json", timedelta(days=3)).read_all()
    assert len(stored) == 4


# ---------------------------------------------------------------------------
# earnings history (amendment A12 / audit BUG-036)
# ---------------------------------------------------------------------------


def test_earnings_history_returns_past_and_future_dates_in_the_window(
    test_cfg: Config,
) -> None:
    """A12: the backtest needs where the blackouts *were*, not the next one."""
    factory = TickerFactory(
        {
            "AAPL": FakeTicker(
                earnings=earnings_frame(["2024-02-01", "2025-05-02", "2025-08-01", "2026-11-02"])
            )
        }
    )
    provider = build_provider(test_cfg, ticker_factory=factory)

    out = provider.earnings_history(["aapl"], date(2025, 1, 1), date(2025, 12, 31), now=NOW)

    assert out == {"AAPL": (date(2025, 5, 2), date(2025, 8, 1))}


def test_earnings_history_is_unknown_rather_than_empty_when_yahoo_has_nothing(
    test_cfg: Config,
) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(earnings=None)})
    provider = build_provider(test_cfg, ticker_factory=factory, retries=1)

    out = provider.earnings_history(["AAPL"], date(2025, 1, 1), date(2025, 12, 31), now=NOW)

    assert out == {"AAPL": ()}


def test_earnings_history_caches_the_whole_list_not_the_window(test_cfg: Config) -> None:
    """Two windows over one symbol must share a single download."""
    factory = TickerFactory(
        {"AAPL": FakeTicker(earnings=earnings_frame(["2024-02-01", "2025-05-02"]))}
    )
    provider = build_provider(test_cfg, ticker_factory=factory)

    provider.earnings_history(["AAPL"], date(2024, 1, 1), date(2024, 12, 31), now=NOW)
    before = factory.count
    second = provider.earnings_history(["AAPL"], date(2025, 1, 1), date(2025, 12, 31), now=NOW)

    assert factory.count == before, "a different window is a filter, not a fetch"
    assert second == {"AAPL": (date(2025, 5, 2),)}


def test_earnings_history_asks_for_enough_history_to_backtest(test_cfg: Config) -> None:
    class Recorder(FakeTicker):
        limits: list[int] = []

        def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
            Recorder.limits.append(limit)
            return earnings_frame(["2025-05-02"])

    provider = build_provider(test_cfg, ticker_factory=TickerFactory({"AAPL": Recorder()}))
    provider.earnings_history(["AAPL"], date(2020, 1, 1), date(2026, 1, 1), now=NOW)

    assert Recorder.limits == [60]


# ---------------------------------------------------------------------------
# how long historical earnings stay fresh (audit REPRO-1)
# ---------------------------------------------------------------------------


def test_settled_history_ttl_floors_at_the_volatile_ttl() -> None:
    """A window reaching into today is exactly as trustworthy as it ever was."""
    assert settled_history_ttl(NOW.date(), now=NOW) == EARNINGS_TTL
    assert settled_history_ttl(NOW.date() - timedelta(days=20), now=NOW) == EARNINGS_TTL
    assert settled_history_ttl(NOW.date() + timedelta(days=90), now=NOW) == EARNINGS_TTL


def test_settled_history_ttl_grows_with_the_age_of_the_window() -> None:
    ttl = settled_history_ttl(date(2024, 12, 31), now=NOW)

    # 2026-08-18 21:00 minus midnight after 2024-12-31, minus the 30-day lag.
    assert ttl == timedelta(days=564, hours=21)
    assert ttl > settled_history_ttl(date(2025, 6, 30), now=NOW)


def test_settled_history_ttl_stops_at_the_ceiling() -> None:
    assert settled_history_ttl(date(1999, 1, 1), now=NOW) == EARNINGS_HISTORY_TTL


def test_a_settled_history_window_is_not_refetched_under_the_live_ttl(test_cfg: Config) -> None:
    """Audit REPRO-1: a company's 2019 earnings date does not change.

    It used to share the three-day TTL of the *next* announcement, so a pair of
    backtests run either side of that boundary silently read different earnings
    — and reported the same ``config_hash``, ``data_hash`` and ``code_ref``,
    because none of the three can see this file.
    """
    factory = TickerFactory(
        {"AAPL": FakeTicker(earnings=earnings_frame(["2019-02-01", "2019-05-02"]))}
    )
    provider = build_provider(test_cfg, ticker_factory=factory)
    window = (date(2018, 1, 1), date(2020, 12, 31))

    provider.earnings_history(["AAPL"], *window, now=NOW)
    before = factory.count
    later = provider.earnings_history(["AAPL"], *window, now=NOW + timedelta(days=400))

    assert factory.count == before, "settled history must not churn"
    assert later == {"AAPL": (date(2019, 2, 1), date(2019, 5, 2))}


def test_a_history_window_running_to_today_still_expires_in_three_days(
    test_cfg: Config,
) -> None:
    """The volatile half keeps its old lifetime: the tail really can move."""
    ticker = FakeTicker(earnings=earnings_frame(["2026-09-01"]))
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory)
    window = (date(2026, 1, 1), NOW.date())

    provider.earnings_history(["AAPL"], *window, now=NOW)
    before = factory.count
    provider.earnings_history(["AAPL"], *window, now=NOW + timedelta(days=2, hours=23))
    assert factory.count == before

    provider.earnings_history(["AAPL"], *window, now=NOW + timedelta(days=3, seconds=1))
    assert factory.count > before


@pytest.mark.parametrize(
    ("end", "refetched"),
    [
        # 2026-07-18 finished 31 days before the fetch, so the record's row for
        # it was already a reported fact when we wrote it down.
        (date(2026, 7, 18), False),
        # One day later, and the window's last day was only 30 days behind the
        # fetch: inside the lag, so that row may still have been an estimate.
        (date(2026, 7, 19), True),
    ],
)
def test_the_settled_boundary_is_measured_from_the_fetch_not_from_today(
    test_cfg: Config, end: date, refetched: bool
) -> None:
    """The upcoming-to-history transition, one day either side of it.

    Both calls happen at the same moment on the same cached record. The only
    difference is how far the *window* reaches, and that is the whole scheme:
    a date does not become trustworthy because a month has passed on our clock,
    only because a month had passed when the vendor was asked.
    """
    factory = TickerFactory({"AAPL": FakeTicker(earnings=earnings_frame(["2026-07-15"]))})
    provider = build_provider(test_cfg, ticker_factory=factory)

    provider.earnings_history(["AAPL"], date(2026, 1, 1), end, now=NOW)
    before = factory.count
    provider.earnings_history(["AAPL"], date(2026, 1, 1), end, now=NOW + timedelta(days=10))

    assert (factory.count > before) is refetched


def test_an_unknown_history_is_re_asked_the_same_day_however_old_the_window(
    test_cfg: Config,
) -> None:
    """Audit BUG-051 survives REPRO-1: an absence of data is never settled.

    "Yahoo has no history for this symbol" is the one answer that can turn into
    a real one at any moment, so it keeps its twelve hours no matter how far in
    the past the caller is looking.
    """
    ticker = FakeTicker(earnings=None)
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory, retries=1)
    window = (date(2018, 1, 1), date(2020, 12, 31))

    assert provider.earnings_history(["AAPL"], *window, now=NOW) == {"AAPL": ()}
    before = factory.count
    provider.earnings_history(["AAPL"], *window, now=NOW + timedelta(hours=6))
    assert factory.count == before, "a miss is still worth caching for a few hours"

    ticker._earnings = earnings_frame(["2019-05-02"])
    later = provider.earnings_history(["AAPL"], *window, now=NOW + timedelta(hours=13))

    assert factory.count > before
    assert later == {"AAPL": (date(2019, 5, 2),)}


def test_a_warm_history_cache_serves_a_backtest_without_touching_yahoo(
    test_cfg: Config,
) -> None:
    """Audit REPRO-1, the whole point: a rerun months later downloads nothing.

    A fresh provider instance, so this proves the file is doing the work rather
    than anything remembered in memory.
    """
    symbols = [f"S{i}" for i in range(20)]
    days = ["2019-02-01", "2019-05-02", "2019-08-01", "2019-11-01"]
    expected = {symbol: tuple(date.fromisoformat(day) for day in days) for symbol in symbols}
    factory = TickerFactory({s: FakeTicker(earnings=earnings_frame(days)) for s in symbols})
    window = (date(2018, 1, 1), date(2020, 12, 31))

    build_provider(test_cfg, ticker_factory=factory).earnings_history(symbols, *window, now=NOW)
    cold = factory.count
    assert cold == len(symbols)

    warm = build_provider(test_cfg, ticker_factory=factory).earnings_history(
        symbols, *window, now=NOW + timedelta(days=200)
    )

    assert factory.count == cold, "200 days on, a warm history cache is still a warm cache"
    assert warm == expected


def test_the_old_shared_ttl_is_what_made_the_warm_cache_churn(test_cfg: Config) -> None:
    """The same run under the pre-REPRO-1 setting, to show what changed.

    Pinning ``earnings_history_ttl`` back to the three days it used to share
    with the upcoming-date cache re-downloads every symbol.
    """
    symbols = [f"S{i}" for i in range(20)]
    factory = TickerFactory(
        {s: FakeTicker(earnings=earnings_frame(["2019-02-01"])) for s in symbols}
    )
    window = (date(2018, 1, 1), date(2020, 12, 31))
    old = {"ticker_factory": factory, "earnings_history_ttl": EARNINGS_TTL}

    build_provider(test_cfg, **old).earnings_history(symbols, *window, now=NOW)
    cold = factory.count
    build_provider(test_cfg, **old).earnings_history(symbols, *window, now=NOW + timedelta(days=4))

    assert factory.count == cold + len(symbols)


def test_history_cannot_be_configured_to_rot_faster_than_the_next_date(
    test_cfg: Config,
) -> None:
    with pytest.raises(ValueError, match="must not expire sooner"):
        build_provider(test_cfg, earnings_history_ttl=timedelta(hours=1))
    with pytest.raises(ValueError, match="cannot be negative"):
        build_provider(test_cfg, earnings_settle_lag=timedelta(days=-1))


def test_a_window_ending_today_says_why_it_re_downloaded_history(
    test_cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    """The one place a run is told its earnings inputs may have moved.

    ``earnings_history`` has a single caller — the backtest — and a backtest
    whose window runs to today still expires history in three days, because
    the tail of the record genuinely can still move. Nothing in the run's
    identity triple records that, so the log line has to.
    """
    tickers = {s: FakeTicker(earnings=earnings_frame(["2019-02-01"])) for s in ("AAPL", "MSFT")}
    provider = build_provider(test_cfg, ticker_factory=TickerFactory(tickers))

    with caplog.at_level("INFO", logger="swing.data.yf_provider"):
        provider.earnings_history(["AAPL"], date(2010, 1, 1), NOW.date(), now=NOW)
    assert "an earlier, fixed day" in caplog.text
    assert "30-day period" in caplog.text

    caplog.clear()
    with caplog.at_level("INFO", logger="swing.data.yf_provider"):
        provider.earnings_history(["MSFT"], date(2010, 1, 1), date(2020, 12, 31), now=NOW)
    assert "1 of 1 symbols; 1 had dates" in caplog.text
    assert "fixed day" not in caplog.text, "a settled window has nothing to warn about"


def test_a_warm_settled_window_logs_nothing_at_all(
    test_cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    factory = TickerFactory({"AAPL": FakeTicker(earnings=earnings_frame(["2019-02-01"]))})
    provider = build_provider(test_cfg, ticker_factory=factory)
    window = (date(2010, 1, 1), date(2020, 12, 31))
    provider.earnings_history(["AAPL"], *window, now=NOW)

    caplog.clear()
    with caplog.at_level("INFO", logger="swing.data.yf_provider"):
        provider.earnings_history(["AAPL"], *window, now=NOW + timedelta(days=400))

    assert caplog.text == "", "nothing was downloaded, so there is nothing to say"


def test_two_caches_filled_at_different_times_fingerprint_the_same(tmp_path: Path) -> None:
    """Audit REPRO-1(b): the digest answers "same earnings?", not "same run?".

    Two separate cache directories, filled a year apart from the same vendor
    answers. Everything incidental differs — the files, their ``fetched_at``
    stamps — and the fingerprints must still match, or the number is useless
    for comparing two runs.
    """
    days = ["2019-02-01", "2019-05-02", "2020-08-03"]
    symbols = ["AAPL", "MSFT"]
    window = (date(2018, 1, 1), date(2020, 12, 31))

    def fill(name: str, now: datetime) -> dict[str, Any]:
        cfg = build_config(tmp_path, data={"cache_dir": tmp_path / name})
        factory = TickerFactory({s: FakeTicker(earnings=earnings_frame(days)) for s in symbols})
        provider = build_provider(cfg, ticker_factory=factory)
        return dict(provider.earnings_history(symbols, *window, now=now))

    early = fill("early", NOW)
    late = fill("late", NOW + timedelta(days=365))

    assert early == late
    assert earnings_fingerprint(symbols, early) == earnings_fingerprint(symbols, late)


# ---------------------------------------------------------------------------
# a throttled sweep must not be cached as fact (audit COVER-1)
# ---------------------------------------------------------------------------


def history_batch(n: int, *, dated: int, prefix: str = "S") -> dict[str, FakeTicker]:
    """``n`` tickers of which the first ``dated`` have earnings, the rest none."""
    return {
        f"{prefix}{i}": FakeTicker(earnings=earnings_frame(["2019-02-01"]) if i < dated else None)
        for i in range(n)
    }


def history_entries(cfg: Config) -> dict[str, Any]:
    """What actually reached the earnings-history file, if anything did.

    Read through the cache's own reader rather than ``read_text``: a sweep that
    trusted none of its answers writes no file at all, which is the point.
    """
    return TtlJsonCache(cfg.data.cache_dir / "earnings-history.json", timedelta(days=1)).read_all()


def test_a_batch_that_comes_back_almost_all_empty_is_not_cached(test_cfg: Config) -> None:
    """Audit COVER-1, the heart of it.

    The real cache held 1,507 nulls, among them Microsoft, JPMorgan and Exxon.
    They were not companies without earnings; they were a rate limiter, written
    down as fact. A batch this empty is not evidence of absence, so nothing
    from it may be stored — the symbols must stay stale and be asked again.
    """
    tickers = history_batch(20, dated=2)  # 90% empty: far past the limit
    factory = TickerFactory(tickers)
    provider = build_provider(test_cfg, ticker_factory=factory, history_retries=0)

    out = provider.earnings_history(list(tickers), date(2018, 1, 1), date(2020, 12, 31), now=NOW)

    assert out == dict.fromkeys(tickers, ()), "unknown, as it should be"
    stored = history_entries(test_cfg)
    assert stored == {}, "a refusal is not an answer, so nothing is written down"


def test_a_healthy_batch_with_a_few_empties_is_cached_normally(test_cfg: Config) -> None:
    """ETFs really do have no earnings, and that answer is worth keeping.

    A measured healthy batch answers ~93% of the time; the universe is ~8%
    ETFs. The guard has to let that through or it would never cache anything.
    """
    tickers = history_batch(20, dated=18)  # 10% empty, like the real thing
    factory = TickerFactory(tickers)
    provider = build_provider(test_cfg, ticker_factory=factory)
    window = (date(2018, 1, 1), date(2020, 12, 31))

    provider.earnings_history(list(tickers), *window, now=NOW)
    stored = history_entries(test_cfg)

    assert len(stored) == 20
    assert sum(1 for e in stored.values() if e["value"] is None) == 2

    # And the two kinds of answer keep their own lifetimes. The eighteen with
    # dates are settled history and are never asked about again; the two the
    # vendor had nothing for keep the short miss TTL, because "nothing today"
    # is the one answer that can turn into a real one (audit BUG-051).
    before = factory.count
    provider.earnings_history(list(tickers), *window, now=NOW + timedelta(days=200))

    assert factory.count == before + 2, "only the empties are re-asked"
    assert sorted(factory.calls[before:]) == ["S18", "S19"]


def test_a_symbol_the_vendor_refused_is_never_written_down_as_having_no_earnings(
    test_cfg: Config,
) -> None:
    """ "We could not ask" and "there is nothing" are different facts.

    ``_safe_call`` used to flatten every exception into an empty answer, so a
    rate-limit reply never even reached the retry policy and was stored as a
    statement about the company.
    """
    tickers = history_batch(19, dated=19)
    tickers["BROKEN"] = FakeTicker(explode=True)
    factory = TickerFactory(tickers)
    provider = build_provider(test_cfg, ticker_factory=factory, retries=1)

    provider.earnings_history(list(tickers), date(2018, 1, 1), date(2020, 12, 31), now=NOW)
    stored = history_entries(test_cfg)

    assert "BROKEN" not in stored, "a failed ask must leave no trace"
    assert len(stored) == 19, "its nineteen healthy neighbours are still cached"


def test_a_throttled_batch_is_retried_after_a_cooldown(test_cfg: Config) -> None:
    """Retry-on-empty with backoff: the sweep waits, then asks again."""
    calls = {"n": 0}
    good = earnings_frame(["2019-02-01"])

    class Flaky(FakeTicker):
        def get_earnings_dates(self, limit: int = 12) -> pd.DataFrame | None:
            calls["n"] += 1
            # The first pass over the batch is starved; the second is served.
            return None if calls["n"] <= 20 else good

    tickers = {f"S{i}": Flaky() for i in range(20)}
    sleep = RecordingSleep()
    provider = build_provider(
        test_cfg,
        ticker_factory=TickerFactory(tickers),
        sleep=sleep,
        history_cooldown=15.0,
        history_retries=2,
    )

    out = provider.earnings_history(list(tickers), date(2018, 1, 1), date(2020, 12, 31), now=NOW)

    assert out == dict.fromkeys(tickers, (date(2019, 2, 1),)), "the retry got the real answer"
    assert sleep.waits == [15.0], "one cooldown, before the second attempt"


def test_the_cooldown_doubles_and_then_the_batch_is_abandoned(test_cfg: Config) -> None:
    tickers = history_batch(20, dated=0)
    sleep = RecordingSleep()
    provider = build_provider(
        test_cfg,
        ticker_factory=TickerFactory(tickers),
        sleep=sleep,
        history_cooldown=15.0,
        history_retries=2,
    )

    provider.earnings_history(list(tickers), date(2018, 1, 1), date(2020, 12, 31), now=NOW)

    assert sleep.waits == [15.0, 30.0], "backoff doubles, then we stop asking"
    stored = history_entries(test_cfg)
    assert stored == {}


def test_the_sweep_pauses_between_chunks_but_not_before_the_first(test_cfg: Config) -> None:
    """A cold walk is thousands of requests; pace beats speed (audit COVER-1)."""
    tickers = history_batch(12, dated=12)
    sleep = RecordingSleep()
    provider = build_provider(
        test_cfg,
        ticker_factory=TickerFactory(tickers),
        sleep=sleep,
        chunk_size=5,
        history_pause=2.0,
    )

    provider.earnings_history(list(tickers), date(2018, 1, 1), date(2020, 12, 31), now=NOW)

    assert sleep.waits == [2.0, 2.0], "three chunks, two gaps"


def test_an_abandoned_batch_says_so_loudly(
    test_cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    tickers = history_batch(20, dated=0)
    provider = build_provider(test_cfg, ticker_factory=TickerFactory(tickers), history_retries=0)

    with caplog.at_level("WARNING", logger="swing.data.yf_provider"):
        provider.earnings_history(list(tickers), date(2018, 1, 1), date(2020, 12, 31), now=NOW)

    assert "looks like rate limiting" in caplog.text
    assert "20 symbols went unanswered" in caplog.text
    assert "blackout is weaker than usual" in caplog.text


def test_coverage_tells_the_truth_about_a_throttled_sweep(test_cfg: Config) -> None:
    """The two halves of COVER-1 meeting: the sweep refuses to lie, and the
    coverage number refuses to let the reader assume.

    Before, this run would have cached 18 symbols as "no earnings" and the
    summary would have said the blackout was simulated. Now the sweep declines
    to write them down, and coverage says plainly that the blackout could reach
    two of twenty names.
    """
    tickers = history_batch(20, dated=2)
    provider = build_provider(test_cfg, ticker_factory=TickerFactory(tickers), history_retries=0)
    symbols = list(tickers)

    out = provider.earnings_history(symbols, date(2018, 1, 1), date(2020, 12, 31), now=NOW)
    coverage = earnings_coverage(symbols, out)

    assert coverage.with_dates == 0, "nothing from a refused batch is trusted"
    assert "0 of 20 symbols (0%)" in coverage.describe()

    # ...and once Yahoo answers, the same call reports real coverage.
    healthy = build_provider(test_cfg, ticker_factory=TickerFactory(history_batch(20, dated=18)))
    later = healthy.earnings_history(symbols, date(2018, 1, 1), date(2020, 12, 31), now=NOW)

    assert earnings_coverage(symbols, later).with_dates == 18


def test_a_small_batch_is_never_read_as_rate_limiting(test_cfg: Config) -> None:
    """Below the minimum batch there is nothing to infer, so ETFs still cache.

    A confirm run asking about three held positions, all of them funds, must
    not have its answers thrown away as a suspected rate limit.
    """
    tickers = history_batch(3, dated=0)
    factory = TickerFactory(tickers)
    provider = build_provider(test_cfg, ticker_factory=factory)
    window = (date(2018, 1, 1), date(2020, 12, 31))

    provider.earnings_history(list(tickers), *window, now=NOW)
    stored = history_entries(test_cfg)

    assert len(stored) == 3
    assert all(entry["value"] is None for entry in stored.values())


# ---------------------------------------------------------------------------
# fundamentals
# ---------------------------------------------------------------------------


def income_statement(
    eps: list[float], revenue: list[float], *, periods: list[str] | None = None
) -> pd.DataFrame:
    """Reporting periods newest column first, as yfinance returns them."""
    columns = pd.DatetimeIndex(periods or ["2025-12-31", "2024-12-31"])
    return pd.DataFrame([eps, revenue], index=["Diluted EPS", "Total Revenue"], columns=columns)


def test_fundamentals_prefer_the_company_profile(test_cfg: Config) -> None:
    info = {"earningsQuarterlyGrowth": 0.18, "revenueGrowth": 0.09}
    factory = TickerFactory({"AAPL": FakeTicker(info=info)})

    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)

    assert out["AAPL"] == Fundamentals(
        symbol="AAPL", eps_growth=0.18, revenue_growth=0.09, basis="quarterly"
    )


def test_fundamentals_fall_back_to_the_income_statement(test_cfg: Config) -> None:
    factory = TickerFactory(
        {"AAPL": FakeTicker(info={}, statement=income_statement([6.0, 5.0], [110.0, 100.0]))}
    )

    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)

    assert out["AAPL"].eps_growth == pytest.approx(0.2)
    assert out["AAPL"].revenue_growth == pytest.approx(0.1)
    assert out["AAPL"].basis == "annual", "and it says so, instead of passing as quarterly"


def test_fundamentals_ask_for_the_quarterly_statement_first(test_cfg: Config) -> None:
    """Audit BUG-037: the fallback was the *annual* statement, invisibly.

    ``info``'s growth is quarter-over-quarter year-on-year, so a candidate
    screened on the fallback was screened on a different measurement with
    nothing in the record to say so.
    """
    quarterly = income_statement([1.2, 1.0], [55.0, 50.0], periods=["2025-09-30", "2025-06-30"])
    asked: list[str] = []

    class FreqAwareTicker(FakeTicker):
        def get_income_stmt(self, freq: str = "yearly") -> pd.DataFrame | None:
            asked.append(freq)
            return quarterly if freq == "quarterly" else self._statement

    factory = TickerFactory(
        {"AAPL": FreqAwareTicker(info={}, statement=income_statement([6.0, 5.0], [110.0, 100.0]))}
    )

    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)

    assert asked[0] == "quarterly"
    assert out["AAPL"].eps_growth == pytest.approx(0.2)
    assert out["AAPL"].revenue_growth == pytest.approx(0.1)
    assert out["AAPL"].basis == "quarterly"


def test_fundamentals_refuse_to_compare_non_adjacent_periods(test_cfg: Config) -> None:
    """Audit BUG-037: a two-period gap used to masquerade as one period of growth."""
    gapped = income_statement(
        [6.0, float("nan"), 5.0],
        [110.0, float("nan"), 100.0],
        periods=["2025-12-31", "2024-12-31", "2023-12-31"],
    )
    factory = TickerFactory({"AAPL": FakeTicker(info={}, statement=gapped)})

    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)

    assert out["AAPL"].eps_growth is None
    assert out["AAPL"].revenue_growth is None
    assert out["AAPL"].basis is None


def test_a_mixed_pair_of_sources_is_labelled_mixed(test_cfg: Config) -> None:
    factory = TickerFactory(
        {
            "AAPL": FakeTicker(
                info={"revenueGrowth": 0.05},
                statement=income_statement([6.0, 5.0], [110.0, 100.0]),
            )
        }
    )
    out = build_provider(test_cfg, ticker_factory=factory).fundamentals(["AAPL"], now=NOW)
    assert out["AAPL"].basis == "mixed"


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
    assert cached["AAPL"] == Fundamentals(
        symbol="AAPL", eps_growth=0.18, revenue_growth=0.09, basis="quarterly"
    ), "the basis label survives the round trip too"


def test_fundamentals_are_refetched_after_a_week(test_cfg: Config) -> None:
    ticker = FakeTicker(info={"earningsQuarterlyGrowth": 0.18, "revenueGrowth": 0.09})
    factory = TickerFactory({"AAPL": ticker})
    provider = build_provider(test_cfg, ticker_factory=factory)
    provider.fundamentals(["AAPL"], now=NOW)

    ticker._info = {"earningsQuarterlyGrowth": 0.25, "revenueGrowth": 0.11}
    later = provider.fundamentals(["AAPL"], now=NOW + timedelta(days=8))

    assert later["AAPL"].eps_growth == 0.25
