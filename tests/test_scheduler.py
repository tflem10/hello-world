"""launchd scheduling — plists parsed for real, ``launchctl`` never run for real.

Every test here injects a runner, so no test can reach the real service manager,
and every plist is written under ``tmp_path`` rather than
``~/Library/LaunchAgents``. The timezone-mismatch warning gets its own tests
because it is the one thing about launchd scheduling that will silently do the
wrong thing at the wrong hour, every day, for months.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from conftest import build_config
from swing.scheduler import launchd

#: Captured before the autouse fixture below replaces it, so the real
#: implementation can still be exercised.
REAL_LOCAL_TIMEZONE_NAME = launchd.local_timezone_name


@pytest.fixture
def cfg(tmp_path: Path):
    return build_config(tmp_path)


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "LaunchAgents"
    directory.mkdir()
    return directory


class Runner:
    """Records every ``launchctl`` invocation and replays canned results."""

    def __init__(self, results: dict[str, subprocess.CompletedProcess] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        key = args[1] if len(args) > 1 else ""
        return self.results.get(key, subprocess.CompletedProcess(args, 0, stdout="", stderr=""))

    @property
    def subcommands(self) -> list[str]:
        return [call[1] for call in self.calls]


@pytest.fixture(autouse=True)
def _no_real_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: even an un-injected runner cannot reach the real thing."""

    def refuse(*args, **kwargs):
        raise AssertionError(f"a test tried to run a real subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture(autouse=True)
def _matching_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a machine whose clock agrees with the config, so warnings are opt-in."""
    monkeypatch.setattr(launchd, "local_timezone_name", lambda: "America/New_York")


# ---------------------------------------------------------------------------
# plist generation
# ---------------------------------------------------------------------------


def test_scan_plist_has_the_structure_launchd_expects(cfg, tmp_path: Path) -> None:
    plist = launchd.build_plist(cfg, launchd.LABEL_SCAN)

    assert plist["Label"] == "com.swing.scan"
    assert plist["ProgramArguments"][1] == "scan"
    assert plist["ProgramArguments"][0].endswith("/swing")
    assert plist["StartCalendarInterval"] == {"Hour": 17, "Minute": 30}
    assert plist["RunAtLoad"] is False
    assert plist["WorkingDirectory"] == str(launchd.repo_root())
    logs = tmp_path / "state" / "logs"
    assert plist["StandardOutPath"] == str(logs / "scan.out.log")
    assert plist["StandardErrorPath"] == str(logs / "scan.err.log")


def test_confirm_plist_runs_the_confirm_command_in_the_morning(cfg) -> None:
    plist = launchd.build_plist(cfg, launchd.LABEL_CONFIRM)
    assert plist["Label"] == "com.swing.confirm"
    assert plist["ProgramArguments"][1] == "confirm"
    assert plist["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}


def test_times_come_from_the_configuration(tmp_path: Path) -> None:
    cfg = build_config(tmp_path, schedule={"scan_time": "16:05", "confirm_time": "08:45"})
    assert launchd.build_plist(cfg, launchd.LABEL_SCAN)["StartCalendarInterval"] == {
        "Hour": 16,
        "Minute": 5,
    }
    assert launchd.build_plist(cfg, launchd.LABEL_CONFIRM)["StartCalendarInterval"] == {
        "Hour": 8,
        "Minute": 45,
    }


def test_plist_names_an_absolute_executable(cfg) -> None:
    """launchd has no useful PATH, so a bare 'swing' would never start."""
    plist = launchd.build_plist(cfg, launchd.LABEL_SCAN)
    assert Path(plist["ProgramArguments"][0]).is_absolute()
    assert "PATH" in plist["EnvironmentVariables"]


def test_unknown_label_is_refused(cfg) -> None:
    with pytest.raises(ValueError, match="not a swing launchd agent"):
        launchd.build_plist(cfg, "com.swing.nope")


def test_written_plists_round_trip_through_plistlib(cfg, agents_dir: Path) -> None:
    written = launchd.write_plists(cfg, target_dir=agents_dir)

    assert set(written) == {"com.swing.scan", "com.swing.confirm"}
    for label, path in written.items():
        assert path.parent == agents_dir
        assert path.name == f"{label}.plist"
        with path.open("rb") as handle:
            parsed = plistlib.load(handle)
        assert parsed["Label"] == label
        assert isinstance(parsed["StartCalendarInterval"]["Hour"], int)
        assert parsed["ProgramArguments"][0].endswith("/swing")


def test_write_plists_creates_the_log_directory(cfg, agents_dir: Path, tmp_path: Path) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    assert (tmp_path / "state" / "logs").is_dir()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_bootstraps_both_agents(cfg, agents_dir: Path, capsys) -> None:
    runner = Runner()
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    domain = f"gui/{os.getuid()}"
    assert runner.subcommands == ["bootstrap", "bootstrap"]
    for call in runner.calls:
        assert call[0] == "launchctl"
        assert call[2] == domain
        assert call[3].endswith(".plist")

    out = capsys.readouterr().out
    assert "Installed com.swing.scan" in out
    assert "Installed com.swing.confirm" in out
    assert "17:30" in out and "09:00" in out


def test_install_falls_back_to_load_when_bootstrap_is_unsupported(
    cfg, agents_dir: Path, capsys
) -> None:
    runner = Runner(
        {"bootstrap": subprocess.CompletedProcess([], 64, stdout="", stderr="Unrecognized")}
    )
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    assert runner.subcommands == ["bootstrap", "load", "bootstrap", "load"]
    assert "Installed com.swing.scan" in capsys.readouterr().out


def test_install_is_idempotent_when_already_loaded(cfg, agents_dir: Path, capsys) -> None:
    runner = Runner(
        {
            "bootstrap": subprocess.CompletedProcess(
                [], 5, stdout="", stderr="service already bootstrapped"
            )
        }
    )
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    assert runner.subcommands == ["bootstrap", "bootstrap"]  # no fallback attempted
    assert "Installed com.swing.scan" in capsys.readouterr().out


def test_install_explains_a_launchd_refusal_without_crashing(cfg, agents_dir: Path, capsys) -> None:
    runner = Runner(
        {
            "bootstrap": subprocess.CompletedProcess([], 64, stdout="", stderr="Bootstrap failed"),
            "load": subprocess.CompletedProcess([], 1, stdout="", stderr="Load failed: nope"),
        }
    )
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "launchd would not load com.swing.scan" in out
    assert "Load failed: nope" in out
    assert (agents_dir / "com.swing.scan.plist").is_file()  # still written for manual loading


def test_install_warns_when_the_binary_is_missing(
    cfg, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(launchd, "swing_executable", lambda root=None: Path("/nowhere/swing"))
    launchd.install(cfg, runner=Runner(), target_dir=agents_dir)
    assert "does not exist yet" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the timezone trap
# ---------------------------------------------------------------------------


def test_timezone_warning_when_the_mac_disagrees_with_the_config(
    cfg, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launchd, "local_timezone_name", lambda: "America/Los_Angeles")
    warning = launchd.timezone_warning(cfg)

    assert warning is not None
    assert "WARNING" in warning
    assert "America/Los_Angeles" in warning
    assert "America/New_York" in warning
    assert "17:30" in warning


def test_no_timezone_warning_when_they_agree(cfg) -> None:
    assert launchd.timezone_warning(cfg) is None


def test_install_prints_the_timezone_warning(
    cfg, agents_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(launchd, "local_timezone_name", lambda: "Europe/London")
    launchd.install(cfg, runner=Runner(), target_dir=agents_dir)
    assert "WARNING" in capsys.readouterr().out


def test_timezone_falls_back_to_comparing_offsets(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the zone name cannot be read, an offset difference still warns."""
    monkeypatch.setattr(launchd, "local_timezone_name", lambda: None)
    monkeypatch.setattr(launchd, "_offsets_differ", lambda name: True)
    warning = launchd.timezone_warning(cfg)
    assert warning is not None
    assert "unknown (offsets differ)" in warning


def test_local_timezone_name_reads_the_tz_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert REAL_LOCAL_TIMEZONE_NAME() == "Asia/Tokyo"


def test_local_timezone_name_falls_back_to_the_system_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TZ", raising=False)
    name = REAL_LOCAL_TIMEZONE_NAME()
    assert name is None or "/" in name


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_boots_out_and_deletes_both_plists(cfg, agents_dir: Path, capsys) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    runner = Runner()
    launchd.uninstall(cfg, runner=runner, target_dir=agents_dir)

    domain = f"gui/{os.getuid()}"
    assert runner.subcommands == ["bootout", "bootout"]
    assert [call[2] for call in runner.calls] == [
        f"{domain}/com.swing.scan",
        f"{domain}/com.swing.confirm",
    ]
    assert not list(agents_dir.glob("*.plist"))
    assert "Removed com.swing.scan" in capsys.readouterr().out


def test_uninstall_is_safe_when_nothing_is_installed(cfg, agents_dir: Path, capsys) -> None:
    launchd.uninstall(cfg, runner=Runner(), target_dir=agents_dir)
    assert "was not installed" in capsys.readouterr().out


def test_uninstall_falls_back_to_unload(cfg, agents_dir: Path) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    runner = Runner(
        {"bootout": subprocess.CompletedProcess([], 64, stdout="", stderr="Unrecognized")}
    )
    launchd.uninstall(cfg, runner=runner, target_dir=agents_dir)
    assert runner.subcommands == ["bootout", "unload", "bootout", "unload"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


LIST_OUTPUT = """PID\tStatus\tLabel
-\t0\tcom.swing.scan
4711\t0\tcom.apple.Something
"""


def test_status_reports_both_agents_with_paths_and_next_fire(
    cfg, agents_dir: Path, tmp_path: Path, capsys
) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    runner = Runner({"list": subprocess.CompletedProcess([], 0, stdout=LIST_OUTPUT, stderr="")})
    launchd.status(cfg, runner=runner, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "com.swing.scan" in out
    assert "com.swing.confirm" in out
    assert "loaded, idle" in out  # scan is listed with pid "-"
    assert "NOT loaded" in out  # confirm is not in the launchctl output
    assert str(agents_dir / "com.swing.scan.plist") in out
    assert "every day at 17:30 local machine time" in out
    assert "every day at 09:00 local machine time" in out
    assert str(tmp_path / "state" / "logs" / "scan.out.log") in out
    assert str(tmp_path / "state" / "logs" / "confirm.err.log") in out


def test_status_marks_a_missing_plist(cfg, agents_dir: Path, capsys) -> None:
    runner = Runner({"list": subprocess.CompletedProcess([], 0, stdout=LIST_OUTPUT, stderr="")})
    launchd.status(cfg, runner=runner, target_dir=agents_dir)
    assert "(missing)" in capsys.readouterr().out


def test_status_shows_a_running_job(cfg, agents_dir: Path, capsys) -> None:
    output = "PID\tStatus\tLabel\n9821\t0\tcom.swing.scan\n"
    runner = Runner({"list": subprocess.CompletedProcess([], 0, stdout=output, stderr="")})
    launchd.status(cfg, runner=runner, target_dir=agents_dir)
    assert "running (pid 9821" in capsys.readouterr().out


def test_status_only_calls_launchctl_list(cfg, agents_dir: Path, capsys) -> None:
    runner = Runner({"list": subprocess.CompletedProcess([], 0, stdout=LIST_OUTPUT, stderr="")})
    launchd.status(cfg, runner=runner, target_dir=agents_dir)
    assert runner.subcommands == ["list"]
