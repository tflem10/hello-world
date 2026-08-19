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
    stop: float | None = None,
) -> dict[str, Any]:
    """One Schwab TRIGGER order: BUY LIMIT parent, SELL STOP child (Contract 10).

    ``stop`` defaults to 4% under the limit, but callers building a draft for a
    specific pick pass that pick's own stop — which is what ``draft_orders``
    does in production, and what the payload/plan check compares (BUG-009).
    """
    stop_price = round(price * 0.96, 2) if stop is None else round(float(stop), 2)
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
                "stopPrice": stop_price,
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
            p["symbol"]: draft_variants(
                p["symbol"], price=p["entry"], quantity=p["shares"], stop=p["stop"]
            )
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
    """A dry run explains a broken report and exits 0; it never raises.

    Under the amended contract A4, ``swing.reports.latest_scan_dir`` always
    requires a ``picks.json`` that parses — a directory that crashed before its
    commit point is not a report for anybody — so a corrupt report is invisible
    rather than selected-then-refused. Either way the dry run says what to do
    and exits 0; only the sentence changed.
    """
    scan = Path(test_cfg.paths.reports_dir) / f"scan-{TODAY.isoformat()}"
    scan.mkdir(parents=True)
    (scan / "picks.json").write_text("{not valid json", encoding="utf-8")

    ex.run_execute(test_cfg, now=NOW)  # no SystemExit

    out = capsys.readouterr().out
    assert "No scan report was found" in out
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


# ---------------------------------------------------------------------------
# audit remediation — WP-R6
#
# Every test below pins one finding from CODE_AUDIT_REPORT.md and fails on the
# behaviour that shipped before it. The audit ID is named in each docstring.
# ---------------------------------------------------------------------------


def journal_pick(
    symbol: str = "AAPL",
    *,
    day: dt.date = TODAY,
    status: str = "confirmed",
    entry: float = 100.0,
) -> PickRecord:
    return PickRecord(
        symbol=symbol,
        date=day.isoformat(),
        kind="pick",
        entry=entry,
        stop=round(entry - 4.0, 2),
        shares=3,
        risk_amount=12.0,
        score=1.5,
        atr=2.0,
        earnings_date=None,
        earnings_known=True,
        thesis="breakout",
        status=status,
    )


def filled_order(order_id: int, symbol: str) -> dict[str, Any]:
    return broker_order(order_id, symbol, status="FILLED")


# --- BUG-026: the report's two dates must agree -----------------------------


