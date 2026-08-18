"""``swing journal`` — the hand-trade recording CLI.

The journal is append-only, so a validation bug here is not recoverable by
editing a row: a wrong line can only be argued with by a later event, and every
downstream consumer (position count, risk total, duplicate suppression) has
already believed it. So every validation test asserts two things — the exit
code the shell sees *and* that nothing at all reached the file. A command that
rejected the input but wrote the event anyway would pass a weaker test.

The happy-path test walks a whole position's life (entry, ratcheted stop,
partial exit, full exit) because the individual events only mean anything as a
sequence: it is the replay in ``open_positions`` that has to end at flat.
"""

from __future__ import annotations

import json

import pytest

from swing.cli import main
from swing.config import Config, load_config
from swing.execution.journal import (
    EVENT_STOP_MOVED,
    journal_add,
    journal_exit,
    journal_show,
    journal_stop,
    open_positions,
    read_events,
    record,
    record_entry,
)


@pytest.fixture
def journal_config(tmp_path) -> Config:
    """A config whose journal is a throwaway file under tmp_path."""
    data = load_config().as_dict()
    data["execution"].update(
        journal_path=str(tmp_path / "journal.jsonl"),
        kill_file=str(tmp_path / "KILL"),
    )
    data["account"].update(equity=10_000.0, risk_pct=0.02, max_position_pct=0.25)
    return Config(data)


def journal_file(cfg: Config):
    return cfg.expand_path(cfg.execution.journal_path)


def only_line(text: str) -> str:
    """The single line an error is required to be."""
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one error line, got {lines}"
    return lines[0]


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------
def test_a_position_can_be_opened_ratcheted_and_closed(journal_config, capsys):
    """entry -> stop up -> partial exit -> full exit -> flat again."""
    assert journal_add(journal_config, "aapl", "10", "190.50", "182.00") == 0
    position = open_positions(journal_config)["AAPL"]
    assert (position.shares, position.entry_price, position.stop) == (10, 190.50, 182.00)

    assert journal_stop(journal_config, "AAPL", "185.25") == 0
    assert open_positions(journal_config)["AAPL"].stop == pytest.approx(185.25)

    assert journal_exit(journal_config, "AAPL", "4", "200.00", reason="trim") == 0
    assert open_positions(journal_config)["AAPL"].shares == 6

    assert journal_exit(journal_config, "AAPL", "6", "205.00") == 0
    assert open_positions(journal_config) == {}

    out = capsys.readouterr().out
    assert "6 shares remaining" in out
    assert "position closed" in out


def test_add_prints_the_resulting_position_line(journal_config, capsys):
    """The point of printing it is to catch a fat-fingered price immediately."""
    journal_add(journal_config, "AAA", "10", "100.00", "95.00")
    out = capsys.readouterr().out
    assert "AAA" in out
    # symbol, shares, entry, stop and the resulting dollar risk (10 * 5.00).
    row = [ln for ln in out.splitlines() if "AAA" in ln][-1].split()
    assert row[:5] == ["AAA", "10", "100.00", "95.00", "50.00"]
    assert len(row[5]) == len("2024-05-01")  # entry date


def test_add_records_the_optional_fields(journal_config):
    journal_add(
        journal_config, "AAA", "10", "100", "95",
        trail="2.5", order_id="ORD-1", note="breakout",
    )
    event = read_events(journal_config)[-1]
    assert event["trail_offset"] == pytest.approx(2.5)
    assert event["order_id"] == "ORD-1"
    assert event["note"] == "breakout"


def test_stop_prints_old_then_new(journal_config, capsys):
    journal_add(journal_config, "AAA", "10", "100", "95")
    capsys.readouterr()
    assert journal_stop(journal_config, "AAA", "97.50") == 0
    assert "95.00 -> 97.50" in capsys.readouterr().out
    event = read_events(journal_config)[-1]
    assert event["type"] == EVENT_STOP_MOVED
    assert event["stop"] == pytest.approx(97.50)


def test_an_equal_stop_is_not_a_lowering(journal_config):
    """Re-stating today's stop is a no-op, not a discipline violation."""
    journal_add(journal_config, "AAA", "10", "100", "95")
    assert journal_stop(journal_config, "AAA", "95") == 0


def test_force_allows_correcting_a_typo_downward(journal_config, capsys):
    journal_add(journal_config, "AAA", "10", "100", "95")
    journal_stop(journal_config, "AAA", "99")  # meant 9.9-style typo
    capsys.readouterr()
    assert journal_stop(journal_config, "AAA", "96", force=True) == 0
    assert open_positions(journal_config)["AAA"].stop == pytest.approx(96.0)
    assert read_events(journal_config)[-1]["forced"] is True


