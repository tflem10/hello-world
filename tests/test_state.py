"""Tests for FROZEN CONTRACT 8 — swing.state.

The journal is the only thing that remembers what the system did, and the kill
switch is the last line of defence, so both are tested for the boring failure
modes: crashes mid-write, corrupt files, repeated calls — and, since the audit,
two processes writing at once.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from swing import state as state_mod
from swing.state import (
    ARCHIVE_AFTER_DAYS,
    JOURNAL_ARCHIVE_FILENAME,
    JOURNAL_FILENAME,
    KILL_FILENAME,
    OPEN_ORDER_STATUS,
    STATUSES,
    Journal,
    PickRecord,
    atomic_write_text,
    clear_kill,
    engage_kill,
    file_lock,
    kill_active,
    kill_path,
)


def make_pick(
    symbol: str = "AAPL",
    day: str = "2026-08-18",
    *,
    kind: str = "pick",
    status: str = "drafted",
    shares: int = 3,
) -> PickRecord:
    return PickRecord(
        symbol=symbol,
        date=day,
        kind=kind,
        entry=100.0,
        stop=95.0,
        shares=shares,
        risk_amount=15.0,
        score=1.23,
        atr=2.5,
        earnings_date="2026-09-01",
        earnings_known=True,
        thesis="20-day breakout with volume confirmation",
        status=status,
    )


# ---------------------------------------------------------------------------
# PickRecord
# ---------------------------------------------------------------------------


def test_pick_record_has_the_contract_fields() -> None:
    pick = make_pick()
    expected = {
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
    assert set(pick.to_dict()) == expected


def test_pick_record_round_trips_through_a_dict() -> None:
    pick = make_pick()
    assert PickRecord.from_dict(pick.to_dict()) == pick


def test_pick_record_tolerates_old_and_unknown_keys() -> None:
    restored = PickRecord.from_dict({"symbol": "MSFT", "date": "2026-01-05", "legacy_field": 1})
    assert restored.symbol == "MSFT"
    assert restored.status == "drafted"
    assert restored.earnings_date is None


def test_all_contract_statuses_are_known() -> None:
    assert set(STATUSES) == {
        "drafted",
        "confirmed",
        "invalidated",
        "ordered",
        "filled",
        "closed",
    }


# ---------------------------------------------------------------------------
# Journal basics
# ---------------------------------------------------------------------------


def test_load_on_a_fresh_machine_gives_an_empty_journal(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    assert journal.picks == []
    assert journal.open_orders() == []
    assert journal.positions() == []
    assert not journal.path.exists()  # nothing written until something is recorded


def test_add_picks_persists_immediately(test_cfg) -> None:
    Journal.load(test_cfg).add_picks([make_pick("AAPL"), make_pick("MSFT")])

    reloaded = Journal.load(test_cfg)
    assert [p.symbol for p in reloaded.picks] == ["AAPL", "MSFT"]
    assert reloaded.picks[0].thesis == "20-day breakout with volume confirmation"
    assert (test_cfg.paths.state_dir / JOURNAL_FILENAME).is_file()


def test_journal_file_is_readable_json(test_cfg) -> None:
    Journal.load(test_cfg).add_picks([make_pick()])
    raw = json.loads((test_cfg.paths.state_dir / JOURNAL_FILENAME).read_text())
    assert raw["version"] == 1
    assert raw["picks"][0]["symbol"] == "AAPL"
    assert raw["orders"] == []


def test_rerunning_a_scan_replaces_the_same_day_pick(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", shares=3)])
    journal.add_picks([make_pick("AAPL", shares=9)])

    picks = Journal.load(test_cfg).picks
    assert len(picks) == 1
    assert picks[0].shares == 9


def test_picks_for_filters_by_day(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("AAPL", "2026-08-17"),
            make_pick("MSFT", "2026-08-18"),
            make_pick("NVDA", "2026-08-18"),
        ]
    )

    assert [p.symbol for p in journal.picks_for(date(2026, 8, 18))] == ["MSFT", "NVDA"]
    assert journal.picks_for(date(2026, 8, 16)) == []


def test_picks_are_kept_in_date_then_symbol_order(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("NVDA", "2026-08-18"), make_pick("AAPL", "2026-08-17")])
    assert [(p.date, p.symbol) for p in journal.picks] == [
        ("2026-08-17", "AAPL"),
        ("2026-08-18", "NVDA"),
    ]


# ---------------------------------------------------------------------------
# dedupe window
# ---------------------------------------------------------------------------


def test_recently_picked_uses_the_asof_date(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-10")])

    asof = date(2026, 8, 18)  # 8 days later
    assert journal.recently_picked("AAPL", 10, asof=asof) is True
    assert journal.recently_picked("AAPL", 8, asof=asof) is False  # boundary is exclusive
    assert journal.recently_picked("AAPL", 9, asof=asof) is True
    assert journal.recently_picked("MSFT", 30, asof=asof) is False


def test_a_pick_dated_asof_itself_does_not_block(test_cfg) -> None:
    """Audit BUG-010: re-running tonight's scan must not dedupe away its own picks.

    Run 1 journals the picks; run 2 (the most natural user action there is —
    re-run because the notification did not arrive) used to see ``delta == 0``,
    dedupe every one of them, and overwrite the night's report with an empty
    one. The window now starts the day *after* the record.
    """
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])

    assert journal.recently_picked("AAPL", 7, asof=date(2026, 8, 18)) is False
    # ... while yesterday's pick still blocks, which is what dedupe is for
    assert journal.recently_picked("AAPL", 7, asof=date(2026, 8, 19)) is True


def test_watch_entries_do_not_block_by_default(test_cfg) -> None:
    """Audit BUG-011: a watch line commits no capital, so it suppresses nothing.

    On a small account every qualifying name is a watch entry, so journalling
    them into the dedupe window emptied the watch list within a week — the
    dominant failure mode for the account this system was built for.
    """
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-17", kind="watch")])

    asof = date(2026, 8, 18)
    assert journal.recently_picked("AAPL", 7, asof=asof) is False
    # a caller that really wants both kinds can still ask for them
    assert journal.recently_picked("AAPL", 7, asof=asof, kinds=("pick", "watch")) is True
    assert journal.recently_picked("AAPL", 7, asof=asof, kinds=("watch",)) is True
    assert journal.recently_picked("AAPL", 7, asof=asof, kinds=()) is False


def test_a_real_pick_still_blocks_when_a_watch_entry_shares_the_symbol(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("AAPL", "2026-08-14", kind="watch"),
            make_pick("AAPL", "2026-08-16", kind="pick"),
        ]
    )
    assert journal.recently_picked("AAPL", 7, asof=date(2026, 8, 18)) is True


def test_recently_picked_is_case_insensitive_and_ignores_the_future(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("aapl", "2026-08-10"), make_pick("MSFT", "2026-09-01")])

    asof = date(2026, 8, 18)
    assert journal.recently_picked("AAPL", 30, asof=asof) is True
    assert journal.recently_picked("MSFT", 30, asof=asof) is False  # dated after asof


def test_recently_picked_with_a_zero_window_is_always_false(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])
    assert journal.recently_picked("AAPL", 0, asof=date(2026, 8, 18)) is False


def test_recently_picked_defaults_to_today(test_cfg) -> None:
    """The default reference is today — and today's own pick does not block (BUG-010)."""
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("AAPL", (date.today() - timedelta(days=2)).isoformat()),
            make_pick("MSFT", date.today().isoformat()),
        ]
    )
    assert journal.recently_picked("AAPL", 5) is True
    assert journal.recently_picked("MSFT", 5) is False


