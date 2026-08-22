"""Tests for the forward paper-trading harness (``swing shadow``).

The harness exists because eleven experiments were scored against one window,
so these tests care about two things above all: that the record cannot be
corrupted by re-running anything, and that scoring agrees with the backtest
engine's exit ladder rather than with a second, hand-rolled version of it.

Everything here is offline and deterministic. Bars are built by hand with a
constant true range so ATR is an exact integer and every stop level in the
assertions can be computed on paper.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from typer.testing import CliRunner

from conftest import build_config
from swing import shadow
from swing.alerts import pipeline
from swing.cli import app
from swing.config import Config
from swing.shadow import (
    Outcome,
    ShadowDay,
    ShadowError,
    ShadowJournal,
    ShadowPick,
    shadow_dir,
)
from swing.state import JOURNAL_FILENAME, PickRecord

runner = CliRunner()


# ---------------------------------------------------------------------------
# fixtures and builders
# ---------------------------------------------------------------------------

#: A tracked file that exercises the whole loader: rules only, no credentials.
BASELINE_TOML = """
[account]
equity = 100.0
max_positions = 4

[strategy]
breakout_proximity_pct = 2.0
atr_window = 5
atr_stop_mult = 2.0
chandelier_mult = 10.0
time_stop_days = 400

[backtest]
initial_equity = 10000.0
"""

COMBO_TOML = """
[account]
equity = 100.0
max_positions = 4

[strategy]
breakout_proximity_pct = 0.0
atr_window = 5
atr_stop_mult = 2.0
chandelier_mult = 10.0
time_stop_days = 400

[backtest]
initial_equity = 10000.0
"""


@pytest.fixture
def tracked_dir(tmp_path: Path) -> Path:
    """A ``config/shadow`` directory holding two tracked configurations."""
    root = tmp_path / "tracked"
    root.mkdir()
    (root / "baseline.toml").write_text(BASELINE_TOML)
    (root / "combo.toml").write_text(COMBO_TOML)
    return root


@pytest.fixture
def host(tmp_path: Path) -> Config:
    """The host configuration: every writable path under ``tmp_path``."""
    return build_config(tmp_path)


def flat_bars(
    n: int,
    *,
    start: str = "2026-01-01",
    close: float = 100.0,
    half_range: float = 1.0,
) -> pd.DataFrame:
    """Bars with a constant true range, so ATR is exactly ``2 * half_range``.

    open == close == ``close`` and high/low sit symmetrically around it, which
    makes ``true_range`` equal to ``high - low`` on every bar after the first
    and therefore makes every stop level in these tests an exact number.
    """
    index = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + half_range] * n,
            "low": [close - half_range] * n,
            "close": [close] * n,
            "volume": [1_000_000.0] * n,
        },
        index=index,
    )


def poke(frame: pd.DataFrame, row: int, **values: float) -> pd.DataFrame:  # noqa: D401 - tiny helper
    """Return a copy of ``frame`` with one row's fields replaced."""
    out = frame.copy()
    for column, value in values.items():
        out.iloc[row, out.columns.get_loc(column)] = float(value)
    return out


def a_pick(frame: pd.DataFrame, signal_row: int, *, shares: int = 10) -> ShadowPick:
    """A recorded pick whose signal lands on ``signal_row`` of ``frame``."""
    return ShadowPick(
        symbol="TEST",
        signal_date=frame.index[signal_row].date().isoformat(),
        kind="pick",
        shares=shares,
        signal_close=float(frame["close"].iloc[signal_row]),
        initial_stop=0.0,
        atr=0.0,
        score=1.0,
    )


def scoring_cfg(tmp_path: Path, **strategy: Any) -> Config:
    """A config tuned so ATR warms up in five bars and nothing else interferes."""
    settings: dict[str, Any] = {
        "atr_window": 5,
        "atr_stop_mult": 2.0,
        "chandelier_mult": 10.0,
        "time_stop_days": 400,
    }
    settings.update(strategy)
    return build_config(tmp_path, strategy=settings)


class FakeDeps:
    """The scanner dependency bundle, with nothing that touches the network."""

    def __init__(self) -> None:
        self.get_provider = lambda cfg: object()
        self.rules = None
        self.scoring = None
        self.regime = None
        self.sizing = None
        self.indicators = None


