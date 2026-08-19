"""Tests for the Schwab provider — every client call is a mock.

The Schwab path is the one a user reaches only after an approved developer app
and a live token, so the tests concentrate on the two things that decide
whether it is safe to switch to it: candles landing on the right *trading date*
after the epoch-millisecond conversion, and every failure degrading to a
plain-English message rather than a stack trace mid-scan.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest

import swing.broker
from conftest import make_bars
from swing.config import Config
from swing.data.provider import Fundamentals, Quote
from swing.data.schwab_provider import (
    SCHWAB_CACHE_SUBDIR,
    SchwabProvider,
    SchwabUnavailable,
    candles_to_bars,
    default_client_factory,
)

NOW = datetime(2026, 8, 18, 21, 0, tzinfo=UTC)
START = date(2020, 1, 2)
END = date(2020, 2, 12)
BARS = make_bars(30, start="2020-01-02")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def millis(day: str, *, at: str = "00:00", tz: str = "America/New_York") -> int:
    """Epoch milliseconds for a wall-clock moment in an exchange timezone."""
    return int(pd.Timestamp(f"{day} {at}", tz=tz).timestamp() * 1000)


def candle_payload(frame: pd.DataFrame = BARS, *, at: str = "00:00") -> dict[str, Any]:
    """A ``price_history`` payload shaped exactly like Schwab's."""
    return {
        "symbol": "AAPL",
        "empty": False,
        "candles": [
            {
                "datetime": millis(stamp.strftime("%Y-%m-%d"), at=at),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": int(row.volume),
            }
            for stamp, row in zip(frame.index, frame.itertuples(), strict=True)
        ],
    }


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeClient:
    """A stand-in ``schwab-py`` client that records what it was asked for."""

    def __init__(
        self,
        *,
        history: Any = None,
        quotes: Any = None,
        error: Exception | None = None,
    ) -> None:
        self.history = history
        self.quotes = quotes
        self.error = error
        self.history_calls: list[tuple[str, Any, Any]] = []
        self.quote_calls: list[list[str]] = []

    def get_price_history_every_day(
        self, symbol: str, *, start_datetime: Any = None, end_datetime: Any = None
    ) -> Any:
        self.history_calls.append((symbol, start_datetime, end_datetime))
        if self.error is not None:
            raise self.error
        payload = self.history(symbol) if callable(self.history) else self.history
        return FakeResponse(payload)

    def get_quotes(self, symbols: list[str]) -> Any:
        self.quote_calls.append(list(symbols))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.quotes)


class StubFallback:
    """Records the delegated calls so we can prove Schwab does not fake them."""

    def __init__(self) -> None:
        self.earnings_calls: list[Any] = []
        self.fundamentals_calls: list[Any] = []

    def earnings_dates(self, symbols: Any, **kwargs: Any) -> dict[str, date | None]:
        self.earnings_calls.append(list(symbols))
        return {"AAPL": date(2026, 9, 1)}

    def fundamentals(self, symbols: Any, **kwargs: Any) -> dict[str, Fundamentals]:
        self.fundamentals_calls.append(list(symbols))
        return {"AAPL": Fundamentals("AAPL", 0.2, 0.1)}


def build_provider(cfg: Config, client: FakeClient | None = None, **kwargs: Any) -> SchwabProvider:
    kwargs.setdefault("retry_backoff", 0.0)
    if client is not None:
        kwargs.setdefault("client_factory", lambda: client)
    return SchwabProvider(cfg, **kwargs)


# ---------------------------------------------------------------------------
# candles → bars
# ---------------------------------------------------------------------------


def test_candles_convert_to_contract_bars() -> None:
    bars = candles_to_bars(candle_payload())

    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
    assert bars.index.name is None
    assert bars.index.tz is None
    assert bars.index.is_monotonic_increasing
    assert all(dtype == "float64" for dtype in bars.dtypes)
    assert list(bars.index.date) == list(BARS.index.date)
    assert bars["close"].to_list() == pytest.approx(BARS["close"].to_list())


def test_candles_stamped_mid_session_still_land_on_their_trading_date() -> None:
    """09:30 New York is 14:30 UTC — a naive UTC conversion would be a day off."""
    bars = candles_to_bars(candle_payload(BARS.iloc[:3], at="09:30"))
    assert list(bars.index.date) == list(BARS.index[:3].date)


def test_candles_accept_the_alternative_timestamp_key() -> None:
    payload = {
        "candles": [
            {
                "datetime_ms": millis("2020-01-02"),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
            }
        ]
    }
    bars = candles_to_bars(payload)
    assert list(bars.index.date) == [date(2020, 1, 2)]
    assert bars["volume"].to_list() == [0.0]


def test_an_empty_candle_list_is_an_empty_frame_not_an_error() -> None:
    assert candles_to_bars({"symbol": "AAPL", "empty": True, "candles": []}).empty
    assert candles_to_bars({"symbol": "AAPL"}).empty


def test_candles_without_a_timestamp_are_refused_in_plain_english() -> None:
    with pytest.raises(ValueError, match="no timestamp field"):
        candles_to_bars({"candles": [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]})


