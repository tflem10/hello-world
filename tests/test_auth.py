"""Schwab auth and provider contract tests, against mocked responses.

Nothing here touches Schwab. The point is to pin the behaviour that only shows
up at inconvenient times: an expired refresh token on a Friday night, a
missing optional dependency, a provider outage mid-scan.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from swing.auth import (
    REFRESH_TOKEN_LIFETIME_DAYS,
    SchwabNotConfigured,
    get_client,
    token_status,
    warn_if_token_expiring,
)
from swing.config import Config, load_config
from swing.data.schwab_provider import SchwabProvider, market_is_open


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def schwab_config(tmp_path) -> Config:
    data = load_config().as_dict()
    data["schwab"].update(
        api_key="key", app_secret="secret",
        callback_url="https://127.0.0.1:8182",
        token_path=str(tmp_path / "schwab_token.json"),
        token_warn_days=6,
    )
    data["data"]["cache_dir"] = str(tmp_path / "cache")
    data["data"]["provider"] = "schwab"
    return Config(data)


def _write_token(cfg: Config, age_days: float):
    path = cfg.expand_path(cfg.schwab.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = (datetime.now() - timedelta(days=age_days)).timestamp()
    path.write_text(json.dumps({"creation_timestamp": created, "token": {"x": 1}}))
    return path


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Just enough of the schwab-py client surface for these tests."""

    class Instrument:
        class Projection:
            FUNDAMENTAL = "fundamental"

    def __init__(self, candles=None, quotes=None, accounts=None, fail: str | None = None):
        self._candles = candles or {}
        self._quotes = quotes or {}
        self._accounts = accounts or [{"accountNumber": "12345678", "hashValue": "HASH"}]
        self._fail = fail

    def get_price_history_every_day(self, symbol, **kw):
        if self._fail == "history":
            raise RuntimeError("history unavailable")
        candles = self._candles.get(symbol)
        if candles is None:
            return FakeResponse({"empty": True, "candles": []})
        return FakeResponse({"empty": False, "candles": candles})

    def get_quotes(self, symbols):
        if self._fail == "quotes":
            raise RuntimeError("quotes unavailable")
        return FakeResponse(
            {s: {"quote": self._quotes[s]} for s in symbols if s in self._quotes}
        )

    def get_quote(self, symbol):
        return FakeResponse({symbol: {"quote": self._quotes.get(symbol, {})}})

    def get_account_numbers(self):
        return FakeResponse(self._accounts)

    def get_instruments(self, symbols, projection):
        return FakeResponse(
            {"instruments": [{"assetType": "EQUITY",
                              "fundamental": {"eps": 5.0,
                                              "totalRevenueChangeInPercent": 12.0}}]}
        )