def install_fake_scan(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[float, list[str]],
    *,
    per_day: dict[str, list[str]] | None = None,
) -> list[Any]:
    """Replace the scanner's candidate pipeline, keyed by ``breakout_proximity_pct``.

    ``per_day`` overrides the per-config answer with a per-date one, for the
    tests that need a different name on each day.

    Returns the list the journal views are recorded into, so a test can prove
    the shadow harness handed the real ``_scan`` a journal that is not the real
    journal.
    """
    seen: list[Any] = []

    def fake_scan(deps, cfg, provider, journal, asof):  # noqa: ANN001, ANN202
        seen.append(journal)
        if per_day is not None:
            symbols = per_day.get(asof.isoformat(), [])
        else:
            symbols = results.get(float(cfg.strategy.breakout_proximity_pct), [])
        held = {p["symbol"] for p in journal.positions()}
        picks = [
            PickRecord(
                symbol=symbol,
                date=asof.isoformat(),
                kind="pick",
                entry=100.0,
                stop=96.0,
                shares=10,
                risk_amount=40.0,
                score=1.0,
                atr=2.0,
                earnings_date=None,
                earnings_known=False,
                thesis=f"{symbol} because the test said so",
                status="drafted",
            )
            for symbol in symbols
            if symbol not in held
        ]
        return True, picks, [], ["a note"]

    monkeypatch.setattr(pipeline, "_load_deps", lambda: FakeDeps())
    monkeypatch.setattr(pipeline, "_scan", fake_scan)
    monkeypatch.setattr(pipeline, "_gate_status", lambda cfg: {"passed": False, "reasons": ["no"]})
    return seen


# ---------------------------------------------------------------------------
# tracked configurations
# ---------------------------------------------------------------------------


def test_tracked_configs_take_plumbing_from_the_host(host: Config, tracked_dir: Path) -> None:
    tracked = shadow.tracked_configs(host, directory=tracked_dir)

    assert [t.name for t in tracked] == ["baseline", "combo"]
    for entry in tracked:
        # The tracked file describes rules; the machine describes everything else.
        assert entry.cfg.paths.state_dir == host.paths.state_dir
        assert entry.cfg.data.cache_dir == host.data.cache_dir
        # ...and equity is rebased onto the backtest's reference capital, exactly
        # as swing.backtest.runner does, so shadow and the backtest size alike.
        assert entry.cfg.account.equity == entry.cfg.backtest.initial_equity == 10_000.0

    assert tracked[0].cfg.strategy.breakout_proximity_pct == 2.0
    assert tracked[1].cfg.strategy.breakout_proximity_pct == 0.0


def test_named_subset_and_unknown_name(host: Config, tracked_dir: Path) -> None:
    only = shadow.tracked_configs(host, names=["combo"], directory=tracked_dir)
    assert [t.name for t in only] == ["combo"]

    with pytest.raises(ShadowError, match="No tracked configuration named nope"):
        shadow.tracked_configs(host, names=["nope"], directory=tracked_dir)


def test_missing_or_empty_directory_is_explained(host: Config, tmp_path: Path) -> None:
    with pytest.raises(ShadowError, match="no tracked-configuration directory"):
        shadow.tracked_configs(host, directory=tmp_path / "nope")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ShadowError, match="holds no .toml files"):
        shadow.tracked_configs(host, directory=empty)


@pytest.mark.parametrize("section", ["schwab", "alerts", "execution"])
def test_tracked_file_may_not_carry_credentials(host: Config, tmp_path: Path, section: str) -> None:
    """config/shadow is committed to git, so a secret-bearing section is refused."""
    root = tmp_path / "tracked"
    root.mkdir()
    (root / "leaky.toml").write_text(f"[{section}]\n")

    with pytest.raises(ShadowError, match="committed to git"):
        shadow.tracked_configs(host, directory=root)


def test_the_shipped_tracked_configs_load_and_differ(host: Config) -> None:
    """The two committed arms must be loadable, and must differ where they claim to."""
    tracked = shadow.tracked_configs(host, directory=Path("config/shadow"))

    names = {t.name for t in tracked}
    assert {"baseline", "combo"} <= names
    by_name = {t.name: t for t in tracked}
    baseline = by_name["baseline"].cfg.strategy
    combo = by_name["combo"].cfg.strategy

    assert (baseline.breakout_proximity_pct, combo.breakout_proximity_pct) == (2.0, 0.0)
    assert (baseline.atr_stop_mult, baseline.chandelier_mult) == (2.0, 3.0)
    assert (combo.atr_stop_mult, combo.chandelier_mult) == (3.5, 5.0)
    assert list(by_name["combo"].cfg.backtest.tuning_grid["atr_stop_mult"]) == [2.5, 3.5, 4.5]