def test_a_report_whose_directory_and_body_disagree_is_refused(test_cfg: Config) -> None:
    """Audit BUG-026: the editable `asof` used to override the directory name.

    One edited line made a three-week-old report claim to be tonight's and walk
    straight past ``stale_scan``.
    """
    scan = write_scan(test_cfg, day=TODAY)
    payload = json.loads((scan / "picks.json").read_text(encoding="utf-8"))
    payload["asof"] = (TODAY - dt.timedelta(days=21)).isoformat()
    (scan / "picks.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ex.BrokerError) as excinfo:
        ex.load_scan(scan)

    message = str(excinfo.value)
    assert TODAY.isoformat() in message
    assert (TODAY - dt.timedelta(days=21)).isoformat() in message


def test_a_malformed_asof_is_refused_rather_than_silently_ignored(test_cfg: Config) -> None:
    """Audit BUG-026: an unparseable `asof` reverted to the directory with no note."""
    scan = write_scan(test_cfg, day=TODAY)
    payload = json.loads((scan / "picks.json").read_text(encoding="utf-8"))
    payload["asof"] = "last Tuesday"
    (scan / "picks.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ex.BrokerError) as excinfo:
        ex.load_scan(scan)
    assert "last Tuesday" in str(excinfo.value)


def test_a_matching_asof_is_accepted(test_cfg: Config) -> None:
    bundle = ex.load_scan(write_scan(test_cfg, day=TODAY))
    assert bundle.scan_date == TODAY


# --- BUG-025: one symbol, one order -----------------------------------------


def test_a_symbol_listed_twice_is_planned_only_once(test_cfg: Config) -> None:
    """Audit BUG-025: two AAPL rows sent two identical live orders.

    The journal-based ``duplicate`` guardrail cannot catch it — it runs before
    either order has been journalled.
    """
    scan = write_scan(test_cfg, picks=[make_pick("AAPL"), make_pick("AAPL")])
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert [p.symbol for p in plans] == ["AAPL"]
    assert any("more than once" in p for p in problems)


# --- BUG-009: the wire payload must be the trade that was approved ----------


def test_a_payload_buying_a_different_symbol_is_dropped(test_cfg: Config) -> None:
    """Audit BUG-009: symbol, side, size and price were never cross-checked."""
    scan = write_scan(test_cfg, picks=[make_pick("AAPL")], orders={"AAPL": draft_variants("MSFT")})
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert plans == []
    assert any("buys MSFT, not AAPL" in p for p in problems)


def test_a_zero_quantity_payload_no_longer_displays_the_picks_size(
    test_cfg: Config,
) -> None:
    """Audit BUG-009: `leg quantity or pick shares or 0` showed 50 for a payload of 0."""
    scan = write_scan(
        test_cfg,
        picks=[make_pick("AAPL", shares=50)],
        orders={"AAPL": draft_variants("AAPL", quantity=0)},
    )
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert plans == []
    assert any("buys 0 shares but the scan sized the position at 50" in p for p in problems)


def test_a_sell_short_parent_is_dropped(test_cfg: Config) -> None:
    """Audit BUG-009: BUY vs SELL_SHORT was never checked against the pick."""
    order = make_order("AAPL")
    order["orderLegCollection"][0]["instruction"] = "SELL_SHORT"
    scan = write_scan(test_cfg, picks=[make_pick("AAPL")], orders={"AAPL": order})
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert plans == []
    assert any("SELL_SHORT" in p and "BUY" in p for p in problems)


def test_a_payload_priced_away_from_the_pick_is_dropped(test_cfg: Config) -> None:
    """Audit BUG-009: `order price or pick entry` hid a stale limit price."""
    scan = write_scan(
        test_cfg,
        picks=[make_pick("AAPL", entry=100.0)],
        orders={"AAPL": draft_variants("AAPL", price=133.0)},
    )
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert plans == []
    assert any("limit price is 133.0" in p for p in problems)


def test_a_draft_with_no_protective_stop_is_dropped(test_cfg: Config) -> None:
    """Audit BUG-009: the stop fell back to the pick's when the order had none.

    The table, the confirmation prompt and the journal then all showed a
    protective level that existed nowhere on the wire.
    """
    order = make_order("AAPL")
    del order["childOrderStrategies"]
    scan = write_scan(test_cfg, picks=[make_pick("AAPL")], orders={"AAPL": order})

    plans, problems = ex.plan_orders(ex.load_scan(scan))

    assert plans == []
    assert any("no protective stop child" in p for p in problems)


def test_a_draft_whose_stop_disagrees_with_the_pick_is_dropped(test_cfg: Config) -> None:
    """Audit BUG-009: the risk on the wire must be the risk that was approved."""
    scan = write_scan(
        test_cfg,
        picks=[make_pick("AAPL", entry=100.0)],  # stop 96.00
        orders={"AAPL": draft_variants("AAPL", price=100.0, stop=90.0)},
    )

    plans, problems = ex.plan_orders(ex.load_scan(scan))

    assert plans == []
    assert any("stop is 90.0" in p and "96.0" in p for p in problems)


def test_an_agreeing_stop_is_planned_from_the_order_itself(test_cfg: Config) -> None:
    scan = write_scan(test_cfg, picks=[make_pick("MSFT", entry=50.0, shares=4)])
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert problems == []
    assert plans[0].stop_price == 46.0  # the pick's stop, because the draft carries it


def test_a_trailing_stop_child_is_accepted_and_claims_no_price(test_cfg: Config) -> None:
    """A TRAILING_STOP child has an offset, not a level — so none is displayed."""
    order = make_order("AAPL")
    order["childOrderStrategies"] = [
        {
            "orderType": "TRAILING_STOP",
            "session": "NORMAL",
            "stopPriceLinkBasis": "LAST",
            "stopPriceLinkType": "VALUE",
            "stopPriceOffset": 6.0,
            "duration": "GOOD_TILL_CANCEL",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL",
                    "quantity": 3,
                    "instrument": {"symbol": "AAPL", "assetType": "EQUITY"},
                }
            ],
        }
    ]
    scan = write_scan(test_cfg, picks=[make_pick("AAPL")], orders={"AAPL": order})

    plans, problems = ex.plan_orders(ex.load_scan(scan))

    assert problems == []
    assert plans[0].stop_price is None