# ---------------------------------------------------------------------------
# status transitions
# ---------------------------------------------------------------------------


def test_update_status_persists(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])
    journal.update_status("AAPL", date(2026, 8, 18), "confirmed")

    assert Journal.load(test_cfg).picks[0].status == "confirmed"


@pytest.mark.parametrize("status", STATUSES)
def test_every_contract_status_is_accepted(test_cfg, status: str) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])
    journal.update_status("AAPL", date(2026, 8, 18), status)
    assert journal.picks[0].status == status


def test_unknown_status_is_refused(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick()])
    with pytest.raises(ValueError, match="not a valid pick status"):
        journal.update_status("AAPL", date(2026, 8, 18), "sold-ish")


def test_updating_a_pick_that_is_not_there_is_an_error(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    with pytest.raises(KeyError):
        journal.update_status("AAPL", date(2026, 8, 18), "filled")


# ---------------------------------------------------------------------------
# orders and positions
# ---------------------------------------------------------------------------


def test_record_order_and_open_orders(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "open"})
    journal.record_order({"id": 2, "symbol": "MSFT", "status": "filled"})
    journal.record_order({"id": 3, "symbol": "NVDA"})  # no status ⇒ assumed open

    reloaded = Journal.load(test_cfg)
    assert [o["id"] for o in reloaded.open_orders()] == [1, 3]
    assert len(reloaded.orders) == 3