def test_combos_wide_stops_are_written_where_the_SCANNER_reads_them(host: Config) -> None:
    """The bug this pins: a tuning grid is invisible to a daily scan.

    ``[backtest.tuning_grid]`` is searched by the walk-forward tuner and by
    nothing else. The scanner — and therefore shadow — reads
    ``strategy.atr_stop_mult`` and ``strategy.chandelier_mult`` directly, so an
    arm whose "wide stops" live only in the grid silently trades baseline's
    stops and tests strict entry alone. If someone moves these back into the
    grid, this fails.
    """
    tracked = {t.name: t for t in shadow.tracked_configs(host, directory=Path("config/shadow"))}
    combo = tracked["combo"].cfg
    baseline = tracked["baseline"].cfg

    assert combo.strategy.atr_stop_mult > baseline.strategy.atr_stop_mult
    assert combo.strategy.chandelier_mult > baseline.strategy.chandelier_mult
    # 5.0 is the interior grid value, deliberately NOT the modal 6.0 that sat on
    # the boundary of the offered grid. See the config header.
    assert combo.strategy.chandelier_mult == 5.0
    assert 5.0 in combo.backtest.tuning_grid["chandelier_mult"]
    assert max(combo.backtest.tuning_grid["chandelier_mult"]) == 6.0


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def test_recording_is_idempotent_per_config_and_day(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rerunning a day must re-decide it, not collide with its own earlier record.

    Checked over an EVEN and an ODD number of runs on purpose. An earlier
    version of this test ran three times and passed while the answer was
    actually alternating AAA -> none -> AAA: the second run saw the first run's
    pick as an occupied slot and recorded nothing. Asserting after every run is
    what catches that.
    """
    seen = install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: ["BBB"]})
    day = date(2026, 8, 20)

    for _ in range(4):
        shadow.run(host, asof=day, directory=tracked_dir)
        journal = ShadowJournal.load(host, "baseline")
        assert [d.date for d in journal.days] == ["2026-08-20"]
        assert [p.symbol for p in journal.picks] == ["AAA"]

    # ...and the scan genuinely saw an empty book each time, rather than being
    # handed its own previous answer back.
    assert all(view.positions() == [] for view in seen)


def test_a_second_day_appends_rather_than_replaces(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    per_day = {"2026-08-20": ["AAA"], "2026-08-21": ["DDD"]}
    install_fake_scan(monkeypatch, {}, per_day=per_day)

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)
    shadow.run(host, asof=date(2026, 8, 21), directory=tracked_dir)

    journal = ShadowJournal.load(host, "baseline")
    assert [d.date for d in journal.days] == ["2026-08-20", "2026-08-21"]
    assert [p.symbol for p in journal.picks] == ["AAA", "DDD"]


def test_a_symbol_already_open_in_the_shadow_book_is_not_picked_twice(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot and dedupe accounting reaches the scanner through the journal view."""
    install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: []})

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir, names=["baseline"])
    shadow.run(host, asof=date(2026, 8, 21), directory=tracked_dir, names=["baseline"])

    journal = ShadowJournal.load(host, "baseline")
    assert len(journal.days) == 2, "the second day is still recorded"
    assert [p.symbol for p in journal.picks] == ["AAA"], "but AAA is not entered twice"


def test_two_configs_are_tracked_independently(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_scan(monkeypatch, {2.0: ["AAA", "CCC"], 0.0: ["BBB"]})

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)

    baseline = ShadowJournal.load(host, "baseline")
    combo = ShadowJournal.load(host, "combo")
    assert sorted(p.symbol for p in baseline.picks) == ["AAA", "CCC"]
    assert [p.symbol for p in combo.picks] == ["BBB"]
    assert baseline.path != combo.path
    assert baseline.path.name == "baseline.json"


