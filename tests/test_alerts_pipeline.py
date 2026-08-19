"""The scan and confirm pipeline, driven entirely by fakes.

Every consumed module — data, rules, scoring, regime, sizing, indicators, gate —
is replaced at ``pipeline``'s own lazy-import seam, so these tests describe what
the *pipeline* does rather than what its siblings compute. No network, no real
provider, and nothing written outside ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from conftest import build_config, make_bars
from swing import universe as universe_mod
from swing.alerts import channels, pipeline
from swing.state import Journal, PickRecord

ASOF = date(2026, 8, 18)
RICH_ACCOUNT = {"equity": 50_000.0}


# ---------------------------------------------------------------------------
# a synthetic world
# ---------------------------------------------------------------------------


def bars_ending_at(asof: date = ASOF, periods: int = 300, **kwargs: Any) -> pd.DataFrame:
    """Deterministic bars whose final row is exactly ``asof``."""
    index = pd.bdate_range(end=pd.Timestamp(asof), periods=periods)
    frame = make_bars(periods, start=index[0].strftime("%Y-%m-%d"), **kwargs)
    frame.index = index
    return frame


@dataclass
class World:
    """Everything the fake modules answer questions from."""

    symbols: tuple[str, ...] = ("AAA", "BBB", "CCC")
    etfs: tuple[str, ...] = ()
    regime_ok: bool = True
    atr: float = 2.0
    liquid: set[str] | None = None
    trending: set[str] | None = None
    signalling: set[str] | None = None
    scores: dict[str, float] = field(default_factory=dict)
    earnings: dict[str, date | None] = field(default_factory=dict)
    fundamentals_ok: dict[str, bool] = field(default_factory=dict)
    quotes: dict[str, float] = field(default_factory=dict)
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    provider_error: str | None = None

    def __post_init__(self) -> None:
        every = {*self.symbols, "SPY"}
        if not self.bars:
            for offset, symbol in enumerate(sorted(every)):
                frame = bars_ending_at(base=100.0 + offset, seed=7 + offset)
                frame.attrs["symbol"] = symbol
                self.bars[symbol] = frame
        for symbol, frame in self.bars.items():
            frame.attrs["symbol"] = symbol
        if self.liquid is None:
            self.liquid = set(self.symbols)
        if self.trending is None:
            self.trending = set(self.symbols)
        if self.signalling is None:
            self.signalling = set(self.symbols)
        if not self.scores:
            self.scores = {s: float(len(self.symbols) - i) for i, s in enumerate(self.symbols)}

    def kind(self, symbol: str) -> str:
        return "etf" if symbol in self.etfs else "stock"

    def close(self, symbol: str, asof: date = ASOF) -> float:
        frame = self.bars[symbol]
        return round(float(frame.loc[frame.index <= pd.Timestamp(asof), "close"].iloc[-1]), 2)

    def stop(self, symbol: str, atr_mult: float = 2.0, asof: date = ASOF) -> float:
        return round(self.close(symbol, asof) - atr_mult * self.atr, 2)


def _symbol_of(bars: pd.DataFrame) -> str:
    return str(bars.attrs.get("symbol", ""))


def _flags(bars: pd.DataFrame, value: bool) -> pd.Series:
    return pd.Series(value, index=bars.index, dtype=bool)


class FakeProvider:
    """A Contract 3 provider backed entirely by the World."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.bar_calls: list[tuple[list[str], date, date]] = []
        self.quote_calls: list[list[str]] = []

    def daily_bars(self, symbols, start, end):
        self.bar_calls.append((list(symbols), start, end))
        if self.world.provider_error == "bars":
            raise ConnectionError("yahoo is down")
        return {s: self.world.bars[s] for s in symbols if s in self.world.bars}

    def latest_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        if self.world.provider_error == "quotes":
            raise ConnectionError("no quotes")
        now = datetime(2026, 8, 19, 9, 0)
        return {
            s: SimpleNamespace(symbol=s, price=self.world.quotes[s], asof=now)
            for s in symbols
            if s in self.world.quotes
        }

    def earnings_dates(self, symbols):
        if self.world.provider_error == "earnings":
            raise ConnectionError("no earnings")
        return {s: self.world.earnings[s] for s in symbols if s in self.world.earnings}

    def fundamentals(self, symbols):
        if self.world.provider_error == "fundamentals":
            raise ConnectionError("no fundamentals")
        return {s: SimpleNamespace(symbol=s, eps_growth=0.1, revenue_growth=0.1) for s in symbols}


@dataclass(frozen=True)
class FakeSize:
    shares: int
    risk_amount: float
    notional: float
    affordable: bool
    capped_by: str | None