def _candles(n: int = 5, start_ms: int = 1_600_000_000_000):
    day = 86_400_000
    return [
        {
            "datetime": start_ms + i * day,
            "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i,
            "close": 100.5 + i, "volume": 1_000_000,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# token status
# ---------------------------------------------------------------------------
def test_missing_token_is_reported_clearly(schwab_config):
    status = token_status(schwab_config)
    assert not status.exists
    assert "swing auth" in status.describe()


def test_fresh_token_has_a_full_week(schwab_config):
    _write_token(schwab_config, age_days=0.0)
    status = token_status(schwab_config)
    assert status.exists and not status.expired and not status.warn
    assert status.days_left == pytest.approx(REFRESH_TOKEN_LIFETIME_DAYS, abs=0.01)


def test_token_warns_before_it_expires(schwab_config):
    _write_token(schwab_config, age_days=6.2)
    status = token_status(schwab_config)
    assert status.warn and not status.expired
    assert "re-authenticate soon" in status.describe()


def test_expired_token_is_flagged(schwab_config):
    _write_token(schwab_config, age_days=8.0)
    status = token_status(schwab_config)
    assert status.expired
    assert "EXPIRED" in status.describe()


def test_token_without_a_timestamp_falls_back_to_mtime_and_says_so(schwab_config):
    path = schwab_config.expand_path(schwab_config.schwab.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": {"x": 1}}))
    status = token_status(schwab_config)
    assert status.exists
    assert "modification time" in status.detail


def test_unparseable_token_file_does_not_crash(schwab_config):
    path = schwab_config.expand_path(schwab_config.schwab.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    status = token_status(schwab_config)
    assert status.exists and status.detail


def test_expiry_warning_text_for_the_nightly_job(schwab_config):
    _write_token(schwab_config, age_days=6.5)
    assert "expires in" in warn_if_token_expiring(schwab_config)
    _write_token(schwab_config, age_days=9.0)
    assert "expired" in warn_if_token_expiring(schwab_config)
    _write_token(schwab_config, age_days=1.0)
    assert warn_if_token_expiring(schwab_config) == ""


def test_no_warning_when_schwab_is_not_in_use():
    data = load_config().as_dict()
    data["data"]["provider"] = "yfinance"
    data["execution"]["enabled"] = False
    assert warn_if_token_expiring(Config(data)) == ""


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------
def test_missing_credentials_name_the_missing_keys(tmp_path):
    data = load_config().as_dict()
    data["schwab"]["token_path"] = str(tmp_path / "t.json")
    with pytest.raises(SchwabNotConfigured, match="api_key"):
        get_client(Config(data), interactive=True)


def test_non_interactive_never_opens_a_browser(schwab_config):
    """A scheduled job that waits for a login prompt hangs until someone notices."""
    with pytest.raises(SchwabNotConfigured, match="Run `swing auth`"):
        get_client(schwab_config, interactive=False)


def test_expired_token_refuses_non_interactively(schwab_config):
    _write_token(schwab_config, age_days=10.0)
    with pytest.raises(SchwabNotConfigured, match="expired"):
        get_client(schwab_config, interactive=False)


# ---------------------------------------------------------------------------
# provider: bars
# ---------------------------------------------------------------------------
def test_price_history_is_normalised(schwab_config):
    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(candles={"AAA": _candles(5)})

    from datetime import date

    bars = provider.daily_bars(["AAA"], date(2020, 1, 1), date(2030, 1, 1))
    frame = bars["AAA"]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.tz is None
    assert frame.index.is_monotonic_increasing
    assert len(frame) == 5


def test_empty_history_yields_no_symbol_not_an_exception(schwab_config):
    from datetime import date

    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(candles={})
    assert provider.daily_bars(["AAA"], date(2020, 1, 1), date(2030, 1, 1)) == {}


def test_history_failure_falls_back_to_yfinance(schwab_config, monkeypatch):
    from datetime import date

    from swing.data import yfinance_provider

    called = {}

    def fake_daily_bars(self, symbols, start, end):
        called["hit"] = tuple(symbols)
        return {"AAA": pd.DataFrame()}

    monkeypatch.setattr(yfinance_provider.YFinanceProvider, "daily_bars", fake_daily_bars)

    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(fail="history")
    provider.daily_bars(["AAA"], date(2020, 1, 1), date(2030, 1, 1))
    assert called["hit"] == ("AAA",)


# ---------------------------------------------------------------------------
# provider: quotes
# ---------------------------------------------------------------------------
def test_quotes_carry_bid_ask_and_are_not_marked_stale(schwab_config):
    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(
        quotes={"AAA": {"lastPrice": 100.0, "bidPrice": 99.98, "askPrice": 100.02}}
    )
    quote = provider.quotes(["AAA"])["AAA"]
    assert quote.price == 100.0
    assert quote.bid == 99.98 and quote.ask == 100.02
    assert quote.stale is False
    assert quote.spread_pct == pytest.approx(0.0004, abs=1e-5)


def test_quote_falls_back_through_mark_and_close(schwab_config):
    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(quotes={"AAA": {"closePrice": 42.0}})
    assert provider.quotes(["AAA"])["AAA"].price == 42.0


def test_symbol_with_no_usable_quote_is_absent_not_zero(schwab_config):
    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(quotes={"AAA": {}})
    assert provider.quotes(["AAA"]) == {}


def test_quote_failure_falls_back_to_yfinance(schwab_config, monkeypatch):
    from swing.data import yfinance_provider

    called = {}
    monkeypatch.setattr(
        yfinance_provider.YFinanceProvider, "quotes",
        lambda self, symbols: called.setdefault("hit", tuple(symbols)) or {},
    )
    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient(fail="quotes")
    provider.quotes(["AAA"])
    assert called["hit"] == ("AAA",)


# ---------------------------------------------------------------------------
# provider: fundamentals and earnings
# ---------------------------------------------------------------------------
def test_fundamental_percentages_are_converted_to_fractions(schwab_config):
    provider = SchwabProvider(schwab_config)
    provider._client = FakeClient()
    fundamentals = provider.fundamentals(["AAA"])["AAA"]
    assert fundamentals.trailing_eps == 5.0
    assert fundamentals.revenue_growth == pytest.approx(0.12)   # 12.0% -> 0.12


def test_earnings_always_come_from_yfinance(schwab_config, monkeypatch):
    from swing.data import yfinance_provider

    called = {}
    monkeypatch.setattr(
        yfinance_provider.YFinanceProvider, "earnings_dates",
        lambda self, symbols: called.setdefault("hit", tuple(symbols)) or {},
    )
    SchwabProvider(schwab_config).earnings_dates(["AAA"])
    assert called["hit"] == ("AAA",)


# ---------------------------------------------------------------------------
# provider selection
# ---------------------------------------------------------------------------
def test_provider_factory_honours_the_config():
    from swing.data.provider import get_provider

    data = load_config().as_dict()
    data["data"]["provider"] = "yfinance"
    assert get_provider(Config(data)).name == "yfinance"
    data["data"]["provider"] = "schwab"
    assert get_provider(Config(data)).name == "schwab"
    data["data"]["provider"] = "bloomberg"
    with pytest.raises(ValueError, match="unknown data.provider"):
        get_provider(Config(data))


def test_both_providers_satisfy_the_protocol(schwab_config):
    from swing.data.provider import DataProvider
    from swing.data.yfinance_provider import YFinanceProvider

    assert isinstance(YFinanceProvider(schwab_config), DataProvider)
    assert isinstance(SchwabProvider(schwab_config), DataProvider)


# ---------------------------------------------------------------------------
# market hours
# ---------------------------------------------------------------------------
def test_market_hours_check():
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    assert market_is_open(datetime(2024, 5, 1, 10, 0, tzinfo=eastern))     # Wednesday
    assert not market_is_open(datetime(2024, 5, 1, 3, 0, tzinfo=eastern))  # overnight
    assert not market_is_open(datetime(2024, 5, 1, 16, 30, tzinfo=eastern))
    assert not market_is_open(datetime(2024, 5, 4, 11, 0, tzinfo=eastern))  # Saturday