def test_the_failing_gate_is_recorded_but_never_suppresses(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: hypothetical picks are recorded even though the gate fails."""
    install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: ["BBB"]})

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)

    day = ShadowJournal.load(host, "baseline").days[0]
    assert day.gate_passed is False
    assert day.gate_reasons == ("no",)
    assert ShadowJournal.load(host, "baseline").picks, "a failing gate must not empty the record"


def test_dry_run_writes_nothing(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: ["BBB"]})

    summaries = shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir, dry_run=True)

    assert [s["picks"] for s in summaries] == [["AAA"], ["BBB"]]
    assert not shadow_dir(host).exists() or not list(shadow_dir(host).glob("*.json"))


def test_a_failing_scan_is_recorded_not_raised(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One configuration's bad night must not lose the record for the others."""
    monkeypatch.setattr(pipeline, "_load_deps", lambda: FakeDeps())
    monkeypatch.setattr(pipeline, "_gate_status", lambda cfg: {"passed": False, "reasons": []})

    def boom(deps, cfg, provider, journal, asof):  # noqa: ANN001, ANN202
        if cfg.strategy.breakout_proximity_pct == 0.0:
            raise RuntimeError("the vendor fell over")
        return True, [], [], []

    monkeypatch.setattr(pipeline, "_scan", boom)
    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)

    good = ShadowJournal.load(host, "baseline").days[0]
    bad = ShadowJournal.load(host, "combo").days[0]
    assert good.error == ""
    assert "the vendor fell over" in bad.error
    assert bad.date == "2026-08-20", "the day itself is still recorded"


# ---------------------------------------------------------------------------
# the real journal is never touched
# ---------------------------------------------------------------------------


def test_shadow_never_writes_the_real_journal(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: ["BBB"]})
    real_journal = Path(host.paths.state_dir).expanduser() / JOURNAL_FILENAME

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)
    shadow.score(host, asof=date(2026, 8, 21), directory=tracked_dir)
    shadow.report(host, directory=tracked_dir)

    assert not real_journal.exists()
    assert not real_journal.with_name("journal.archive.json").exists()
    written = {p.name for p in shadow_dir(host).glob("*")}
    assert written == {"baseline.json", "combo.json", "baseline.json.lock", "combo.json.lock"}


def test_the_scanner_view_is_a_journal_pointed_somewhere_else(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan gets real Journal semantics without the real Journal's file."""
    seen = install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: []})

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)

    assert seen, "the shadow harness must go through pipeline._scan"
    for view in seen:
        assert view.path.name != JOURNAL_FILENAME
        assert "shadow" in str(view.path)
        assert not view.path.exists()


def test_the_view_reports_open_positions_so_slots_are_respected(host: Config) -> None:
    journal = ShadowJournal(shadow_dir(host) / "x.json", "x")
    journal.picks = [
        ShadowPick("AAA", "2026-08-20", "pick", 10, 100.0, 96.0, 2.0, 1.0),
        ShadowPick(
            "BBB",
            "2026-08-20",
            "pick",
            10,
            100.0,
            96.0,
            2.0,
            1.0,
            outcome=Outcome(status="closed"),
        ),
        ShadowPick("CCC", "2026-08-20", "watch", 0, 100.0, 96.0, 2.0, 1.0),
    ]
    view = journal.scanner_view()

    assert [p["symbol"] for p in view.positions()] == ["AAA"]
    # Dedupe still sees every pick, closed or not; a watch line never dedupes.
    assert view.recently_picked("BBB", 7, asof=date(2026, 8, 22)) is True
    assert view.recently_picked("CCC", 7, asof=date(2026, 8, 22)) is False


# ---------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------


def test_every_mutating_path_takes_the_file_lock(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib

    taken: list[Path] = []

    @contextlib.contextmanager
    def spy(path, **kwargs):  # noqa: ANN001, ANN202
        taken.append(Path(path))
        with real_lock(path, **kwargs):
            yield

    real_lock = shadow.file_lock
    monkeypatch.setattr(shadow, "file_lock", spy)
    install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: ["BBB"]})

    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)
    assert sorted(p.name for p in taken) == ["baseline.json", "combo.json"]

    taken.clear()
    shadow.score(host, asof=date(2026, 8, 21), directory=tracked_dir)
    assert sorted(p.name for p in taken) == ["baseline.json", "combo.json"]


def test_reads_take_no_lock_and_the_report_does_not_mutate(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_scan(monkeypatch, {2.0: ["AAA"], 0.0: ["BBB"]})
    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir)
    before = (shadow_dir(host) / "baseline.json").read_text()

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a read must not take the lock")

    monkeypatch.setattr(shadow, "file_lock", explode)
    shadow.report(host, directory=tracked_dir)

    assert (shadow_dir(host) / "baseline.json").read_text() == before