def test_a_payload_that_is_not_a_record_is_refused() -> None:
    with pytest.raises(ValueError, match="Expected a Schwab price-history record"):
        candles_to_bars(["nope"])


# ---------------------------------------------------------------------------
# daily_bars
# ---------------------------------------------------------------------------


def test_daily_bars_reads_the_price_history_endpoint(test_cfg: Config) -> None:
    client = FakeClient(history=candle_payload())
    provider = build_provider(test_cfg, client)

    out = provider.daily_bars(["aapl"], START, END)

    assert list(out) == ["AAPL"]
    assert len(out["AAPL"]) == len(BARS)
    symbol, start_dt, end_dt = client.history_calls[0]
    assert symbol == "AAPL"
    assert start_dt.date() == START
    assert end_dt.date() == END


def test_daily_bars_uses_a_cache_of_its_own(test_cfg: Config) -> None:
    """Schwab candles are not dividend-adjusted, so they never share yfinance's files."""
    provider = build_provider(test_cfg, FakeClient(history=candle_payload()))
    assert provider.cache.root.name == SCHWAB_CACHE_SUBDIR

    provider.daily_bars(["AAPL"], START, END)
    assert provider.cache.path_for("AAPL").is_file()
    assert not (test_cfg.data.cache_dir / "daily" / "AAPL.parquet").exists()


def test_daily_bars_are_cached_so_a_rerun_is_free(test_cfg: Config) -> None:
    client = FakeClient(history=candle_payload())
    provider = build_provider(test_cfg, client)

    provider.daily_bars(["AAPL"], START, END)
    before = len(client.history_calls)
    provider.daily_bars(["AAPL"], START, END)

    assert len(client.history_calls) == before


def test_one_bad_symbol_does_not_lose_the_others(test_cfg: Config) -> None:
    def history(symbol: str) -> Any:
        return candle_payload() if symbol == "AAPL" else {"candles": [{"open": 1.0}]}

    provider = build_provider(test_cfg, FakeClient(history=history))
    assert list(provider.daily_bars(["AAPL", "BAD"], START, END)) == ["AAPL"]


def test_an_http_error_is_retried_then_skipped(test_cfg: Config) -> None:
    class ErrorClient(FakeClient):
        def get_price_history_every_day(self, symbol: str, **kwargs: Any) -> Any:
            self.history_calls.append((symbol, None, None))
            return FakeResponse({"error": "nope"}, status_code=503)

    client = ErrorClient()
    provider = build_provider(test_cfg, client, retries=2)

    assert provider.daily_bars(["AAPL"], START, END) == {}
    assert len(client.history_calls) == 2


def test_a_thrown_client_error_never_escapes(test_cfg: Config) -> None:
    provider = build_provider(test_cfg, FakeClient(error=RuntimeError("socket closed")), retries=1)
    assert provider.daily_bars(["AAPL"], START, END) == {}


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------


def test_latest_quotes_reads_the_nested_quote_record(test_cfg: Config) -> None:
    payload = {
        "AAPL": {"assetMainType": "EQUITY", "quote": {"lastPrice": 191.25, "closePrice": 188.0}},
        "MSFT": {"quote": {"lastPrice": 402.5}},
    }
    provider = build_provider(test_cfg, FakeClient(quotes=payload))

    quotes = provider.latest_quotes(["aapl", "msft"], now=NOW)

    assert quotes["AAPL"] == Quote(symbol="AAPL", price=191.25, asof=NOW)
    assert quotes["MSFT"].price == 402.5


def test_a_naive_injected_clock_still_produces_a_timezone_aware_quote(test_cfg: Config) -> None:
    payload = {"AAPL": {"quote": {"lastPrice": 191.25}}}
    naive = datetime(2026, 8, 18, 21, 0)

    provider = build_provider(test_cfg, FakeClient(quotes=payload))
    quote = provider.latest_quotes(["AAPL"], now=naive)["AAPL"]

    assert quote.asof.tzinfo is not None
    assert quote.asof == NOW


def test_latest_quotes_falls_back_through_the_price_fields(test_cfg: Config) -> None:
    payload = {"AAPL": {"quote": {"lastPrice": 0.0, "mark": 190.0}}}
    quotes = build_provider(test_cfg, FakeClient(quotes=payload)).latest_quotes(["AAPL"], now=NOW)
    assert quotes["AAPL"].price == 190.0


def test_latest_quotes_skips_symbols_schwab_cannot_price(test_cfg: Config) -> None:
    payload = {"AAPL": {"quote": {"lastPrice": 100.0}}, "DEAD": {"quote": {}}}
    quotes = build_provider(test_cfg, FakeClient(quotes=payload)).latest_quotes(
        ["AAPL", "DEAD", "MISSING"], now=NOW
    )
    assert sorted(quotes) == ["AAPL"]


def test_latest_quotes_batches_big_requests(test_cfg: Config) -> None:
    payload = {"AAPL": {"quote": {"lastPrice": 1.0}}, "MSFT": {"quote": {"lastPrice": 2.0}}}
    client = FakeClient(quotes=payload)
    provider = build_provider(test_cfg, client, quote_batch=1)

    quotes = provider.latest_quotes(["AAPL", "MSFT"], now=NOW)

    assert client.quote_calls == [["AAPL"], ["MSFT"]]
    assert sorted(quotes) == ["AAPL", "MSFT"]


