"""The nightly scan, the pick sheet, alert fan-out and the pre-open confirm.

Everything runs against a seeded synthetic cache; no network, no SMTP, no
osascript. The alert channels are monkeypatched so that "a channel is broken"
can be tested as a first-class case — that is the failure mode most likely to
happen in real life at 17:30 on a Tuesday.
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from swing.alerts import dispatch
from swing.config import Config, load_config
from swing.data.cache import BarCache
from swing.data.provider import Fundamentals, Quote
from swing.picks import (
    STATUS_ADJUSTED,
    STATUS_CONFIRMED,
    STATUS_INVALIDATED,
    STATUS_TRADABLE,
    Pick,
    PickSheet,
    latest_sheet_path,
    load_sheet,
    write_sheet,
)
from swing.scan import run_scan

from .conftest import make_bars


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def scan_config(tmp_path) -> Config:
    """A config wired to a temp cache/reports/journal, with alerts off."""
    data = load_config().as_dict()
    data["data"]["cache_dir"] = str(tmp_path / "cache")
    data["reports"]["dir"] = str(tmp_path / "reports")
    data["execution"]["journal_path"] = str(tmp_path / "journal.jsonl")
    data["account"].update(equity=10_000.0, risk_pct=0.02, max_position_pct=0.25,
                           max_concurrent_positions=4)
    data["universe"].update(
        sp500=False, sp400=False, sp600=False, etfs=False,
        extra_symbols=[f"S{i:02d}" for i in range(6)],
    )
    data["backtest"]["gate"]["enabled"] = False      # gate is tested separately
    data["alerts"]["enabled"] = False
    data["strategy"]["regime"]["enabled"] = False
    data["strategy"]["trend_template"]["enabled"] = False
    data["strategy"]["entry"]["volume_mult"] = 0.0
    data["strategy"]["fundamentals"]["enabled"] = False
    return Config(data)


def _seed_cache(cfg: Config, breakout_on_last_bar: bool = True, n: int = 400):
    """Six symbols; optionally every one breaks out on the final bar."""
    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    rng = np.random.default_rng(17)
    for i in range(6):
        base = 20.0 + 15.0 * i
        closes = list(np.full(n - 1, base))
        closes.append(base * 1.10 if breakout_on_last_bar else base)
        cache.write(f"S{i:02d}", make_bars(closes, start="2020-01-01",
                                           volume=rng.uniform(3e6, 9e6, n)))
    cache.write("SPY", make_bars(list(np.linspace(200.0, 400.0, n)),
                                 start="2020-01-01", volume=1e8))
    cache.write_fundamentals(
        {f"S{i:02d}": Fundamentals(symbol=f"S{i:02d}", trailing_eps=1.0) for i in range(6)}
    )
    return cache


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------
def test_scan_produces_a_sheet_with_picks(scan_config, capsys):
    _seed_cache(scan_config)
    assert run_scan(scan_config, dry_run=True, refresh=False) == 0

    path = latest_sheet_path(scan_config)
    assert path is not None
    sheet = load_sheet(path)
    assert sheet.picks
    assert sheet.candidates_considered >= len(sheet.picks)
    assert all(p.shares >= 1 for p in sheet.picks)
    assert all(p.stop < p.close for p in sheet.picks)


def test_scan_writes_every_rendering_and_the_order_files(scan_config):
    _seed_cache(scan_config)
    run_scan(scan_config, dry_run=True, refresh=False)
    out = latest_sheet_path(scan_config).parent

    for name in ("picks.json", "picks.md", "picks.html", "picks.txt"):
        assert (out / name).exists(), name
    order_files = list((out / "orders").glob("*.json"))
    assert order_files
    for path in order_files:
        payload = json.loads(path.read_text())
        assert payload["orderStrategyType"] == "TRIGGER"


def test_picks_respect_the_concurrent_position_cap(scan_config):
    _seed_cache(scan_config)
    data = scan_config.as_dict()
    data["account"]["max_concurrent_positions"] = 2
    run_scan(Config(data), dry_run=True, refresh=False)
    sheet = load_sheet(latest_sheet_path(Config(data)))
    assert len(sheet.picks) <= 2


def test_picks_are_ranked_best_first(scan_config):
    _seed_cache(scan_config)
    run_scan(scan_config, dry_run=True, refresh=False)
    sheet = load_sheet(latest_sheet_path(scan_config))
    scores = [p.rank_score for p in sheet.picks]
    assert scores == sorted(scores, reverse=True)


def test_a_hundred_dollar_account_gets_a_watch_list_not_an_empty_sheet(scan_config):
    """The whole point of the watch list: found something, cannot buy most of it.

    The seeded symbols run from $20 to $105. At $100 of equity with a 25%
    position cap only the cheapest are buyable, and every rejection must state
    its reason in dollars rather than silently vanishing.
    """
    _seed_cache(scan_config)
    data = scan_config.as_dict()
    data["account"]["equity"] = 100.0
    cfg = Config(data)
    run_scan(cfg, dry_run=True, refresh=False)

    sheet = load_sheet(latest_sheet_path(cfg))
    assert sheet.watch
    assert all("$" in p.sizing_note for p in sheet.watch)      # says why, in dollars
    # Anything actually bought has to fit inside the cap.
    for pick in sheet.picks:
        assert pick.notional <= 100.0 * 0.25 + 1e-6
    # ...and the expensive names are the ones on the watch list.
    if sheet.picks:
        assert max(p.close for p in sheet.picks) < max(p.close for p in sheet.watch)


def test_a_tiny_account_against_expensive_names_finds_nothing_tradable(scan_config):
    _seed_cache(scan_config)
    data = scan_config.as_dict()
    data["account"]["equity"] = 40.0          # 25% cap = $10; nothing costs that little
    cfg = Config(data)
    run_scan(cfg, dry_run=True, refresh=False)

    sheet = load_sheet(latest_sheet_path(cfg))
    assert sheet.picks == []
    assert len(sheet.watch) == 6
    assert all("cap" in p.sizing_note or "cash" in p.sizing_note for p in sheet.watch)


def test_regime_off_blocks_entries_and_says_so(scan_config):
    _seed_cache(scan_config)
    cache = BarCache(scan_config.expand_path(scan_config.data.cache_dir))
    cache.write("SPY", make_bars(list(np.linspace(400.0, 200.0, 400)),
                                 start="2020-01-01", volume=1e8))
    data = scan_config.as_dict()
    data["strategy"]["regime"]["enabled"] = True
    cfg = Config(data)

    run_scan(cfg, dry_run=True, refresh=False)
    sheet = load_sheet(latest_sheet_path(cfg))
    assert not sheet.regime_ok
    assert sheet.picks == []
    # The watch reason must name the regime, not blame the account size.
    assert any("risk-off" in p.sizing_note for p in sheet.watch)


def test_open_positions_reduce_the_available_slots(scan_config):
    _seed_cache(scan_config)
    from swing.execution.journal import record_entry

    for i in range(3):
        record_entry(scan_config, f"S{i:02d}", 1, 20.0, 18.0)

    run_scan(scan_config, dry_run=True, refresh=False)
    sheet = load_sheet(latest_sheet_path(scan_config))
    assert len(sheet.picks) <= 1
    assert not any(p.symbol in {"S00", "S01", "S02"} for p in sheet.picks)
    assert sheet.holdings                      # the sheet reports what is held


def test_scan_reports_a_breached_stop_on_a_holding(scan_config):
    _seed_cache(scan_config)
    from swing.execution.journal import record_entry

    # Stop far above the current price: must be flagged.
    record_entry(scan_config, "S00", 1, 500.0, 499.0)
    run_scan(scan_config, dry_run=True, refresh=False)
    sheet = load_sheet(latest_sheet_path(scan_config))
    assert any(h.action == "STOP BREACHED" for h in sheet.holdings)


def test_stale_cache_produces_a_loud_warning(scan_config):
    _seed_cache(scan_config)                    # bars end in 2021
    run_scan(scan_config, dry_run=True, refresh=False)
    sheet = load_sheet(latest_sheet_path(scan_config))
    assert any("stale" in w or "days old" in w for w in sheet.warnings)


def test_empty_cache_exits_with_guidance(scan_config, capsys):
    assert run_scan(scan_config, dry_run=True, refresh=False) == 4
    assert "swing data --backfill" in capsys.readouterr().out


def test_no_signals_produces_an_empty_but_valid_sheet(scan_config):
    _seed_cache(scan_config, breakout_on_last_bar=False)
    assert run_scan(scan_config, dry_run=True, refresh=False) == 0
    sheet = load_sheet(latest_sheet_path(scan_config))
    assert sheet.picks == [] and sheet.watch == []
    assert "no candidates" in sheet.headline()


# ---------------------------------------------------------------------------
# the gate, from the scan's point of view
# ---------------------------------------------------------------------------
def test_scan_refuses_to_emit_picks_without_a_passing_backtest(scan_config, capsys):
    _seed_cache(scan_config)
    data = scan_config.as_dict()
    data["backtest"]["gate"]["enabled"] = True
    cfg = Config(data)

    assert run_scan(cfg, dry_run=True, refresh=False) == 3
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "swing backtest --walk-forward" in out
    assert latest_sheet_path(cfg) is None       # nothing was written at all


def test_force_overrides_the_gate_but_stamps_the_sheet(scan_config):
    _seed_cache(scan_config)
    data = scan_config.as_dict()
    data["backtest"]["gate"]["enabled"] = True
    cfg = Config(data)

    assert run_scan(cfg, dry_run=True, refresh=False, force=True) == 0
    sheet = load_sheet(latest_sheet_path(cfg))
    assert not sheet.gate_passed
    assert any("OVERRIDDEN" in w for w in sheet.warnings)
    assert "OVERRIDDEN" in sheet.gate_note


# ---------------------------------------------------------------------------
# pick sheet serialisation
# ---------------------------------------------------------------------------
def _sample_pick(**kw):
    defaults = dict(
        symbol="AAA", rank=1, status=STATUS_TRADABLE, close=100.0, atr=2.0, stop=96.0,
        trail_offset=6.0, shares=50, notional=5000.0, risk_dollars=200.0,
        risk_pct=0.02, equity_pct=0.5, sizing_limit="risk", thesis="20d breakout",
    )
    defaults.update(kw)
    return Pick(**defaults)


def _sample_sheet(**kw):
    defaults = dict(
        as_of="2024-05-01", generated_at="2024-05-01T17:30:00", equity=10_000.0,
        available_cash=10_000.0, regime_ok=True, regime_note="SPY above",
        gate_passed=True, gate_note="cleared", config_hash="abc123",
        universe_size=500, candidates_considered=3,
    )
    defaults.update(kw)
    sheet = PickSheet(**defaults)
    return sheet


def test_sheet_json_round_trip_preserves_everything():
    sheet = _sample_sheet()
    sheet.picks = [_sample_pick()]
    sheet.watch = [_sample_pick(symbol="BBB", rank=2, status="watch_unaffordable", shares=0)]

    back = PickSheet.from_json(sheet.to_json())
    assert back.as_of == sheet.as_of
    assert back.picks[0].symbol == "AAA"
    assert back.picks[0].shares == 50
    assert back.watch[0].shares == 0
    assert back.gate_passed is True


def test_sheet_renders_in_every_format():
    sheet = _sample_sheet()
    sheet.picks = [_sample_pick()]
    for text in (sheet.to_text(), sheet.to_markdown(), sheet.to_html()):
        assert "AAA" in text
    assert "<table>" in sheet.to_html()
    assert sheet.to_markdown().startswith("# swing pick sheet")


def test_headline_states_the_blocking_condition_first():
    assert "BLOCKED" in _sample_sheet(gate_passed=False).headline()
    assert "regime" in _sample_sheet(regime_ok=False).headline()
    assert "no candidates" in _sample_sheet().headline()


def test_sheet_text_always_carries_the_not_advice_line():
    assert "not advice" in _sample_sheet().to_text()


def test_write_sheet_creates_order_files_per_variant(scan_config):
    sheet = _sample_sheet()
    from swing.orders import draft_orders

    pick = _sample_pick()
    pick.orders = draft_orders("AAA", 50, 100.0, 96.0, 6.0)
    sheet.picks = [pick]
    out = write_sheet(scan_config, sheet)
    assert len(list((out / "orders").glob("AAA-*.json"))) == 3


# ---------------------------------------------------------------------------
# alert dispatch
# ---------------------------------------------------------------------------
def _alerts_on(cfg: Config, **channels) -> Config:
    data = cfg.as_dict()
    data["alerts"]["enabled"] = True
    for name, enabled in channels.items():
        data["alerts"][name]["enabled"] = enabled
    data["alerts"]["ntfy"]["topic"] = "swing-test-topic"
    data["alerts"]["email"].update(
        to_addrs=["someone@example.com"], from_addr="bot@example.com",
        username="bot@example.com", password="app-password",
    )
    return Config(data)


def test_every_enabled_channel_is_attempted(scan_config, monkeypatch):
    called = []
    monkeypatch.setattr(dispatch.ntfy, "send", lambda **kw: called.append("ntfy"))
    monkeypatch.setattr(dispatch.email_channel, "send", lambda **kw: called.append("email"))
    monkeypatch.setattr(dispatch.macos, "available", lambda: False)

    cfg = _alerts_on(scan_config, ntfy=True, email=True, macos=False, sms=False)
    results = dispatch.deliver(cfg, "title", "body")
    assert set(called) == {"ntfy", "email"}
    assert all(r.ok for r in results)


def test_one_broken_channel_does_not_stop_the_others(scan_config, monkeypatch):
    """The property that matters at 17:30 on a Tuesday."""
    sent = []

    def boom(**kw):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(dispatch.ntfy, "send", lambda **kw: sent.append("ntfy"))
    monkeypatch.setattr(dispatch.email_channel, "send", boom)
    monkeypatch.setattr(dispatch.macos, "available", lambda: False)

    cfg = _alerts_on(scan_config, ntfy=True, email=True, macos=False, sms=False)
    results = dispatch.deliver(cfg, "title", "body")

    assert sent == ["ntfy"]
    by_channel = {r.channel: r for r in results}
    assert by_channel["ntfy"].ok
    assert not by_channel["email"].ok
    assert "smtp is down" in by_channel["email"].detail


def test_deliver_never_raises_even_when_everything_fails(scan_config, monkeypatch):
    def boom(**kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(dispatch.ntfy, "send", boom)
    monkeypatch.setattr(dispatch.email_channel, "send", boom)
    monkeypatch.setattr(dispatch.macos, "available", lambda: False)

    cfg = _alerts_on(scan_config, ntfy=True, email=True, macos=False, sms=False)
    results = dispatch.deliver(cfg, "title", "body")
    assert not any(r.ok for r in results if not r.skipped)


def test_disabled_channels_are_reported_as_skipped_not_failed(scan_config):
    cfg = _alerts_on(scan_config, ntfy=False, email=False, macos=False, sms=False)
    results = dispatch.deliver(cfg, "t", "b")
    assert all(r.skipped for r in results)


def test_enabled_channel_with_missing_settings_fails_loudly(scan_config):
    data = _alerts_on(scan_config, ntfy=True, email=False, macos=False, sms=False).as_dict()
    data["alerts"]["ntfy"]["topic"] = ""
    results = dispatch.deliver(Config(data), "t", "b")
    ntfy_result = next(r for r in results if r.channel == "ntfy")
    assert not ntfy_result.ok and "topic" in ntfy_result.detail


def test_notify_test_returns_nonzero_when_nothing_is_configured(scan_config, capsys):
    data = scan_config.as_dict()
    data["alerts"]["enabled"] = True
    for channel in ("ntfy", "email", "sms", "macos"):
        data["alerts"][channel]["enabled"] = False
    assert dispatch.notify_test(Config(data)) == 1
    assert "No channels are enabled" in capsys.readouterr().out


def test_notify_test_reports_per_channel_failures(scan_config, monkeypatch, capsys):
    monkeypatch.setattr(dispatch.ntfy, "send", lambda **kw: None)
    monkeypatch.setattr(dispatch.macos, "available", lambda: False)
    cfg = _alerts_on(scan_config, ntfy=True, email=False, macos=False, sms=False)
    assert dispatch.notify_test(cfg) == 0
    assert "ntfy" in capsys.readouterr().out


def test_ntfy_titles_are_header_safe():
    from swing.alerts.ntfy import _header_safe

    assert "\n" not in _header_safe("line one\nline two")
    assert _header_safe("naïve") .isascii()


def test_macos_escapes_applescript_strings():
    from swing.alerts.macos import _escape

    assert _escape('say "hi"\nthen') == 'say \\"hi\\" then'


# ---------------------------------------------------------------------------
# pre-open confirm
# ---------------------------------------------------------------------------
def _confirm_setup(scan_config):
    _seed_cache(scan_config)
    run_scan(scan_config, dry_run=True, refresh=False)
    path = latest_sheet_path(scan_config)
    return path, load_sheet(path)


def test_confirm_leaves_an_unchanged_price_alone(scan_config):
    from swing.confirm import run_confirm

    path, sheet = _confirm_setup(scan_config)
    quotes = {p.symbol: Quote(p.symbol, p.close) for p in sheet.picks}
    assert run_confirm(scan_config, dry_run=True, sheet_path=path, quotes=quotes) == 0

    updated = load_sheet(path.parent / "picks-confirmed.json")
    assert all(p.status == STATUS_CONFIRMED for p in updated.picks)
    assert all(p.shares > 0 for p in updated.picks)


def test_confirm_cancels_a_pick_that_gapped_past_the_entry(scan_config):
    from swing.confirm import run_confirm

    path, sheet = _confirm_setup(scan_config)
    # Gap up two ATR: beyond the 1-ATR tolerance.
    quotes = {p.symbol: Quote(p.symbol, p.close + 2.5 * p.atr) for p in sheet.picks}
    run_confirm(scan_config, dry_run=True, sheet_path=path, quotes=quotes)

    updated = load_sheet(path.parent / "picks-confirmed.json")
    assert all(p.status == STATUS_INVALIDATED for p in updated.picks)
    assert all(p.shares == 0 and not p.orders for p in updated.picks)
    assert all("CANCELLED" in p.confirm_note for p in updated.picks)


def test_confirm_resizes_a_small_move_and_keeps_the_risk_budget(scan_config):
    from swing.confirm import run_confirm

    path, sheet = _confirm_setup(scan_config)
    quotes = {p.symbol: Quote(p.symbol, p.close * 1.01) for p in sheet.picks}
    run_confirm(scan_config, dry_run=True, sheet_path=path, quotes=quotes)

    updated = load_sheet(path.parent / "picks-confirmed.json")
    adjusted = [p for p in updated.picks if p.status == STATUS_ADJUSTED]
    assert adjusted
    budget = updated.equity * float(scan_config.account.risk_pct)
    for pick in adjusted:
        assert pick.risk_dollars <= budget + 1e-6
        assert pick.stop < pick.confirm_price


def test_confirm_rewrites_the_order_files_so_cancelled_picks_cannot_be_placed(scan_config):
    from swing.confirm import run_confirm

    path, sheet = _confirm_setup(scan_config)
    before = {p.name for p in (path.parent / "orders").glob("*.json")}
    assert before

    quotes = {p.symbol: Quote(p.symbol, p.close + 5 * p.atr) for p in sheet.picks}
    run_confirm(scan_config, dry_run=True, sheet_path=path, quotes=quotes)
    after = list((path.parent / "orders").glob("*.json"))
    assert after == []


def test_confirm_notes_a_missing_quote_instead_of_guessing(scan_config):
    from swing.confirm import run_confirm

    path, sheet = _confirm_setup(scan_config)
    run_confirm(scan_config, dry_run=True, sheet_path=path, quotes={})
    updated = load_sheet(path.parent / "picks-confirmed.json")
    assert any("no quote" in w for w in updated.warnings)


def test_confirm_without_a_sheet_is_a_clear_error(scan_config, capsys):
    from swing.confirm import run_confirm

    assert run_confirm(scan_config, dry_run=True) == 4
    assert "swing scan" in capsys.readouterr().out


def test_confirm_flags_delayed_quotes(scan_config):
    from swing.confirm import run_confirm

    path, sheet = _confirm_setup(scan_config)
    quotes = {p.symbol: Quote(p.symbol, p.close, stale=True) for p in sheet.picks}
    run_confirm(scan_config, dry_run=True, sheet_path=path, quotes=quotes)
    updated = load_sheet(path.parent / "picks-confirmed.json")
    assert any("delayed" in w for w in updated.warnings)


def test_sheet_freshness_check():
    from swing.confirm import sheet_is_for_today

    assert sheet_is_for_today(_sample_sheet(as_of=str(date.today())))
    assert not sheet_is_for_today(_sample_sheet(as_of="2020-01-01"))
    assert not sheet_is_for_today(_sample_sheet(as_of="not a date"))