def test_record_order_defaults_the_status_to_open(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL"})

    stored = Journal.load(test_cfg).orders[0]
    assert stored["status"] == OPEN_ORDER_STATUS
    assert (
        json.loads((test_cfg.paths.state_dir / JOURNAL_FILENAME).read_text())["orders"][0]["status"]
        == "open"
    )


def test_record_order_does_not_mutate_the_caller_dict(test_cfg) -> None:
    order = {"id": 1, "symbol": "AAPL"}
    Journal.load(test_cfg).record_order(order)
    assert "status" not in order


def test_open_orders_counts_only_open_ones(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    for status in ("open", "filled", "cancelled", "rejected", "replaced"):
        journal.record_order({"symbol": "AAPL", "status": status})

    assert [o["status"] for o in journal.open_orders()] == ["open"]


def test_open_orders_returns_copies(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL"})
    journal.open_orders()[0]["symbol"] = "TAMPERED"
    assert journal.open_orders()[0]["symbol"] == "AAPL"


def test_record_order_rejects_non_dicts(test_cfg) -> None:
    with pytest.raises(TypeError):
        Journal.load(test_cfg).record_order(["not", "a", "dict"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# order status transitions
#
# Without these an order recorded as "open" could never move, so cancelling it
# at the broker left the journal insisting it was still working and the
# duplicate guardrail refused that symbol forever.
# ---------------------------------------------------------------------------


def test_update_order_status_closes_an_order_and_reports_the_count(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "open"})

    assert journal.update_order_status("AAPL", "cancelled") == 1
    assert journal.open_orders() == []
    assert journal.orders[0]["status"] == "cancelled"


def test_update_order_status_persists_across_a_reload(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "open"})
    journal.update_order_status("AAPL", "cancelled")

    reloaded = Journal.load(test_cfg)
    assert reloaded.open_orders() == []
    assert reloaded.orders[0]["status"] == "cancelled"


def test_update_order_status_touches_every_order_for_the_symbol(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "open"})
    journal.record_order({"id": 2, "symbol": "AAPL", "status": "open"})
    journal.record_order({"id": 3, "symbol": "MSFT", "status": "open"})

    assert journal.update_order_status("AAPL", "cancelled") == 2
    assert [o["id"] for o in journal.open_orders()] == [3]


def test_update_order_status_only_status_filters(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "open"})
    journal.record_order({"id": 2, "symbol": "AAPL", "status": "filled"})

    assert journal.update_order_status("AAPL", "cancelled", only_status="open") == 1

    by_id = {o["id"]: o["status"] for o in Journal.load(test_cfg).orders}
    assert by_id == {1: "cancelled", 2: "filled"}  # the filled one is left alone


def test_update_order_status_only_status_that_matches_nothing_changes_nothing(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "filled"})

    assert journal.update_order_status("AAPL", "cancelled", only_status="open") == 0
    assert journal.orders[0]["status"] == "filled"


def test_update_order_status_for_an_unknown_symbol_is_a_no_op(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL", "status": "open"})

    assert journal.update_order_status("NVDA", "cancelled") == 0
    assert journal.orders[0]["status"] == "open"


def test_update_order_status_on_an_empty_journal_is_a_no_op(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    assert journal.update_order_status("AAPL", "cancelled") == 0
    assert not journal.path.exists()  # a no-op writes nothing


def test_update_order_status_matches_symbols_case_insensitively(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "aapl", "status": "open"})

    assert journal.update_order_status(" AAPL ", "cancelled") == 1
    assert journal.orders[0]["status"] == "cancelled"


def test_a_legacy_order_without_a_status_is_open_and_still_updatable(test_cfg) -> None:
    """Journals written before the status field existed must keep working."""
    path = test_cfg.paths.state_dir / JOURNAL_FILENAME
    path.write_text(
        json.dumps({"version": 1, "picks": [], "orders": [{"id": 1, "symbol": "AAPL"}]}),
        encoding="utf-8",
    )

    journal = Journal.load(test_cfg)
    assert [o["id"] for o in journal.open_orders()] == [1]  # no status ⇒ open

    assert journal.update_order_status("AAPL", "cancelled", only_status="open") == 1
    assert journal.open_orders() == []
    assert Journal.load(test_cfg).orders[0]["status"] == "cancelled"


def test_update_order_status_needs_a_status_to_write(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"id": 1, "symbol": "AAPL"})
    with pytest.raises(ValueError, match="needs a status"):
        journal.update_order_status("AAPL", "   ")


def test_the_kill_then_reorder_cycle_frees_the_symbol_again(test_cfg) -> None:
    """The bug this amendment exists for, end to end."""
    journal = Journal.load(test_cfg)
    journal.record_order({"symbol": "AAPL", "date": "2026-08-18", "status": "open"})
    assert len(journal.open_orders()) == 1  # duplicate guardrail would refuse AAPL

    # `swing kill` cancels at the broker and tells the journal
    assert journal.update_order_status("AAPL", "cancelled", only_status="open") == 1

    assert Journal.load(test_cfg).open_orders() == []  # AAPL is orderable again