# ---------------------------------------------------------------------------
# add: validation (nothing may reach the journal)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args, expected",
    [
        (("AAA", "10.5", "100", "95"), "shares"),
        (("AAA", "abc", "100", "95"), "shares"),
        (("AAA", "0", "100", "95"), "shares"),
        (("AAA", "-3", "100", "95"), "shares"),
        (("AAA", "10", "0", "95"), "price"),
        (("AAA", "10", "-100", "95"), "price"),
        (("AAA", "10", "nope", "95"), "price"),
        (("AAA", "10", "100", "0"), "stop"),
        (("AAA", "10", "100", "-5"), "stop"),
        (("AAA", "10", "100", "100"), "nonsense"),
        (("AAA", "10", "100", "105"), "nonsense"),
        (("", "10", "100", "95"), "symbol"),
    ],
)
def test_add_rejects_bad_input_without_writing(journal_config, capsys, args, expected):
    assert journal_add(journal_config, *args) == 2
    err = only_line(capsys.readouterr().err)
    assert expected in err
    assert not journal_file(journal_config).exists()


def test_a_stop_above_the_entry_is_refused_by_name(journal_config, capsys):
    """A long stop above the entry would fill instantly for a guaranteed loss."""
    assert journal_add(journal_config, "AAA", "10", "100", "101") == 2
    assert "nonsense" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# exit: validation
# ---------------------------------------------------------------------------
def test_exit_requires_an_open_position(journal_config, capsys):
    assert journal_exit(journal_config, "ZZZ", "1", "10") == 2
    assert "not an open position" in only_line(capsys.readouterr().err)
    assert not journal_file(journal_config).exists()


def test_exit_refuses_to_sell_more_than_is_held(journal_config, capsys):
    journal_add(journal_config, "AAA", "10", "100", "95")
    before = read_events(journal_config)
    capsys.readouterr()

    assert journal_exit(journal_config, "AAA", "11", "110") == 2
    assert "only 10 held" in only_line(capsys.readouterr().err)
    assert read_events(journal_config) == before
    assert open_positions(journal_config)["AAA"].shares == 10


@pytest.mark.parametrize(
    "shares, price, expected",
    [
        ("0", "110", "shares"),
        ("-1", "110", "shares"),
        ("1.5", "110", "shares"),
        ("1", "0", "price"),
        ("1", "-10", "price"),
        ("1", "later", "price"),
    ],
)
def test_exit_rejects_bad_numbers_without_writing(
    journal_config, capsys, shares, price, expected
):
    journal_add(journal_config, "AAA", "10", "100", "95")
    before = read_events(journal_config)
    capsys.readouterr()

    assert journal_exit(journal_config, "AAA", shares, price) == 2
    assert expected in only_line(capsys.readouterr().err)
    assert read_events(journal_config) == before


def test_a_closed_position_cannot_be_exited_again(journal_config, capsys):
    journal_add(journal_config, "AAA", "10", "100", "95")
    journal_exit(journal_config, "AAA", "10", "120")
    capsys.readouterr()
    assert journal_exit(journal_config, "AAA", "1", "120") == 2
    assert "not an open position" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# stop: validation
# ---------------------------------------------------------------------------
def test_stop_requires_an_open_position(journal_config, capsys):
    assert journal_stop(journal_config, "ZZZ", "10") == 2
    assert "not an open position" in only_line(capsys.readouterr().err)
    assert not journal_file(journal_config).exists()


@pytest.mark.parametrize("bad", ["0", "-5", "high"])
def test_stop_rejects_a_nonpositive_stop_without_writing(journal_config, capsys, bad):
    journal_add(journal_config, "AAA", "10", "100", "95")
    before = read_events(journal_config)
    capsys.readouterr()

    assert journal_stop(journal_config, "AAA", bad) == 2
    assert "stop" in only_line(capsys.readouterr().err)
    assert read_events(journal_config) == before


def test_lowering_a_stop_is_refused_and_names_force(journal_config, capsys):
    """Trailing discipline: stops ratchet up. --force exists for typos only, so
    the refusal has to say the word or the user is stuck."""
    journal_add(journal_config, "AAA", "10", "100", "95")
    before = read_events(journal_config)
    capsys.readouterr()

    assert journal_stop(journal_config, "AAA", "90") == 2
    err = only_line(capsys.readouterr().err)
    assert "--force" in err
    assert "95.00" in err and "90.00" in err
    assert read_events(journal_config) == before
    assert open_positions(journal_config)["AAA"].stop == pytest.approx(95.0)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------
def test_show_on_an_empty_journal_is_friendly(journal_config, capsys):
    assert journal_show(journal_config) == 0
    out = capsys.readouterr().out
    assert "empty" in out
    assert "swing journal add" in out