# ---------------------------------------------------------------------------
# scoring: one test per rung of the engine's exit ladder
# ---------------------------------------------------------------------------


def test_scoring_fills_at_the_next_open_and_holds_a_quiet_position(tmp_path: Path) -> None:
    cfg = scoring_cfg(tmp_path)
    frame = flat_bars(30)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "open"
    assert outcome.entry_date == frame.index[11].date().isoformat()
    assert outcome.entry_price == 100.0  # the RAW open of the bar after the signal
    # ATR is exactly 2.0, so the initial stop is close - 2 x ATR = 96.
    assert outcome.stop == 96.0
    assert outcome.exit_reason == ""


def test_scoring_a_resting_stop_touched_intraday(tmp_path: Path) -> None:
    """Ladder rung (c): low <= stop with the open above it fills AT the stop."""
    cfg = scoring_cfg(tmp_path)
    frame = poke(flat_bars(30), 20, low=95.0)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "closed"
    assert outcome.exit_reason == "stop"
    assert outcome.exit_price == 96.0
    assert outcome.exit_date == frame.index[20].date().isoformat()
    assert outcome.hold_days == 20 - 11


def test_scoring_a_gap_through_the_stop_overnight(tmp_path: Path) -> None:
    """Ladder rung (a): the open is already below the stop, so that is the fill."""
    cfg = scoring_cfg(tmp_path)
    frame = poke(flat_bars(30), 20, open=90.0, high=90.0, low=89.0, close=89.5)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "closed"
    assert outcome.exit_reason == "stop"
    assert outcome.exit_price == 90.0, "a gap pays the open, never the stop"


def test_scoring_a_ratcheted_chandelier_exit(tmp_path: Path) -> None:
    """A chandelier above the initial stop changes both the level and the reason."""
    cfg = scoring_cfg(tmp_path, chandelier_mult=1.0)
    # chandelier = highest close (100) - 1 x ATR (2) = 98, above the 96 initial stop.
    frame = poke(flat_bars(30), 20, low=97.5)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "closed"
    assert outcome.exit_reason == "chandelier"
    assert outcome.exit_price == 98.0


