"""Tests for FROZEN CONTRACT 12 — every guardrail, blocking and passing.

AC15 says each guardrail must have a test proving it blocks. That is the spine
of this file: for all twelve checks there is a case where it refuses and a case
where it lets the order through, so a guardrail that quietly stops working
fails a test instead of losing money.

Nothing here touches the network, schwab-py, or ``swing.data``. Guardrails are
pure functions over already-fetched facts, which is exactly what makes that
possible.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import swing.backtest
from swing.broker import guardrails as g
from swing.config import Config
from swing.state import Journal, PickRecord, engage_kill

# ---------------------------------------------------------------------------
# small builders
# ---------------------------------------------------------------------------


def make_pick(symbol: str = "AAPL", *, entry: float = 100.0, atr: float = 2.0) -> dict[str, Any]:
    """A pick as it appears in ``picks.json`` (Contract 9)."""
    return {
        "symbol": symbol,
        "date": "2026-08-18",
        "kind": "pick",
        "entry": entry,
        "stop": entry - 2 * atr,
        "shares": 3,
        "risk_amount": 12.0,
        "score": 1.5,
        "atr": atr,
        "earnings_date": None,
        "earnings_known": False,
        "thesis": "20-day breakout",
        "status": "drafted",
    }


def limit_order(symbol: str = "AAPL", *, quantity: int = 3, price: float = 100.0) -> dict[str, Any]:
    """A Contract 10 style TRIGGER order: BUY LIMIT with a child SELL STOP."""
    return {
        "orderType": "LIMIT",
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
                "stopPrice": price * 0.95,
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


def make_record(symbol: str, day: date, *, status: str = "drafted") -> PickRecord:
    return PickRecord(
        symbol=symbol,
        date=day.isoformat(),
        kind="pick",
        entry=100.0,
        stop=96.0,
        shares=3,
        risk_amount=12.0,
        score=1.5,
        atr=2.0,
        earnings_date=None,
        earnings_known=False,
        thesis="test",
        status=status,
    )


@pytest.fixture
def journal(test_cfg: Config) -> Journal:
    return Journal.load(test_cfg)


def et(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    """A naive datetime, which the guardrails read as Eastern."""
    return datetime(year, month, day, hour, minute)


# ---------------------------------------------------------------------------
# GuardrailResult itself
# ---------------------------------------------------------------------------


def test_result_verdicts_are_distinguishable() -> None:
    assert g.GuardrailResult(True, "n", "r").verdict == "PASS"
    assert g.GuardrailResult(False, "n", "r").verdict == "STOP"
    assert g.GuardrailResult(True, "n", "r", warning=True).verdict == "WARN"
    assert g.skipped("n", "r").verdict == "SKIP"


def test_all_clear_only_cares_about_refusals() -> None:
    assert g.all_clear([g.GuardrailResult(True, "a", "r"), g.skipped("b", "r")]) is True
    assert (
        g.all_clear([g.GuardrailResult(True, "a", "r"), g.GuardrailResult(False, "b", "r")])
        is False
    )
    assert [r.name for r in g.refusals([g.GuardrailResult(False, "b", "r")])] == ["b"]


def test_every_refusal_is_a_sentence_that_tells_you_what_to_do(test_cfg: Config) -> None:
    """A guardrail that blocks without explaining itself is a bug."""
    engage_kill(test_cfg)
    blocking = [
        g.kill_switch(test_cfg),
        g.token_age(test_cfg, age_days=None),
        g.trading_hours(et(2026, 8, 15)),  # Saturday
        g.quote_drift(make_pick(), 130.0, test_cfg),
        g.new_exposure(test_cfg, existing_notional=1e9, proposed_notional=1.0, equity=100.0),
        g.limit_only({"orderType": "MARKET"}),
        g.reconciliation(live_symbols=["MSFT"], journal_symbols=[]),
        g.equity_mismatch(config_equity=100.0, live_equity=10.0),
        g.stale_scan(scan_date=date(2026, 8, 3), today=date(2026, 8, 18)),
    ]
    for result in blocking:
        assert result.ok is False, result.name
        assert result.reason.endswith("."), result.name
        assert len(result.reason.split()) >= 12, result.name


# ---------------------------------------------------------------------------
# 1. kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_blocks_when_the_file_exists(test_cfg: Config) -> None:
    engage_kill(test_cfg, reason="test")
    result = g.kill_switch(test_cfg)
    assert result.ok is False
    assert "kill --off" in result.reason


def test_kill_switch_passes_when_disengaged(test_cfg: Config) -> None:
    assert g.kill_switch(test_cfg).ok is True


# ---------------------------------------------------------------------------
# 2. token age
# ---------------------------------------------------------------------------


def test_token_age_blocks_a_dead_token(test_cfg: Config) -> None:
    result = g.token_age(test_cfg, age_days=7.2)
    assert result.ok is False
    assert "swing auth" in result.reason


def test_token_age_blocks_when_there_is_no_token(test_cfg: Config) -> None:
    result = g.token_age(test_cfg, age_days=None)
    assert result.ok is False
    assert "swing auth" in result.reason


def test_token_age_warns_but_allows_on_day_six(test_cfg: Config) -> None:
    result = g.token_age(test_cfg, age_days=6.4)
    assert result.ok is True
    assert result.warning is True
    assert result.verdict == "WARN"


def test_token_age_passes_on_a_fresh_token(test_cfg: Config) -> None:
    result = g.token_age(test_cfg, age_days=0.5)
    assert (result.ok, result.warning) == (True, False)


# ---------------------------------------------------------------------------
# 3. trading hours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "moment",
    [
        et(2026, 8, 15, 11, 0),  # Saturday
        et(2026, 8, 16, 11, 0),  # Sunday
        et(2026, 8, 18, 9, 0),  # before the open
        et(2026, 8, 18, 16, 30),  # after the close
        et(2026, 8, 18, 3, 0),  # the middle of the night
    ],
)
def test_trading_hours_blocks_outside_the_regular_session(moment: datetime) -> None:
    assert g.trading_hours(moment).ok is False


@pytest.mark.parametrize(
    "moment", [et(2026, 8, 18, 9, 30), et(2026, 8, 18, 16, 0), et(2026, 8, 18, 12, 0)]
)
def test_trading_hours_passes_inside_the_regular_session(moment: datetime) -> None:
    assert g.trading_hours(moment).ok is True


def test_trading_hours_converts_an_aware_clock_to_eastern() -> None:
    from zoneinfo import ZoneInfo

    # 18:00 UTC is 14:00 in New York — inside the session.
    aware = datetime(2026, 8, 18, 18, 0, tzinfo=ZoneInfo("UTC"))
    assert g.trading_hours(aware).ok is True
    # 02:00 UTC is 22:00 the previous evening in New York — outside it.
    assert g.trading_hours(datetime(2026, 8, 18, 2, 0, tzinfo=ZoneInfo("UTC"))).ok is False


# ---------------------------------------------------------------------------
# 4. backtest gate
# ---------------------------------------------------------------------------


class _GateResult:
    def __init__(self, passed: bool, reasons: list[str] | None = None) -> None:
        self.passed = passed
        self.reasons = reasons or []
        self.report_path: Path | None = None


def install_gate(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    module = types.ModuleType("swing.backtest.gate")
    module.check = lambda cfg: result  # type: ignore[attr-defined]
    monkeypatch.setattr(swing.backtest, "gate", module, raising=False)
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", module)


def test_gate_passed_blocks_when_the_gate_module_is_missing(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: no gate means no evidence, and no evidence means no trade."""
    monkeypatch.delattr(swing.backtest, "gate", raising=False)
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", None)
    result = g.gate_passed(test_cfg)
    assert result.ok is False
    assert "backtest gate is unavailable" in result.reason


