"""Stooq fallback provider, against canned CSV bodies.

Stooq is the answer to "yfinance broke again on a Sunday night", so the tests
that matter are the ugly ones: an empty body, the rate-limit sentence Stooq
returns as a *200*, a truncated CSV. Every one of those must leave the symbol
absent and the run alive, because the fallback provider taking a scan down
would defeat the entire point of having a fallback.

All network I/O goes through ``StooqProvider._fetch_csv``; nothing here touches
the network, and the one test that exercises the real URL construction
monkeypatches ``requests.get``.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import pytest

from swing.config import Config, load_config
from swing.data.provider import DataProvider, get_provider
from swing.data.stooq_provider import StooqProvider, stooq_symbol

START = date(2024, 1, 1)
END = date(2024, 1, 8)

GOOD_CSV = """Date,Open,High,Low,Close,Volume
2024-01-02,100.0,101.5,99.5,101.0,1500000
2024-01-03,101.0,102.0,100.0,100.5,1200000
2024-01-04,100.5,103.0,100.2,102.75,1800000
"""

#: Stooq hands the rows back newest-first for some windows; the canonical frame
#: must come out sorted regardless.
UNSORTED_CSV = """Date,Open,High,Low,Close,Volume
2024-01-04,100.5,103.0,100.2,102.75,1800000
2024-01-02,100.0,101.5,99.5,101.0,1500000
2024-01-03,101.0,102.0,100.0,100.5,1200000
"""

#: A 200 response whose body is a sentence, not a CSV.
LIMIT_BODY = "Exceeded the daily hits limit"

#: Header present, one row truncated mid-line, and no OHLC columns at all.
MALFORMED_CSV = "Date,Something,Else\n2024-01-02,1,2\n"


@pytest.fixture
def stooq_config(tmp_path) -> Config:
    """A config that selects Stooq, with the politeness pause switched off."""
    data = load_config().as_dict()
    data["data"]["provider"] = "stooq"
    data["data"]["cache_dir"] = str(tmp_path / "cache")
    data["data"]["request_pause_sec"] = 0.0
    return Config(data)


@pytest.fixture
def provider(stooq_config) -> StooqProvider:
    return StooqProvider(stooq_config)


def canned(bodies: dict[str, str | None]):
    """Build a ``_fetch_csv`` replacement serving ``bodies`` by symbol."""

    def _fetch(self, symbol, start, end):
        return bodies.get(symbol)

    return _fetch


# ---------------------------------------------------------------------------
# provider selection
# ---------------------------------------------------------------------------
def test_factory_returns_stooq_and_still_knows_the_others(stooq_config):
    assert isinstance(get_provider(stooq_config), StooqProvider)
    assert get_provider(stooq_config).name == "stooq"

    data = stooq_config.as_dict()
    data["data"]["provider"] = "yfinance"
    assert get_provider(Config(data)).name == "yfinance"
    data["data"]["provider"] = "schwab"
    assert get_provider(Config(data)).name == "schwab"


def test_unknown_provider_names_all_three(stooq_config):
    """The error is the only place a typo'd provider name gets explained."""
    data = stooq_config.as_dict()
    data["data"]["provider"] = "bloomberg"
    with pytest.raises(ValueError, match="unknown data.provider") as exc:
        get_provider(Config(data))
    message = str(exc.value)
    assert "yfinance" in message and "schwab" in message and "stooq" in message


def test_stooq_satisfies_the_provider_protocol(provider):
    assert isinstance(provider, DataProvider)


# ---------------------------------------------------------------------------
# symbol mapping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("AAPL", "aapl.us"),
        ("aapl", "aapl.us"),
        ("BRK.B", "brk-b.us"),      # share classes: dot -> dash, then suffix
        ("BF.A", "bf-a.us"),
        (" MSFT ", "msft.us"),
    ],
)
def test_symbol_mapping(ticker, expected):
    assert stooq_symbol(ticker) == expected


def test_fetch_builds_the_documented_url(provider, monkeypatch):
    """The one test that exercises the real seam — with requests stubbed out."""
    import requests

    seen = {}

    class FakeResponse:
        status_code = 200
        text = GOOD_CSV

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    body = provider._fetch_csv("BRK.B", START, END)
    assert body == GOOD_CSV
    assert seen["url"] == "https://stooq.com/q/d/l/"
    assert seen["params"] == {"s": "brk-b.us", "i": "d",
                              "d1": "20240101", "d2": "20240108"}
    assert seen["timeout"] and seen["timeout"] > 0


# ---------------------------------------------------------------------------
# bars: the happy path
# ---------------------------------------------------------------------------
def test_bars_come_back_in_the_canonical_schema(provider, monkeypatch):
    monkeypatch.setattr(StooqProvider, "_fetch_csv", canned({"AAA": GOOD_CSV}))

    frame = provider.daily_bars(["AAA"], START, END)["AAA"]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.tz is None
    assert frame.index.name == "date"
    assert frame.index.is_monotonic_increasing
    assert not frame["close"].isna().any()
    assert len(frame) == 3
    assert frame["close"].iloc[-1] == pytest.approx(102.75)
    assert str(frame.dtypes["volume"]) == "float64"


def test_rows_are_sorted_even_when_stooq_returns_them_newest_first(provider, monkeypatch):
    monkeypatch.setattr(StooqProvider, "_fetch_csv", canned({"AAA": UNSORTED_CSV}))

    frame = provider.daily_bars(["AAA"], START, END)["AAA"]
    assert frame.index.is_monotonic_increasing
    assert frame["close"].iloc[0] == pytest.approx(101.0)
    assert frame["close"].iloc[-1] == pytest.approx(102.75)


