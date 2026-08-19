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

    # "dry_run" joined the payload with audit BUG-019: a dry-run report used to
    # be indistinguishable from a real one, so the morning confirm consumed it.
    assert set(payload) == {
        "generated_at",
        "asof",
        "equity",
        "regime_ok",
        "dry_run",
        "gate",
        "picks",
        "watch",
    }
    assert payload["asof"] == "2026-08-18"
    assert payload["equity"] == 50_000.0
    assert payload["regime_ok"] is True
    assert payload["dry_run"] is True
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
    # "skipped" joined the payload with audit BUG-018/BUG-019: a pick the confirm
    # deliberately left alone, or could not journal, is now visible instead of
    # being a swallowed exception in a log file.
    assert set(payload) == {"asof", "results", "skipped"}
    assert payload["skipped"] == {}
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
    assert set(sent["confirm"][0]) == {"asof", "results", "skipped"}


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

    assert pipeline.latest_scan_dir(Path(cfg.paths.reports_dir)).name == "scan-2026-08-18"
    world.quotes = {"AAA": 100.0}
    assert pipeline.run_confirm(cfg).parent.name == "scan-2026-08-18"


def test_confirm_without_a_scan_says_so_plainly(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    with pytest.raises(pipeline.ScanError, match="no scan-YYYY-MM-DD folder"):
        pipeline.run_confirm(cfg)


def test_confirm_skips_an_unreadable_report(tmp_path: Path) -> None:
    """A report whose picks.json will not parse is a crash artefact (audit DEBT-001)."""
    cfg = build_config(tmp_path)
    scan_dir = tmp_path / "reports" / "scan-2026-08-18"
    scan_dir.mkdir(parents=True)
    (scan_dir / "picks.json").write_text("{not json")
    with pytest.raises(pipeline.ScanError, match="readable picks.json"):
        pipeline.run_confirm(cfg)


def test_latest_scan_dir_ignores_junk(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    reports = Path(cfg.paths.reports_dir)
    (reports / "backtest").mkdir()
    (reports / "scan-nope").mkdir()
    (reports / "scan-2026-08-18").mkdir()  # no picks.json inside
    assert pipeline.latest_scan_dir(reports) is None


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
        "dry_run",  # audit BUG-019
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


# ---------------------------------------------------------------------------
# audit regressions — every test here fails on the pre-remediation behaviour
# ---------------------------------------------------------------------------


def test_a_same_day_rerun_reproduces_the_same_report(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-010: the second run of an evening used to wipe the first one.

    Run 1 journalled its picks; run 2 deduped every one of them away (delta == 0)
    and overwrote the same directory with "Nothing is tradable tonight", leaving
    run 1's order drafts orphaned beside it.
    """
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    first_dir = pipeline.run_scan(cfg, asof=ASOF)
    first = read_report(first_dir)
    first_orders = {p.name: p.read_text() for p in sorted((first_dir / "orders").glob("*.json"))}

    second_dir = pipeline.run_scan(cfg, asof=ASOF)
    second = read_report(second_dir)
    second_orders = {p.name: p.read_text() for p in sorted((second_dir / "orders").glob("*.json"))}

    assert second_dir == first_dir
    assert [p["symbol"] for p in second["picks"]] == ["AAA", "BBB", "CCC"]
    assert second["picks"] == first["picks"]
    assert second_orders == first_orders != {}


def test_a_rerun_clears_order_drafts_from_the_previous_run(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-010: orders/ must never describe a report that no longer exists."""
    wire(World(symbols=("AAA", "BBB")))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    orphan = report_dir / "orders" / "ZZZ.json"
    orphan.write_text('{"oto_stop": {}}', encoding="utf-8")

    pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert not orphan.exists()
    assert sorted(p.name for p in (report_dir / "orders").glob("*.json")) == [
        "AAA.json",
        "BBB.json",
    ]


def test_a_watch_entry_does_not_suppress_the_symbol_the_next_day(
    tmp_path: Path, wire, sent
) -> None:
    """Audit BUG-011: printing a name on the watch list blocked it for seven days.

    On the configured $100 account every qualifying name is a watch entry, so
    within a week the watch list — the whole output of a small account — emptied
    itself.
    """
    wire(World())
    poor = build_config(tmp_path)  # $100: everything sizes to zero shares
    pipeline.run_scan(poor, asof=ASOF)
    assert [p.kind for p in Journal.load(poor).picks_for(ASOF)] == ["watch"] * 3

    rich = build_config(tmp_path, account=RICH_ACCOUNT)  # the money arrived on day 2
    payload = read_report(pipeline.run_scan(rich, dry_run=True, asof=ASOF + timedelta(days=1)))
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "BBB", "CCC"]


def test_a_real_pick_still_blocks_its_symbol_for_the_week(tmp_path: Path, wire, sent) -> None:
    """The other half of BUG-011: dedupe still means "we committed capital here"."""
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    seed_journal(cfg, [picked_record("BBB", ASOF - timedelta(days=2))])

    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))
    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "CCC"]