def test_show_renders_one_line_per_event(journal_config, capsys):
    journal_add(journal_config, "AAA", "10", "100.00", "95.00", note="breakout")
    journal_stop(journal_config, "AAA", "97.00")
    journal_exit(journal_config, "AAA", "4", "110.00", reason="trim")
    capsys.readouterr()

    assert journal_show(journal_config) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 + 3  # header + one line per event

    entry, moved, exited = lines[1:]
    assert "entry" in entry and "AAA" in entry and "10 @ 100.00" in entry
    assert "stop 95.00 -> 97.00" in moved
    assert "exit" in exited and "4 @ 110.00" in exited and "trim" in exited
    for line in lines[1:]:
        assert line.strip().startswith("20")  # timestamp first


def test_show_tails_the_log_and_honours_limit(journal_config, capsys):
    for i in range(5):
        record_entry(journal_config, f"S{i}", 1, 10.0 + i, 9.0)
    capsys.readouterr()

    assert journal_show(journal_config, limit="2") == 0
    out = capsys.readouterr().out
    assert "S4" in out and "S3" in out
    assert "S0" not in out and "S2" not in out


def test_show_default_limit_is_twenty(journal_config, capsys):
    for i in range(25):
        record_entry(journal_config, f"S{i:02d}", 1, 10.0, 9.0)
    capsys.readouterr()

    journal_show(journal_config)
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1 + 20
    assert "S24" in lines[-1]


def test_show_skips_corrupt_lines(journal_config, capsys):
    """A half-written line (crash mid-append) must not hide the rest of the log."""
    record_entry(journal_config, "AAA", 10, 100.0, 95.0)
    with journal_file(journal_config).open("a") as fh:
        fh.write("{not json at all\n")
    record_entry(journal_config, "BBB", 5, 50.0, 45.0)
    capsys.readouterr()

    assert journal_show(journal_config) == 0
    out = capsys.readouterr().out
    assert "AAA" in out and "BBB" in out
    assert "not json" not in out


def test_show_renders_events_it_did_not_write(journal_config, capsys):
    """The log also carries drafted/placed/kill events from the executor."""
    record(journal_config, "placed", symbol="AAA", shares=10, price=100.0, order_id="777")
    capsys.readouterr()

    journal_show(journal_config)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "placed" in line and "AAA" in line and "10 @ 100.00" in line and "777" in line


def test_show_rejects_a_nonsense_limit(journal_config, capsys):
    record_entry(journal_config, "AAA", 10, 100.0, 95.0)
    assert journal_show(journal_config, limit="0") == 2
    assert "--limit" in only_line(capsys.readouterr().err)


# ---------------------------------------------------------------------------
# wiring: argparse -> commands -> journal
# ---------------------------------------------------------------------------
@pytest.fixture
def cli_config_path(tmp_path):
    """A real config.toml on disk; everything else comes from the example."""
    path = tmp_path / "config.toml"
    path.write_text(
        "[execution]\n"
        f'journal_path = "{tmp_path / "journal.jsonl"}"\n'
        f'kill_file = "{tmp_path / "KILL"}"\n'
    )
    return path


def test_the_cli_wires_every_subcommand_through(cli_config_path, tmp_path, capsys):
    base = ["-c", str(cli_config_path), "journal"]
    assert main(base + ["add", "aapl", "10", "190.50", "182.00", "--note", "manual"]) == 0
    assert main(base + ["stop", "AAPL", "185.00"]) == 0
    assert main(base + ["exit", "AAPL", "4", "200.00", "--reason", "trim"]) == 0
    assert main(base + ["show", "--limit", "10"]) == 0

    out = capsys.readouterr().out
    assert "AAPL" in out and "6 shares remaining" in out

    events = [json.loads(ln) for ln in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert [e["type"] for e in events] == ["entry", "stop_moved", "exit"]

    # `swing positions` still reads the same journal.
    assert main(["-c", str(cli_config_path), "positions"]) == 0
    assert "AAPL" in capsys.readouterr().out


def test_the_cli_returns_two_on_bad_input(cli_config_path, tmp_path, capsys):
    rc = main(["-c", str(cli_config_path), "journal", "add", "AAA", "10", "100", "105"])
    assert rc == 2
    assert "nonsense" in capsys.readouterr().err
    assert not (tmp_path / "journal.jsonl").exists()


def test_journal_help_lists_the_four_actions(capsys):
    with pytest.raises(SystemExit):
        main(["journal", "--help"])
    out = capsys.readouterr().out
    for action in ("add", "exit", "stop", "show"):
        assert action in out
