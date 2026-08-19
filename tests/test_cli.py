"""Tests for FROZEN CONTRACT 2 — the ``swing`` command line.

The CLI is a shell around entry points that other work packages own, so these
tests check the shell: that every command exists, that ``--help`` works with
nothing else implemented, that a missing module produces one sentence instead
of a traceback, and that the kill switch works today regardless.
"""

from __future__ import annotations

import contextlib
import importlib
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from swing import cli as cli_module
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

#: (argv, module, entry function) for every command whose work another package owns.
#: This is the Contract 2 wiring table — the module and function names are the contract.
ENTRY_POINTS: list[tuple[list[str], str, str]] = [
    (["scan"], "swing.alerts.pipeline", "run_scan"),
    (["confirm"], "swing.alerts.pipeline", "run_confirm"),
    (["backtest"], "swing.backtest.runner", "run_backtest"),
    (["report"], "swing.backtest.report", "print_latest"),
    (["auth"], "swing.broker.auth", "login"),
    (["auth", "--check"], "swing.broker.auth", "check"),
    (["notify-test"], "swing.alerts.channels", "notify_test"),
    (["schedule", "install"], "swing.scheduler.launchd", "install"),
    (["execute"], "swing.broker.executor", "run_execute"),
    (["positions"], "swing.broker.executor", "print_positions"),
]

#: pytest ids that stay readable as the parametrisation grows.
ENTRY_IDS = [f"{' '.join(argv)} -> {module}.{func}" for argv, module, func in ENTRY_POINTS]


def entry_available(module: str, func: str) -> bool:
    """True when ``module`` imports and exposes ``func`` in this checkout.

    The package is built one work package at a time, so which entry points
    exist depends on construction order. Tests that care about the *missing*
    case ask this first rather than assuming.
    """
    try:
        return hasattr(importlib.import_module(module), func)
    except ImportError:
        return False


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


@pytest.fixture
def break_imports(monkeypatch: pytest.MonkeyPatch):
    """Make the CLI's lazy import of the named modules raise ImportError.

    This is how the graceful-degradation path stays covered forever: it no
    longer depends on which sibling packages happen to be built yet.
    """
    real_import = cli_module.import_module

    def _break(*modules: str) -> None:
        broken = set(modules)

        def fake_import(name: str, package: str | None = None):
            if name in broken:
                raise ImportError(f"No module named {name!r}")
            return real_import(name, package)

        monkeypatch.setattr(cli_module, "import_module", fake_import)

    return _break


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


@pytest.mark.parametrize(("argv", "module", "func"), ENTRY_POINTS, ids=ENTRY_IDS)
def test_unimplemented_commands_fail_cleanly(
    argv: list[str], module: str, func: str, cfg_file: Path
) -> None:
    """A command whose module has not been built yet explains itself and stops.

    Skipped once the owning package lands: from that point the command runs
    real code, and how it behaves is that package's test surface, not this
    one's. The guarantee itself never stops being checked — see
    ``test_graceful_degradation_covers_every_command``, which forces the
    missing-module case for all ten commands regardless of build order.
    """
    if entry_available(module, func):
        pytest.skip(f"{module}.{func} has landed; its behaviour is that package's test surface")

    result = runner.invoke(app, ["--config", str(cfg_file), *argv])
    text = output(result)

    assert result.exit_code == 2
    assert module in text
    assert "not available yet" in text
    assert "Traceback" not in text
    # a clean typer.Exit, never a leaked exception
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize(("argv", "module", "func"), ENTRY_POINTS, ids=ENTRY_IDS)
def test_graceful_degradation_covers_every_command(
    argv: list[str], module: str, func: str, cfg_file: Path, break_imports
) -> None:
    """Force the import to fail, and assert the clean exit — for every command.

    Unlike the test above this never skips, because the failure is constructed
    rather than observed. It also never invokes a real entry point, so no
    sibling package's config or network paths are touched.
    """
    break_imports(module)

    result = runner.invoke(app, ["--config", str(cfg_file), *argv])
    text = output(result)

    assert result.exit_code == 2
    assert module in text
    assert "not available yet" in text
    assert "not been implemented" in text
    assert "Traceback" not in text
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


class FakeScanError(Exception):
    """Stands in for ``swing.alerts.pipeline.ScanError`` in these tests."""