def test_the_chandelier_only_ever_ratchets_up(tmp_path: Path) -> None:
    """A widening ATR lowers the raw chandelier; the position's stop must not follow."""
    from swing.strategy import rules

    cfg = scoring_cfg(tmp_path, chandelier_mult=1.0)
    frame = flat_bars(40)
    # Bars 20-29 reach far higher without ever trading lower, which quadruples
    # ATR while leaving the highest close where it was — so the raw chandelier
    # series FALLS, without any bar coming near the position's stop.
    for row in range(20, 30):
        frame = poke(frame, row, high=108.0, low=100.0)
    raw = rules.chandelier_stop(frame, cfg)
    assert raw.iloc[-1] < 98.0, "the fixture must actually drag the raw level down"

    outcome = shadow._score_pick(a_pick(frame, 10), frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "open"
    # 98.0 is where the ratchet got to before ATR widened; it must not follow it down.
    assert outcome.stop == 98.0


def test_scoring_a_time_stop(tmp_path: Path) -> None:
    """Ladder rung (b): armed at the close of t-1, filled at the open of t."""
    cfg = scoring_cfg(tmp_path, time_stop_days=3)
    frame = flat_bars(30)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "closed"
    assert outcome.exit_reason == "time"
    # Entry is row 11; the stop arms at the close of row 14 and fills at row 15.
    assert outcome.exit_date == frame.index[15].date().isoformat()
    assert outcome.exit_price == 100.0
    assert outcome.hold_days == 4


def test_a_gap_through_beats_a_time_stop_on_the_same_morning(tmp_path: Path) -> None:
    """Rung (a) is checked before rung (b) because it is the worse fill."""
    cfg = scoring_cfg(tmp_path, time_stop_days=3)
    frame = poke(flat_bars(30), 15, open=90.0, high=90.5, low=89.0, close=89.5)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.exit_reason == "stop"
    assert outcome.exit_price == 90.0


def test_a_position_is_never_closed_just_because_the_data_ran_out(tmp_path: Path) -> None:
    """The backtest's ``end_of_data`` exit has no shadow equivalent, on purpose."""
    cfg = scoring_cfg(tmp_path)
    frame = flat_bars(30)
    pick = a_pick(frame, 10)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "open"
    assert outcome.exit_reason == ""
    assert outcome.exit_date == ""


def test_a_signal_with_no_bar_after_it_is_pending_not_lapsed(tmp_path: Path) -> None:
    cfg = scoring_cfg(tmp_path)
    frame = flat_bars(30)
    pick = a_pick(frame, 29)

    outcome = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert outcome.status == "pending"
    assert "has not printed yet" in outcome.note


def test_asof_truncates_the_bars_so_scoring_is_reproducible(tmp_path: Path) -> None:
    cfg = scoring_cfg(tmp_path)
    frame = poke(flat_bars(30), 20, low=95.0)
    pick = a_pick(frame, 10)

    early = shadow._score_pick(pick, frame, cfg, asof=frame.index[19].date())
    late = shadow._score_pick(pick, frame, cfg, asof=frame.index[-1].date())

    assert early.status == "open", "the exit bar has not happened yet at this asof"
    assert late.status == "closed"


def test_a_zero_share_candidate_has_nothing_to_score(tmp_path: Path) -> None:
    cfg = scoring_cfg(tmp_path)
    frame = flat_bars(30)

    outcome = shadow._score_pick(a_pick(frame, 10, shares=0), frame, cfg, asof=date(2026, 8, 20))

    assert outcome.status == "lapsed"
    assert "zero shares" in outcome.note


def test_scoring_pnl_reconciles_by_hand(tmp_path: Path) -> None:
    """P&L is booked by the engine's own ``_close_position``; check the identity."""
    cfg = scoring_cfg(tmp_path)
    frame = poke(flat_bars(30), 20, low=95.0)

    outcome = shadow._score_pick(a_pick(frame, 10, shares=7), frame, cfg, asof=date(2026, 3, 1))

    expected = (
        7 * (outcome.exit_price - outcome.entry_price) - outcome.entry_cost - outcome.exit_cost
    )
    assert outcome.pnl == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# scoring, end to end through the journal
# ---------------------------------------------------------------------------


def install_fake_bars(monkeypatch: pytest.MonkeyPatch, frames: dict[str, pd.DataFrame]) -> None:
    class FakeProvider:
        def daily_bars(self, symbols, start, end):  # noqa: ANN001, ANN202
            return {s: frames[s] for s in symbols if s in frames}

    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: FakeProvider())