def test_dedupe_only_asks_the_journal_about_symbols_it_actually_holds(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit PERF-008/DEBT-017: O(candidates x journal), plus a per-candidate
    ``inspect.signature`` probe of a method whose signature never changes."""
    calls: list[tuple[str, dict]] = []

    class CountingJournal(Journal):
        def recently_picked(self, symbol, within_days, **kwargs):
            calls.append((symbol, dict(kwargs)))
            return super().recently_picked(symbol, within_days, **kwargs)

    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    seed_journal(cfg, [picked_record("BBB", ASOF - timedelta(days=30))])
    monkeypatch.setattr(pipeline, "Journal", CountingJournal)

    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    assert [p["symbol"] for p in payload["picks"]] == ["AAA", "BBB", "CCC"]
    assert [symbol for symbol, _kw in calls] == ["BBB"]  # AAA and CCC are not in the journal
    assert calls[0][1] == {"asof": ASOF}  # passed directly, and kinds stays at its default
    assert not hasattr(pipeline, "_recently_picked")  # the compat shim is gone


def test_a_missing_regime_symbol_is_a_data_failure_not_a_market_reading(
    tmp_path: Path, wire, sent
) -> None:
    """Audit BUG-015: an absent SPY was reported as "the regime gate is OFF"."""
    world = World()
    del world.bars["SPY"]
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    markdown = (report_dir / "picks.md").read_text()

    assert read_report(report_dir)["regime_ok"] is False
    assert "No price history came back for SPY" in markdown
    assert "DATA problem" in markdown
    assert "is not above its" not in markdown  # the confident market statement is gone


def test_a_missing_regime_symbol_is_reported_even_when_the_gate_blocks(
    tmp_path: Path, wire, sent
) -> None:
    """The gate-blocked path fetches only the regime symbol — and must say so too."""
    world = World()
    del world.bars["SPY"]
    wire(world, gate_passed=False)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    assert "No price history came back for SPY" in (report_dir / "picks.md").read_text()


def test_the_regime_off_wording_is_unchanged_when_the_data_is_there(
    tmp_path: Path, wire, sent
) -> None:
    wire(World(regime_ok=False))
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    markdown = (pipeline.run_scan(cfg, dry_run=True, asof=ASOF) / "picks.md").read_text()
    assert "is not above its" in markdown
    assert "DATA problem" not in markdown


# ---------------------------------------------------------------------------
# report writing (audit BUG-019, BUG-020, LEAK-002)
# ---------------------------------------------------------------------------


def test_a_real_run_is_stamped_as_not_a_dry_run(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-019: the two kinds of report used to be indistinguishable."""
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    assert read_report(pipeline.run_scan(cfg, asof=ASOF))["dry_run"] is False
    assert read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))["dry_run"] is True


def test_picks_json_is_written_last(tmp_path: Path, wire, sent, monkeypatch) -> None:
    """Audit BUG-020: picks.json is the directory's commit point."""
    written: list[str] = []
    real_write = pipeline._write

    def recording(path: Path, text: str) -> None:
        written.append(path.name)
        real_write(path, text)

    wire(World(symbols=("AAA",)))
    monkeypatch.setattr(pipeline, "_write", recording)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert written[-1] == "picks.json"
    assert written.index("AAA.json") < written.index("picks.json")
    assert written.index("picks.md") < written.index("picks.json")