def test_gate_passed_blocks_when_the_module_has_no_check(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = types.ModuleType("swing.backtest.gate")
    monkeypatch.setattr(swing.backtest, "gate", module, raising=False)
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", module)
    result = g.gate_passed(test_cfg)
    assert result.ok is False
    assert "backtest gate is unavailable" in result.reason


def test_gate_passed_blocks_when_the_gate_says_no(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_gate(monkeypatch, _GateResult(False, ["profit factor 0.9 < 1.3"]))
    result = g.gate_passed(test_cfg)
    assert result.ok is False
    assert "profit factor 0.9 < 1.3" in result.reason


def test_gate_passed_blocks_when_the_gate_explodes(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = types.ModuleType("swing.backtest.gate")

    def boom(cfg: Config) -> None:
        raise RuntimeError("latest.json is corrupt")

    module.check = boom  # type: ignore[attr-defined]
    monkeypatch.setattr(swing.backtest, "gate", module, raising=False)
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", module)
    result = g.gate_passed(test_cfg)
    assert result.ok is False
    assert "latest.json is corrupt" in result.reason


def test_gate_passed_allows_a_passing_gate(
    test_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_gate(monkeypatch, _GateResult(True))
    assert g.gate_passed(test_cfg).ok is True


# ---------------------------------------------------------------------------
# 5. quote drift
# ---------------------------------------------------------------------------


def test_quote_drift_blocks_when_price_ran_away_in_atrs(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_quote_drift_atr": 1.0, "max_quote_drift_pct": 50.0})
    result = g.quote_drift(make_pick(entry=100.0, atr=2.0), 103.0, cfg)
    assert result.ok is False
    assert "swing scan" in result.reason


def test_quote_drift_blocks_when_price_ran_away_in_percent(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_quote_drift_atr": 100.0, "max_quote_drift_pct": 3.0})
    result = g.quote_drift(make_pick(entry=100.0, atr=2.0), 104.0, cfg)
    assert result.ok is False
    assert "4.0%" in result.reason


def test_quote_drift_blocks_without_a_quote(test_cfg: Config) -> None:
    result = g.quote_drift(make_pick(), None, test_cfg)
    assert result.ok is False
    assert "No live quote" in result.reason


def test_quote_drift_blocks_when_the_scan_recorded_no_entry(test_cfg: Config) -> None:
    result = g.quote_drift(make_pick(entry=0.0), 100.0, test_cfg)
    assert result.ok is False


def test_quote_drift_refuses_a_pick_whose_entry_is_not_a_number(test_cfg: Config) -> None:
    """Audit BUG-044: raw float() on an editable file field raised, mid-run."""
    pick = {**make_pick(), "entry": "n/a"}
    result = g.quote_drift(pick, 100.0, test_cfg)
    assert result.ok is False
    assert "not a number" in result.reason
    assert result.reason.endswith(".")


def test_quote_drift_survives_a_junk_atr(test_cfg: Config) -> None:
    """Audit BUG-044: an unusable ATR falls back to the percentage limit, not a traceback."""
    pick = {**make_pick(), "atr": "unknown"}
    result = g.quote_drift(pick, 100.5, test_cfg)
    assert result.ok is True


def test_quote_drift_quotes_the_reason_the_price_was_missing(test_cfg: Config) -> None:
    """Audit DEBT-004: "fix the data provider" without saying what it said is useless."""
    result = g.quote_drift(make_pick(), None, test_cfg, quote_error="yfinance said HTTP 429")
    assert result.ok is False
    assert "yfinance said HTTP 429" in result.reason


def test_quote_drift_passes_when_the_price_barely_moved(test_cfg: Config) -> None:
    assert g.quote_drift(make_pick(entry=100.0, atr=2.0), 100.4, test_cfg).ok is True


# ---------------------------------------------------------------------------
# 6. orders per day
# ---------------------------------------------------------------------------


def test_orders_today_blocks_once_the_budget_is_spent(cfg_factory: Any, tmp_path: Path) -> None:
    cfg = cfg_factory(execution={"max_orders_per_day": 2})
    journal = Journal.load(cfg)
    for symbol in ("AAPL", "MSFT"):
        journal.record_order({"symbol": symbol, "date": "2026-08-18", "status": "open"})
    result = g.orders_today(journal, cfg, today=date(2026, 8, 18))
    assert result.ok is False
    assert "max_orders_per_day" in result.reason


def test_orders_today_blocks_when_the_limit_is_zero(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_orders_per_day": 0})
    result = g.orders_today(Journal.load(cfg), cfg, today=date(2026, 8, 18))
    assert result.ok is False


def test_orders_today_ignores_yesterdays_orders(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_orders_per_day": 1})
    journal = Journal.load(cfg)
    journal.record_order({"symbol": "AAPL", "date": "2026-08-17", "status": "open"})
    assert g.orders_today(journal, cfg, today=date(2026, 8, 18)).ok is True


def test_orders_placed_on_reads_either_timestamp_key(test_cfg: Config) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"symbol": "AAPL", "placed_at": "2026-08-18T10:30:00-04:00"})
    assert len(g.orders_placed_on(journal, date(2026, 8, 18))) == 1


def test_remaining_order_budget_is_what_the_guardrail_enforces(cfg_factory: Any) -> None:
    """Audit DEBT-007: the budget was computed here and again in the placement loop."""
    cfg = cfg_factory(execution={"max_orders_per_day": 2})
    journal = Journal.load(cfg)
    today = date(2026, 8, 18)
    assert g.remaining_order_budget(journal, cfg, today=today) == 2

    journal.record_order({"symbol": "AAPL", "date": today.isoformat(), "status": "open"})
    assert g.remaining_order_budget(journal, cfg, today=today) == 1
    assert g.orders_today(journal, cfg, today=today).ok is True

    journal.record_order({"symbol": "MSFT", "date": today.isoformat(), "status": "open"})
    assert g.remaining_order_budget(journal, cfg, today=today) == 0
    assert g.orders_today(journal, cfg, today=today).ok is False


def test_remaining_order_budget_never_goes_negative(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_orders_per_day": 1})
    journal = Journal.load(cfg)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        journal.record_order({"symbol": symbol, "date": "2026-08-18", "status": "open"})
    assert g.remaining_order_budget(journal, cfg, today=date(2026, 8, 18)) == 0


# ---------------------------------------------------------------------------
# 7. new exposure
# ---------------------------------------------------------------------------


def test_new_exposure_blocks_when_the_daily_budget_is_exceeded(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_new_exposure_pct": 50.0})
    result = g.new_exposure(
        cfg, existing_notional=4000.0, proposed_notional=2000.0, equity=10_000.0
    )
    assert result.ok is False
    assert "max_new_exposure_pct" in result.reason


def test_new_exposure_blocks_on_unknown_equity(test_cfg: Config) -> None:
    assert (
        g.new_exposure(test_cfg, existing_notional=0.0, proposed_notional=1.0, equity=0.0).ok
        is False
    )


def test_new_exposure_passes_inside_the_budget(cfg_factory: Any) -> None:
    cfg = cfg_factory(execution={"max_new_exposure_pct": 50.0})
    assert (
        g.new_exposure(cfg, existing_notional=1000.0, proposed_notional=2000.0, equity=10_000.0).ok
        is True
    )


# ---------------------------------------------------------------------------
# 8. limit orders only
# ---------------------------------------------------------------------------


def test_limit_only_blocks_a_market_parent() -> None:
    order = limit_order()
    order["orderType"] = "MARKET"
    result = g.limit_only(order)
    assert result.ok is False
    assert "MARKET" in result.reason


def test_limit_only_blocks_a_market_child_however_deeply_nested() -> None:
    order = limit_order()
    order["childOrderStrategies"][0]["orderType"] = "MARKET"
    result = g.limit_only(order)
    assert result.ok is False
    assert "MARKET" in result.reason


def test_limit_only_blocks_a_non_limit_parent() -> None:
    order = limit_order()
    order["orderType"] = "STOP_LIMIT"
    result = g.limit_only(order)
    assert result.ok is False
    assert "STOP_LIMIT" in result.reason


def test_limit_only_blocks_an_order_with_no_type_at_all() -> None:
    assert g.limit_only({"orderLegCollection": []}).ok is False


def test_limit_only_passes_a_limit_parent_with_a_stop_child() -> None:
    assert g.limit_only(limit_order()).ok is True


# ---------------------------------------------------------------------------
# 9. duplicate suppression
# ---------------------------------------------------------------------------


def test_duplicate_blocks_a_symbol_with_a_working_order(journal: Journal) -> None:
    journal.record_order({"symbol": "AAPL", "date": "2026-08-18", "status": "open"})
    result = g.duplicate(journal, "AAPL", asof=date(2026, 8, 18))
    assert result.ok is False
    assert "already working" in result.reason


def test_duplicate_blocks_a_symbol_already_held(journal: Journal) -> None:
    journal.add_picks([make_record("AAPL", date(2026, 8, 10), status="filled")])
    result = g.duplicate(journal, "AAPL", asof=date(2026, 8, 18))
    assert result.ok is False
    assert "already an open position" in result.reason


def test_duplicate_blocks_a_symbol_picked_yesterday(journal: Journal) -> None:
    """Audit BUG-006: with no bundle to exempt, yesterday's pick still blocks.

    The old code subtracted a day from ``asof`` *before* asking the journal,
    which shifted the whole cooling-off window and let this case through as
    "today's own pick". The exemption is now by identity — see the scan_date
    tests below — so the plain window is measured honestly again.
    """
    journal.add_picks([make_record("AAPL", date(2026, 8, 17))])
    result = g.duplicate(journal, "AAPL", asof=date(2026, 8, 18), within_days=5)
    assert result.ok is False
    assert "within the last 5 days" in result.reason


def test_duplicate_does_not_block_todays_own_pick(journal: Journal) -> None:
    """The pick being executed is already in the journal — it must not block itself."""
    journal.add_picks([make_record("AAPL", date(2026, 8, 18))])
    assert g.duplicate(journal, "AAPL", asof=date(2026, 8, 18), within_days=5).ok is True


def test_duplicate_exempts_the_scan_being_executed(journal: Journal) -> None:
    """Audit BUG-006: the designed flow is scan tonight, execute tomorrow morning.

    Picks are stamped with the *scan* date (17:30, after the close), so at
    execution time they are always dated the previous day. Treating that as a
    repeat entry refused 100% of orders in the only workflow the system has.
    """
    journal.add_picks([make_record("AAPL", date(2026, 8, 17))])
    result = g.duplicate(
        journal,
        "AAPL",
        asof=date(2026, 8, 18),
        within_days=5,
        scan_date=date(2026, 8, 17),
    )
    assert result.ok is True


def test_duplicate_still_refuses_a_genuinely_recent_pick(journal: Journal) -> None:
    """Audit BUG-006: exempting the bundle must not switch dedupe off."""
    journal.add_picks([make_record("AAPL", date(2026, 8, 15))])
    result = g.duplicate(
        journal,
        "AAPL",
        asof=date(2026, 8, 18),
        within_days=5,
        scan_date=date(2026, 8, 17),
    )
    assert result.ok is False
    assert "within the last 5 days" in result.reason


def test_duplicate_exempts_by_identity_not_by_age(journal: Journal) -> None:
    """Audit BUG-006: only the bundle's own picks are exempt, not everything that day."""
    journal.add_picks(
        [make_record("AAPL", date(2026, 8, 17)), make_record("AAPL", date(2026, 8, 14))]
    )
    result = g.duplicate(
        journal,
        "AAPL",
        asof=date(2026, 8, 18),
        within_days=5,
        scan_date=date(2026, 8, 17),
    )
    assert result.ok is False


def test_duplicate_ignores_closed_orders(journal: Journal) -> None:
    journal.record_order({"symbol": "AAPL", "date": "2026-08-18", "status": "filled"})
    assert g.duplicate(journal, "AAPL", asof=date(2026, 8, 18), within_days=0).ok is True


def test_duplicate_blocks_a_symbol_with_a_pending_order(journal: Journal) -> None:
    """Audit BUG-002: a row written just before the network call may be a live order."""
    journal.record_order({"symbol": "AAPL", "date": "2026-08-18", "status": "pending"})
    assert g.working_order_symbols(journal) == ["AAPL"]


# ---------------------------------------------------------------------------
# 10. reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_blocks_when_the_broker_holds_something_unknown() -> None:
    result = g.reconciliation(live_symbols=["AAPL", "TSLA"], journal_symbols=["AAPL"])
    assert result.ok is False
    assert "TSLA" in result.reason


def test_reconciliation_blocks_when_the_journal_holds_something_the_broker_does_not() -> None:
    result = g.reconciliation(live_symbols=[], journal_symbols=["AAPL"])
    assert result.ok is False
    assert "AAPL" in result.reason


def test_reconciliation_blocks_when_live_positions_are_unknown() -> None:
    assert g.reconciliation(live_symbols=None, journal_symbols=["AAPL"]).ok is False


def test_reconciliation_downgrades_an_acknowledged_difference() -> None:
    result = g.reconciliation(live_symbols=["TSLA"], journal_symbols=[], acknowledged=True)
    assert (result.ok, result.warning) == (True, True)


def test_reconciliation_passes_when_both_views_agree() -> None:
    assert g.reconciliation(live_symbols=["aapl"], journal_symbols=["AAPL"]).ok is True


def test_reconciliation_refuses_an_order_the_journal_never_saw() -> None:
    """Audit BUG-007/BUG-002: positions alone cannot see an unrecorded live order."""
    result = g.reconciliation(
        live_symbols=[],
        journal_symbols=[],
        live_order_symbols=["MSFT"],
        journal_order_symbols=[],
    )
    assert result.ok is False
    assert "working at Schwab but not in the journal: MSFT" in result.reason


def test_reconciliation_refuses_an_order_the_broker_no_longer_has() -> None:
    """Audit BUG-007: cancelled in the Schwab app, still 'working' in the journal."""
    result = g.reconciliation(
        live_symbols=[],
        journal_symbols=[],
        live_order_symbols=[],
        journal_order_symbols=["AAPL"],
    )
    assert result.ok is False
    assert "working in the journal but not at Schwab: AAPL" in result.reason


def test_reconciliation_passes_when_both_order_books_agree() -> None:
    result = g.reconciliation(
        live_symbols=["AAPL"],
        journal_symbols=["AAPL"],
        live_order_symbols=["msft"],
        journal_order_symbols=["MSFT"],
    )
    assert result.ok is True
    assert "1 working order(s)" in result.reason


def test_reconciliation_does_not_compare_orders_it_was_not_given() -> None:
    """``None`` means "the order book was not read", which is not a disagreement."""
    result = g.reconciliation(
        live_symbols=["AAPL"], journal_symbols=["AAPL"], journal_order_symbols=["MSFT"]
    )
    assert result.ok is True


def test_working_order_symbols_counts_every_status_that_might_be_alive(
    journal: Journal,
) -> None:
    """Audit BUG-002: pending and unknown rows may be real orders at Schwab."""
    for symbol, status in (
        ("AAPL", "open"),
        ("MSFT", "pending"),
        ("NVDA", "unknown"),
        ("TSLA", "filled"),
        ("AMD", "cancelled"),
    ):
        journal.record_order({"symbol": symbol, "date": "2026-08-18", "status": status})
    assert g.working_order_symbols(journal) == ["AAPL", "MSFT", "NVDA"]


# ---------------------------------------------------------------------------
# 11. equity mismatch
# ---------------------------------------------------------------------------


def test_equity_mismatch_blocks_a_stale_config_equity() -> None:
    result = g.equity_mismatch(config_equity=10_000.0, live_equity=5_000.0)
    assert result.ok is False
    assert "account.equity" in result.reason


def test_equity_mismatch_blocks_when_the_balance_is_unknown() -> None:
    assert g.equity_mismatch(config_equity=10_000.0, live_equity=None).ok is False


def test_equity_mismatch_blocks_on_nonsense_config_equity() -> None:
    assert g.equity_mismatch(config_equity=0.0, live_equity=100.0).ok is False


def test_equity_mismatch_passes_inside_the_tolerance() -> None:
    assert g.equity_mismatch(config_equity=10_000.0, live_equity=9_000.0).ok is True


# ---------------------------------------------------------------------------
# 12. stale scan
# ---------------------------------------------------------------------------


def test_stale_scan_blocks_a_week_old_report() -> None:
    result = g.stale_scan(scan_date=date(2026, 8, 11), today=date(2026, 8, 18))
    assert result.ok is False
    assert "swing scan" in result.reason


def test_stale_scan_blocks_a_report_from_the_future() -> None:
    result = g.stale_scan(scan_date=date(2026, 8, 19), today=date(2026, 8, 18))
    assert result.ok is False
    assert "clock" in result.reason


def test_stale_scan_allows_friday_night_to_monday_morning() -> None:
    friday, monday = date(2026, 8, 14), date(2026, 8, 17)
    assert friday.weekday() == 4 and monday.weekday() == 0
    assert g.stale_scan(scan_date=friday, today=monday).ok is True


def test_stale_scan_allows_last_nights_scan_and_todays() -> None:
    assert g.stale_scan(scan_date=date(2026, 8, 17), today=date(2026, 8, 18)).ok is True
    assert g.stale_scan(scan_date=date(2026, 8, 18), today=date(2026, 8, 18)).ok is True


def test_stale_scan_blocks_two_trading_days_later() -> None:
    assert g.stale_scan(scan_date=date(2026, 8, 17), today=date(2026, 8, 19)).ok is False


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


def clean_cfg(cfg_factory: Any) -> Config:
    return cfg_factory(account={"equity": 10_000.0}, execution={"max_orders_per_day": 3})


def test_run_guardrails_reports_every_check_and_passes_when_all_is_well(
    cfg_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = clean_cfg(cfg_factory)
    install_gate(monkeypatch, _GateResult(True))
    results = g.run_guardrails(
        cfg,
        now=et(2026, 8, 18, 10, 0),
        journal=Journal.load(cfg),
        scan_date=date(2026, 8, 18),
        token_age_days=1.0,
        live_symbols=[],
        live_equity=10_500.0,
    )
    names = [r.name for r in results]
    assert names == [
        "kill_switch",
        "token_age",
        "trading_hours",
        "gate_passed",
        "stale_scan",
        "orders_today",
        "reconciliation",
        "equity_mismatch",
    ]
    assert g.all_clear(results) is True


def test_run_guardrails_skips_live_only_checks_in_a_dry_run(
    cfg_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = clean_cfg(cfg_factory)
    install_gate(monkeypatch, _GateResult(True))
    results = g.run_guardrails(
        cfg,
        now=et(2026, 8, 18, 10, 0),
        journal=Journal.load(cfg),
        scan_date=date(2026, 8, 18),
        token_age_days=1.0,
        dry_run=True,
    )
    by_name = {r.name: r for r in results}
    assert by_name["reconciliation"].skipped is True
    assert by_name["equity_mismatch"].skipped is True
    assert g.all_clear(results) is True


def test_run_guardrails_refuses_live_when_account_data_is_missing(
    cfg_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = clean_cfg(cfg_factory)
    install_gate(monkeypatch, _GateResult(True))
    results = g.run_guardrails(
        cfg,
        now=et(2026, 8, 18, 10, 0),
        journal=Journal.load(cfg),
        scan_date=date(2026, 8, 18),
        token_age_days=1.0,
        dry_run=False,
    )
    by_name = {r.name: r for r in results}
    assert by_name["reconciliation"].ok is False
    assert by_name["equity_mismatch"].ok is False


def test_run_order_guardrails_covers_the_per_order_checks(cfg_factory: Any) -> None:
    cfg = clean_cfg(cfg_factory)
    journal = Journal.load(cfg)
    results = g.run_order_guardrails(
        cfg,
        pick=make_pick(),
        order=limit_order(),
        quote=100.2,
        journal=journal,
        asof=date(2026, 8, 18),
        existing_notional=0.0,
        proposed_notional=300.0,
        equity=10_000.0,
    )
    assert [r.name for r in results] == ["limit_only", "quote_drift", "duplicate", "new_exposure"]
    assert g.all_clear(results) is True


def test_run_order_guardrails_skips_quote_drift_without_a_quote_in_dry_run(
    cfg_factory: Any,
) -> None:
    cfg = clean_cfg(cfg_factory)
    results = g.run_order_guardrails(
        cfg,
        pick=make_pick(),
        order=limit_order(),
        quote=None,
        journal=Journal.load(cfg),
        asof=date(2026, 8, 18),
        existing_notional=0.0,
        proposed_notional=300.0,
        equity=10_000.0,
        dry_run=True,
    )
    drift = next(r for r in results if r.name == "quote_drift")
    assert (drift.skipped, drift.ok) == (True, True)
    assert "dry run" in drift.reason


def test_run_order_guardrails_refuses_without_a_quote_when_live(cfg_factory: Any) -> None:
    cfg = clean_cfg(cfg_factory)
    results = g.run_order_guardrails(
        cfg,
        pick=make_pick(),
        order=limit_order(),
        quote=None,
        journal=Journal.load(cfg),
        asof=date(2026, 8, 18),
        existing_notional=0.0,
        proposed_notional=300.0,
        equity=10_000.0,
        dry_run=False,
    )
    assert g.all_clear(results) is False


def test_dedupe_window_default_is_documented() -> None:
    assert g.DEFAULT_DEDUPE_DAYS >= 1
    assert g.EQUITY_MISMATCH_TOLERANCE_PCT == 20.0


def test_trading_day_counting_skips_the_weekend() -> None:
    saturday = date(2026, 8, 15)
    assert g._trading_days_between(saturday, saturday + timedelta(days=2)) == 1
