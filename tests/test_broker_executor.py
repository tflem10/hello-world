"""Tests for ``swing execute``, ``swing positions`` and ``swing kill``.

The rule this file exists to enforce is that **the default does nothing**. A
dry run must produce a full report of what would happen without constructing a
broker client, opening a socket, or needing schwab-py to be installed at all —
so the first test asserts that the client factory is never even called.

Everything live is driven through a hand-written ``FakeClient`` rather than a
loose ``MagicMock``, because a mock that answers every attribute with another
mock will happily let a broken payload parser pass. The fake answers exactly
what schwab-py answers, in the shape schwab-py answers it.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import swing.backtest
import swing.data
from swing.broker import auth as auth_mod
from swing.broker import executor as ex
from swing.broker import guardrails as g
from swing.config import Config
from swing.state import Journal, PickRecord, kill_active, kill_path

#: A Tuesday, 10:30 in New York — a weekday inside the regular session.
NOW = dt.datetime(2026, 8, 18, 10, 30, tzinfo=ZoneInfo("America/New_York"))
TODAY = NOW.date()


# ---------------------------------------------------------------------------
# fixtures: a scan report on disk, a fake broker, a passing gate
# ---------------------------------------------------------------------------


def make_pick(
    symbol: str = "AAPL",
    *,
    entry: float = 100.0,
    atr: float = 2.0,
    shares: int = 3,
    status: str = "confirmed",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "date": TODAY.isoformat(),
        "kind": "pick",
        "entry": entry,
        "stop": round(entry - 2 * atr, 2),
        "shares": shares,
        "risk_amount": 12.0,
        "score": 1.5,
        "atr": atr,
        "earnings_date": None,
        "earnings_known": True,
        "thesis": "20-day breakout with volume confirmation",
        "status": status,
    }


def make_order(
    symbol: str = "AAPL",
    *,
    quantity: int = 3,
    price: float = 100.0,
    order_type: str = "LIMIT",
) -> dict[str, Any]:
    """One Schwab TRIGGER order: BUY LIMIT parent, SELL STOP child (Contract 10)."""
    return {
        "orderType": order_type,
        "session": "NORMAL",
        "price": price,
        "duration": "DAY",
        "orderStrategyType": "TRIGGER",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": quantity,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
        "childOrderStrategies": [
            {
                "orderType": "STOP",
                "session": "NORMAL",
                "stopPrice": round(price * 0.96, 2),
                "duration": "GOOD_TILL_CANCEL",
                "orderStrategyType": "SINGLE",
                "orderLegCollection": [
                    {
                        "instruction": "SELL",
                        "quantity": quantity,
                        "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                    }
                ],
            }
        ],
    }


def draft_variants(symbol: str = "AAPL", **kwargs: Any) -> dict[str, Any]:
    """The full ``draft_orders()`` dictionary WP-F writes per symbol."""
    return {
        "oto_stop": make_order(symbol, **kwargs),
        "oto_stop_limit": make_order(symbol, **kwargs),
        "trailing_stop": make_order(symbol, **kwargs),
    }


def write_scan(
    cfg: Config,
    *,
    day: dt.date = TODAY,
    picks: list[dict[str, Any]] | None = None,
    orders: dict[str, Any] | None = None,
) -> Path:
    """Write a Contract 9 scan directory to ``reports_dir``."""
    picks = [make_pick()] if picks is None else picks
    if orders is None:
        orders = {
            p["symbol"]: draft_variants(p["symbol"], price=p["entry"], quantity=p["shares"])
            for p in picks
        }
    scan_dir = Path(cfg.paths.reports_dir) / f"scan-{day.isoformat()}"
    (scan_dir / "orders").mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": f"{day.isoformat()}T17:30:00-04:00",
        "asof": day.isoformat(),
        "equity": cfg.account.equity,
        "regime_ok": True,
        "gate": {"passed": True, "reasons": []},
        "picks": picks,
        "watch": [],
    }
    (scan_dir / "picks.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for symbol, body in orders.items():
        (scan_dir / "orders" / f"{symbol}.json").write_text(json.dumps(body), encoding="utf-8")
    return scan_dir


class FakeResponse:
    """The parts of ``httpx.Response`` that the executor touches."""

    def __init__(
        self, payload: Any = None, *, headers: dict[str, str] | None = None, status_code: int = 200
    ) -> None:
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeClient:
    """A schwab-py client that answers exactly what schwab-py answers."""

    def __init__(
        self,
        *,
        equity: float = 10_000.0,
        positions: list[tuple[str, float]] | None = None,
        orders: list[dict[str, Any]] | None = None,
        place_error: Exception | None = None,
    ) -> None:
        self.placed: list[tuple[str, dict[str, Any]]] = []
        self.cancelled: list[Any] = []
        self._equity = equity
        self._positions = positions or []
        self._orders = orders or []
        self._place_error = place_error

    def get_account_numbers(self) -> FakeResponse:
        return FakeResponse([{"accountNumber": "123456789", "hashValue": "HASH1234"}])

    def get_account(self, account_hash: str, *, fields: Any = None) -> FakeResponse:
        return FakeResponse(
            {
                "securitiesAccount": {
                    "accountNumber": "123456789",
                    "currentBalances": {"liquidationValue": self._equity},
                    "positions": [
                        {
                            "instrument": {"symbol": symbol, "assetType": "EQUITY"},
                            "longQuantity": quantity,
                            "marketValue": quantity * 100.0,
                        }
                        for symbol, quantity in self._positions
                    ],
                }
            }
        )

    def place_order(self, account_hash: str, order_spec: dict[str, Any]) -> FakeResponse:
        if self._place_error is not None:
            raise self._place_error
        self.placed.append((account_hash, order_spec))
        location = (
            f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/"
            f"{1000 + len(self.placed)}"
        )
        return FakeResponse({}, headers={"Location": location})

    def get_orders_for_account(self, account_hash: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(self._orders)

    def cancel_order(self, order_id: Any, account_hash: str) -> FakeResponse:
        self.cancelled.append(order_id)
        return FakeResponse({})

    def get_quotes(self, symbols: Any) -> FakeResponse:
        return FakeResponse({s: {"quote": {"lastPrice": 100.0}} for s in symbols})


class FakeQuote:
    def __init__(self, symbol: str, price: float) -> None:
        self.symbol = symbol
        self.price = price
        self.asof = NOW


class FakeProvider:
    def __init__(self, prices: dict[str, float]) -> None:
        self.prices = prices

    def latest_quotes(self, symbols: Any) -> dict[str, FakeQuote]:
        return {s: FakeQuote(s, self.prices[s]) for s in symbols if s in self.prices}


@pytest.fixture
def gate_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a ``swing.backtest.gate`` whose check() passes."""

    class _Result:
        passed = True
        reasons: list[str] = []
        report_path = None

    module = types.ModuleType("swing.backtest.gate")
    module.check = lambda cfg: _Result()  # type: ignore[attr-defined]
    monkeypatch.setattr(swing.backtest, "gate", module, raising=False)
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", module)


