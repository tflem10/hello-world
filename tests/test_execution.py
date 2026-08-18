"""Execution guardrails.

Every guardrail gets a test that proves it **blocks**, against a mocked client
that records what it was asked to place. The client asserts nothing was
transmitted in every blocking case — a guardrail that merely logs a warning and
places the order anyway would pass a weaker test.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from swing.config import Config, load_config
from swing.execution.executor import run_execute
from swing.execution.guardrails import (
    check_order,
    choose_order_variant,
    kill_switch_engaged,
    preflight,
    release_kill_switch,
    set_kill_switch,
)
from swing.execution.journal import (
    EVENT_PLACED,
    open_positions,
    read_events,
    record_entry,
    record_exit,
)
from swing.orders import draft_orders
from swing.picks import STATUS_TRADABLE, Pick, PickSheet, write_sheet

EASTERN = ZoneInfo("America/New_York")
# A Wednesday at 10:00 ET: the market is open.
MARKET_OPEN = datetime(2024, 5, 1, 10, 0, tzinfo=EASTERN)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=201, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {"Location": "https://api.schwab.com/orders/999888"}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: order rejected")


class RecordingClient:
    """Records placements. Never actually calls anything."""

    class Account:
        class Fields:
            POSITIONS = "positions"

    def __init__(self, equity=10_000.0, positions=None, fail=False, prices=None):
        self.placed: list[tuple[str, dict]] = []
        self._equity = equity
        self._positions = positions or {}
        self._fail = fail
        # Default: quote every symbol at the reference price used by _pick(),
        # so the quote-drift guardrail passes and the happy path is reachable.
        self._prices = prices if prices is not None else {}

    def get_quotes(self, symbols):
        return FakeResponse(
            status=200,
            payload={
                s: {"quote": {"lastPrice": self._prices.get(s, 100.0),
                              "bidPrice": self._prices.get(s, 100.0) - 0.01,
                              "askPrice": self._prices.get(s, 100.0) + 0.01}}
                for s in symbols
            },
        )

    def place_order(self, account_hash, order_spec):
        self.placed.append((account_hash, order_spec))
        if self._fail:
            return FakeResponse(status=400)
        return FakeResponse()

    def get_account(self, account_hash, fields=None):
        payload = {
            "securitiesAccount": {
                "currentBalances": {"liquidationValue": self._equity},
                "positions": [
                    {"instrument": {"symbol": s}, "longQuantity": q}
                    for s, q in self._positions.items()
                ],
            }
        }
        return FakeResponse(status=200, payload=payload)


@pytest.fixture
def exec_config(tmp_path) -> Config:
    data = load_config().as_dict()
    data["reports"]["dir"] = str(tmp_path / "reports")
    data["execution"].update(
        enabled=True,
        autopilot=True,                 # per-order prompts are tested separately
        journal_path=str(tmp_path / "journal.jsonl"),
        kill_file=str(tmp_path / "KILL"),
        max_orders_per_day=3,
        max_new_exposure_pct=0.50,
        max_quote_drift_atr=1.0,
        max_quote_drift_pct=0.03,
        child_stop_type="stop",
        trading_hours_only=True,
    )
    data["schwab"].update(
        api_key="key", app_secret="secret", account_hash="HASH",
        token_path=str(tmp_path / "token.json"),
    )
    data["account"].update(equity=10_000.0, risk_pct=0.02, max_position_pct=0.25)
    cfg = Config(data)
    _write_token(cfg, age_days=1.0)
    return cfg


def _write_token(cfg: Config, age_days: float, ref: datetime = MARKET_OPEN):
    """Token aged relative to the simulated clock, not to wall-clock now.

    The whole suite runs against a fixed MARKET_OPEN so that trading-hours and
    sheet-freshness are deterministic; the token has to live on the same clock.
    """
    path = cfg.expand_path(cfg.schwab.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = (ref - timedelta(days=age_days)).timestamp()
    path.write_text(json.dumps({"creation_timestamp": created}))


def _pick(symbol="AAA", shares=20, close=100.0, stop=None, atr=2.0) -> Pick:
    stop = close - 2 * atr if stop is None else stop
    pick = Pick(
        symbol=symbol, rank=1, status=STATUS_TRADABLE, close=close, atr=atr,
        stop=stop, trail_offset=3 * atr, shares=shares, notional=shares * close,
        risk_dollars=shares * (close - stop), risk_pct=0.02,
        equity_pct=shares * close / 10_000.0, sizing_limit="risk",
    )
    if shares >= 1:
        pick.orders = draft_orders(symbol, shares, close, stop, 3 * atr)
    return pick


def _sheet(cfg: Config, picks=None, **kw) -> PickSheet:
    defaults = dict(
        as_of=str(MARKET_OPEN.date()),
        generated_at=MARKET_OPEN.isoformat(),
        equity=10_000.0, available_cash=10_000.0,
        regime_ok=True, regime_note="ok",
        gate_passed=True, gate_note="cleared",
        config_hash=cfg.hash, universe_size=500, candidates_considered=5,
    )
    defaults.update(kw)
    sheet = PickSheet(**defaults)
    sheet.picks = picks if picks is not None else [_pick()]
    return sheet


def _write(cfg: Config, sheet: PickSheet):
    out = write_sheet(cfg, sheet)
    return out / "picks.json"


# ---------------------------------------------------------------------------
# the two switches
# ---------------------------------------------------------------------------
def test_dry_run_is_the_default_and_transmits_nothing(exec_config, capsys):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()

    assert run_execute(exec_config, live=False, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 0
    assert client.placed == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Nothing was transmitted" in out


def test_live_flag_alone_is_not_enough(exec_config):
    """execution.enabled must also be true. Two independent affirmations."""
    data = exec_config.as_dict()
    data["execution"]["enabled"] = False
    cfg = Config(data)
    path = _write(cfg, _sheet(cfg))
    client = RecordingClient()

    assert run_execute(cfg, live=True, sheet_path=path, client=client, now=MARKET_OPEN) == 5
    assert client.placed == []


def test_config_enabled_alone_is_not_enough(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    run_execute(exec_config, live=False, sheet_path=path, client=client, now=MARKET_OPEN)
    assert client.placed == []


def test_both_switches_plus_clean_guardrails_places_the_order(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()

    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 0
    assert len(client.placed) == 1
    account_hash, order = client.placed[0]
    assert account_hash == "HASH"
    assert order["orderLegCollection"][0]["instrument"]["symbol"] == "AAA"
    assert order["orderType"] == "LIMIT"


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------
def test_kill_switch_blocks_everything(exec_config, capsys):
    set_kill_switch(exec_config)
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()

    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 6
    assert client.placed == []
    assert "KILL SWITCH ENGAGED" in capsys.readouterr().out


def test_kill_switch_round_trip(exec_config, capsys):
    assert not kill_switch_engaged(exec_config)
    set_kill_switch(exec_config)
    assert kill_switch_engaged(exec_config)
    release_kill_switch(exec_config)
    assert not kill_switch_engaged(exec_config)
    assert any(e.get("type") == "kill" for e in read_events(exec_config))


def test_kill_switch_says_it_does_not_cancel_resting_orders(exec_config, capsys):
    set_kill_switch(exec_config)
    assert "does NOT cancel orders already resting" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# pre-flight blocks
# ---------------------------------------------------------------------------
def test_expired_token_blocks_execution(exec_config):
    _write_token(exec_config, age_days=9.0)
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


def test_outside_trading_hours_blocks_execution(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    three_am = datetime(2024, 5, 1, 3, 0, tzinfo=EASTERN)
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=three_am) == 5
    assert client.placed == []


def test_trading_hours_check_can_be_disabled(exec_config):
    data = exec_config.as_dict()
    data["execution"]["trading_hours_only"] = False
    cfg = Config(data)
    path = _write(cfg, _sheet(cfg))
    client = RecordingClient()
    three_am = datetime(2024, 5, 1, 3, 0, tzinfo=EASTERN)
    # The sheet is dated for that day, so freshness still passes.
    run_execute(cfg, live=True, sheet_path=path, client=client, now=three_am)
    assert len(client.placed) == 1


def test_a_stale_sheet_blocks_execution(exec_config):
    sheet = _sheet(exec_config, as_of="2020-01-01")
    path = _write(exec_config, sheet)
    client = RecordingClient()
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


def test_a_sheet_from_a_different_config_blocks_execution(exec_config):
    """Edit a strategy parameter after scanning and the sheet is void."""
    sheet = _sheet(exec_config, config_hash="stale-hash")
    path = _write(exec_config, sheet)
    client = RecordingClient()
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


def test_a_force_generated_sheet_cannot_be_auto_executed(exec_config):
    """--force lets you look at unvalidated picks. It does not let a robot trade them."""
    sheet = _sheet(exec_config, gate_passed=False)
    path = _write(exec_config, sheet)
    client = RecordingClient()
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


def test_equity_mismatch_blocks_execution(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient(equity=1_000.0)      # config says 10,000
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


def test_small_equity_drift_is_tolerated(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient(equity=10_500.0)     # 5%, under the 20% tolerance
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 0
    assert len(client.placed) == 1


def test_reconciliation_mismatch_blocks_execution(exec_config):
    """The broker holds something the journal has never heard of."""
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient(positions={"ZZZ": 100})
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


def test_reconciliation_passes_when_journal_and_broker_agree(exec_config):
    record_entry(exec_config, "ZZZ", 100, 10.0, 9.0)
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient(positions={"ZZZ": 100})
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 0
    assert len(client.placed) == 1


def test_daily_order_cap_blocks_further_orders(exec_config):
    from swing.execution.journal import record

    for i in range(3):
        record(exec_config, EVENT_PLACED, symbol=f"X{i}", status="accepted",
               ts=MARKET_OPEN.isoformat())
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 5
    assert client.placed == []


# ---------------------------------------------------------------------------
# per-order blocks
# ---------------------------------------------------------------------------
def test_quote_drift_beyond_one_atr_skips_that_order(exec_config):
    from swing.data.provider import Quote

    pick = _pick()
    path = _write(exec_config, _sheet(exec_config, picks=[pick]))
    client = RecordingClient()
    quotes = {"AAA": Quote("AAA", pick.close + 3 * pick.atr)}

    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       quotes=quotes, now=MARKET_OPEN) == 0
    assert client.placed == []


def test_missing_quote_refuses_to_place_blind(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    run_execute(exec_config, live=True, sheet_path=path, client=client,
                quotes={}, now=MARKET_OPEN)
    assert client.placed == []


def test_duplicate_suppression_stops_a_second_run_doubling_the_position(exec_config):
    """Re-running execute after a timeout must not buy twice."""
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()

    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert len(client.placed) == 1

    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert len(client.placed) == 1        # not 2


def test_a_symbol_already_held_is_skipped(exec_config):
    record_entry(exec_config, "AAA", 5, 90.0, 85.0)
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient(positions={"AAA": 5})
    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert client.placed == []


def test_new_exposure_cap_stops_after_the_budget_is_spent(exec_config):
    """Cap is 50% of $10k = $5,000. Three $2,000 orders: only two fit."""
    picks = [_pick(symbol=s, shares=20, close=100.0) for s in ("AAA", "BBB", "CCC")]
    for i, pick in enumerate(picks, start=1):
        pick.rank = i
    path = _write(exec_config, _sheet(exec_config, picks=picks))
    client = RecordingClient()

    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert len(client.placed) == 2
    symbols = [o["orderLegCollection"][0]["instrument"]["symbol"] for _, o in client.placed]
    assert symbols == ["AAA", "BBB"]


def test_daily_cap_limits_a_long_sheet(exec_config):
    data = exec_config.as_dict()
    data["execution"]["max_orders_per_day"] = 2
    data["execution"]["max_new_exposure_pct"] = 10.0     # not the binding constraint
    cfg = Config(data)
    picks = [_pick(symbol=s, shares=5, close=50.0) for s in ("AAA", "BBB", "CCC", "DDD")]
    path = _write(cfg, _sheet(cfg, picks=picks))
    client = RecordingClient(prices=dict.fromkeys(("AAA", "BBB", "CCC", "DDD"), 50.0))

    run_execute(cfg, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert len(client.placed) == 2


def test_zero_share_picks_are_never_transmitted(exec_config):
    pick = _pick(shares=0)
    pick.orders = {}
    path = _write(exec_config, _sheet(exec_config, picks=[pick]))
    client = RecordingClient()
    assert run_execute(exec_config, live=True, sheet_path=path, client=client,
                       now=MARKET_OPEN) == 0
    assert client.placed == []


def test_an_invalid_drafted_order_is_rejected_before_transmission(exec_config):
    pick = _pick()
    # Corrupt the protective child so it covers fewer shares than the entry.
    pick.orders["bracket_stop"]["childOrderStrategies"][0][
        "orderLegCollection"
    ][0]["quantity"] = 1
    path = _write(exec_config, _sheet(exec_config, picks=[pick]))
    client = RecordingClient()
    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert client.placed == []


def test_unknown_child_stop_type_is_refused(exec_config):
    data = exec_config.as_dict()
    data["execution"]["child_stop_type"] = "hope"
    cfg = Config(data)
    assert choose_order_variant(cfg, _pick()) is None


@pytest.mark.parametrize(
    "kind,expected",
    [("stop", "STOP"), ("stop_limit", "STOP_LIMIT"), ("trailing_stop", "TRAILING_STOP")],
)
def test_configured_child_stop_type_selects_the_right_variant(exec_config, kind, expected):
    data = exec_config.as_dict()
    data["execution"]["child_stop_type"] = kind
    cfg = Config(data)
    _, order = choose_order_variant(cfg, _pick())
    assert order["childOrderStrategies"][0]["orderType"] == expected


# ---------------------------------------------------------------------------
# journalling
# ---------------------------------------------------------------------------
def test_a_placed_order_is_journalled_before_and_after_the_call(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)

    events = read_events(exec_config)
    statuses = [e.get("status") for e in events if e.get("type") == EVENT_PLACED]
    assert "submitting" in statuses      # written before the API call
    assert "accepted" in statuses        # written after
    assert "AAA" in open_positions(exec_config)


def test_a_rejected_order_is_journalled_and_leaves_no_position(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient(fail=True)
    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)

    events = read_events(exec_config)
    rejections = [e for e in events if e.get("type") == "rejected"]
    assert rejections
    assert any("HTTP 400" in str(e.get("reason", "")) for e in rejections)
    assert open_positions(exec_config) == {}


def test_missing_account_hash_is_caught_before_the_api_call(exec_config):
    data = exec_config.as_dict()
    data["schwab"]["account_hash"] = ""
    cfg = Config(data)
    path = _write(cfg, _sheet(cfg))
    client = RecordingClient()
    run_execute(cfg, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    assert client.placed == []


def test_the_order_id_is_taken_from_the_location_header(exec_config):
    path = _write(exec_config, _sheet(exec_config))
    client = RecordingClient()
    run_execute(exec_config, live=True, sheet_path=path, client=client, now=MARKET_OPEN)
    ids = [e.get("order_id") for e in read_events(exec_config) if e.get("order_id")]
    assert "999888" in ids


# ---------------------------------------------------------------------------
# journal semantics
# ---------------------------------------------------------------------------
def test_journal_replays_entries_and_exits(exec_config):
    record_entry(exec_config, "AAA", 10, 100.0, 95.0)
    assert open_positions(exec_config)["AAA"].shares == 10

    record_exit(exec_config, "AAA", 4, 105.0, reason="partial")
    assert open_positions(exec_config)["AAA"].shares == 6

    record_exit(exec_config, "AAA", 6, 106.0, reason="stop")
    assert "AAA" not in open_positions(exec_config)


def test_a_second_entry_averages_the_cost_basis(exec_config):
    record_entry(exec_config, "AAA", 10, 100.0, 95.0)
    record_entry(exec_config, "AAA", 10, 120.0, 110.0)
    position = open_positions(exec_config)["AAA"]
    assert position.shares == 20
    assert position.entry_price == pytest.approx(110.0)


def test_a_corrupt_journal_line_is_skipped_not_fatal(exec_config):
    record_entry(exec_config, "AAA", 10, 100.0, 95.0)
    path = exec_config.expand_path(exec_config.execution.journal_path)
    with path.open("a") as fh:
        fh.write("this is not json\n")
    record_entry(exec_config, "BBB", 5, 50.0, 45.0)
    assert set(open_positions(exec_config)) == {"AAA", "BBB"}


def test_journal_is_written_with_restrictive_permissions(exec_config):
    record_entry(exec_config, "AAA", 1, 10.0, 9.0)
    path = exec_config.expand_path(exec_config.execution.journal_path)
    assert oct(path.stat().st_mode)[-3:] == "600"


# ---------------------------------------------------------------------------
# guardrail units
# ---------------------------------------------------------------------------
def test_preflight_report_is_readable(exec_config):
    report = preflight(exec_config, live=True, sheet=_sheet(exec_config), now=MARKET_OPEN)
    text = report.describe()
    assert "kill switch" in text and "schwab token" in text


def test_check_order_report_names_each_blocker(exec_config):
    pick = _pick()
    report = check_order(exec_config, pick, quote_price=None, new_exposure_so_far=0.0,
                         equity=10_000.0, orders_placed_today=0, now=MARKET_OPEN)
    assert not report.passed
    assert any("no live quote" in g.detail for g in report.blockers)


def test_no_sheet_is_a_clear_error(exec_config, capsys):
    assert run_execute(exec_config, live=False, now=MARKET_OPEN) == 4
    assert "swing scan" in capsys.readouterr().out


def test_executor_prefers_the_confirmed_sheet(exec_config):
    sheet = _sheet(exec_config)
    out = write_sheet(exec_config, sheet)
    (out / "picks-confirmed.json").write_text(sheet.to_json())

    from swing.execution.executor import _newest_sheet

    assert _newest_sheet(exec_config).name == "picks-confirmed.json"


def test_today_order_count_only_counts_today(exec_config):
    from swing.execution.journal import placed_today, record

    record(exec_config, EVENT_PLACED, symbol="AAA")
    assert len(placed_today(exec_config, date.today())) == 1
    assert len(placed_today(exec_config, date(2020, 1, 1))) == 0