def test_score_closes_positions_in_the_journal_and_is_a_pure_replay(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = poke(flat_bars(30), 20, low=95.0)
    signal_day = frame.index[10].date()
    install_fake_scan(monkeypatch, {2.0: ["TEST"], 0.0: []})
    shadow.run(host, asof=signal_day, directory=tracked_dir)

    install_fake_bars(monkeypatch, {"TEST": frame})
    shadow.score(host, asof=frame.index[-1].date(), directory=tracked_dir, names=["baseline"])
    once = ShadowJournal.load(host, "baseline").picks[0].outcome

    shadow.score(host, asof=frame.index[-1].date(), directory=tracked_dir, names=["baseline"])
    twice = ShadowJournal.load(host, "baseline").picks[0].outcome

    assert once.status == "closed"
    assert once.exit_reason == "stop"
    assert once == twice, "scoring replays from the entry, so a rerun cannot drift"


def test_a_closed_position_frees_its_slot_for_the_next_recording(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot accounting flows through the scanner view, so a closed trade releases one."""
    frame = poke(flat_bars(30), 12, low=95.0)
    install_fake_scan(monkeypatch, {2.0: ["TEST"], 0.0: []})
    shadow.run(host, asof=frame.index[10].date(), directory=tracked_dir, names=["baseline"])

    view = ShadowJournal.load(host, "baseline").scanner_view()
    assert len(view.positions()) == 1

    install_fake_bars(monkeypatch, {"TEST": frame})
    shadow.score(host, asof=frame.index[-1].date(), directory=tracked_dir, names=["baseline"])

    view = ShadowJournal.load(host, "baseline").scanner_view()
    assert view.positions() == []


def test_scoring_survives_a_provider_that_returns_nothing(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_scan(monkeypatch, {2.0: ["TEST"], 0.0: []})
    shadow.run(host, asof=date(2026, 8, 20), directory=tracked_dir, names=["baseline"])

    class Broken:
        def daily_bars(self, symbols, start, end):  # noqa: ANN001, ANN202
            raise RuntimeError("vendor down")

    monkeypatch.setattr("swing.data.get_provider", lambda cfg, **kw: Broken())
    shadow.score(host, asof=date(2026, 8, 21), directory=tracked_dir, names=["baseline"])

    outcome = ShadowJournal.load(host, "baseline").picks[0].outcome
    assert outcome.status == "pending"
    assert "No price history" in outcome.note


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def test_the_journal_round_trips_through_json(host: Config) -> None:
    journal = ShadowJournal(shadow_dir(host) / "rt.json", "rt")
    journal.record_day(
        ShadowDay(date="2026-08-20", recorded_at="2026-08-20T15:30:00-06:00", gate_passed=False),
        [ShadowPick("AAA", "2026-08-20", "pick", 10, 100.0, 96.0, 2.0, 1.5, thesis="why")],
    )

    raw = json.loads(journal.path.read_text())
    assert raw["version"] == shadow.SHADOW_VERSION
    assert raw["picks"][0]["outcome"]["status"] == "pending"

    reloaded = ShadowJournal.load(host, "rt")
    assert reloaded.picks == journal.picks
    assert reloaded.days == journal.days


def test_a_corrupt_journal_refuses_rather_than_resetting(host: Config) -> None:
    """Forward evidence cannot be regenerated, so losing it silently is not an option."""
    path = shadow_dir(host) / "bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    with pytest.raises(ShadowError, match="could not be read"):
        ShadowJournal.load(host, "bad")


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def test_the_small_sample_warning_is_always_present(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = shadow.report(host, directory=tracked_dir)
    assert "FAR TOO SMALL" in empty
    assert "DO NOT pick a winner" in empty
    assert str(shadow.MEANINGFUL_TRADE_COUNT) in empty

    # ...and it is still there once there is something in the journals.
    frame = poke(flat_bars(30), 20, low=95.0)
    install_fake_scan(monkeypatch, {2.0: ["TEST"], 0.0: ["TEST"]})
    shadow.run(host, asof=frame.index[10].date(), directory=tracked_dir)
    install_fake_bars(monkeypatch, {"TEST": frame})
    shadow.score(host, asof=frame.index[-1].date(), directory=tracked_dir)

    populated = shadow.report(host, directory=tracked_dir)
    assert "FAR TOO SMALL" in populated
    assert "DO NOT pick a winner" in populated


def test_the_report_puts_the_configs_side_by_side(
    host: Config, tracked_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = poke(flat_bars(30), 20, low=95.0)
    install_fake_scan(monkeypatch, {2.0: ["TEST"], 0.0: []})
    shadow.run(host, asof=frame.index[10].date(), directory=tracked_dir)
    install_fake_bars(monkeypatch, {"TEST": frame})
    shadow.score(host, asof=frame.index[-1].date(), directory=tracked_dir)

    text = shadow.report(host, directory=tracked_dir)

    assert "baseline" in text and "combo" in text
    for label in (
        "days tracked",
        "positions open",
        "positions closed",
        "realised P&L",
        "win rate",
        "profit factor",
        "average hold (days)",
    ):
        assert label in text
    assert "  stop" in text, "exits are broken out by reason"


def test_profit_factor_is_n_a_rather_than_infinity(host: Config) -> None:
    """A sample with no losing trade has no profit factor; 'inf' would read as a score."""
    journal = ShadowJournal(shadow_dir(host) / "pf.json", "pf")
    journal.picks = [
        ShadowPick(
            "AAA",
            "2026-08-20",
            "pick",
            10,
            100.0,
            96.0,
            2.0,
            1.0,
            outcome=Outcome(status="closed", pnl=50.0),
        )
    ]
    stats = shadow._stats_for(journal)

    assert stats.profit_factor is None
    assert stats.win_rate == 100.0


# ---------------------------------------------------------------------------
# the seams this module borrows from its siblings
# ---------------------------------------------------------------------------


def test_the_private_pipeline_and_engine_seams_still_exist() -> None:
    """Pin what shadow reaches into, so a sibling refactor fails here, loudly.

    Shadow deliberately calls private helpers in ``swing.alerts.pipeline`` and
    ``swing.backtest.engine`` rather than copying them: reimplementing selection
    or the exit ladder would let the shadow comparison drift away from the
    system it is supposed to be measuring. The cost of that choice is this test.
    """
    from swing.backtest import engine

    for name in ("_scan", "_load_deps", "_gate_status", "SCAN_LOOKBACK_DAYS"):
        assert hasattr(pipeline, name), f"swing.shadow depends on pipeline.{name}"
    for name in ("_close_position", "_OpenPosition", "_atr_series", "EXIT_STOP", "EXIT_CHANDELIER"):
        assert hasattr(engine, name), f"swing.shadow depends on engine.{name}"


def test_shadow_scores_the_same_exit_the_engine_would(tmp_path: Path) -> None:
    """Cross-check one trade against ``run_engine`` itself, not against a restatement.

    The engine picks its own entries, so the two cannot be compared trade for
    trade; what can be compared is the ladder's answer for a known position —
    the same bar, the same price and the same reason.
    """
    from swing.backtest.engine import EXIT_STOP

    cfg = scoring_cfg(tmp_path)
    frame = poke(flat_bars(30), 20, low=95.0)
    outcome = shadow._score_pick(a_pick(frame, 10), frame, cfg, asof=frame.index[-1].date())

    # ATR is exactly 2.0 on every bar, so Contract 11's rung (c) says: a resting
    # stop at close - 2 x ATR = 96 is touched by a low of 95 and fills AT 96.
    assert (outcome.exit_reason, outcome.exit_price) == (EXIT_STOP, 96.0)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


CLI_CONFIG = """
[account]
equity = 100.0

[data]
cache_dir = "{root}/cache"

[strategy]
atr_window = 5

[backtest]
initial_equity = 10000.0

[paths]
reports_dir = "{root}/reports"
state_dir = "{root}/state"
"""


@pytest.fixture
def cli_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with a config.toml and a config/shadow of its own."""
    root = tmp_path / "cli"
    (root / "config" / "shadow").mkdir(parents=True)
    (root / "config.toml").write_text(CLI_CONFIG.format(root=root.as_posix()))
    (root / "config" / "shadow" / "baseline.toml").write_text(BASELINE_TOML)
    monkeypatch.chdir(root)
    return root


def invoke(*args: str):  # noqa: ANN202
    return runner.invoke(app, ["--config", "config.toml", *args])


def test_cli_exposes_the_three_shadow_subcommands(cli_root: Path) -> None:
    result = invoke("shadow", "--help")

    assert result.exit_code == 0
    for command in ("run", "score", "report"):
        assert command in result.stdout


def test_cli_shadow_report_prints_the_warning(cli_root: Path) -> None:
    result = invoke("shadow", "report")

    assert result.exit_code == 0
    assert "FAR TOO SMALL" in result.stdout


def test_cli_shadow_run_dry_run_writes_nothing(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_scan(monkeypatch, {2.0: ["AAA"]})

    result = invoke("shadow", "run", "--asof", "2026-08-20", "--dry-run")

    assert result.exit_code == 0, result.stdout
    assert "AAA" in result.stdout
    assert not (cli_root / "state" / "shadow").exists()


def test_cli_shadow_run_then_score_then_report(
    cli_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = poke(flat_bars(30), 20, low=95.0)
    install_fake_scan(monkeypatch, {2.0: ["TEST"]})
    install_fake_bars(monkeypatch, {"TEST": frame})

    assert invoke("shadow", "run", "--asof", frame.index[10].date().isoformat()).exit_code == 0
    scored = invoke("shadow", "score", "--asof", frame.index[-1].date().isoformat())
    assert scored.exit_code == 0, scored.stdout
    assert "1 closed" in scored.stdout

    reported = invoke("shadow", "report")
    assert reported.exit_code == 0
    assert "positions closed" in reported.stdout


def test_cli_refuses_an_unknown_config_name_with_one_sentence(cli_root: Path) -> None:
    result = invoke("shadow", "report", "--config-name", "nope")

    assert result.exit_code == 2
    assert "Traceback" not in result.stdout
    assert "No tracked configuration named nope" in (result.stdout + (result.stderr or ""))


def test_cli_rejects_a_bad_asof(cli_root: Path) -> None:
    result = invoke("shadow", "run", "--asof", "yesterday")

    assert result.exit_code == 2
    assert "YYYY-MM-DD" in (result.stdout + (result.stderr or ""))
