"""Tests for FROZEN CONTRACT 2 — the ``swing`` command line.

The CLI is a shell around entry points that other work packages own, so these
tests check the shell: that every command exists, that ``--help`` works with
nothing else implemented, that a missing module produces one sentence instead
of a traceback, and that the kill switch works today regardless.
"""

from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from swing.cli import app
from swing.state import KILL_FILENAME, kill_active

runner = CliRunner()

#: Every command the contract requires, plus `universe` which WP-A implements.
CONTRACT_COMMANDS = [
    "scan",
    "confirm",
    "backtest",
    "report",
    "auth",
    "notify-test",
    "schedule",
    "execute",
    "positions",
    "kill",
]
ALL_COMMANDS = [*CONTRACT_COMMANDS, "universe"]

#: Commands that call into a module another work package owns.
STUB_COMMANDS = [
    (["scan"], "swing.alerts.pipeline"),
    (["confirm"], "swing.alerts.pipeline"),
    (["backtest"], "swing.backtest.runner"),
    (["report"], "swing.backtest.report"),
    (["auth"], "swing.broker.auth"),
    (["auth", "--check"], "swing.broker.auth"),
    (["notify-test"], "swing.alerts.channels"),
    (["schedule", "install"], "swing.scheduler.launchd"),
    (["execute"], "swing.broker.executor"),
    (["positions"], "swing.broker.executor"),
]