def test_a_crash_mid_report_leaves_no_picks_json_to_trust(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit BUG-020: a half-written report must not become `latest_scan_dir`."""
    from swing import reports as reports_mod

    real_write = pipeline._write

    def explode(path: Path, text: str) -> None:
        if path.name == "picks.html":
            raise OSError("the disk filled up")
        real_write(path, text)

    wire(World(symbols=("AAA",)))
    monkeypatch.setattr(pipeline, "_write", explode)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)

    with pytest.raises(OSError, match="disk filled up"):
        pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    report_dir = tmp_path / "reports" / "scan-2026-08-18"
    assert not (report_dir / "picks.json").exists()
    assert reports_mod.latest_scan_dir(Path(cfg.paths.reports_dir)) is None
    assert not list(report_dir.glob(".*tmp*"))  # no temp files left behind either


def test_report_writes_never_truncate_in_place(tmp_path: Path) -> None:
    """Audit BUG-020: `write_text` truncates, so a reader can see half a file."""
    target = tmp_path / "picks.json"
    pipeline._write(target, '{"picks": []}\n')

    original = Path.write_text
    try:
        Path.write_text = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
            AssertionError("report files must be written atomically")
        )
        pipeline._write(target, '{"picks": [1]}\n')
    finally:
        Path.write_text = original  # type: ignore[method-assign]

    assert target.read_text() == '{"picks": [1]}\n'
    assert not list(tmp_path.glob(".*tmp*"))


def test_a_scan_prunes_reports_older_than_the_retention_window(tmp_path: Path, wire, sent) -> None:
    """Audit LEAK-002: one directory per calendar day, kept forever."""
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    reports = Path(cfg.paths.reports_dir)
    ancient = reports / "scan-2025-01-02"
    ancient.mkdir(parents=True)
    (ancient / "picks.json").write_text('{"picks": []}', encoding="utf-8")
    recent = reports / "scan-2026-08-01"
    recent.mkdir()
    (recent / "picks.json").write_text('{"picks": []}', encoding="utf-8")
    (reports / "backtest").mkdir()

    pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert not ancient.exists()
    assert recent.is_dir()
    assert (reports / "backtest").is_dir()  # not a scan directory; never touched


def test_a_recovered_journal_is_announced_in_the_report(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-024: the reset used to surface only as a UserWarning on stderr."""
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    (Path(cfg.paths.state_dir) / "journal.json").write_text("{not json", encoding="utf-8")

    with pytest.warns(UserWarning):
        report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert "Check the broker before acting" in (report_dir / "picks.md").read_text()


# ---------------------------------------------------------------------------
# sizing guards and order validation (audit BUG-030, DEBT-002)
# ---------------------------------------------------------------------------


def test_a_negative_stop_never_reaches_the_sizing_module(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit BUG-030: size_position was the one strategy call left unguarded."""
    deps = wire(World(symbols=("AAA",)))
    monkeypatch.setattr(
        deps.rules, "initial_stop", lambda bars, cfg: bars["close"] * -1.0, raising=False
    )

    def refuse(**kwargs):
        raise AssertionError("sizing must not be asked about a negative stop")

    monkeypatch.setattr(deps.sizing, "size_position", refuse, raising=False)

    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert read_report(report_dir)["picks"] == []
    assert "not a sane pair of prices" in (report_dir / "picks.md").read_text()


def test_a_non_positive_entry_is_dropped_with_a_note(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-030: a zero or negative close is not something to size against."""
    world = World(symbols=("AAA",))
    frame = world.bars["AAA"]
    frame.loc[frame.index[-1], "close"] = -5.0
    wire(world)

    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert read_report(report_dir)["picks"] == []
    assert "not a sane pair of prices" in (report_dir / "picks.md").read_text()


def test_a_raising_sizing_module_drops_one_candidate_not_the_scan(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit BUG-030: every other strategy call was already wrapped."""
    world = World(symbols=("AAA", "BBB"))
    deps = wire(world)
    real_size = deps.sizing.size_position
    doomed = world.close("AAA")

    def explode_for_aaa(*, equity, cash, entry, stop, cfg):
        if abs(entry - doomed) < 1e-9:
            raise ZeroDivisionError("risk per share is zero")
        return real_size(equity=equity, cash=cash, entry=entry, stop=stop, cfg=cfg)

    monkeypatch.setattr(deps.sizing, "size_position", explode_for_aaa, raising=False)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    payload = read_report(report_dir)
    assert [p["symbol"] for p in payload["picks"]] == ["BBB"]
    assert "the position size could not be computed" in (report_dir / "picks.md").read_text()


@pytest.mark.parametrize(
    ("capped_by", "expected"),
    [
        ("risk_floor", "risk-per-share floor"),
        ("something_new", "'something_new'"),
        ("cash", None),
    ],
)
def test_a_zero_share_result_explains_an_unusual_cap(
    tmp_path: Path, wire, sent, monkeypatch, capped_by, expected
) -> None:
    """Audit BUG-030 / contract A6: `capped_by` grows, and the report must cope."""
    deps = wire(World(symbols=("AAA",)))
    monkeypatch.setattr(
        deps.sizing,
        "size_position",
        lambda **kwargs: FakeSize(0, 0.0, 0.0, False, capped_by),
        raising=False,
    )
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    markdown = (report_dir / "picks.md").read_text()

    assert [w["symbol"] for w in read_report(report_dir)["watch"]] == ["AAA"]
    if expected is None:
        assert "sized to zero shares because" not in markdown
    else:
        assert expected in markdown


def test_an_order_draft_that_fails_its_own_check_is_not_written(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit DEBT-002: the validator existed but only the tests ever ran it."""
    from swing.alerts import orders as orders_mod

    wire(World(symbols=("AAA", "BBB")))
    real_draft = orders_mod.draft_orders

    def bad_draft_for_aaa(pick, cfg):
        draft = real_draft(pick, cfg)
        if pick.symbol == "AAA":
            draft["oto_stop"]["childOrderStrategies"] = []  # an entry with no stop
        return draft

    monkeypatch.setattr(orders_mod, "draft_orders", bad_draft_for_aaa)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    report_dir = pipeline.run_scan(cfg, dry_run=True, asof=ASOF)

    assert sorted(p.name for p in (report_dir / "orders").glob("*.json")) == ["BBB.json"]
    markdown = (report_dir / "picks.md").read_text()
    assert "did not pass its own structural check" in markdown
    assert "AAA" in markdown


# ---------------------------------------------------------------------------
# confirm authority (audit BUG-018, BUG-019, DEBT-006)
# ---------------------------------------------------------------------------


def test_confirm_writes_its_verdict_into_picks_json(tmp_path: Path, wire, sent) -> None:
    """Contract A7 / audit BUG-018: picks.json was written once, always "drafted"."""
    cfg, world = prepare_scan(tmp_path, wire)
    scan_dir = tmp_path / "reports" / "scan-2026-08-18"
    pick = read_report(scan_dir)["picks"][0]
    world.quotes = {"AAA": pick["entry"] + 5.0 * pick["atr"]}

    pipeline.run_confirm(cfg)

    assert read_report(scan_dir)["picks"][0]["status"] == "invalidated"
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "invalidated"


def test_confirm_cannot_resurrect_an_invalidated_pick(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-018, reproduced: invalidated -> confirmed when the price came back."""
    cfg, world = prepare_scan(tmp_path, wire)
    scan_dir = tmp_path / "reports" / "scan-2026-08-18"
    pick = read_report(scan_dir)["picks"][0]

    world.quotes = {"AAA": pick["entry"] + 5.0 * pick["atr"]}
    pipeline.run_confirm(cfg)

    world.quotes = {"AAA": pick["entry"]}  # the price came back
    results = json.loads(pipeline.run_confirm(cfg).read_text())["results"]

    assert results == {}
    assert read_report(scan_dir)["picks"][0]["status"] == "invalidated"
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "invalidated"


def test_confirm_leaves_a_pick_the_journal_has_finished_with(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-018: the executor may have ordered it since the scan."""
    cfg, world = prepare_scan(tmp_path, wire)
    Journal.load(cfg).update_status("AAA", ASOF, "ordered")
    world.quotes = {"AAA": 1.0}

    payload = json.loads(pipeline.run_confirm(cfg).read_text())

    assert payload["results"] == {}
    assert "already records this pick as ordered" in payload["skipped"]["AAA"]
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "ordered"


def test_confirm_refuses_a_dry_run_report(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-019: a dry run wrote a full report and confirm re-quoted it."""
    world = World(symbols=("AAA",))
    wire(world)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    pipeline.run_scan(cfg, dry_run=True, asof=ASOF)
    world.quotes = {"AAA": 100.0}

    with pytest.raises(pipeline.ScanError, match="came from a dry run"):
        pipeline.run_confirm(cfg)
    assert sent["confirm"] == []


def test_confirm_says_when_a_pick_is_not_in_the_journal(tmp_path: Path, wire, sent) -> None:
    """Audit BUG-019: the journal miss used to be a swallowed KeyError."""
    cfg, world = prepare_scan(tmp_path, wire)
    pick = read_report(tmp_path / "reports" / "scan-2026-08-18")["picks"][0]
    (Path(cfg.paths.state_dir) / "journal.json").unlink()
    world.quotes = {"AAA": pick["entry"]}

    payload = json.loads(pipeline.run_confirm(cfg).read_text())

    assert payload["results"]["AAA"]["status"] == "confirmed"
    assert "not in the journal" in payload["skipped"]["AAA"]
    # The report still records the verdict, so the executor sees the truth.
    assert read_report(tmp_path / "reports" / "scan-2026-08-18")["picks"][0]["status"] == (
        "confirmed"
    )


def test_confirm_reads_its_threshold_from_the_configuration(tmp_path: Path, wire, sent) -> None:
    """Audit DEBT-006: the same 1xATR rule lived in two places that could drift."""
    cfg, world = prepare_scan(tmp_path, wire, execution={"max_quote_drift_atr": 0.25})
    pick = read_report(tmp_path / "reports" / "scan-2026-08-18")["picks"][0]
    world.quotes = {"AAA": pick["entry"] + 0.5 * pick["atr"]}  # inside 1 ATR, outside 0.25

    result = json.loads(pipeline.run_confirm(cfg).read_text())["results"]["AAA"]

    assert result["status"] == "invalidated"
    assert "0.25 ATR" in result["reason"]


def test_the_confirm_constant_is_only_the_documented_default(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    assert pipeline._drift_multiple(cfg) == cfg.execution.max_quote_drift_atr
    assert pipeline.CONFIRM_DRIFT_ATR_MULT == 1.0


# ---------------------------------------------------------------------------
# strict delivery (audit BUG-021, the CLI's exit code)
# ---------------------------------------------------------------------------


def all_channels_fail(monkeypatch) -> None:
    monkeypatch.setattr(
        channels, "deliver_scan", lambda cfg, report, **kw: {"ntfy": False, "email": False}
    )
    monkeypatch.setattr(channels, "deliver_confirm", lambda cfg, payload: {"ntfy": False})


def test_a_scan_nobody_heard_about_is_a_failure(tmp_path: Path, wire, sent, monkeypatch) -> None:
    """Audit BUG-021: all-channels-failed used to exit 0, exactly like success."""
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    all_channels_fail(monkeypatch)

    with pytest.raises(pipeline.ScanError, match="every notification channel"):
        pipeline.run_scan(cfg, asof=ASOF, strict_delivery=True)

    message = str(
        pytest.raises(
            pipeline.ScanError, pipeline.run_scan, cfg, asof=ASOF, strict_delivery=True
        ).value
    )
    assert "email, ntfy" in message  # the dead channels are named
    # The work is not lost: the report is on disk and the picks are journalled.
    assert read_report(tmp_path / "reports" / "scan-2026-08-18")["picks"]
    assert Journal.load(cfg).picks_for(ASOF)


def test_a_scan_without_strict_delivery_still_returns_its_report(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    all_channels_fail(monkeypatch)
    assert pipeline.run_scan(cfg, asof=ASOF).name == "scan-2026-08-18"


def test_having_no_channels_configured_is_not_a_delivery_failure(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Nothing configured is a choice; every configured channel failing is not."""
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    monkeypatch.setattr(channels, "deliver_scan", lambda cfg, report, **kw: {})
    assert pipeline.run_scan(cfg, asof=ASOF, strict_delivery=True).is_dir()


def test_one_surviving_channel_is_enough(tmp_path: Path, wire, sent, monkeypatch) -> None:
    wire(World())
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    monkeypatch.setattr(
        channels, "deliver_scan", lambda cfg, report, **kw: {"ntfy": False, "email": True}
    )
    assert pipeline.run_scan(cfg, asof=ASOF, strict_delivery=True).is_dir()


def test_a_confirmation_nobody_heard_about_is_a_failure(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit BUG-021, the morning half."""
    cfg, world = prepare_scan(tmp_path, wire)
    world.quotes = {
        "AAA": read_report(tmp_path / "reports" / "scan-2026-08-18")["picks"][0]["entry"]
    }
    all_channels_fail(monkeypatch)

    with pytest.raises(pipeline.ScanError, match="every notification channel"):
        pipeline.run_confirm(cfg, strict_delivery=True)

    # The verdicts were still recorded before the delivery was attempted.
    assert (tmp_path / "reports" / "scan-2026-08-18" / "confirm.json").is_file()
    assert Journal.load(cfg).picks_for(ASOF)[0].status == "confirmed"


def test_a_confirmation_without_strict_delivery_returns_its_path(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    world.quotes = {"AAA": 100.0}
    all_channels_fail(monkeypatch)
    assert pipeline.run_confirm(cfg).name == "confirm.json"


def test_a_dry_run_confirmation_never_fails_on_delivery(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    cfg, world = prepare_scan(tmp_path, wire)
    world.quotes = {"AAA": 100.0}
    all_channels_fail(monkeypatch)
    assert pipeline.run_confirm(cfg, dry_run=True, strict_delivery=True).is_file()


def test_the_scanner_computes_atr_once_per_sized_candidate(
    tmp_path: Path, wire, sent, monkeypatch
) -> None:
    """Audit PERF-009, the half that lives in this module.

    The scanner reads one ATR series per sized candidate and uses it for the
    pick's own ``atr``. The *second* computation PERF-009 names for this loop is
    inside ``rules.initial_stop``, which takes no series argument — closing that
    one needs a change in ``swing.strategy.rules``.
    """
    calls: list[str] = []
    deps = wire(World(symbols=("AAA", "BBB", "CCC")))
    real_atr = deps.indicators.atr

    def counting_atr(bars, n=14):
        calls.append(str(bars.attrs.get("symbol", "")))
        return real_atr(bars, n)

    monkeypatch.setattr(deps.indicators, "atr", counting_atr, raising=False)
    cfg = build_config(tmp_path, account=RICH_ACCOUNT)
    payload = read_report(pipeline.run_scan(cfg, dry_run=True, asof=ASOF))

    assert len(payload["picks"]) == 3
    assert sorted(calls) == ["AAA", "BBB", "CCC"]


def test_confirm_refuses_a_report_that_is_not_an_object(tmp_path: Path) -> None:
    """A JSON file that parses but is not a report must refuse, not crash."""
    cfg = build_config(tmp_path)
    scan_dir = tmp_path / "reports" / "scan-2026-08-18"
    scan_dir.mkdir(parents=True)
    (scan_dir / "picks.json").write_text('["AAA"]', encoding="utf-8")

    with pytest.raises(pipeline.ScanError, match="not a scan report"):
        pipeline.run_confirm(cfg)