def test_positions_shows_filled_picks_only(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("AAPL", "2026-08-18", shares=3),
            make_pick("MSFT", "2026-08-18"),
            make_pick("NVDA", "2026-08-18"),
        ]
    )
    journal.update_status("AAPL", date(2026, 8, 18), "filled")
    journal.update_status("MSFT", date(2026, 8, 18), "ordered")
    journal.update_status("NVDA", date(2026, 8, 18), "closed")

    positions = Journal.load(test_cfg).positions()
    assert [p["symbol"] for p in positions] == ["AAPL"]
    assert positions[0]["shares"] == 3
    assert positions[0]["entry"] == 100.0
    assert positions[0]["stop"] == 95.0


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------


def test_writes_leave_no_temporary_files_behind(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick()])
    journal.record_order({"id": 1})

    leftovers = [p.name for p in test_cfg.paths.state_dir.iterdir() if ".tmp" in p.name]
    assert leftovers == []


def _backups(cfg) -> list[Path]:
    return sorted(Path(cfg.paths.state_dir).glob("journal.corrupt.*.json"))


def test_a_corrupt_journal_is_backed_up_and_replaced(test_cfg) -> None:
    path = test_cfg.paths.state_dir / JOURNAL_FILENAME
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.warns(UserWarning, match="could not be read"):
        journal = Journal.load(test_cfg)

    assert journal.picks == []
    backups = _backups(test_cfg)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{ this is not json"
    assert not path.exists()

    journal.add_picks([make_pick()])
    assert Journal.load(test_cfg).picks[0].symbol == "AAPL"


def test_the_backup_name_carries_the_pid_so_two_processes_cannot_collide(test_cfg) -> None:
    """Audit BUG-024: two processes hitting one corrupt journal must not fight over a name."""
    path = test_cfg.paths.state_dir / JOURNAL_FILENAME
    path.write_text("nonsense", encoding="utf-8")
    with pytest.warns(UserWarning):
        Journal.load(test_cfg)

    name = _backups(test_cfg)[0].name
    assert name.startswith(f"journal.corrupt.{os.getpid()}-")
    assert name.endswith(".json")


def test_losing_the_race_to_back_up_a_corrupt_journal_is_not_fatal(
    test_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit BUG-024 (TOCTOU): the process that arrives second must not die on it."""
    (test_cfg.paths.state_dir / JOURNAL_FILENAME).write_text("nonsense", encoding="utf-8")

    def racing_replace(src, dst):
        raise FileNotFoundError(2, "No such file or directory", str(src))

    monkeypatch.setattr(state_mod.os, "replace", racing_replace)

    with pytest.warns(UserWarning, match="moved it aside at the same moment"):
        journal = Journal.load(test_cfg)

    assert journal.picks == []
    assert journal.recovered is True


def test_recovery_is_announced_so_callers_can_shout_about_it(test_cfg) -> None:
    """Audit BUG-024: an empty journal means full slots and full cash — say so."""
    assert Journal.load(test_cfg).recovered is False

    Journal.load(test_cfg).add_picks([make_pick()])
    assert Journal.load(test_cfg).recovered is False  # a healthy reload is not a recovery

    (test_cfg.paths.state_dir / JOURNAL_FILENAME).write_text("{ nope", encoding="utf-8")
    with pytest.warns(UserWarning) as caught:
        journal = Journal.load(test_cfg)

    assert journal.recovered is True
    assert journal.positions() == []
    assert "check the broker before trading" in str(caught[0].message)


def test_a_second_corruption_does_not_clobber_the_first_backup(test_cfg) -> None:
    path = test_cfg.paths.state_dir / JOURNAL_FILENAME
    for body in ("first corruption", "second corruption"):
        path.write_text(body, encoding="utf-8")
        with pytest.warns(UserWarning):
            Journal.load(test_cfg)

    bodies = {p.read_text(encoding="utf-8") for p in _backups(test_cfg)}
    assert bodies == {"first corruption", "second corruption"}


def test_a_journal_that_is_valid_json_but_the_wrong_shape_is_handled(test_cfg) -> None:
    (test_cfg.paths.state_dir / JOURNAL_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.warns(UserWarning):
        assert Journal.load(test_cfg).picks == []


def test_a_missing_state_directory_is_created_on_demand(tmp_path: Path, cfg_factory) -> None:
    cfg = cfg_factory(
        paths={"state_dir": tmp_path / "never" / "made", "reports_dir": tmp_path / "r"}
    )
    journal = Journal.load(cfg)
    journal.add_picks([make_pick()])
    assert (tmp_path / "never" / "made" / JOURNAL_FILENAME).is_file()


# ---------------------------------------------------------------------------
# kill switch
# ---------------------------------------------------------------------------


def test_kill_switch_is_off_until_engaged(test_cfg) -> None:
    assert kill_active(test_cfg) is False
    assert kill_path(test_cfg) == test_cfg.paths.state_dir / KILL_FILENAME


def test_engage_and_clear_the_kill_switch(test_cfg) -> None:
    path = engage_kill(test_cfg)
    assert kill_active(test_cfg) is True
    assert path.is_file()

    clear_kill(test_cfg)
    assert kill_active(test_cfg) is False


def test_engage_records_the_reason(test_cfg) -> None:
    engage_kill(test_cfg, reason="broker went haywire")
    assert "broker went haywire" in kill_path(test_cfg).read_text()


def test_engaging_twice_and_clearing_twice_are_both_safe(test_cfg) -> None:
    engage_kill(test_cfg)
    engage_kill(test_cfg)
    assert kill_active(test_cfg) is True

    clear_kill(test_cfg)
    clear_kill(test_cfg)  # must not raise
    assert kill_active(test_cfg) is False


def test_kill_switch_works_when_the_state_directory_does_not_exist(
    tmp_path: Path, cfg_factory
) -> None:
    cfg = cfg_factory(
        paths={"state_dir": tmp_path / "brand" / "new", "reports_dir": tmp_path / "r"}
    )
    assert kill_active(cfg) is False
    engage_kill(cfg)
    assert kill_active(cfg) is True


def test_a_kill_file_made_by_hand_counts(test_cfg) -> None:
    (test_cfg.paths.state_dir / KILL_FILENAME).write_text("touched at 3am\n")
    assert kill_active(test_cfg) is True


# ---------------------------------------------------------------------------
# cross-process locking (audit BUG-001)
#
# The nightly scan holds its journal object across minutes of network fetch.
# Anything that wrote in the meantime — an order from `swing execute`, a fill
# from the morning confirm — used to be erased when the scan finally saved.
# ---------------------------------------------------------------------------


def test_two_journal_objects_writing_different_things_both_survive(test_cfg) -> None:
    """The reproduced lost update: this fails on the pre-lock code.

    ``scan`` loads an empty journal, ``execute`` records an order while the
    scan is still fetching, and the scan then saves its own picks. Before the
    fix the scan's save serialised its stale snapshot and the order vanished.
    """
    scan = Journal.load(test_cfg)  # loaded at the top of the run
    execute = Journal.load(test_cfg)

    execute.record_order({"id": 1, "symbol": "MSFT", "status": "open"})
    scan.add_picks([make_pick("AAPL", "2026-08-18")])

    on_disk = Journal.load(test_cfg)
    assert [p.symbol for p in on_disk.picks] == ["AAPL"]
    assert [o["symbol"] for o in on_disk.orders] == ["MSFT"]


def test_interleaved_mutations_in_both_directions_all_survive(test_cfg) -> None:
    """Audit BUG-001: whoever writes last contributes its change, not its whole world."""
    first = Journal.load(test_cfg)
    first.add_picks([make_pick("AAPL", "2026-08-18")])

    second = Journal.load(test_cfg)  # a fresh process, sees AAPL
    first.record_order({"id": 1, "symbol": "AAPL", "status": "open"})  # stale object writes
    second.add_picks([make_pick("MSFT", "2026-08-18")])  # so does the other one
    first.update_status("AAPL", date(2026, 8, 18), "ordered")

    on_disk = Journal.load(test_cfg)
    assert [(p.symbol, p.status) for p in on_disk.picks] == [
        ("AAPL", "ordered"),
        ("MSFT", "drafted"),
    ]
    assert [o["id"] for o in on_disk.orders] == [1]


def test_a_status_change_sees_a_pick_added_by_another_process(test_cfg) -> None:
    """The merge is a re-read, so a mutator can act on records it never loaded."""
    stale = Journal.load(test_cfg)  # empty
    Journal.load(test_cfg).add_picks([make_pick("NVDA", "2026-08-18")])

    stale.update_status("NVDA", date(2026, 8, 18), "confirmed")
    assert Journal.load(test_cfg).picks[0].status == "confirmed"


def test_file_lock_puts_its_sidecar_next_to_the_target(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "thing.json"
    with file_lock(target):
        assert (tmp_path / "nested" / "thing.json.lock").is_file()
    assert not target.exists()  # locking creates the lock, never the file


def test_file_lock_can_be_taken_again_once_released(tmp_path: Path) -> None:
    target = tmp_path / "thing.json"
    for _ in range(3):
        with file_lock(target, timeout=1.0):
            pass
    with pytest.raises(RuntimeError), file_lock(target, timeout=1.0):
        raise RuntimeError("boom")
    with file_lock(target, timeout=1.0):  # released even when the body raised
        pass


def test_a_second_holder_times_out_with_a_plain_sentence(tmp_path: Path) -> None:
    target = tmp_path / "thing.json"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with file_lock(target, timeout=5.0):
            holding.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(timeout=5.0)
        with pytest.raises(TimeoutError) as excinfo, file_lock(target, timeout=0.1):
            pass  # pragma: no cover - the lock is held, so this never runs
    finally:
        release.set()
        holder.join(timeout=5.0)

    message = str(excinfo.value)
    assert str(target) in message
    assert "0.1 seconds" in message
    assert "Traceback" not in message
    assert message.endswith(".")


def test_the_journal_lock_serialises_two_threads(test_cfg) -> None:
    """Both threads' picks are on disk afterwards, in one consistent file."""
    barrier = threading.Barrier(2, timeout=5.0)
    errors: list[BaseException] = []

    def write(symbol: str) -> None:
        journal = Journal.load(test_cfg)
        try:
            barrier.wait()
            journal.add_picks([make_pick(symbol, "2026-08-18")])
        except BaseException as exc:  # noqa: BLE001 - re-raised in the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(sym,)) for sym in ("AAA", "BBB")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert errors == []
    assert [p.symbol for p in Journal.load(test_cfg).picks] == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# batched status changes (audit LEAK-001)
# ---------------------------------------------------------------------------


def test_update_statuses_applies_everything_in_one_write(
    test_cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("AAPL", "2026-08-18"),
            make_pick("MSFT", "2026-08-18"),
            make_pick("NVDA", "2026-08-18"),
        ]
    )
    writes: list[Path] = []
    original = state_mod.atomic_write_text

    def counting_write(path: Path, text: str) -> None:
        writes.append(Path(path))
        original(path, text)

    monkeypatch.setattr(state_mod, "atomic_write_text", counting_write)
    day = date(2026, 8, 18)
    changed = journal.update_statuses(
        [("AAPL", day, "filled"), ("MSFT", day, "invalidated"), ("nvda", day, "confirmed")]
    )

    assert changed == 3
    assert len(writes) == 1  # one save, not one per pick
    assert {(p.symbol, p.status) for p in Journal.load(test_cfg).picks} == {
        ("AAPL", "filled"),
        ("MSFT", "invalidated"),
        ("NVDA", "confirmed"),
    }


def test_update_statuses_ignores_picks_it_does_not_have(test_cfg) -> None:
    """A fill can arrive for an order older than anything in the live journal."""
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])

    changed = journal.update_statuses(
        [("AAPL", date(2026, 8, 18), "filled"), ("GONE", date(2020, 1, 2), "filled")]
    )
    assert changed == 1