def fake_pipeline(monkeypatch: pytest.MonkeyPatch, **entries) -> types.ModuleType:
    """Install a stand-in ``swing.alerts.pipeline`` carrying its own ``ScanError``.

    The CLI imports ``ScanError`` from the pipeline lazily, so a fake module has
    to supply one — which keeps these tests decoupled from whatever the alerts
    package is doing to the real one.
    """
    pipeline = types.ModuleType("swing.alerts.pipeline")
    pipeline.ScanError = FakeScanError  # type: ignore[attr-defined]
    for name, func in entries.items():
        setattr(pipeline, name, func)
    monkeypatch.setitem(sys.modules, "swing.alerts.pipeline", pipeline)
    return pipeline


def test_an_implemented_command_is_called_with_the_contract_signature(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def run_scan(cfg, **kwargs):
        calls.append({"cfg": cfg, **kwargs})
        return tmp_path / "reports" / "scan-2026-08-18"

    fake_pipeline(monkeypatch, run_scan=run_scan)

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
# BUG-021 — a failed nightly run must look failed
# ---------------------------------------------------------------------------


def test_scan_asks_the_pipeline_to_be_strict_about_delivery(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole value of this system is the notification arriving."""
    calls: list[dict] = []

    def run_scan(cfg, **kwargs):
        calls.append(kwargs)
        return tmp_path / "reports" / "scan-2026-08-18"

    fake_pipeline(monkeypatch, run_scan=run_scan)
    result = runner.invoke(app, ["--config", str(cfg_file), "scan"])

    assert result.exit_code == 0, output(result)
    assert calls[0]["strict_delivery"] is True


def test_confirm_asks_the_pipeline_to_be_strict_about_delivery(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def run_confirm(cfg, **kwargs):
        calls.append(kwargs)
        return tmp_path / "reports" / "scan-2026-08-18" / "confirm.json"

    fake_pipeline(monkeypatch, run_confirm=run_confirm)
    result = runner.invoke(app, ["--config", str(cfg_file), "confirm"])

    assert result.exit_code == 0, output(result)
    assert calls[0] == {"dry_run": False, "strict_delivery": True}


@pytest.mark.parametrize("command", ["scan", "confirm"])
def test_a_refusal_is_one_sentence_and_exit_2(
    command: str, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit BUG-021: a ScanError used to arrive as a traceback, with exit code 0."""
    sentence = "Every notification channel failed (ntfy, email), so nobody was told."

    def boom(cfg, **kwargs):
        raise FakeScanError(sentence)

    fake_pipeline(monkeypatch, run_scan=boom, run_confirm=boom)
    result = runner.invoke(app, ["--config", str(cfg_file), command])
    text = output(result)

    assert result.exit_code == 2
    assert sentence in text
    assert "Traceback" not in text
    assert "FakeScanError" not in text
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_unexpected_error_is_not_swallowed(
    cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only refusals are turned into sentences; a real bug still surfaces as one."""

    def boom(cfg, **kwargs):
        raise ValueError("a genuine bug")

    fake_pipeline(monkeypatch, run_scan=boom)
    result = runner.invoke(app, ["--config", str(cfg_file), "scan"])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)


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


def test_kill_engages_the_switch_without_the_broker_module(
    cfg_file: Path, tmp_path: Path, break_imports
) -> None:
    break_imports("swing.broker.executor")

    result = runner.invoke(app, ["--config", str(cfg_file), "kill"])
    text = output(result)

    assert result.exit_code == 0, text
    assert (tmp_path / "state" / KILL_FILENAME).is_file()
    assert "ENGAGED" in text
    assert "broker" in text.lower()  # it says the cancel step could not run
    assert "Traceback" not in text


def test_kill_off_releases_the_switch(cfg_file: Path, tmp_path: Path, break_imports) -> None:
    break_imports("swing.broker.executor")
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


def test_kill_switch_state_is_visible_to_the_state_module(cfg_file: Path, break_imports) -> None:
    from swing.config import load_config

    break_imports("swing.broker.executor")

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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["schedule"], "status"),
        (["schedule", "install"], "install"),
        (["schedule", "uninstall"], "uninstall"),
    ],
)
def test_schedule_action_maps_to_the_matching_entry_point(
    argv: list[str], expected: str, cfg_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare `schedule` command means `status`; each action calls its own function."""
    called: list[str] = []
    launchd = types.ModuleType("swing.scheduler.launchd")
    for name in ("install", "uninstall", "status"):
        setattr(launchd, name, lambda cfg, _name=name: called.append(_name))
    monkeypatch.setitem(sys.modules, "swing.scheduler.launchd", launchd)

    result = runner.invoke(app, ["--config", str(cfg_file), *argv])

    assert result.exit_code == 0, output(result)
    assert called == [expected]


def test_schedule_rejects_an_unknown_action(cfg_file: Path) -> None:
    result = runner.invoke(app, ["--config", str(cfg_file), "schedule", "explode"])
    assert result.exit_code != 0