@pytest.fixture
def no_client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Make any attempt to build a broker client an outright test failure."""
    factory = Mock(side_effect=AssertionError("a dry run must not build a broker client"))
    monkeypatch.setattr(auth_mod, "get_client", factory)
    return factory


def write_token(cfg: Config, *, age_days: float = 1.0) -> Path:
    path = Path(cfg.schwab.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = int(NOW.timestamp() - age_days * 86_400.0)
    path.write_text(
        json.dumps({"creation_timestamp": created, "token": {"access_token": "PLACEHOLDER"}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def live_cfg(cfg_factory: Any) -> Config:
    """A config where live execution is switched on and sized for real trades."""
    return cfg_factory(
        account={"equity": 10_000.0},
        execution={"enabled": True, "max_orders_per_day": 3},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )


def arrange_live(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    client: FakeClient,
    *,
    prices: dict[str, float] | None = None,
    token_age: float = 1.0,
) -> None:
    """Put every live precondition in place: token, client, quotes."""
    write_token(cfg, age_days=token_age)
    monkeypatch.setattr(auth_mod, "get_client", lambda c: client)
    monkeypatch.setattr(
        swing.data,
        "get_provider",
        lambda c, **kw: FakeProvider(prices if prices is not None else {"AAPL": 100.2}),
    )


# ---------------------------------------------------------------------------
# reading the scan report
# ---------------------------------------------------------------------------


def test_latest_scan_dir_returns_none_when_there_are_no_reports(test_cfg: Config) -> None:
    assert ex.latest_scan_dir(test_cfg) is None


def test_latest_scan_dir_picks_the_newest_dated_directory(test_cfg: Config) -> None:
    write_scan(test_cfg, day=dt.date(2026, 8, 10))
    newest = write_scan(test_cfg, day=dt.date(2026, 8, 17))
    (Path(test_cfg.paths.reports_dir) / "backtest").mkdir(exist_ok=True)
    (Path(test_cfg.paths.reports_dir) / "scan-not-a-date").mkdir(exist_ok=True)
    assert ex.latest_scan_dir(test_cfg) == newest


def test_load_scan_refuses_a_directory_with_no_picks_json(test_cfg: Config) -> None:
    empty = Path(test_cfg.paths.reports_dir) / "scan-2026-08-18"
    empty.mkdir(parents=True)
    with pytest.raises(ex.BrokerError) as excinfo:
        ex.load_scan(empty)
    assert "swing scan" in str(excinfo.value)


def test_load_scan_skips_picks_that_were_already_acted_on(test_cfg: Config) -> None:
    scan = write_scan(
        test_cfg,
        picks=[make_pick("AAPL"), make_pick("MSFT", status="invalidated")],
        orders={"AAPL": draft_variants("AAPL"), "MSFT": draft_variants("MSFT")},
    )
    bundle = ex.load_scan(scan)
    assert [p["symbol"] for p in bundle.picks] == ["AAPL"]
    assert any("invalidated" in p for p in bundle.problems)


def test_plan_orders_reads_quantity_limit_and_stop_from_the_draft(test_cfg: Config) -> None:
    scan = write_scan(test_cfg, picks=[make_pick("AAPL", entry=100.0, shares=3)])
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert problems == []
    (plan,) = plans
    assert (plan.symbol, plan.quantity, plan.limit_price) == ("AAPL", 3, 100.0)
    assert plan.stop_price == 96.0
    assert plan.notional == 300.0
    assert plan.variant == "oto_stop"


def test_plan_orders_accepts_a_single_order_document(test_cfg: Config) -> None:
    """``orders/<SYMBOL>.json`` may hold one order rather than the variant dict."""
    scan = write_scan(test_cfg, orders={"AAPL": make_order("AAPL")})
    plans, _ = ex.plan_orders(ex.load_scan(scan))
    assert plans[0].variant == "order"


def test_plan_orders_reports_a_pick_with_no_drafted_order(test_cfg: Config) -> None:
    scan = write_scan(test_cfg, picks=[make_pick("AAPL")], orders={})
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert plans == []
    assert any("does not exist" in p for p in problems)


def test_plan_orders_skips_an_unaffordable_zero_share_pick(test_cfg: Config) -> None:
    scan = write_scan(
        test_cfg,
        picks=[make_pick("AAPL", shares=0)],
        orders={"AAPL": draft_variants("AAPL", quantity=0)},
    )
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert plans == []
    assert any("nothing to buy" in p for p in problems)


# ---------------------------------------------------------------------------
# dry run — the default, and it must do nothing
# ---------------------------------------------------------------------------


def test_dry_run_places_nothing_and_never_builds_a_client(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scan(test_cfg)
    ex.run_execute(test_cfg, now=NOW)
    assert no_client.called is False
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "nothing was sent" in out
    assert "AAPL" in out


def test_dry_run_prints_the_order_table_with_totals(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scan(test_cfg, picks=[make_pick("AAPL"), make_pick("MSFT", entry=50.0, shares=4)])
    ex.run_execute(test_cfg, now=NOW)
    out = capsys.readouterr().out
    assert "SYMBOL" in out and "NOTIONAL" in out
    assert "500.00" in out  # 3*100 + 4*50


def test_dry_run_prints_every_guardrail_verdict(
    test_cfg: Config, no_client: Mock, gate_passes: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scan(test_cfg)
    ex.run_execute(test_cfg, now=NOW)
    out = capsys.readouterr().out
    for name in (
        "kill_switch",
        "token_age",
        "trading_hours",
        "gate_passed",
        "stale_scan",
        "orders_today",
        "reconciliation",
        "equity_mismatch",
        "limit_only",
        "quote_drift",
        "duplicate",
        "new_exposure",
    ):
        assert name in out, name


def test_dry_run_skips_the_checks_that_need_a_live_connection(
    test_cfg: Config,
    no_client: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(ex, "fetch_quotes", lambda cfg, symbols, **kw: {})
    write_scan(test_cfg)
    ex.run_execute(test_cfg, now=NOW)
    out = capsys.readouterr().out
    assert "SKIP  reconciliation" in out
    assert "SKIP  equity_mismatch" in out
    assert "SKIP  quote_drift" in out


def test_dry_run_exits_zero_even_when_guardrails_refuse(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run reports the refusals; it does not become a failure itself."""
    from swing.state import engage_kill

    engage_kill(test_cfg)
    write_scan(test_cfg)
    ex.run_execute(test_cfg, now=NOW)  # no SystemExit
    out = capsys.readouterr().out
    assert "STOP  kill_switch" in out