def test_update_statuses_writes_nothing_when_nothing_changes(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    assert journal.update_statuses([]) == 0
    assert not journal.path.exists()

    journal.add_picks([make_pick("AAPL", "2026-08-18", status="filled")])
    assert journal.update_statuses([("AAPL", date(2026, 8, 18), "filled")]) == 0


def test_update_statuses_refuses_an_unknown_status_before_changing_anything(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])

    day = date(2026, 8, 18)
    with pytest.raises(ValueError, match="not a valid pick status"):
        journal.update_statuses([("AAPL", day, "confirmed"), ("AAPL", day, "sold-ish")])

    assert Journal.load(test_cfg).picks[0].status == "drafted"  # nothing applied


def test_update_statuses_accepts_iso_strings_as_well_as_dates(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", "2026-08-18")])
    assert journal.update_statuses([("AAPL", "2026-08-18", "ordered")]) == 1


def test_update_statuses_merges_like_every_other_mutator(test_cfg) -> None:
    stale = Journal.load(test_cfg)
    Journal.load(test_cfg).add_picks([make_pick("AAPL", "2026-08-18")])

    assert stale.update_statuses([("AAPL", date(2026, 8, 18), "filled")]) == 1
    assert Journal.load(test_cfg).picks[0].status == "filled"


# ---------------------------------------------------------------------------
# annotating a recorded order (audit BUG-002)
#
# The order row is written BEFORE the network call, so it has to be findable
# by something that exists before the broker has answered: the client_ref.
# ---------------------------------------------------------------------------


def test_annotate_order_fills_in_the_broker_answer_and_persists(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"symbol": "AAPL", "client_ref": "ref-1", "status": "pending"})

    assert journal.annotate_order("ref-1", order_id="12345", status="open") == 1

    stored = Journal.load(test_cfg).orders[0]
    assert stored["status"] == "open"
    assert stored["order_id"] == "12345"
    assert stored["client_ref"] == "ref-1"
    assert stored["symbol"] == "AAPL"  # untouched fields survive
    assert [o["order_id"] for o in Journal.load(test_cfg).open_orders()] == ["12345"]


