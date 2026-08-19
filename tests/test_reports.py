"""Which scan directory is *the* one — contract A4 (audit DEBT-001, BUG-012).

``swing confirm`` and ``swing execute`` used to answer this question with two
different implementations that disagreed on exactly the states that happen in
practice: a crash halfway through writing a report, and a weekend run that
produced nothing. Every rule of the shared answer is pinned here, including the
retention sweep that stops the directory growing forever (audit LEAK-002).

Nothing here needs a config, a provider or a clock: the module takes a path and
a date.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from swing import reports


def write_report(
    root: Path, name: str, *, picks: list[Any] | None = None, watch: list[Any] | None = None
) -> Path:
    """Create ``<root>/<name>/picks.json`` holding the given entries."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"asof": name.removeprefix("scan-"), "picks": picks or [], "watch": watch or []}
    (directory / "picks.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def pick(symbol: str = "AAA") -> dict[str, Any]:
    return {"symbol": symbol, "shares": 3, "entry": 45.1, "stop": 41.8, "status": "drafted"}


# ---------------------------------------------------------------------------
# parsing directory names
# ---------------------------------------------------------------------------


def test_only_strict_scan_names_are_recognised(tmp_path: Path) -> None:
    assert reports.scan_dir_date(tmp_path / "scan-2026-08-18") == date(2026, 8, 18)
    assert reports.scan_dir_date(tmp_path / "scan-2026-8-1") is None
    assert reports.scan_dir_date(tmp_path / "scan-latest") is None
    assert reports.scan_dir_date(tmp_path / "backtest") is None
    assert reports.scan_dir_date(tmp_path / "scan-2026-08-18.bak") is None


def test_a_missing_reports_directory_is_not_an_error(tmp_path: Path) -> None:
    assert reports.scan_dirs(tmp_path / "nope") == []
    assert reports.latest_scan_dir(tmp_path / "nope") is None


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_latest_is_by_parsed_date_not_lexicographic_name(tmp_path: Path) -> None:
    """Audit DEBT-001: the two old implementations sorted differently.

    These names sort the same way either way; the point of the test is that the
    ordering key is a real date, so a future change to zero padding or to the
    prefix cannot quietly reverse it.
    """
    write_report(tmp_path, "scan-2026-09-02", picks=[pick("CCC")])
    write_report(tmp_path, "scan-2026-10-01", picks=[pick("AAA")])
    write_report(tmp_path, "scan-2026-08-31", picks=[pick("BBB")])

    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-10-01"
    assert [path.name for _when, path in reports.scan_dirs(tmp_path)] == [
        "scan-2026-08-31",
        "scan-2026-09-02",
        "scan-2026-10-01",
    ]


@pytest.mark.parametrize("require_picks", [True, False])
def test_a_crash_artefact_without_picks_json_is_never_a_report(
    tmp_path: Path, require_picks: bool
) -> None:
    """Audit BUG-012/DEBT-001: picks.json is the directory's commit point.

    Unconditional: no caller may opt out of it. A half-written directory that
    counted as "latest" for the executor but not for the confirm is exactly the
    divergence DEBT-001 describes.
    """
    write_report(tmp_path, "scan-2026-08-18", picks=[pick()])
    (tmp_path / "scan-2026-08-19").mkdir()  # crashed before picks.json was written
    (tmp_path / "scan-2026-08-19" / "picks.md").write_text("half a report", encoding="utf-8")

    chosen = reports.latest_scan_dir(tmp_path, require_picks=require_picks)
    assert chosen.name == "scan-2026-08-18"


@pytest.mark.parametrize("require_picks", [True, False])
def test_an_unparseable_picks_json_is_never_a_report(tmp_path: Path, require_picks: bool) -> None:
    """A truncated picks.json is the other half of the same crash (audit BUG-020)."""
    write_report(tmp_path, "scan-2026-08-18", picks=[pick()])
    truncated = tmp_path / "scan-2026-08-19"
    truncated.mkdir()
    (truncated / "picks.json").write_text('{"picks": [', encoding="utf-8")

    chosen = reports.latest_scan_dir(tmp_path, require_picks=require_picks)
    assert chosen.name == "scan-2026-08-18"


@pytest.mark.parametrize("require_picks", [True, False])
def test_nothing_parseable_means_no_scan_at_all(tmp_path: Path, require_picks: bool) -> None:
    (tmp_path / "scan-2026-08-19").mkdir()
    assert reports.latest_scan_dir(tmp_path, require_picks=require_picks) is None


# ---------------------------------------------------------------------------
# the empty-report rule
# ---------------------------------------------------------------------------


def test_an_empty_weekend_report_does_not_shadow_fridays_picks(tmp_path: Path) -> None:
    """Audit BUG-012, reproduced: Saturday's empty scan hid Friday's real one."""
    write_report(tmp_path, "scan-2026-08-14", picks=[pick("AAA")])  # Friday
    write_report(tmp_path, "scan-2026-08-15")  # Saturday: nothing survived dedupe

    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-08-14"


def test_a_watch_only_report_counts_as_populated(tmp_path: Path) -> None:
    """On a $100 account the watch list *is* the report."""
    write_report(tmp_path, "scan-2026-08-14", picks=[pick("AAA")])
    write_report(tmp_path, "scan-2026-08-15", watch=[pick("BBB")])

    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-08-15"


def test_an_empty_report_wins_when_it_is_the_only_candidate(tmp_path: Path) -> None:
    """A genuinely empty night must show its own report, not "there is no scan"."""
    write_report(tmp_path, "scan-2026-08-15")
    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-08-15"


def test_the_newest_empty_report_is_chosen_among_empties(tmp_path: Path) -> None:
    write_report(tmp_path, "scan-2026-08-14")
    write_report(tmp_path, "scan-2026-08-15")
    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-08-15"


def test_require_picks_false_keeps_an_empty_report_as_the_latest(tmp_path: Path) -> None:
    """The executor's view: today's empty scan must not defer to a superseded day.

    Re-executing an older report at today's prices is worse than refusing, so
    ``require_picks=False`` returns the newest *parseable* report even when it
    is empty — while still skipping the crash artefacts above.
    """
    write_report(tmp_path, "scan-2026-08-14", picks=[pick("AAA")])
    write_report(tmp_path, "scan-2026-08-15")  # parseable, and empty

    assert reports.latest_scan_dir(tmp_path, require_picks=False).name == "scan-2026-08-15"
    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-08-14"


def test_the_two_flags_agree_whenever_the_newest_report_holds_anything(tmp_path: Path) -> None:
    write_report(tmp_path, "scan-2026-08-14", picks=[pick("AAA")])
    write_report(tmp_path, "scan-2026-08-15", watch=[pick("BBB")])

    assert reports.latest_scan_dir(tmp_path).name == "scan-2026-08-15"
    assert reports.latest_scan_dir(tmp_path, require_picks=False).name == "scan-2026-08-15"


def test_the_answer_does_not_depend_on_directory_iteration_order(tmp_path: Path) -> None:
    for day in range(10, 20):
        write_report(tmp_path, f"scan-2026-08-{day}")
    write_report(tmp_path, "scan-2026-08-11", picks=[pick()])

    assert {reports.latest_scan_dir(tmp_path).name for _ in range(5)} == {"scan-2026-08-11"}


# ---------------------------------------------------------------------------
# retention (contract A17 / audit LEAK-002)
# ---------------------------------------------------------------------------


def test_reports_older_than_the_retention_window_are_removed(tmp_path: Path) -> None:
    old = write_report(tmp_path, "scan-2026-01-01", picks=[pick()])
    kept = write_report(tmp_path, "scan-2026-08-01", picks=[pick()])

    removed = reports.prune_scan_dirs(tmp_path, asof=date(2026, 8, 18))

    assert removed == [old]
    assert not old.exists()
    assert kept.is_dir()


def test_pruning_keeps_everything_that_is_not_a_scan_directory(tmp_path: Path) -> None:
    (tmp_path / "backtest").mkdir()
    (tmp_path / "scan-notes.txt").write_text("mine", encoding="utf-8")
    (tmp_path / "scan-nope").mkdir()
    write_report(tmp_path, "scan-2020-01-01", picks=[pick()])

    reports.prune_scan_dirs(tmp_path, asof=date(2026, 8, 18))

    assert (tmp_path / "backtest").is_dir()
    assert (tmp_path / "scan-notes.txt").is_file()
    assert (tmp_path / "scan-nope").is_dir()
    assert not (tmp_path / "scan-2020-01-01").exists()


def test_the_retention_boundary_is_the_configured_window(tmp_path: Path) -> None:
    asof = date(2026, 8, 18)
    edge = write_report(tmp_path, f"scan-{asof.replace(month=5, day=20)}")  # 90 days back
    older = write_report(tmp_path, f"scan-{asof.replace(month=5, day=19)}")  # 91 days back

    assert reports.RETAIN_SCAN_DAYS == 90
    reports.prune_scan_dirs(tmp_path, asof=asof)

    assert edge.is_dir()
    assert not older.exists()


def test_pruning_can_be_switched_off(tmp_path: Path) -> None:
    old = write_report(tmp_path, "scan-2001-01-01")
    assert reports.prune_scan_dirs(tmp_path, asof=date(2026, 8, 18), keep_days=0) == []
    assert old.is_dir()