def output(result) -> str:
    """Combined stdout + stderr, whichever the installed click exposes."""
    text = result.stdout or ""
    with contextlib.suppress(ValueError, AttributeError):  # pragma: no cover - older click
        text += result.stderr or ""
    return text


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    """A minimal config file whose state and reports live under tmp_path."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[paths]\nstate_dir = "{tmp_path / "state"}"\nreports_dir = "{tmp_path / "reports"}"\n',
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# help and discovery
# ---------------------------------------------------------------------------


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ALL_COMMANDS:
        assert command in result.stdout, f"`{command}` is missing from --help"


def test_the_app_registers_exactly_the_expected_commands() -> None:
    registered = {
        c.name or (c.callback.__name__ if c.callback else "") for c in app.registered_commands
    }
    assert registered == {c.replace("-", "_") if c != "notify-test" else c for c in ALL_COMMANDS}
    assert len(registered) == 11


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_each_command_has_its_own_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert command in result.stdout


def test_no_arguments_prints_help_rather_than_failing_obscurely() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in output(result)


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "swing" in result.stdout


def test_global_config_option_is_documented() -> None:
    result = runner.invoke(app, ["--help"])
    assert "--config" in result.stdout


def test_help_states_the_gating_principle() -> None:
    text = runner.invoke(app, ["--help"]).stdout
    assert "backtest" in text.lower()


# ---------------------------------------------------------------------------
# graceful degradation while other packages are unimplemented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("argv", "module"), STUB_COMMANDS)
def test_unimplemented_commands_fail_cleanly(argv: list[str], module: str, cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), *argv])
    text = output(result)

    assert result.exit_code == 2
    assert module in text
    assert "not available yet" in text
    assert "Traceback" not in text
    # a clean typer.Exit, never a leaked exception
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_module_missing_its_entry_point_also_fails_cleanly(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = types.ModuleType("swing.backtest.report")
    monkeypatch.setitem(sys.modules, "swing.backtest.report", empty)

    result = runner.invoke(app, ["--config", str(cfg_file), "report"])
    text = output(result)

    assert result.exit_code == 2
    assert "print_latest" in text
    assert "Traceback" not in text


def test_an_implemented_command_is_called_with_the_contract_signature(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []
    pipeline = types.ModuleType("swing.alerts.pipeline")

    def run_scan(cfg, *, dry_run=False, force=False, asof=None):
        calls.append({"cfg": cfg, "dry_run": dry_run, "force": force, "asof": asof})
        return tmp_path / "reports" / "scan-2026-08-18"

    pipeline.run_scan = run_scan  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "swing.alerts.pipeline", pipeline)

    result = runner.invoke(
        app, ["--config", str(cfg_file), "scan", "--dry-run", "--force", "--asof", "2026-08-18"]
    )

    assert result.exit_code == 0, output(result)
    assert len(calls) == 1
    assert calls[0]["dry_run"] is True
    assert calls[0]["force"] is True
    assert calls[0]["asof"].isoformat() == "2026-08-18"
    assert calls[0]["cfg"].paths.state_dir == tmp_path / "state"
    assert "scan-2026-08-18" in result.stdout


# ---------------------------------------------------------------------------
# configuration handling
# ---------------------------------------------------------------------------


def test_a_missing_config_file_is_reported_in_plain_english(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.toml"), "report"])
    text = output(result)

    assert result.exit_code == 2
    assert "Configuration problem" in text
    assert "Traceback" not in text


def test_an_invalid_config_value_is_reported_in_plain_english(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[account]\nrisk_pct = 99\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(path), "report"])
    text = output(result)

    assert result.exit_code == 2
    assert "account.risk_pct" in text
    assert "Traceback" not in text


def test_a_bad_asof_date_is_reported_before_anything_else_happens(cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "scan", "--asof", "yesterday"])
    text = output(result)

    assert result.exit_code == 2
    assert "--asof" in text
    assert "YYYY-MM-DD" in text


# ---------------------------------------------------------------------------
# the kill switch must work today, with no broker module in sight
# ---------------------------------------------------------------------------


def test_kill_engages_the_switch_without_the_broker_module(cfg_file: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "kill"])
    text = output(result)

    assert result.exit_code == 0, text
    assert (tmp_path / "state" / KILL_FILENAME).is_file()
    assert "ENGAGED" in text
    assert "broker" in text.lower()  # it says the cancel step could not run
    assert "Traceback" not in text


def test_kill_off_releases_the_switch(cfg_file: Path, tmp_path: Path) -> None:
    runner.invoke(app, ["--config", str(cfg_file), "kill"])
    assert (tmp_path / "state" / KILL_FILENAME).is_file()

    result = runner.invoke(app, ["--config", str(cfg_file), "kill", "--off"])

    assert result.exit_code == 0, output(result)
    assert not (tmp_path / "state" / KILL_FILENAME).exists()
    assert "RELEASED" in output(result)


def test_kill_also_calls_the_broker_when_it_exists(
    cfg_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []
    executor = types.ModuleType("swing.broker.executor")

    def kill(cfg, *, off=False):
        seen.append(off)

    executor.kill = kill  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "swing.broker.executor", executor)

    result = runner.invoke(app, ["--config", str(cfg_file), "kill"])

    assert result.exit_code == 0, output(result)
    assert seen == [False]
    assert (tmp_path / "state" / KILL_FILENAME).is_file()


def test_kill_switch_state_is_visible_to_the_state_module(cfg_file: Path) -> None:
    from swing.config import load_config

    cfg = load_config(cfg_file)
    assert kill_active(cfg) is False
    runner.invoke(app, ["--config", str(cfg_file), "kill"])
    assert kill_active(cfg) is True


# ---------------------------------------------------------------------------
# universe command (implemented by WP-A, so it really runs)
# ---------------------------------------------------------------------------


def test_universe_command_reports_counts(cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "universe"])

    assert result.exit_code == 0, output(result)
    assert "total" in result.stdout
    total = int(result.stdout.split("total:")[1].split()[0])
    assert total > 1400


def test_universe_list_prints_symbols(cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "universe", "--list"])

    assert result.exit_code == 0
    assert "SPY" in result.stdout
    assert "BRK-B" in result.stdout


def test_schedule_defaults_to_status(cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "schedule"])
    assert "swing.scheduler.launchd" in output(result)


def test_schedule_rejects_an_unknown_action(cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "schedule", "explode"])
    assert result.exit_code != 0