def test_annotate_order_touches_only_the_matching_reference(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    journal.record_order({"symbol": "AAPL", "client_ref": "ref-1", "status": "pending"})
    journal.record_order({"symbol": "AAPL", "client_ref": "ref-2", "status": "pending"})
    journal.record_order({"symbol": "MSFT", "status": "open"})  # no ref at all

    assert journal.annotate_order("ref-2", status="open") == 1

    by_ref = {o.get("client_ref"): o["status"] for o in Journal.load(test_cfg).orders}
    assert by_ref == {"ref-1": "pending", "ref-2": "open", None: "open"}


def test_annotate_order_for_an_unknown_reference_writes_nothing(test_cfg) -> None:
    journal = Journal.load(test_cfg)
    assert journal.annotate_order("never-seen", status="open") == 0
    assert not journal.path.exists()

    journal.record_order({"symbol": "AAPL", "client_ref": "ref-1", "status": "pending"})
    before = journal.path.read_text(encoding="utf-8")
    assert journal.annotate_order("ref-9", status="open") == 0
    assert journal.path.read_text(encoding="utf-8") == before


def test_annotate_order_merges_like_every_other_mutator(test_cfg) -> None:
    """Audit BUG-001: the placement path records and annotates across a network call."""
    placing = Journal.load(test_cfg)
    placing.record_order({"symbol": "AAPL", "client_ref": "ref-1", "status": "pending"})

    # something else writes while the order is in flight at the broker
    other = Journal.load(test_cfg)
    other.add_picks([make_pick("MSFT", "2026-08-18")])

    assert placing.annotate_order("ref-1", order_id="42", status="open") == 1

    on_disk = Journal.load(test_cfg)
    assert [p.symbol for p in on_disk.picks] == ["MSFT"]  # the other write survived
    assert on_disk.orders[0]["order_id"] == "42"  # and so did this one


def test_annotate_order_sees_an_order_recorded_by_another_process(test_cfg) -> None:
    stale = Journal.load(test_cfg)  # loaded before the order existed
    Journal.load(test_cfg).record_order(
        {"symbol": "NVDA", "client_ref": "ref-7", "status": "pending"}
    )

    assert stale.annotate_order("ref-7", status="open") == 1
    assert Journal.load(test_cfg).open_orders()[0]["symbol"] == "NVDA"


@pytest.mark.parametrize("bad", ["", "   ", None, 7])
def test_annotate_order_needs_a_real_reference(test_cfg, bad) -> None:
    journal = Journal.load(test_cfg)
    with pytest.raises(ValueError, match="client reference") as excinfo:
        journal.annotate_order(bad, status="open")
    assert str(excinfo.value).endswith(".")


def test_annotate_order_refuses_to_rewrite_the_reference_itself(test_cfg) -> None:
    """The client_ref is the identity: rewriting it orphans the row."""
    journal = Journal.load(test_cfg)
    journal.record_order({"symbol": "AAPL", "client_ref": "ref-1", "status": "pending"})

    with pytest.raises(ValueError, match="cannot change an order's client_ref"):
        journal.annotate_order("ref-1", client_ref="ref-2", status="open")
    with pytest.raises(ValueError, match="cannot change an order's client_ref"):
        journal.annotate_order("ref-1", **{"client_ref": "ref-2"})

    assert Journal.load(test_cfg).orders[0]["client_ref"] == "ref-1"
    assert Journal.load(test_cfg).orders[0]["status"] == "pending"  # nothing applied


def test_the_client_ref_is_positional_so_the_field_check_can_fire(test_cfg) -> None:
    """`client_ref=` in the call means "a field called client_ref", never the lookup key."""
    parameter = inspect.signature(Journal.annotate_order).parameters["client_ref"]
    assert parameter.kind is inspect.Parameter.POSITIONAL_ONLY


# ---------------------------------------------------------------------------
# retention (audit LEAK-001)
#
# Every mutation rewrites the whole document, so an unbounded journal makes the
# dedupe scan, the race window and the write itself all grow forever.
# ---------------------------------------------------------------------------


def _days_before(reference: str, days: int) -> str:
    return (date.fromisoformat(reference) - timedelta(days=days)).isoformat()


def test_stale_watch_and_drafted_picks_move_to_the_archive(test_cfg) -> None:
    """Audit LEAK-001: picks that never became trades do not live forever."""
    today = "2026-08-18"
    old = _days_before(today, ARCHIVE_AFTER_DAYS + 1)
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("OLDWATCH", old, kind="watch"),
            make_pick("OLDDRAFT", old),
            make_pick("OLDFILL", old, status="filled"),
            make_pick("FRESH", today),
        ]
    )

    live = Journal.load(test_cfg)
    assert [p.symbol for p in live.picks] == ["OLDFILL", "FRESH"]  # held positions stay

    archive = json.loads((test_cfg.paths.state_dir / JOURNAL_ARCHIVE_FILENAME).read_text())
    assert {p["symbol"] for p in archive["picks"]} == {"OLDWATCH", "OLDDRAFT"}
    assert archive["orders"] == []
    # nothing is lost: the archived records round-trip as PickRecords
    restored = [PickRecord.from_dict(p) for p in archive["picks"]]
    assert {r.kind for r in restored} == {"watch", "pick"}
    assert all(r.date == old for r in restored)