def test_a_price_written_as_a_string_still_matches(test_cfg: Config) -> None:
    """Real drafts carry prices as two-decimal strings; the check compares cents."""
    order = make_order("AAPL", price=100.0)
    order["price"] = "100.00"
    scan = write_scan(test_cfg, picks=[make_pick("AAPL", entry=100.0)], orders={"AAPL": order})
    plans, problems = ex.plan_orders(ex.load_scan(scan))
    assert problems == []
    assert plans[0].limit_price == 100.0


# --- A7: the journal is the authority on what was already decided -----------


def test_a_pick_the_journal_calls_invalidated_is_skipped(test_cfg: Config) -> None:
    """Frozen contract A7: a rerun must not resurrect what `swing confirm` threw out."""
    journal = Journal.load(test_cfg)
    journal.add_picks([journal_pick("AAPL", status="invalidated")])
    scan = write_scan(test_cfg, picks=[make_pick("AAPL", status="drafted")])

    plans, problems = ex.plan_orders(ex.load_scan(scan), journal=journal)

    assert plans == []
    assert any("already invalidated in the journal" in p for p in problems)


# --- BUG-007: cash sweeps are not positions ---------------------------------


def test_cash_sweeps_and_closed_lines_are_not_positions() -> None:
    """Audit BUG-007: MMDA1 always appeared, so reconciliation always refused."""
    payload = {
        "securitiesAccount": {
            "positions": [
                {
                    "instrument": {"symbol": "MMDA1", "assetType": "CASH_EQUIVALENT"},
                    "longQuantity": 4123.55,
                },
                {
                    "instrument": {"symbol": "SWVXX", "assetType": "MUTUAL_FUND"},
                    "longQuantity": 900.0,
                },
                {"instrument": {"symbol": "GONE", "assetType": "EQUITY"}, "longQuantity": 0.0},
                {"instrument": {"symbol": "SPY", "assetType": "ETF"}, "longQuantity": 10.0},
                {"instrument": {"symbol": "AAPL", "assetType": "EQUITY"}, "longQuantity": 3.0},
            ]
        }
    }
    rows = ex._extract_positions(payload)
    assert [r["symbol"] for r in rows] == ["SPY", "AAPL"]


def test_a_holding_with_no_asset_type_is_kept() -> None:
    """Fail safe: an unrecognised holding must stop reconciliation, not vanish."""
    payload = {
        "securitiesAccount": {"positions": [{"instrument": {"symbol": "WAT"}, "longQuantity": 1.0}]}
    }
    assert [r["symbol"] for r in ex._extract_positions(payload)] == ["WAT"]


# --- BUG-007/A8: the missing half of the order state machine ----------------


def test_a_broker_fill_becomes_a_journal_position(live_cfg: Config) -> None:
    """Audit BUG-007: nothing ever wrote "filled", so positions() was always empty."""
    journal = Journal.load(live_cfg)
    journal.add_picks([journal_pick("AAPL", status="ordered")])
    journal.record_order(
        {
            "symbol": "AAPL",
            "date": TODAY.isoformat(),
            "scan_date": TODAY.isoformat(),
            "status": "open",
            "order_id": "1001",
            "client_ref": "swing-test-AAPL-1",
            "quantity": 3,
        }
    )

    sync = ex.sync_broker_orders(journal, [filled_order(1001, "AAPL")])

    assert sync.filled == ("AAPL",)
    reloaded = Journal.load(live_cfg)
    assert [o["status"] for o in reloaded.orders] == ["filled"]
    assert [p["symbol"] for p in reloaded.positions()] == ["AAPL"]


