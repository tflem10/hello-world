"""Tests for FROZEN CONTRACT 8 — swing.state.

The journal is the only thing that remembers what the system did, and the kill
switch is the last line of defence, so both are tested for the boring failure
modes: crashes mid-write, corrupt files, and repeated calls.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from swing.state import (
    JOURNAL_FILENAME,
    KILL_FILENAME,
    OPEN_ORDER_STATUS,
    STATUSES,
    Journal,
    PickRecord,
    clear_kill,
    engage_kill,
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
    journal = Journal.load(test_cfg)
    journal.add_picks([make_pick("AAPL", date.today().isoformat())])
    assert journal.recently_picked("AAPL", 5) is True


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


def test_a_corrupt_journal_is_backed_up_and_replaced(test_cfg) -> None:
    path = test_cfg.paths.state_dir / JOURNAL_FILENAME
    path.write_text("{ this is not json", encoding="utf-8")

    with pytest.warns(UserWarning, match="could not be read"):
        journal = Journal.load(test_cfg)

    assert journal.picks == []
    backup = test_cfg.paths.state_dir / "journal.corrupt.json"
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == "{ this is not json"
    assert not path.exists()

    journal.add_picks([make_pick()])
    assert Journal.load(test_cfg).picks[0].symbol == "AAPL"


def test_a_second_corruption_does_not_clobber_the_first_backup(test_cfg) -> None:
    path = test_cfg.paths.state_dir / JOURNAL_FILENAME
    for body in ("first corruption", "second corruption"):
        path.write_text(body, encoding="utf-8")
        with pytest.warns(UserWarning):
            Journal.load(test_cfg)

    assert (test_cfg.paths.state_dir / "journal.corrupt.json").read_text() == "first corruption"
    assert (test_cfg.paths.state_dir / "journal.corrupt.1.json").read_text() == "second corruption"


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