def test_symbols_are_uppercased_in_the_result(provider, monkeypatch):
    monkeypatch.setattr(StooqProvider, "_fetch_csv", canned({"AAA": GOOD_CSV}))
    assert set(provider.daily_bars(["aaa"], START, END)) == {"AAA"}


def test_one_bad_symbol_does_not_take_the_batch_down(provider, monkeypatch):
    monkeypatch.setattr(
        StooqProvider, "_fetch_csv",
        canned({"AAA": GOOD_CSV, "BBB": "", "CCC": GOOD_CSV}),
    )
    bars = provider.daily_bars(["AAA", "BBB", "CCC"], START, END)
    assert set(bars) == {"AAA", "CCC"}


# ---------------------------------------------------------------------------
# bars: every failure mode leaves the symbol absent, nothing raises
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty-body"),
        pytest.param("   \n ", id="whitespace-body"),
        pytest.param(None, id="no-body-at-all"),
        pytest.param("No data", id="no-data-sentence"),
        pytest.param(MALFORMED_CSV, id="malformed-csv"),
        pytest.param("Date,Open,High,Low,Close,Volume\n", id="header-only"),
        pytest.param("<html><body>nope</body></html>", id="html-error-page"),
    ],
)
def test_failure_modes_leave_the_symbol_absent(provider, monkeypatch, body):
    monkeypatch.setattr(StooqProvider, "_fetch_csv", canned({"AAA": body}))
    assert provider.daily_bars(["AAA"], START, END) == {}


def test_network_exception_is_swallowed_per_symbol(provider, monkeypatch):
    """A blown-up socket on one symbol must not cost us the other 499."""

    def boom(self, symbol, start, end):
        if symbol == "AAA":
            raise ConnectionError("connection reset by peer")
        return GOOD_CSV

    monkeypatch.setattr(StooqProvider, "_fetch_csv", boom)
    assert set(provider.daily_bars(["AAA", "BBB"], START, END)) == {"BBB"}


def test_daily_limit_warns_exactly_once_for_a_whole_batch(provider, monkeypatch, caplog):
    """Rate limiting fails *every* symbol; 500 identical warnings help nobody."""
    monkeypatch.setattr(
        StooqProvider, "_fetch_csv",
        canned(dict.fromkeys(["AAA", "BBB", "CCC", "DDD"], LIMIT_BODY)),
    )

    with caplog.at_level(logging.WARNING, logger="swing.data.stooq"):
        bars = provider.daily_bars(["AAA", "BBB", "CCC", "DDD"], START, END)

    assert bars == {}
    limit_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "daily hits limit" in r.getMessage()
    ]
    assert len(limit_warnings) == 1


def test_limit_warning_fires_again_on_the_next_run(provider, monkeypatch, caplog):
    """The flag is per-run, so a later scan still reports it was throttled."""
    monkeypatch.setattr(
        StooqProvider, "_fetch_csv", canned({"AAA": LIMIT_BODY})
    )
    with caplog.at_level(logging.WARNING, logger="swing.data.stooq"):
        provider.daily_bars(["AAA"], START, END)
        provider.daily_bars(["AAA"], START, END)

    limit_warnings = [
        r for r in caplog.records if "daily hits limit" in r.getMessage()
    ]
    assert len(limit_warnings) == 2


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------
def test_quotes_are_the_last_close_and_are_marked_stale(provider, monkeypatch):
    """Stooq is end-of-day; stale=True is what widens the confirm tolerances."""
    monkeypatch.setattr(StooqProvider, "_fetch_csv", canned({"AAA": GOOD_CSV}))

    quote = provider.quotes(["AAA"])["AAA"]
    assert quote.symbol == "AAA"
    assert quote.price == pytest.approx(102.75)
    assert quote.stale is True
    assert quote.bid is None and quote.ask is None
    assert quote.spread_pct is None


def test_quotes_ask_for_a_short_recent_window(provider, monkeypatch):
    windows = []

    def _fetch(self, symbol, start, end):
        windows.append((start, end))
        return GOOD_CSV

    monkeypatch.setattr(StooqProvider, "_fetch_csv", _fetch)
    provider.quotes(["AAA"])

    start, end = windows[0]
    assert end == date.today()
    assert 1 <= (end - start).days <= 10


def test_symbol_without_data_has_no_quote(provider, monkeypatch):
    monkeypatch.setattr(
        StooqProvider, "_fetch_csv", canned({"AAA": GOOD_CSV, "BBB": ""})
    )
    assert set(provider.quotes(["AAA", "BBB"])) == {"AAA"}


# ---------------------------------------------------------------------------
# earnings / fundamentals: absent, and loudly so
# ---------------------------------------------------------------------------
def test_earnings_are_unknown_for_every_symbol(provider):
    assert provider.earnings_dates(["AAA", "BBB"]) == {"AAA": None, "BBB": None}


def test_fundamentals_are_empty_records_not_missing_keys(provider):
    """``None`` fields mean "no opinion"; a missing symbol would mean "fails"."""
    funds = provider.fundamentals(["AAA", "BBB"])
    assert set(funds) == {"AAA", "BBB"}
    assert funds["AAA"].symbol == "AAA"
    assert funds["AAA"].trailing_eps is None
    assert funds["AAA"].market_cap is None
    assert funds["AAA"].is_etf is False


def test_the_missing_extras_are_announced_once(provider, caplog):
    with caplog.at_level(logging.INFO, logger="swing.data.stooq"):
        provider.earnings_dates(["AAA"])
        provider.fundamentals(["AAA"])
        provider.earnings_dates(["BBB"])

    notes = [r for r in caplog.records if "fail open" in r.getMessage()]
    assert len(notes) == 1
    assert notes[0].levelno == logging.INFO