@pytest.mark.parametrize(
    ("broker_status", "journal_status"),
    [
        ("CANCELED", "cancelled"),
        ("REJECTED", "rejected"),
        ("EXPIRED", "expired"),
        ("REPLACED", "replaced"),
    ],
)
def test_a_terminal_broker_status_closes_the_journal_order(
    live_cfg: Config, broker_status: str, journal_status: str
) -> None:
    """Audit BUG-007: an order that died at the broker stayed "open" here forever."""
    journal = Journal.load(live_cfg)
    journal.record_order(
        {
            "symbol": "AAPL",
            "date": TODAY.isoformat(),
            "status": "open",
            "order_id": "1001",
            "client_ref": "swing-test-AAPL-1",
        }
    )

    ex.sync_broker_orders(journal, [broker_order(1001, "AAPL", status=broker_status)])

    assert [o["status"] for o in Journal.load(live_cfg).orders] == [journal_status]


def test_a_still_working_order_is_left_alone(live_cfg: Config) -> None:
    journal = Journal.load(live_cfg)
    journal.record_order(
        {"symbol": "AAPL", "date": TODAY.isoformat(), "status": "open", "order_id": "1001"}
    )
    sync = ex.sync_broker_orders(journal, [broker_order(1001, "AAPL", status="WORKING")])
    assert sync.changed == 0
    assert sync.working_symbols == ("AAPL",)
    assert [o["status"] for o in Journal.load(live_cfg).orders] == ["open"]


def test_an_order_the_broker_never_mentions_is_left_alone(live_cfg: Config) -> None:
    """ "Not in this 60-day window" is not the same as "gone"."""
    journal = Journal.load(live_cfg)
    journal.record_order(
        {"symbol": "AAPL", "date": TODAY.isoformat(), "status": "open", "order_id": "999"}
    )
    ex.sync_broker_orders(journal, [broker_order(1001, "MSFT")])
    assert [o["status"] for o in Journal.load(live_cfg).orders] == ["open"]