def test_recent_records_are_never_archived(test_cfg) -> None:
    today = "2026-08-18"
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("A", _days_before(today, ARCHIVE_AFTER_DAYS - 1), kind="watch"),
            make_pick("B", _days_before(today, 1)),
            make_pick("C", today),
        ]
    )
    journal.record_order({"symbol": "A", "date": _days_before(today, 2), "status": "filled"})

    assert [p.symbol for p in Journal.load(test_cfg).picks] == ["A", "B", "C"]
    assert not (test_cfg.paths.state_dir / JOURNAL_ARCHIVE_FILENAME).exists()


def test_finished_orders_are_archived_but_working_ones_are_not(test_cfg) -> None:
    today = "2026-08-18"
    old = _days_before(today, ARCHIVE_AFTER_DAYS + 5)
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("FRESH", today)])
    for status in ("filled", "cancelled", "rejected", "open", "pending", "unknown"):
        journal.record_order({"symbol": status.upper(), "date": old, "status": status})

    live = Journal.load(test_cfg)
    assert [o["status"] for o in live.orders] == ["open", "pending", "unknown"]

    archive = json.loads((test_cfg.paths.state_dir / JOURNAL_ARCHIVE_FILENAME).read_text())
    assert {o["status"] for o in archive["orders"]} == {"filled", "cancelled", "rejected"}