def make_deps(world: World) -> pipeline._Deps:
    """Build a ``_Deps`` whose every member is a faithful, boring fake."""

    def liquidity_ok(bars, cfg, *, is_etf):
        return _flags(bars, _symbol_of(bars) in world.liquid)

    def trend_template(bars, cfg, *, is_etf):
        return _flags(bars, _symbol_of(bars) in world.trending)

    def entry_signal(bars, cfg):
        return _flags(bars, _symbol_of(bars) in world.signalling)

    def initial_stop(bars, cfg):
        return bars["close"] - cfg.strategy.atr_stop_mult * world.atr

    def earnings_blackout(index, earnings, cfg):
        dates = pd.DatetimeIndex(index)
        if earnings is None:
            return pd.Series(False, index=dates, dtype=bool)
        days = (pd.Timestamp(earnings).normalize() - dates.normalize()).days
        blocked = [0 <= d <= cfg.strategy.earnings_blackout_days for d in days]
        return pd.Series(blocked, index=dates, dtype=bool)

    def fundamentals_ok(f, rank_below_median, cfg):
        symbol = getattr(f, "symbol", None)
        return world.fundamentals_ok.get(symbol, True)

    def rank_candidates(bars_by_symbol, asof, cfg):
        rows = [
            {
                "symbol": symbol,
                "score": world.scores.get(symbol, 0.0),
                "atr": world.atr,
                "close": world.close(symbol),
                "high_prox": 0.99,
            }
            for symbol in sorted(bars_by_symbol)
        ]
        if not rows:
            return pd.DataFrame(
                {c: pd.Series(dtype="float64") for c in ("score", "atr", "close", "high_prox")}
                | {"rank": pd.Series(dtype="int64")},
                index=pd.Index([], dtype="object", name="symbol"),
            )
        table = pd.DataFrame(rows).sort_values(["score", "symbol"], ascending=[False, True])
        table = table.set_index("symbol")
        table["rank"] = range(1, len(table) + 1)
        return table

    def entries_allowed(spy_bars, cfg):
        return _flags(spy_bars, world.regime_ok)

    def size_position(equity, cash, entry, stop, cfg):
        per_share = entry - stop
        budget = equity * cfg.account.risk_pct / 100.0
        shares = int(budget // per_share)
        capped_by: str | None = None
        cap = equity * cfg.account.max_position_pct / 100.0
        if shares * entry > cap:
            shares = int(cap // entry)
            capped_by = "position_cap"
        if shares * entry > cash:
            shares = int(cash // entry)
            capped_by = "cash"
        if shares < 1:
            return FakeSize(0, 0.0, 0.0, False, "unaffordable")
        return FakeSize(shares, shares * per_share, shares * entry, True, capped_by)

    def atr(bars, n=14):
        return pd.Series(world.atr, index=bars.index, dtype=float)

    def donchian_high(bars, n):
        return bars["close"] * 0.99

    provider = FakeProvider(world)
    return pipeline._Deps(
        get_provider=lambda cfg, **kw: provider,
        rules=SimpleNamespace(
            liquidity_ok=liquidity_ok,
            trend_template=trend_template,
            entry_signal=entry_signal,
            initial_stop=initial_stop,
            earnings_blackout=earnings_blackout,
            fundamentals_ok=fundamentals_ok,
        ),
        scoring=SimpleNamespace(rank_candidates=rank_candidates),
        regime=SimpleNamespace(entries_allowed=entries_allowed),
        sizing=SimpleNamespace(size_position=size_position),
        indicators=SimpleNamespace(atr=atr, donchian_high=donchian_high),
    )


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture notifications instead of sending them."""
    record: dict[str, list] = {"scan": [], "confirm": []}
    monkeypatch.setattr(
        channels,
        "deliver_scan",
        lambda cfg, report, **kw: record["scan"].append((report, kw)) or {"ntfy": True},
    )
    monkeypatch.setattr(
        channels,
        "deliver_confirm",
        lambda cfg, payload: record["confirm"].append(payload) or {"ntfy": True},
    )
    return record


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Install a World: fake deps, fake universe, and a gate with a chosen answer."""

    def _wire(world: World, *, gate_passed: bool = True, gate_reasons=(), gate_missing=False):
        deps = make_deps(world)
        monkeypatch.setattr(pipeline, "_load_deps", lambda: deps)
        monkeypatch.setattr(
            universe_mod,
            "load",
            lambda cfg: [
                universe_mod.Instrument(symbol=s, name=s, kind=world.kind(s), source=world.kind(s))
                for s in world.symbols
            ],
        )
        if gate_missing:
            monkeypatch.setitem(sys.modules, "swing.backtest.gate", None)
        else:
            module = types.ModuleType("swing.backtest.gate")
            module.check = lambda cfg: SimpleNamespace(
                passed=gate_passed, reasons=list(gate_reasons), report_path=None
            )
            monkeypatch.setitem(sys.modules, "swing.backtest.gate", module)
        return deps

    return _wire


def read_report(report_dir: Path) -> dict:
    return json.loads((report_dir / "picks.json").read_text())


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_dry_run_writes_the_whole_report_directory(tmp_path: Path, wire, sent) -> None:
    world = World()
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert report_dir == tmp_path / "reports" / "scan-2026-08-18"
    assert report_dir.is_dir()
    assert (report_dir / "picks.json").is_file()
    assert (report_dir / "picks.md").read_text().strip()
    assert (report_dir / "picks.html").read_text().strip()

    payload = read_report(report_dir)
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "BBB", "CCC"]
    order_files = sorted(p.name for p in (report_dir / "orders").glob("*.json"))
    assert order_files == ["AAA.json", "BBB.json", "CCC.json"]

    # dry run: nothing persisted, nothing sent
    assert not (Path(cfg.paths.state_dir) / "journal.json").exists()
    assert sent["scan"] == []


def test_picks_json_matches_contract_nine_exactly(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    assert set(payload) == {
        "generated_at",
        "asof",
        "equity",
        "regime_ok",
        "gate",
        "picks",
        "watch",
    }
    assert payload["asof"] == "2026-08-18"
    assert payload["equity"] == 50_000.0
    assert payload["regime_ok"] is True
    assert set(payload["gate"]) == {"passed", "reasons"}
    assert payload["gate"]["passed"] is True

    pick = payload["picks"][0]
    assert set(pick) == {
        "symbol",
        "date",
        "kind",
        "entry",
        "stop",
        "shares",
        "risk_amount",
        "score",
        "atr",
        "earnings_date",
        "earnings_known",
        "thesis",
        "status",
    }
    assert pick["kind"] == "pick"
    assert pick["status"] == "drafted"
    assert pick["date"] == "2026-08-18"
    assert pick["shares"] >= 1


def test_entry_stop_and_thesis_come_from_the_bars(tmp_path: Path, wire, sent) -> None:
    world = World(symbols=("AAA",))
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    (pick,) = payload["picks"]
    assert pick["entry"] == world.close("AAA")
    assert pick["stop"] == world.stop("AAA")
    assert pick["atr"] == pytest.approx(world.atr)
    assert "Broke the 20d high" in pick["thesis"]
    assert "momentum rank 1/1" in pick["thesis"]
    assert f"risk ${pick['entry'] - pick['stop']:.2f}/share" in pick["thesis"]
    assert pick["thesis"].endswith(".")


def test_orders_are_drafted_only_for_picks(tmp_path: Path, wire, sent) -> None:
    from swing.alerts import orders as orders_mod

    wire(World(symbols=("AAA",)))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    draft = json.loads((report_dir / "orders" / "AAA.json").read_text())
    assert orders_mod.validate_order_draft(draft) == []
    payload = read_report(report_dir)
    assert draft["oto_stop"]["price"] == f"{payload['picks'][0]['entry']:.2f}"


def test_symbols_are_processed_in_sorted_order(tmp_path: Path, wire, sent) -> None:
    deps = wire(World(symbols=("ZZZ", "AAA", "MMM")))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    provider = deps.get_provider(cfg)
    requested, start, end = provider.bar_calls[0]
    assert requested == sorted(requested)
    assert "SPY" in requested
    assert end == ASOF
    assert start == ASOF - timedelta(days=pipeline.SCAN_LOOKBACK_DAYS)


def test_two_runs_agree_on_everything_but_the_timestamp(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    first = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    second = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


# ---------------------------------------------------------------------------
# the $100 reality — the watch list
# ---------------------------------------------------------------------------


def test_hundred_dollar_account_produces_watch_entries_not_picks(
    tmp_path: Path, wire, sent
) -> None:
    wire(World())
    cfg = build_config(tmp_path)  # the default $100 account
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    payload = read_report(report_dir)

    assert payload["picks"] == []
    assert [w["symbol"] for w in payload["watch"]] == ["AAA", "BBB", "CCC"]
    assert all(w["kind"] == "watch" for w in payload["watch"])
    assert all(w["shares"] == 0 for w in payload["watch"])
    assert not list((report_dir / "orders").glob("*.json"))


def test_watch_entries_are_prominent_in_both_renderings(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    markdown = (report_dir / "picks.md").read_text()
    assert "## Watch — passed every rule, sized to zero shares (3)" in markdown
    assert "AAA" in markdown

    html = (report_dir / "picks.html").read_text()
    assert "Watch — passed every rule, sized to zero shares (3)" in html
    assert 'class="watch"' in html


def test_a_note_explains_why_everything_landed_on_the_watch_list(
    tmp_path: Path, wire, sent
) -> None:
    wire(World())
    cfg = build_config(tmp_path)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    assert "cannot buy one share" in (report_dir / "picks.md").read_text()


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_failing_gate_emits_no_picks_but_still_reports_and_notifies(
    tmp_path: Path, wire, sent
) -> None:
    wire(World(), gate_passed=False, gate_reasons=["Profit factor 0.9 is below the 1.3 minimum."])
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, asof=ASOF)
    payload = read_report(report_dir)

    assert payload["picks"] == []
    assert payload["watch"] == []
    assert payload["gate"] == {
        "passed": False,
        "reasons": ["Profit factor 0.9 is below the 1.3 minimum."],
    }
    assert "NOT PASSED" in (report_dir / "picks.md").read_text()
    assert len(sent["scan"]) == 1  # the "scan ran, gate failing" summary still goes out


def test_missing_gate_module_fails_closed(tmp_path: Path, wire, sent) -> None:
    wire(World(), gate_missing=True)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    assert payload["gate"]["passed"] is False
    assert payload["picks"] == []
    assert "not available in this checkout" in payload["gate"]["reasons"][0]


def test_a_raising_gate_fails_closed(tmp_path: Path, monkeypatch, wire, sent) -> None:
    wire(World())
    module = types.ModuleType("swing.backtest.gate")

    def explode(cfg):
        raise RuntimeError("latest.json is corrupt")

    module.check = explode
    monkeypatch.setitem(sys.modules, "swing.backtest.gate", module)

    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert payload["gate"]["passed"] is False
    assert "latest.json is corrupt" in payload["gate"]["reasons"][0]
    assert payload["picks"] == []


def test_force_emits_picks_over_a_failing_gate_and_says_so(tmp_path: Path, wire, sent) -> None:
    wire(World(), gate_passed=False, gate_reasons=["Only 4 trades; 30 are required."])
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, dry_run=True, force=True, asof=ASOF)
    payload = read_report(report_dir)

    assert len(payload["picks"]) == 3
    assert payload["gate"]["passed"] is False
    markdown = (report_dir / "picks.md").read_text()
    assert "UNVALIDATED" in markdown
    assert "--force was given" in markdown


def test_a_blocked_scan_still_reports_the_regime_honestly(tmp_path: Path, wire, sent) -> None:
    deps = wire(World(regime_ok=False), gate_passed=False)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    assert payload["regime_ok"] is False
    # Only the regime symbol was fetched — no point downloading 1,500 names.
    provider = deps.get_provider(cfg)
    assert provider.bar_calls[0][0] == ["SPY"]


# ---------------------------------------------------------------------------
# the regime
# ---------------------------------------------------------------------------


def test_regime_off_means_no_picks_and_an_explanation(tmp_path: Path, wire, sent) -> None:
    wire(World(regime_ok=False))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    payload = read_report(report_dir)

    assert payload["regime_ok"] is False
    assert payload["picks"] == []
    assert payload["watch"] == []
    markdown = (report_dir / "picks.md").read_text()
    assert "entries BLOCKED" in markdown
    assert "Positions you already hold are unaffected" in markdown


def test_missing_regime_data_blocks_entries(tmp_path: Path, wire, sent) -> None:
    world = World()
    del world.bars["SPY"]
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert payload["regime_ok"] is False
    assert payload["picks"] == []


# ---------------------------------------------------------------------------
# candidate filtering
# ---------------------------------------------------------------------------


def test_each_rule_can_remove_a_candidate(tmp_path: Path, wire, sent) -> None:
    world = World(liquid={"BBB", "CCC"}, trending={"AAA", "CCC"}, signalling={"AAA", "BBB", "CCC"})
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["CCC"]


def test_no_survivors_is_explained_not_silent(tmp_path: Path, wire, sent) -> None:
    wire(World(signalling=set()))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    assert read_report(report_dir)["picks"] == []
    assert "None of the 3 symbols scanned passed" in (report_dir / "picks.md").read_text()


def test_earnings_blackout_removes_a_candidate(tmp_path: Path, wire, sent) -> None:
    wire(World(earnings={"AAA": ASOF + timedelta(days=3), "BBB": ASOF + timedelta(days=90)}))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    symbols = [p["symbol"] for p in payload["picks"]]
    assert "AAA" not in symbols
    assert symbols == ["BBB", "CCC"]


def test_known_and_unknown_earnings_dates_are_both_recorded(tmp_path: Path, wire, sent) -> None:
    wire(World(symbols=("AAA", "BBB"), earnings={"AAA": date(2026, 9, 30)}))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    by_symbol = {p["symbol"]: p for p in payload["picks"]}
    assert by_symbol["AAA"]["earnings_date"] == "2026-09-30"
    assert by_symbol["AAA"]["earnings_known"] is True
    assert by_symbol["BBB"]["earnings_date"] is None
    assert by_symbol["BBB"]["earnings_known"] is False


def test_unknown_earnings_are_flagged_in_the_report(tmp_path: Path, wire, sent) -> None:
    wire(World(symbols=("AAA",)))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    assert "UNKNOWN" in (report_dir / "picks.md").read_text()
    assert "UNKNOWN" in (report_dir / "picks.html").read_text()


def test_fundamentals_screen_can_reject_a_stock(tmp_path: Path, wire, sent) -> None:
    wire(World(fundamentals_ok={"BBB": False}))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "CCC"]


def test_a_provider_failure_is_treated_as_fundamentals_ok(tmp_path: Path, wire, sent) -> None:
    wire(World(provider_error="fundamentals"))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert len(payload["picks"]) == 3


def test_etfs_skip_the_fundamentals_screen(tmp_path: Path, wire, sent) -> None:
    wire(World(symbols=("SPYX",), etfs=("SPYX",), fundamentals_ok={"SPYX": False}))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["SPYX"]


def test_a_bars_outage_is_reported_rather_than_raised(tmp_path: Path, wire, sent) -> None:
    wire(World(provider_error="bars"))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    assert read_report(report_dir)["picks"] == []
    assert "No price history came back" in (report_dir / "picks.md").read_text()


# ---------------------------------------------------------------------------
# dedupe and slots
# ---------------------------------------------------------------------------


def seed_journal(cfg, records: list[PickRecord]) -> Journal:
    journal = Journal.load(cfg)
    journal.add_picks(records)
    return journal


def picked_record(symbol: str, day: date, status: str = "drafted") -> PickRecord:
    return PickRecord(
        symbol=symbol,
        date=day.isoformat(),
        kind="pick",
        entry=100.0,
        stop=96.0,
        shares=5,
        risk_amount=20.0,
        score=1.0,
        atr=2.0,
        earnings_date=None,
        earnings_known=False,
        thesis="earlier pick",
        status=status,
    )


def test_a_symbol_picked_in_the_last_week_is_skipped(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    seed_journal(cfg, [picked_record("BBB", ASOF - timedelta(days=3))])

    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "CCC"]


def test_a_symbol_picked_more_than_a_week_ago_is_eligible_again(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    seed_journal(cfg, [picked_record("BBB", ASOF - timedelta(days=8))])

    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "BBB", "CCC"]


def test_dedupe_window_is_seven_days(tmp_path: Path) -> None:
    assert pipeline.DEDUPE_WITHIN_DAYS == 7


def test_open_positions_consume_slots(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT | {"max_positions": 2})
    journal = seed_journal(cfg, [picked_record("ZZZ", ASOF - timedelta(days=30))])
    journal.update_status("ZZZ", ASOF - timedelta(days=30), "filled")

    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["AAA"]  # 2 slots - 1 held = 1


def test_a_full_book_proposes_nothing_and_says_why(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT | {"max_positions": 1})
    journal = seed_journal(cfg, [picked_record("ZZZ", ASOF - timedelta(days=30))])
    journal.update_status("ZZZ", ASOF - timedelta(days=30), "filled")

    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    assert read_report(report_dir)["picks"] == []
    assert "position slots are full" in (report_dir / "picks.md").read_text()


def test_open_notional_reduces_the_cash_available(tmp_path: Path, wire, sent) -> None:
    """A held position's cost is not available to spend twice."""
    wire(World(symbols=("AAA",)))
    cfg = build_config(tmp_path, account={"equity": 1000.0, "max_position_pct": 100.0})
    journal = seed_journal(cfg, [picked_record("ZZZ", ASOF - timedelta(days=30))])
    journal.update_status("ZZZ", ASOF - timedelta(days=30), "filled")  # 5 x $100 = $500

    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    (pick,) = payload["picks"]
    assert pick["shares"] * pick["entry"] <= 500.0


# ---------------------------------------------------------------------------
# side effects
# ---------------------------------------------------------------------------


def test_a_real_run_journals_everything_and_notifies_once(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    pipeline.run_scan(cfg, asof=ASOF)

    journal = Journal.load(cfg)
    assert [p.symbol for p in journal.picks_for(ASOF)] == ["AAA", "BBB", "CCC"]
    assert all(p.status == "drafted" for p in journal.picks_for(ASOF))

    assert len(sent["scan"]) == 1
    report, kwargs = sent["scan"][0]
    assert report["asof"] == "2026-08-18"
    assert set(kwargs["orders"]) == {"AAA", "BBB", "CCC"}


def test_watch_entries_are_journalled_too(tmp_path: Path, wire, sent) -> None:
    wire(World())
    cfg = build_config(tmp_path)  # $100 account: everything is a watch entry
    pipeline.run_scan(cfg, asof=ASOF)
    assert [p.kind for p in Journal.load(cfg).picks_for(ASOF)] == ["watch"] * 3


def test_asof_defaults_to_today_in_the_configured_timezone(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    wire(World())
    monkeypatch.setattr(pipeline, "_today", lambda cfg: date(2026, 3, 4))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    assert pipeline.run_scan(cfg, dry_run=True).name == "scan-2026-03-04"


def test_today_uses_the_configured_timezone(tmp_path: Path) -> None:
    cfg = build_config(tmp_path, schedule={"timezone": "Pacific/Kiritimati"})
    other = build_config(tmp_path, schedule={"timezone": "Pacific/Midway"})
    # Two zones a day apart: whatever "today" is, they cannot both be wrong.
    assert (pipeline._today(cfg) - pipeline._today(other)).days in (0, 1)


# ---------------------------------------------------------------------------
# confirmation
# ---------------------------------------------------------------------------


def prepare_scan(tmp_path: Path, wire, world: World | None = None, **cfg_kwargs):
    """Run a scan so there is something to confirm, and return (cfg, world)."""
    world = world or World(symbols=("AAA",))
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT, **cfg_kwargs)
    pipeline.run_scan(cfg, asof=ASOF)
    return cfg, world


def test_confirm_keeps_a_pick_that_has_barely_moved(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    payload = read_report(tmp_path / "reports" / "scan-2026-08-18")
    pick = payload["picks"][0]
    world.quotes = {"AAA": pick["entry"] + 0.99 * pick["atr"]}

    confirm_path = pipeline.run_confirm(cfg)
    result = json.loads(confirm_path.read_text())["results"]["AAA"]

    assert result["status"] == "confirmed"
    assert result["quote"] == pytest.approx(pick["entry"] + 0.99 * pick["atr"])
    assert "within" in result["reason"]
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "confirmed"


def test_confirm_invalidates_a_pick_that_gapped_past_the_entry(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    payload = read_report(tmp_path / "reports" / "scan-2026-08-18")
    pick = payload["picks"][0]
    world.quotes = {"AAA": pick["entry"] + 1.01 * pick["atr"]}

    confirm_path = pipeline.run_confirm(cfg)
    result = json.loads(confirm_path.read_text())["results"]["AAA"]

    assert result["status"] == "invalidated"
    assert "happened without us" in result["reason"]
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "invalidated"


def test_confirm_writes_the_documented_shape(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    world.quotes = {"AAA": 100.0}
    confirm_path = pipeline.run_confirm(cfg)

    assert confirm_path.name == "confirm.json"
    assert confirm_path.parent.name == "scan-2026-08-18"
    payload = json.loads(confirm_path.read_text())
    assert set(payload) == {"asof", "results"}
    assert set(payload["results"]["AAA"]) == {"quote", "status", "reason"}
    assert (confirm_path.parent / "confirm.md").read_text().strip()


def test_confirm_handles_a_missing_quote(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    world.quotes = {}
    result = json.loads(pipeline.run_confirm(cfg).read_text())["results"]["AAA"]

    assert result["status"] == "unknown"
    assert result["quote"] is None
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "drafted"  # left alone


def test_confirm_survives_a_quote_outage(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    world.provider_error = "quotes"
    assert (
        json.loads(pipeline.run_confirm(cfg).read_text())["results"]["AAA"]["status"] == "unknown"
    )


def test_confirm_notifies_unless_dry_run(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    world.quotes = {"AAA": 100.0}

    pipeline.run_confirm(cfg, dry_run=True)
    assert sent["confirm"] == []
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "drafted"

    pipeline.run_confirm(cfg)
    assert len(sent["confirm"]) == 1
    assert set(sent["confirm"][0]) == {"asof", "results"}


def test_confirm_only_looks_at_drafted_and_confirmed_picks(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    scan_dir = tmp_path / "reports" / "scan-2026-08-18"
    payload = json.loads((scan_dir / "picks.json").read_text())
    payload["picks"][0]["status"] = "closed"
    (scan_dir / "picks.json").write_text(json.dumps(payload))

    world.quotes = {"AAA": 100.0}
    assert json.loads(pipeline.run_confirm(cfg).read_text())["results"] == {}


def test_confirm_uses_the_most_recent_scan(tmp_path: Path, wire, sent) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    for name in ("scan-2026-08-01", "scan-2026-07-04"):
        older = tmp_path / "reports" / name
        older.mkdir()
        (older / "picks.json").write_text('{"picks": []}')

    assert pipeline.latest_scan_dir(cfg).name == "scan-2026-08-18"
    world.quotes = {"AAA": 100.0}
    assert pipeline.run_confirm(cfg).parent.name == "scan-2026-08-18"


def test_confirm_without_a_scan_says_so_plainly(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    with pytest.raises(pipeline.ScanError, match="no scan-YYYY-MM-DD folder"):
        pipeline.run_confirm(cfg)


def test_confirm_with_an_unreadable_report_says_so_plainly(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    scan_dir = tmp_path / "reports" / "scan-2026-08-18"
    scan_dir.mkdir(parents=True)
    (scan_dir / "picks.json").write_text("{not json")
    with pytest.raises(pipeline.ScanError, match="could not be read"):
        pipeline.run_confirm(cfg)


def test_latest_scan_dir_ignores_junk(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    reports = Path(cfg.paths.reports_dir)
    (reports / "backtest").mkdir()
    (reports / "scan-nope").mkdir()
    (reports / "scan-2026-08-18").mkdir()  # no picks.json inside
    assert pipeline.latest_scan_dir(cfg) is None


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------


def test_a_missing_data_layer_is_explained_when_picks_are_allowed(
    tmp_path: Path, monkeypatch, wire, sent
) -> None:
    wire(World())

    def missing() -> None:
        raise pipeline.ScanError("The scan cannot run because part of the system is missing.")

    monkeypatch.setattr(pipeline, "_load_deps", missing)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    with pytest.raises(pipeline.ScanError, match="part of the system is missing"):
        pipeline.run_scan(cfg, dry_run=True, asof=ASOF)


def test_a_missing_data_layer_still_reports_when_the_gate_blocks(
    tmp_path: Path, monkeypatch, wire, sent
) -> None:
    wire(World(), gate_passed=False)

    def missing() -> None:
        raise pipeline.ScanError("swing.data is not installed in this checkout.")

    monkeypatch.setattr(pipeline, "_load_deps", missing)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert read_report(report_dir)["picks"] == []
    assert "swing.data is not installed" in (report_dir / "picks.md").read_text()


def test_an_impossible_stop_drops_the_candidate_with_a_note(
    tmp_path: Path, monkeypatch, wire, sent
) -> None:
    deps = wire(World(symbols=("AAA",)))
    monkeypatch.setattr(
        deps.rules, "initial_stop", lambda bars, cfg: bars["close"] * 1.5, raising=False
    )
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert read_report(report_dir)["picks"] == []
    assert "not below the entry" in (report_dir / "picks.md").read_text()


# ---------------------------------------------------------------------------
# integration smoke test — real sibling modules, fake provider
# ---------------------------------------------------------------------------


def real_modules_or_skip():
    """Import the real siblings, skipping the test cleanly when one is absent."""
    pytest.importorskip("swing.strategy.rules")
    pytest.importorskip("swing.strategy.scoring")
    pytest.importorskip("swing.strategy.regime")
    pytest.importorskip("swing.strategy.sizing")
    pytest.importorskip("swing.indicators")
    return pytest.importorskip("swing.data")


def breakout_bars(base: float, seed: int, periods: int = 420) -> pd.DataFrame:
    """A long, steady uptrend whose final bar breaks the 20-day high on 3x volume.

    Engineered rather than random so the real rule set has something it must
    fire on: the trend template needs 252 bars of history stacked in order, and
    ``entry_signal`` needs a genuine donchian breakout with volume confirmation.
    """
    frame = bars_ending_at(periods=periods, trend=0.0015, base=base, seed=seed)
    last = frame.index[-1]
    prior_high = float(frame["high"].iloc[-21:-1].max())
    close = prior_high * 1.03
    frame.loc[last, "open"] = prior_high * 1.005
    frame.loc[last, "close"] = close
    frame.loc[last, "high"] = close * 1.005
    frame.loc[last, "low"] = prior_high
    frame.loc[last, "volume"] = float(frame["volume"].iloc[-51:-1].mean()) * 3.0
    return frame


def wire_real_modules(monkeypatch, world: World) -> None:
    data_mod = real_modules_or_skip()
    provider = FakeProvider(world)
    monkeypatch.setattr(data_mod, "get_provider", lambda cfg, **kw: provider)
    monkeypatch.setattr(
        universe_mod,
        "load",
        lambda cfg: [
            universe_mod.Instrument(symbol=s, name=s, kind="stock", source="sp500")
            for s in world.symbols
        ],
    )


def breakout_world() -> World:
    bars = {
        "AAA": breakout_bars(base=40.0, seed=11),
        "BBB": breakout_bars(base=55.0, seed=12),
        "SPY": bars_ending_at(periods=420, trend=0.0015, base=300.0, seed=13),
    }
    return World(symbols=("AAA", "BBB"), bars=bars)


def test_the_real_modules_fit_the_pipeline_seams(tmp_path: Path, monkeypatch, sent) -> None:
    """Guarded smoke test: the frozen signatures actually line up in practice.

    Everything above runs against fakes on purpose. This one runs the *real*
    data, strategy, indicator, sizing and gate modules — only the provider is
    faked, because the network is off — so a signature drift anywhere in the
    chain fails here rather than at 17:30 on a Tuesday.
    """
    world = breakout_world()
    wire_real_modules(monkeypatch, world)

    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    # --force, because the real gate correctly refuses: no backtest has ever been
    # run inside this tmp_path. Forcing is the only way to reach the code below it.
    report_dir = pipeline.run_scan(cfg, dry_run=True, force=True, asof=ASOF)
    payload = read_report(report_dir)

    assert set(payload) == {
        "generated_at",
        "asof",
        "equity",
        "regime_ok",
        "gate",
        "picks",
        "watch",
    }
    assert payload["regime_ok"] is True
    assert payload["gate"]["passed"] is False  # nothing has been backtested here
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "BBB"]
    assert (report_dir / "picks.md").read_text().strip()
    assert (report_dir / "picks.html").read_text().strip()

    from swing.alerts import orders as orders_mod

    for pick in payload["picks"]:
        assert pick["shares"] >= 1
        assert pick["stop"] < pick["entry"]
        assert pick["atr"] > 0
        assert "Broke the 20d high" in pick["thesis"]
        draft = json.loads((report_dir / "orders" / f"{pick['symbol']}.json").read_text())
        assert orders_mod.validate_order_draft(draft) == []


def test_the_real_sizing_sends_a_hundred_dollar_account_to_the_watch_list(
    tmp_path: Path, monkeypatch, sent
) -> None:
    """AC6/AC11 end to end: the real sizing module, the real $100 outcome."""
    wire_real_modules(monkeypatch, breakout_world())

    cfg = build_config(tmp_path)  # the default $100 account
    report_dir = pipeline.run_scan(cfg, dry_run=True, force=True, asof=ASOF)
    payload = read_report(report_dir)

    assert payload["picks"] == []
    assert [w["symbol"] for w in payload["watch"]] == ["AAA", "BBB"]
    assert all(w["shares"] == 0 for w in payload["watch"])
    assert not list((report_dir / "orders").glob("*.json"))
    assert "sized to zero shares (2)" in (report_dir / "picks.md").read_text()


def test_the_real_confirm_path_runs_against_a_real_journal(
    tmp_path: Path, monkeypatch, sent
) -> None:
    """A scan and its confirmation, back to back, with nothing but the provider faked."""
    world = breakout_world()
    wire_real_modules(monkeypatch, world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    pipeline.run_scan(cfg, force=True, asof=ASOF)
    picks = {p["symbol"]: p for p in read_report(tmp_path / "reports" / "scan-2026-08-18")["picks"]}
    world.quotes = {
        "AAA": picks["AAA"]["entry"] + 0.5 * picks["AAA"]["atr"],
        "BBB": picks["BBB"]["entry"] + 2.0 * picks["BBB"]["atr"],
    }

    results = json.loads(pipeline.run_confirm(cfg).read_text())["results"]
    assert results["AAA"]["status"] == "confirmed"
    assert results["BBB"]["status"] == "invalidated"

    journal = {p.symbol: p.status for p in Journal.load(cfg).picks_for(ASOF)}
    assert journal == {"AAA": "confirmed", "BBB": "invalidated"}