def test_live_syncs_fills_before_it_judges_anything(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-007: last night's fill has to be known before today's checks run."""
    journal = Journal.load(live_cfg)
    journal.add_picks(
        [journal_pick("MSFT", day=TODAY - dt.timedelta(days=1), status="ordered", entry=50.0)]
    )
    journal.record_order(
        {
            "symbol": "MSFT",
            "date": (TODAY - dt.timedelta(days=1)).isoformat(),
            "scan_date": (TODAY - dt.timedelta(days=1)).isoformat(),
            "status": "open",
            "order_id": "900",
            "client_ref": "swing-yesterday-MSFT",
            "quantity": 3,
        }
    )
    client = FakeClient(positions=[("MSFT", 3.0)], orders=[filled_order(900, "MSFT")])
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    out = capsys.readouterr().out
    assert "Broker sync : filled: MSFT" in out
    assert "PASS  reconciliation" in out
    assert [p["symbol"] for p in Journal.load(live_cfg).positions()] == ["MSFT"]


def test_live_refuses_when_the_two_order_books_disagree(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-007/BUG-002: an order at Schwab the journal never recorded."""
    client = FakeClient(orders=[broker_order(555, "TSLA", status="WORKING")])
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    out = capsys.readouterr().out
    assert "reconciliation" in out
    assert "working at Schwab but not in the journal: TSLA" in out


def test_live_stops_when_the_broker_will_not_list_orders(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Not knowing what is working is not a state in which to send more orders."""
    client = FakeClient()
    monkeypatch.setattr(
        client, "get_orders_for_account", Mock(side_effect=RuntimeError("503 unavailable"))
    )
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 2
    assert client.placed == []
    assert "503 unavailable" in capsys.readouterr().out


def test_print_positions_syncs_fills_and_shows_both_order_books(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit BUG-007: `swing positions` printed "(none)" on a funded account."""
    journal = Journal.load(live_cfg)
    journal.add_picks([journal_pick("AAPL", status="ordered")])
    journal.record_order(
        {
            "symbol": "AAPL",
            "date": TODAY.isoformat(),
            "scan_date": TODAY.isoformat(),
            "status": "open",
            "order_id": "1001",
            "client_ref": "swing-test-AAPL-1",
            "quantity": 3,
            "limit": 100.0,
        }
    )
    write_token(live_cfg)
    client = FakeClient(positions=[("AAPL", 3.0)], orders=[filled_order(1001, "AAPL")])
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)

    ex.print_positions(live_cfg)

    out = capsys.readouterr().out
    assert "Broker sync: filled: AAPL" in out
    assert "Reconciliation: PASS" in out
    assert [p["symbol"] for p in Journal.load(live_cfg).positions()] == ["AAPL"]


# --- BUG-002/A8: journal first, then send -----------------------------------


class RecordingClient(FakeClient):
    """A client that looks at the journal on disk at the moment of placement."""

    def __init__(self, cfg: Config, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cfg = cfg
        self.journal_at_placement: list[list[dict[str, Any]]] = []

    def place_order(self, account_hash: str, order_spec: dict[str, Any]) -> FakeResponse:
        self.journal_at_placement.append(Journal.load(self._cfg).orders)
        return super().place_order(account_hash, order_spec)


def test_the_order_is_journalled_before_it_is_sent(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit BUG-002: the journal write came after the network call.

    A crash, a full disk or a Ctrl-C in between left a live order at Schwab that
    the local safety state had never heard of.
    """
    client = RecordingClient(live_cfg)
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    (during,) = client.journal_at_placement
    assert [o["status"] for o in during] == ["pending"]
    assert during[0]["order_id"] is None
    assert during[0]["client_ref"].startswith("swing-")

    after = Journal.load(live_cfg).orders
    assert [o["status"] for o in after] == ["open"]
    assert after[0]["order_id"] == "1001"
    assert after[0]["client_ref"] == during[0]["client_ref"]


class UnreadableIdClient(FakeClient):
    """A 201 with no Location header and a body that will not parse — window A."""

    def place_order(self, account_hash: str, order_spec: dict[str, Any]) -> Any:
        self.placed.append((account_hash, order_spec))

        class _Response:
            status_code = 201
            headers: dict[str, str] = {}

            def json(self) -> Any:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")

        return _Response()


def test_an_unreadable_order_id_never_claims_the_order_was_not_sent(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-002 window A: a 201 with a non-JSON body printed "so it was not sent".

    The order was live. The run must keep going, say the id is unknown, and warn
    that the order may exist.
    """
    client = UnreadableIdClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)  # no SystemExit

    out = capsys.readouterr().out
    assert "MAY BE LIVE" in out
    # The exact sentence the old code printed about a live order.
    assert "so it was not sent" not in out
    assert "Sent 3 AAPL" in out
    orders = Journal.load(live_cfg).orders
    assert [o["status"] for o in orders] == ["open"]
    assert orders[0]["order_id"] is None


def test_a_refused_placement_is_journalled_as_unknown(
    cfg_factory: Any,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-002 window B: a failed call can still have left a live order."""
    cfg = cfg_factory(
        account={"equity": 100_000.0},
        execution={"enabled": True, "max_orders_per_day": 3, "max_new_exposure_pct": 100.0},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    client = FakeClient(equity=100_000.0, place_error=RuntimeError("read timed out"))
    arrange_live(monkeypatch, cfg, client)
    write_scan(cfg)

    with pytest.raises(SystemExit):
        ex.run_execute(cfg, live=True, assume_yes=True, now=NOW)

    orders = Journal.load(cfg).orders
    assert [o["status"] for o in orders] == ["unknown"]
    assert orders[0]["error"] == "read timed out"
    out = capsys.readouterr().out
    assert "journalled as unknown rather than sent" in out
    assert "held back" in out


def test_a_journal_failure_after_placement_shouts_the_order_id(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-002/DEBT-004: this was an INFO log nobody would ever see."""
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)

    real = Journal.annotate_order

    def explode(self: Journal, client_ref: str, **fields: Any) -> int:
        if fields.get("status") == "open":
            raise OSError("No space left on device")
        return real(self, client_ref, **fields)

    monkeypatch.setattr(Journal, "annotate_order", explode)

    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    out = capsys.readouterr().out
    assert "MAY BE LIVE" in out
    assert "1001" in out
    assert "No space left on device" in out


# --- BUG-008: the kill switch and the clock are re-read before each order ----


def test_a_kill_switch_thrown_mid_run_stops_the_remaining_orders(
    cfg_factory: Any,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-008: `swing kill` from a second terminal did not stop queued orders."""
    from swing.state import engage_kill

    cfg = cfg_factory(
        account={"equity": 100_000.0},
        execution={"enabled": True, "max_orders_per_day": 5, "max_new_exposure_pct": 100.0},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    picks = [make_pick("AAPL"), make_pick("MSFT"), make_pick("NVDA")]
    client = FakeClient(equity=100_000.0)
    arrange_live(monkeypatch, cfg, client, prices=dict.fromkeys(("AAPL", "MSFT", "NVDA"), 100.2))
    write_scan(cfg, picks=picks)

    answers = iter(["y", "y", "y"])

    def prompt(question: str = "") -> str:
        # The panicking human reaches for `swing kill` while the second
        # confirmation is on screen.
        if client.placed:
            engage_kill(cfg)
        return next(answers)

    monkeypatch.setattr("builtins.input", prompt)

    ex.run_execute(cfg, live=True, now=NOW)

    assert [
        order["orderLegCollection"][0]["instrument"]["symbol"] for _, order in client.placed
    ] == ["AAPL"]
    out = capsys.readouterr().out
    assert "Stopping before the MSFT order" in out
    assert "kill switch is engaged" in out


def test_the_closing_bell_stops_the_remaining_orders(
    cfg_factory: Any,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-008: a run begun at 15:55 could place after the close."""
    cfg = cfg_factory(
        account={"equity": 100_000.0},
        execution={"enabled": True, "max_orders_per_day": 5, "max_new_exposure_pct": 100.0},
        schwab={"api_key": "EXAMPLE-APP-KEY", "app_secret": "EXAMPLE-APP-SECRET"},
    )
    client = FakeClient(equity=100_000.0)
    arrange_live(monkeypatch, cfg, client, prices={"AAPL": 100.2, "MSFT": 100.2})
    write_scan(cfg, picks=[make_pick("AAPL"), make_pick("MSFT")])

    late = dt.datetime(2026, 8, 18, 15, 55, tzinfo=ZoneInfo("America/New_York"))
    after_close = dt.datetime(2026, 8, 18, 16, 3, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(ex, "_now_eastern", lambda: after_close if client.placed else late)

    ex.run_execute(cfg, live=True, assume_yes=True)

    assert len(client.placed) == 1
    out = capsys.readouterr().out
    assert "Stopping before the MSFT order" in out
    assert "regular session" in out


# --- DEBT-003/A11: a dry run really does touch no network -------------------


def test_a_dry_run_never_asks_the_provider_for_a_quote(
    test_cfg: Config,
    no_client: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit DEBT-003: the docstring said "touches no network" and it fetched quotes.

    Offline that meant a stall through every provider retry, invisible to the
    suite because the socket block turned it into a broad-except SKIP. Note the
    shape of this test: raising from the spy proves nothing, because
    ``fetch_quotes`` catches everything by design. The call itself is what is
    counted.
    """
    calls: list[str] = []

    def spy(cfg: Config, **kwargs: Any) -> Any:
        calls.append("get_provider")
        raise RuntimeError("there is no network")

    monkeypatch.setattr(swing.data, "get_provider", spy)
    write_scan(test_cfg)

    ex.run_execute(test_cfg, now=NOW)

    assert calls == []
    out = capsys.readouterr().out
    assert "SKIP  quote_drift" in out
    assert "Not checked in a dry run" in out


def test_a_live_run_still_fetches_quotes(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A11 changes the dry run only: live still checks the price it is buying at."""
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client, prices={"AAPL": 100.2})
    write_scan(live_cfg)

    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert "PASS  quote_drift" in capsys.readouterr().out


def test_a_live_refusal_carries_the_providers_own_words(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit DEBT-004: the provider's error never reached the refusal text."""

    def explode(cfg: Config, **kwargs: Any) -> Any:
        raise RuntimeError("HTTP 429 from Yahoo")

    client = FakeClient()
    write_token(live_cfg)
    monkeypatch.setattr(auth_mod, "get_client", lambda c: client)
    monkeypatch.setattr(swing.data, "get_provider", explode)
    monkeypatch.setattr(client, "get_quotes", Mock(side_effect=RuntimeError("no market data")))
    write_scan(live_cfg)

    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    out = capsys.readouterr().out
    assert "HTTP 429 from Yahoo" in out
    assert "no market data" in out


# --- DEBT-008: one clock, one "today" ---------------------------------------


def test_a_naive_clock_is_refused_at_the_boundary(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit DEBT-008: auth reads a naive datetime as local, guardrails as Eastern."""
    write_scan(test_cfg)
    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(test_cfg, now=dt.datetime(2026, 8, 18, 10, 30))
    assert excinfo.value.code == 2
    assert "no timezone" in capsys.readouterr().out


def test_the_clock_is_converted_to_eastern_once(
    test_cfg: Config, no_client: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit DEBT-008: "today" must be derived from one Eastern reading, not two."""
    write_scan(test_cfg, day=dt.date(2026, 8, 18))
    # 23:30 in London on the 18th is 18:30 Eastern on the *same* day.
    london = dt.datetime(2026, 8, 18, 23, 30, tzinfo=ZoneInfo("Europe/London"))
    ex.run_execute(test_cfg, now=london)
    out = capsys.readouterr().out
    assert "2026-08-18 18:30 EDT" in out
    assert "PASS  stale_scan" in out


# --- LEAK-004: every client that is built is closed -------------------------


class ClosableClient(FakeClient):
    """A client whose connection pool can be observed being released."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.session = FakeSession()


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_a_live_run_closes_the_client(
    live_cfg: Config, gate_passes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit LEAK-004: the httpx pool behind the client was never released."""
    client = ClosableClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)
    ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.session.closed is True


def test_a_live_run_closes_the_client_even_when_it_refuses(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ClosableClient(positions=[("TSLA", 10.0)])
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)
    with pytest.raises(SystemExit):
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)
    assert client.session.closed is True


def test_print_positions_closes_the_client(
    live_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token(live_cfg)
    client = ClosableClient()
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)
    ex.print_positions(live_cfg)
    assert client.session.closed is True


def test_kill_closes_the_client(live_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    write_token(live_cfg)
    client = ClosableClient()
    monkeypatch.setattr(auth_mod, "get_client", lambda cfg: client)
    ex.kill(live_cfg)
    assert client.session.closed is True


def test_an_order_that_cannot_be_journalled_is_not_sent(
    live_cfg: Config,
    gate_passes: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Audit BUG-002: journalling first is only safe if a failure stops the send."""
    client = FakeClient()
    arrange_live(monkeypatch, live_cfg, client)
    write_scan(live_cfg)
    monkeypatch.setattr(Journal, "record_order", Mock(side_effect=OSError("Read-only file system")))

    with pytest.raises(SystemExit) as excinfo:
        ex.run_execute(live_cfg, live=True, assume_yes=True, now=NOW)

    assert excinfo.value.code == 1
    assert client.placed == []
    out = capsys.readouterr().out
    assert "NOT sent" in out
    assert "Read-only file system" in out