def test_an_order_with_no_usable_date_is_kept(test_cfg) -> None:
    """Fail safe: if we cannot tell how old it is, it stays."""
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("FRESH", "2026-08-18")])
    journal.record_order({"symbol": "AAPL", "status": "filled"})
    journal.record_order({"symbol": "MSFT", "date": "whenever", "status": "filled"})

    assert len(Journal.load(test_cfg).orders) == 2


def test_the_archive_is_appended_to_never_replaced(test_cfg) -> None:
    today = date(2026, 8, 18)
    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("FIRST", (today - timedelta(days=ARCHIVE_AFTER_DAYS + 1)).isoformat()),
            make_pick("ANCHOR", today.isoformat()),
        ]
    )
    journal.add_picks(
        [make_pick("SECOND", (today - timedelta(days=ARCHIVE_AFTER_DAYS + 2)).isoformat())]
    )

    archive = json.loads((test_cfg.paths.state_dir / JOURNAL_ARCHIVE_FILENAME).read_text())
    assert [p["symbol"] for p in archive["picks"]] == ["FIRST", "SECOND"]
    assert [p.symbol for p in Journal.load(test_cfg).picks] == ["ANCHOR"]


def test_an_unreadable_archive_keeps_the_records_in_the_live_journal(test_cfg) -> None:
    """Losing history quietly is worse than a journal that stays a little too big."""
    (test_cfg.paths.state_dir / JOURNAL_ARCHIVE_FILENAME).write_text("{ broken", encoding="utf-8")
    today = "2026-08-18"

    journal = Journal.load(test_cfg)
    journal.add_picks(
        [
            make_pick("OLD", _days_before(today, ARCHIVE_AFTER_DAYS + 1)),
            make_pick("FRESH", today),
        ]
    )

    assert [p.symbol for p in Journal.load(test_cfg).picks] == ["OLD", "FRESH"]


def test_retention_is_measured_against_the_journal_not_the_wall_clock(test_cfg) -> None:
    """A journal that has gone quiet stops archiving instead of emptying itself."""
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("A", "2020-01-02"), make_pick("B", "2020-01-03")])

    assert [p.symbol for p in Journal.load(test_cfg).picks] == ["A", "B"]

    # an explicit asof is the override, and it retires them
    journal.save(asof=date(2020, 6, 1))
    assert Journal.load(test_cfg).picks == []


# ---------------------------------------------------------------------------
# atomic writes and orphaned temp files (audit LEAK-002)
# ---------------------------------------------------------------------------


def test_atomic_write_text_replaces_the_whole_file(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "file.txt"
    atomic_write_text(target, "one\n")
    atomic_write_text(target, "two\n")

    assert target.read_text(encoding="utf-8") == "two\n"
    assert [p.name for p in tmp_path.joinpath("deep").iterdir()] == ["file.txt"]


def test_a_stale_temp_file_is_swept_on_the_next_write(tmp_path: Path) -> None:
    """A SIGKILL between write and rename leaves an orphan; nothing used to remove it."""
    orphan = tmp_path / ".journal.json.tmp99999"
    orphan.write_text("half a document", encoding="utf-8")
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(orphan, (two_days_ago, two_days_ago))

    fresh = tmp_path / ".journal.json.tmp12345"
    fresh.write_text("a live writer's temp file", encoding="utf-8")

    atomic_write_text(tmp_path / "journal.json", "{}\n")

    assert not orphan.exists()
    assert fresh.exists()  # too young to be an orphan; a concurrent write is safe