def test_dry_run_with_no_scan_report_says_run_scan_first(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    ex.run_execute(test_cfg, now=NOW)
    assert "run `swing scan` first" in capsys.readouterr().out


def test_dry_run_with_an_unreadable_scan_report_does_not_raise(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run explains a broken report and exits 0; it never raises."""
    scan = Path(test_cfg.paths.reports_dir) / f"scan-{TODAY.isoformat()}"
    scan.mkdir(parents=True)
    (scan / "picks.json").write_text("{not valid json", encoding="utf-8")

    ex.run_execute(test_cfg, now=NOW)  # no SystemExit

    out = capsys.readouterr().out
    assert "could not be read as JSON" in out
    assert "swing scan" in out


def test_live_with_an_unreadable_scan_report_refuses(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same broken report is a hard failure once real money is involved."""
    arrange_live(monkeypatch, live_cfg, FakeClient())
    scan = Path(live_cfg.paths.reports_dir) / f"scan-{TODAY.isoformat()}"
    scan.mkdir(parents=True)
    (scan / "picks.json").write_text("{not valid json", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert excinfo.value.code == 1


def test_dry_run_does_not_need_the_data_provider_to_work(
    test_cfg: Config, no_client: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(cfg: Config, **kwargs: Any) -> Any:
        raise RuntimeError("yfinance is down")

    monkeypatch.setattr(swing.data, "get_provider", explode)
    write_scan(test_cfg)
    ex.run_execute(test_cfg, now=NOW)  # must not raise


# ---------------------------------------------------------------------------
# live — the two switches
# ---------------------------------------------------------------------------


def test_live_without_execution_enabled_refuses_naming_both_switches(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scan(test_cfg)
    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(test_cfg, live=True, now=NOW)
    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "--live" in out
    assert "execution.enabled" in out


def test_live_refuses_when_no_client_can_be_built(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_scan(live_cfg)

    def boom(cfg: Config) -> Any:
        raise auth_mod.AuthError("There is no Schwab token: run `swing auth` to log in.")

    monkeypatch.setattr(auth_mod, "get_client", boom)
    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, now=NOW)
    assert excinfo.value.code == 2
    assert "Nothing was sent" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# live — confirmation and placement
# ---------------------------------------------------------------------------


def test_live_prompts_for_each_order_and_sends_nothing_when_you_say_no(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)
    ex.run_execute(live_cfg, live=True, now=NOW)

    assert len(asked) == 1
    assert "AAPL" in asked[0]
    assert client.placed == []
    assert "Skipped AAPL" in capsys.readouterr().out


def test_live_places_the_order_when_you_confirm(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    ex.run_execute(live_cfg, live=True, now=NOW)

    assert len(client.placed) == 1
    account_hash, order = client.placed[0]
    assert account_hash == "HASH1234"
    assert order["orderType"] == "LIMIT"
    assert order["orderLegCollection"][0]["instrument"]["symbol"] == "AAPL"


def test_live_records_every_placement_in_the_journal(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    ex.run_execute(live_cfg, live=True, now=NOW)

    orders = Journal.load(live_cfg).orders
    assert len(orders) == 1
    recorded = orders[0]
    assert recorded["symbol"] == "AAPL"
    assert recorded["date"] == TODAY.isoformat()
    assert recorded["status"] == "open"
    assert recorded["order_id"] == "1001"
    assert recorded["quantity"] == 3
    assert recorded["notional"] == 300.0
    assert recorded["order"]["orderType"] == "LIMIT"


def test_live_marks_the_pick_as_ordered_in_the_journal(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Journal.load(live_cfg)
    journal.add_picks(
        [
            PickRecord(
                symbol="AAPL",
                date=TODAY.isoformat(),
                kind="pick",
                entry=100.0,
                stop=96.0,
                shares=3,
                risk_amount=12.0,
                score=1.5,
                atr=2.0,
                earnings_date=None,
                earnings_known=True,
                thesis="breakout",
                status="confirmed",
            )
        ]
    )
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    ex.run_execute(live_cfg, live=True, now=NOW)

    statuses = {p.symbol: p.status for p in Journal.load(live_cfg).picks}
    assert statuses["AAPL"] == "ordered"


def test_autopilot_skips_the_prompt(
    cfg_factory: Any, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = cfg_factory(
        account={"equity": 10_000.0},
        execution={"enabled": True, "autopilot": True, "max_orders_per_day": 3},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    client = FakeClient()
    arrange_live(monkeypatch, cfg, client)
    write_scan(cfg)

    def never(prompt: str = "") -> str:
        raise AssertionError("autopilot must not prompt")

    monkeypatch.setattr("builtins.input", never)
    ex.run_execute(cfg, live=True, now=NOW)
    assert len(client.placed) == 1


def test_assume_yes_skips_the_prompt(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    def never(prompt: str = "") -> str:
        raise AssertionError("--yes must not prompt")

    monkeypatch.setattr("builtins.input", never)
    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert len(client.placed) == 1


def test_a_closed_stdin_counts_as_no(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    def eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    ex.run_execute(live_cfg, live=True, now=NOW)
    assert client.placed == []


def test_live_stops_at_max_orders_per_day(
    cfg_factory: Any,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = cfg_factory(
        account={"equity": 100_000.0},
        execution={"enabled": True, "max_orders_per_day": 2, "max_new_exposure_pct": 100.0},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    picks = [make_pick("AAPL"), make_pick("MSFT"), make_pick("NVDA")]
    client = FakeClient(equity=100_000.0)
    arrange_live(monkeypatch, cfg, client, prices={"AAPL": 100.2, "MSFT": 100.2, "NVDA": 100.2})
    write_scan(cfg, picks=picks)

    ex.run_execute(cfg, live=True, assume_yes=True, now=NOW)

    assert [
        order["orderLegCollection"][0]["instrument"]["symbol"] for _, order in client.placed
    ] == [
        "AAPL",
        "MSFT",
    ]
    assert "max_orders_per_day" in capsys.readouterr().out


def test_live_refuses_when_the_day_budget_is_already_spent(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = Journal.load(live_cfg)
    for symbol in ("A", "B", "C"):
        journal.record_order(
            {"symbol": symbol, "date": TODAY.isoformat(), "status": "open", "notional": 10.0}
        )
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert excinfo.value.code == 1
    assert client.placed == []
    # Name the guardrail, so this cannot pass because something else refused.
    assert "orders_today" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# live — every guardrail blocks the whole run
# ---------------------------------------------------------------------------


def test_live_refuses_a_market_order(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg, orders={"AAPL": make_order("AAPL", order_type="MARKET")})

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    out = capsys.readouterr().out
    assert "limit_only" in out
    assert "only ever sends limit orders" in out


def test_live_refuses_when_the_broker_and_journal_disagree(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient(positions=[("TSLA", 10.0)])
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    out = capsys.readouterr().out
    assert "reconciliation" in out
    assert "TSLA" in out


def test_live_refuses_on_an_equity_mismatch(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient(equity=2_000.0)  # config says 10,000
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    assert "equity_mismatch" in capsys.readouterr().out


def test_live_refuses_when_new_exposure_exceeds_the_daily_budget(
    cfg_factory: Any,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A $300 order against a 5%-of-$1,000 daily budget must not go out."""
    cfg = cfg_factory(
        account={"equity": 1_000.0},
        execution={"enabled": True, "max_orders_per_day": 3, "max_new_exposure_pct": 5.0},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    client = FakeClient(equity=1_000.0)
    arrange_live(monkeypatch, cfg, client)
    write_scan(cfg)  # AAPL, 3 shares at 100.00 = $300 notional

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    out = capsys.readouterr().out
    assert "new_exposure" in out
    assert "max_new_exposure_pct" in out


def test_one_blocked_pick_stops_every_other_pick(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All-or-nothing: a refusal on one symbol holds back the clean ones too.

    Executing a subset of a scan silently changes the portfolio the sizing was
    computed for, so a picture that is wrong anywhere stops the whole run.
    """
    journal = Journal.load(live_cfg)
    journal.add_picks(
        [
            PickRecord(
                symbol="AAPL",
                date=(TODAY - dt.timedelta(days=20)).isoformat(),
                kind="pick",
                entry=90.0,
                stop=86.0,
                shares=3,
                risk_amount=12.0,
                score=1.0,
                atr=2.0,
                earnings_date=None,
                earnings_known=True,
                thesis="older entry, still held",
                status="filled",
            )
        ]
    )
    # The broker agrees AAPL is held, so reconciliation passes and `duplicate`
    # is the only thing refusing — on AAPL alone. MSFT is entirely clean.
    client = FakeClient(positions=[("AAPL", 3.0)])
    arrange_live(monkeypatch, live_cfg, client, prices={"AAPL": 100.2, "MSFT": 100.2})
    write_scan(live_cfg, picks=[make_pick("AAPL"), make_pick("MSFT")])

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []  # neither the blocked one nor the clean one
    out = capsys.readouterr().out
    assert "AAPL/duplicate" in out
    assert "MSFT checks" in out  # the clean pick was still evaluated and shown


def test_live_refuses_a_dead_token(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client, token_age=8.0)
    write_scan(live_cfg)

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    assert "token_age" in capsys.readouterr().out


def test_live_refuses_outside_trading_hours(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg, day=dt.date(2026, 8, 15))
    saturday = dt.datetime(2026, 8, 15, 11, 0, tzinfo=ZoneInfo("America/New_York"))

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=saturday)
    assert client.placed == []


def test_live_refuses_a_stale_scan(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg, day=TODAY - dt.timedelta(days=9))

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.placed == []
    assert "stale_scan" in capsys.readouterr().out


def test_live_refuses_when_the_kill_switch_is_on(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from swing.state import engage_kill

    engage_kill(live_cfg)
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.placed == []


def test_live_refuses_without_a_passing_gate(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No gate module at all must fail closed, not open."""
    monkeypatch.delattr(swing.backtest, "gate", raising=False)
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", None)
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.placed == []
    assert "gate_passed" in capsys.readouterr().out


def test_live_refuses_a_quote_that_drifted_too_far(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client, prices={"AAPL": 115.0})
    write_scan(live_cfg)

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.placed == []
    assert "quote_drift" in capsys.readouterr().out


def test_live_refuses_a_duplicate_of_an_open_position(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = Journal.load(live_cfg)
    journal.add_picks(
        [
            PickRecord(
                symbol="AAPL",
                date=(TODAY - dt.timedelta(days=20)).isoformat(),
                kind="pick",
                entry=90.0,
                stop=86.0,
                shares=3,
                risk_amount=12.0,
                score=1.0,
                atr=2.0,
                earnings_date=None,
                earnings_known=True,
                thesis="older entry",
                status="filled",
            )
        ]
    )
    client = FakeClient(positions=[("AAPL", 3.0)])
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.placed == []
    assert "duplicate" in capsys.readouterr().out


def test_live_stops_the_whole_run_when_schwab_rejects_an_order(
    cfg_factory: Any,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = cfg_factory(
        account={"equity": 100_000.0},
        execution={"enabled": True, "max_orders_per_day": 3, "max_new_exposure_pct": 100.0},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    client = FakeClient(equity=100_000.0, place_error=RuntimeError("400 invalid order"))
    arrange_live(monkeypatch, cfg, client, prices={"AAPL": 100.2, "MSFT": 100.2})
    write_scan(cfg, picks=[make_pick("AAPL"), make_pick("MSFT")])

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "400 invalid order" in out
    assert "held back" in out


# ---------------------------------------------------------------------------
# quotes
# ---------------------------------------------------------------------------


def test_fetch_quotes_prefers_the_configured_provider(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(swing.data, "get_provider", lambda cfg, **kw: FakeProvider({"AAPL": 99.5}))
    assert ex.fetch_quotes(test_cfg, ["AAPL"]) == {"AAPL": 99.5}


def test_fetch_quotes_falls_back_to_schwab_when_the_provider_fails(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(cfg: Config, **kwargs: Any) -> Any:
        raise RuntimeError("cache is cold and there is no network")

    monkeypatch.setattr(swing.data, "get_provider", explode)
    assert ex.fetch_quotes(test_cfg, ["AAPL"], client=FakeClient()) == {"AAPL": 100.0}


def test_fetch_quotes_never_raises(test_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(cfg: Config, **kwargs: Any) -> Any:
        raise RuntimeError("nope")

    monkeypatch.setattr(swing.data, "get_provider", explode)
    broken = Mock()
    broken.get_quotes.side_effect = RuntimeError("also nope")
    assert ex.fetch_quotes(test_cfg, ["AAPL"], client=broken) == {}


def test_fetch_quotes_short_circuits_on_an_empty_symbol_list(test_cfg: Config) -> None:
    assert ex.fetch_quotes(test_cfg, []) == {}


# ---------------------------------------------------------------------------
# payload parsing
# ---------------------------------------------------------------------------


def test_account_snapshot_reads_equity_and_positions(live_cfg: Config) -> None:
    snapshot = ex.fetch_account(live_cfg, FakeClient(equity=25_000.0, positions=[("AAPL", 3.0)]))
    assert snapshot.hash_value == "HASH1234"
    assert snapshot.masked_number == "****6789"
    assert snapshot.equity == 25_000.0
    assert snapshot.symbols == ["AAPL"]


def test_account_snapshot_turns_an_http_error_into_a_broker_error(live_cfg: Config) -> None:
    client = Mock()
    client.get_account_numbers.return_value = FakeResponse(
        [{"accountNumber": "123456789", "hashValue": "HASH1234"}]
    )
    client.get_account.return_value = FakeResponse({}, status_code=401)
    with pytest.raises(ex.BrokerError) as excinfo:
        ex.fetch_account(live_cfg, client)
    assert "401" in str(excinfo.value)


def test_order_id_comes_from_the_location_header() -> None:
    response = FakeResponse({}, headers={"Location": "https://api.schwabapi.com/x/orders/98765"})
    assert ex._order_id_of(response) == "98765"


def test_order_id_falls_back_to_the_body() -> None:
    assert ex._order_id_of(FakeResponse({"orderId": 4242})) == "4242"


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------


def test_print_positions_works_with_no_broker_connection(
    test_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            PickRecord(
                symbol="AAPL",
                date=TODAY.isoformat(),
                kind="pick",
                entry=100.0,
                stop=96.0,
                shares=3,
                risk_amount=12.0,
                score=1.5,
                atr=2.0,
                earnings_date=None,
                earnings_known=True,
                thesis="breakout",
                status="filled",
            )
        ]
    )
    ex.print_positions(test_cfg)
    out = capsys.readouterr().out
    assert "Journal positions" in out
    assert "AAPL" in out
    assert "not available" in out


def test_print_positions_diffs_the_two_views(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: FakeClient(positions=[("TSLA", 5.0)]))
    ex.print_positions(live_cfg)
    out = capsys.readouterr().out
    assert "Broker positions" in out
    assert "TSLA" in out
    assert "Reconciliation: STOP" in out


def test_print_positions_lists_working_orders(
    test_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order(
        {
            "symbol": "AAPL",
            "date": TODAY.isoformat(),
            "status": "open",
            "quantity": 3,
            "limit": 100.0,
            "order_id": "1001",
        }
    )
    ex.print_positions(test_cfg)
    out = capsys.readouterr().out
    assert "Working orders" in out
    assert "1001" in out


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------


def test_kill_engages_the_file_and_is_idempotent(
    test_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """`swing kill` in the CLI engages the file first, then calls this."""
    from swing.state import engage_kill

    engage_kill(test_cfg, reason="engaged via `swing kill`")
    ex.kill(test_cfg)
    ex.kill(test_cfg)
    assert kill_active(test_cfg) is True
    assert kill_path(test_cfg).read_text(encoding="utf-8").strip() == "cli"
    # The CLI engaged it first, so this must confirm rather than re-announce.
    out = capsys.readouterr().out
    assert "confirmed engaged" in out
    assert "Kill switch engaged" not in out


def test_kill_works_with_no_broker_at_all(
    test_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    ex.kill(test_cfg)
    assert kill_active(test_cfg) is True
    out = capsys.readouterr().out
    assert "No broker connection" in out
    assert "Schwab app" in out


def test_kill_cancels_only_the_working_orders(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient(
        orders=[
            {"orderId": 111, "status": "WORKING"},
            {"orderId": 222, "status": "FILLED"},
            {"orderId": 333, "status": "PENDING_ACTIVATION"},
        ]
    )
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    assert client.cancelled == [111, 333]
    assert kill_active(live_cfg) is True
    assert "Cancelled 2 working order(s)" in capsys.readouterr().out


def test_kill_reports_when_there_was_nothing_to_cancel(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: FakeClient(orders=[]))
    ex.kill(live_cfg)
    assert "no working orders" in capsys.readouterr().out


def test_kill_still_engages_when_cancelling_explodes(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = Mock()
    client.get_account_numbers.side_effect = RuntimeError("connection reset")
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    assert kill_active(live_cfg) is True
    out = capsys.readouterr().out
    assert "connection reset" in out
    assert "kill switch is set" in out


def broker_order(order_id: int, symbol: str, *, status: str = "WORKING") -> dict[str, Any]:
    """A working order as Schwab reports it, with the leg that names the symbol."""
    return {
        "orderId": order_id,
        "status": status,
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 3,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
    }


def test_kill_marks_cancelled_orders_as_cancelled_in_the_journal(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cancelling at the broker must free the symbol locally too.

    Otherwise the journal keeps claiming the order is working and the
    ``duplicate`` guardrail refuses that symbol forever.
    """
    journal = Journal.load(live_cfg)
    journal.record_order(
        {
            "symbol": "AAPL",
            "date": TODAY.isoformat(),
            "status": "open",
            "order_id": "111",
            "quantity": 3,
            "notional": 300.0,
        }
    )
    assert g.duplicate(journal, "AAPL", asof=TODAY).ok is False  # blocked before

    client = FakeClient(orders=[broker_order(111, "AAPL")])
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    assert client.cancelled == [111]
    reloaded = Journal.load(live_cfg)
    assert [o["status"] for o in reloaded.orders] == ["cancelled"]
    assert reloaded.open_orders() == []
    assert g.duplicate(reloaded, "AAPL", asof=TODAY).ok is True  # orderable again
    assert "Marked 1 journal order(s) as cancelled" in capsys.readouterr().out


def test_kill_leaves_a_failed_cancellation_open_in_the_journal(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: if we are unsure the order is gone, keep refusing the symbol."""
    journal = Journal.load(live_cfg)
    journal.record_order(
        {"symbol": "AAPL", "date": TODAY.isoformat(), "status": "open", "order_id": "111"}
    )
    client = FakeClient(orders=[broker_order(111, "AAPL")])

    def refuse(order_id: Any, account_hash: str) -> None:
        raise RuntimeError("order is being filled right now")

    client.cancel_order = refuse  # type: ignore[assignment]
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    reloaded = Journal.load(live_cfg)
    assert [o["status"] for o in reloaded.orders] == ["open"]
    assert g.duplicate(reloaded, "AAPL", asof=TODAY).ok is False


def test_kill_only_frees_the_symbols_it_actually_cancelled(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failure must not free the other symbols, nor block them."""
    journal = Journal.load(live_cfg)
    for symbol, order_id in (("AAPL", "111"), ("MSFT", "222")):
        journal.record_order(
            {"symbol": symbol, "date": TODAY.isoformat(), "status": "open", "order_id": order_id}
        )
    client = FakeClient(orders=[broker_order(111, "AAPL"), broker_order(222, "MSFT")])
    real_cancel = client.cancel_order

    def selective(order_id: Any, account_hash: str) -> Any:
        if order_id == 111:
            raise RuntimeError("too late, it filled")
        return real_cancel(order_id, account_hash)

    client.cancel_order = selective  # type: ignore[assignment]
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    statuses = {o["symbol"]: o["status"] for o in Journal.load(live_cfg).orders}
    assert statuses == {"AAPL": "open", "MSFT": "cancelled"}


def test_kill_without_symbols_on_the_broker_orders_changes_no_journal_rows(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An order we cannot attribute to a symbol must not flip anything."""
    journal = Journal.load(live_cfg)
    journal.record_order(
        {"symbol": "AAPL", "date": TODAY.isoformat(), "status": "open", "order_id": "111"}
    )
    client = FakeClient(orders=[{"orderId": 111, "status": "WORKING"}])  # no legs
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    assert client.cancelled == [111]
    assert [o["status"] for o in Journal.load(live_cfg).orders] == ["open"]


def test_kill_reports_orders_it_could_not_cancel(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeClient(orders=[{"orderId": 111, "status": "WORKING"}])

    def refuse(order_id: Any, account_hash: str) -> None:
        raise RuntimeError("order already filled")

    client.cancel_order = refuse  # type: ignore[assignment]
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.kill(live_cfg)

    assert kill_active(live_cfg) is True
    assert "could not be cancelled" in capsys.readouterr().out


def test_kill_off_releases_the_switch(test_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    ex.kill(test_cfg)
    assert kill_active(test_cfg) is True
    ex.kill(test_cfg, off=True)
    assert kill_active(test_cfg) is False
    assert "Kill switch is off" in capsys.readouterr().out


def test_kill_off_is_idempotent_too(test_cfg: Config) -> None:
    ex.kill(test_cfg, off=True)
    ex.kill(test_cfg, off=True)
    assert kill_active(test_cfg) is False