def test_a_failing_quote_call_returns_nothing_rather_than_raising(test_cfg: Config) -> None:
    provider = build_provider(test_cfg, FakeClient(error=RuntimeError("timeout")), retries=1)
    assert provider.latest_quotes(["AAPL"], now=NOW) == {}


# ---------------------------------------------------------------------------
# delegation
# ---------------------------------------------------------------------------


def test_earnings_and_fundamentals_delegate_to_yahoo(test_cfg: Config) -> None:
    fallback = StubFallback()
    provider = build_provider(test_cfg, FakeClient(), fallback=fallback)

    assert provider.earnings_dates(["AAPL"]) == {"AAPL": date(2026, 9, 1)}
    assert provider.fundamentals(["AAPL"])["AAPL"].eps_growth == 0.2
    assert fallback.earnings_calls == [["AAPL"]]
    assert fallback.fundamentals_calls == [["AAPL"]]


def test_delegation_does_not_need_a_schwab_client_at_all(test_cfg: Config) -> None:
    """Earnings must still work when the token is dead — that is the point."""

    def broken() -> Any:
        raise RuntimeError("token expired")

    provider = SchwabProvider(test_cfg, client_factory=broken, fallback=StubFallback())
    assert provider.earnings_dates(["AAPL"]) == {"AAPL": date(2026, 9, 1)}


# ---------------------------------------------------------------------------
# the lazily-built client
# ---------------------------------------------------------------------------


def test_the_client_is_not_built_until_it_is_needed(test_cfg: Config) -> None:
    built: list[str] = []

    def factory() -> object:
        built.append("client")
        return FakeClient(history=candle_payload())

    provider = SchwabProvider(test_cfg, client_factory=factory)
    assert built == [], "constructing a provider must never need a token"

    provider.daily_bars(["AAPL"], START, END)
    assert built == ["client"]


def test_the_client_is_built_only_once(test_cfg: Config) -> None:
    built: list[str] = []

    def factory() -> object:
        built.append("client")
        return FakeClient(quotes={"AAPL": {"quote": {"lastPrice": 1.0}}})

    provider = SchwabProvider(test_cfg, client_factory=factory)
    provider.ensure_client()
    provider.latest_quotes(["AAPL"], now=NOW)
    assert built == ["client"]


def test_a_broken_factory_becomes_a_plain_english_error(test_cfg: Config) -> None:
    def broken() -> Any:
        raise RuntimeError("token expired")

    with pytest.raises(SchwabUnavailable, match="swing auth"):
        SchwabProvider(test_cfg, client_factory=broken).ensure_client()


def test_a_factory_returning_nothing_becomes_a_plain_english_error(test_cfg: Config) -> None:
    with pytest.raises(SchwabUnavailable, match="no client was returned"):
        SchwabProvider(test_cfg, client_factory=lambda: None).ensure_client()


def test_quote_batch_must_make_sense(test_cfg: Config) -> None:
    with pytest.raises(ValueError, match="quote_batch"):
        SchwabProvider(test_cfg, client_factory=lambda: None, quote_batch=0)


# ---------------------------------------------------------------------------
# default_client_factory — the seam onto WP-G's broker package
# ---------------------------------------------------------------------------


class FakeAuthModule:
    def __init__(self, **attrs: Any) -> None:
        for name, value in attrs.items():
            setattr(self, name, value)


def install_auth(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """Put ``module`` where ``from swing.broker import auth`` will find it."""
    monkeypatch.setitem(sys.modules, "swing.broker.auth", module)
    monkeypatch.setattr(swing.broker, "auth", module, raising=False)


def test_default_client_factory_uses_the_broker_login(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    install_auth(monkeypatch, FakeAuthModule(client_from_config=lambda cfg: sentinel))
    assert default_client_factory(test_cfg) is sentinel


def test_default_client_factory_explains_a_broker_without_a_client_builder(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_auth(monkeypatch, FakeAuthModule(login=lambda cfg: None))
    with pytest.raises(SchwabUnavailable, match="does not offer a way to build a Schwab client"):
        default_client_factory(test_cfg)


def test_default_client_factory_explains_a_failed_login(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cfg: Config) -> Any:
        raise RuntimeError("refresh token expired")

    install_auth(monkeypatch, FakeAuthModule(client_from_config=boom))
    with pytest.raises(SchwabUnavailable, match="Could not log in to Schwab"):
        default_client_factory(test_cfg)


def test_default_client_factory_explains_a_missing_token(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_auth(monkeypatch, FakeAuthModule(get_client=lambda cfg: None))
    with pytest.raises(SchwabUnavailable, match="saved token has expired"):
        default_client_factory(test_cfg)


def test_default_client_factory_explains_a_missing_broker_package(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "swing.broker.auth", None)
    monkeypatch.delattr(swing.broker, "auth", raising=False)
    with pytest.raises(SchwabUnavailable, match='provider = "yfinance"'):
        default_client_factory(test_cfg)
