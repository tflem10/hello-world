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

#: Likewise for ``subprocess.run``: the autouse fixture refuses every real
#: subprocess so no test can reach ``launchctl``, but the generated nightly
#: wrapper is a shell script whose whole value is its runtime behaviour, and
#: asserting on the *text* of a shell script proves nothing. These tests run it
#: for real against a stub ``swing``.
REAL_RUN = subprocess.run


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


class FakeLaunchd:
    """A runner that models the part of launchd that actually bit us.

    Real ``launchctl`` keeps a job's definition **in memory**. Bootstrapping a
    label it already holds is a no-op: the rewritten plist on disk is ignored
    and the previous argument vector goes on running. An installer that only
    asks "is something loaded?" therefore proves nothing, which is how the
    evening agent ran ``swing scan`` directly for two nights after an install
    that had already rewritten the plist to run the nightly wrapper.

    ``loaded`` maps label -> the argument vector launchd would execute.
    """

    def __init__(self, loaded: dict[str, list[str]] | None = None) -> None:
        self.loaded: dict[str, list[str]] = dict(loaded or {})
        self.calls: list[list[str]] = []

    # -- launchctl subcommands ---------------------------------------------

    def _bootstrap(self, plist_path: str) -> subprocess.CompletedProcess:
        with Path(plist_path).open("rb") as handle:
            parsed = plistlib.load(handle)
        label = parsed["Label"]
        if label in self.loaded:
            # The definition already in memory wins, and launchctl says so in a
            # way that reads like success to anything looking for "already".
            return subprocess.CompletedProcess(
                [], 37, stdout="", stderr=f"Bootstrap failed: 37: {label} is already loaded"
            )
        self.loaded[label] = [str(a) for a in parsed["ProgramArguments"]]
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def _print(self, target: str) -> subprocess.CompletedProcess:
        label = target.rsplit("/", 1)[-1]
        arguments = self.loaded.get(label)
        if arguments is None:
            return subprocess.CompletedProcess(
                [], 113, stdout="", stderr="Could not find service in domain"
            )
        rendered = "\n".join(f"\t\t{argument}" for argument in arguments)
        return subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                f"{target} = {{\n\tactive count = 0\n\tstate = not running\n\n"
                f"\tprogram = {arguments[0]}\n\targuments = {{\n{rendered}\n\t}}\n\n"
                f"\tdomain = gui/501\n}}\n"
            ),
            stderr="",
        )

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        subcommand = args[1] if len(args) > 1 else ""
        if subcommand == "bootstrap":
            return self._bootstrap(args[3])
        if subcommand == "bootout":
            label = args[2].rsplit("/", 1)[-1]
            if self.loaded.pop(label, None) is None:
                return subprocess.CompletedProcess(
                    [], 3, stdout="", stderr="Boot-out failed: 3: No such process"
                )
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        if subcommand == "print":
            return self._print(args[2])
        if subcommand == "list":
            label = args[2] if len(args) > 2 else ""
            code = 0 if label in self.loaded else 113
            return subprocess.CompletedProcess([], code, stdout="", stderr="")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

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
    # The evening agent runs the generated wrapper — scan, then the shadow arms
    # — because launchd executes exactly one ProgramArguments list.
    assert plist["ProgramArguments"] == ["/bin/sh", str(launchd.nightly_script_path(cfg))]
    assert plist["StartCalendarInterval"] == [
        {"Hour": 17, "Minute": 30, "Weekday": day} for day in (1, 2, 3, 4, 5)
    ]
    assert plist["RunAtLoad"] is False
    assert plist["WorkingDirectory"] == str(launchd.repo_root())
    logs = tmp_path / "state" / "logs"
    assert plist["StandardOutPath"] == str(logs / "scan.out.log")
    assert plist["StandardErrorPath"] == str(logs / "scan.err.log")


def test_confirm_plist_runs_the_confirm_command_in_the_morning(cfg) -> None:
    plist = launchd.build_plist(cfg, launchd.LABEL_CONFIRM)
    assert plist["Label"] == "com.swing.confirm"
    assert plist["ProgramArguments"][1] == "confirm"
    assert [entry["Hour"] for entry in plist["StartCalendarInterval"]] == [9] * 5
    assert [entry["Minute"] for entry in plist["StartCalendarInterval"]] == [0] * 5


def test_both_agents_fire_on_weekdays_only(cfg) -> None:
    """Audit BUG-012: seven-day agents let a Saturday scan shadow Friday's picks.

    launchd's own schema documents each StartCalendarInterval field as a single
    integer, so Monday-to-Friday is five entries rather than one entry holding a
    list of weekdays — a plist launchd will not parse is a schedule that never
    runs at all.
    """
    for label in launchd.AGENTS:
        intervals = launchd.build_plist(cfg, label)["StartCalendarInterval"]
        assert isinstance(intervals, list)
        assert [entry["Weekday"] for entry in intervals] == [1, 2, 3, 4, 5]
        assert all(isinstance(entry["Weekday"], int) for entry in intervals)


def test_times_come_from_the_configuration(tmp_path: Path) -> None:
    cfg = build_config(tmp_path, schedule={"scan_time": "16:05", "confirm_time": "08:45"})
    scan = launchd.build_plist(cfg, launchd.LABEL_SCAN)["StartCalendarInterval"]
    confirm = launchd.build_plist(cfg, launchd.LABEL_CONFIRM)["StartCalendarInterval"]
    assert {(entry["Hour"], entry["Minute"]) for entry in scan} == {(16, 5)}
    assert {(entry["Hour"], entry["Minute"]) for entry in confirm} == {(8, 45)}


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
        assert isinstance(parsed["StartCalendarInterval"][0]["Hour"], int)
        assert Path(parsed["ProgramArguments"][0]).is_absolute()
        if label == launchd.LABEL_CONFIRM:
            assert parsed["ProgramArguments"][0].endswith("/swing")
        else:
            assert parsed["ProgramArguments"] == ["/bin/sh", str(launchd.nightly_script_path(cfg))]


def test_write_plists_creates_the_log_directory(cfg, agents_dir: Path, tmp_path: Path) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    assert (tmp_path / "state" / "logs").is_dir()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


#: launchctl's answer when the job genuinely is not loaded — what `print` says
#: after a failed bootstrap.
NOT_LOADED = {
    "print": subprocess.CompletedProcess([], 113, stdout="", stderr="Could not find service"),
    "list": subprocess.CompletedProcess([], 113, stdout="", stderr="Could not find service"),
}


def test_install_bootstraps_both_agents(cfg, agents_dir: Path, capsys) -> None:
    fake = FakeLaunchd()
    launchd.install(cfg, runner=fake, target_dir=agents_dir)

    domain = f"gui/{os.getuid()}"
    # Old definition out, new one in, then read back what launchd actually holds.
    assert fake.subcommands == ["bootout", "bootstrap", "print"] * 2
    for call in fake.calls:
        assert call[0] == "launchctl"
    bootout, bootstrap, printed = fake.calls[0], fake.calls[1], fake.calls[2]
    assert bootout[2] == f"{domain}/com.swing.scan"
    assert bootstrap[2] == domain
    assert bootstrap[3].endswith(".plist")
    assert printed[2] == f"{domain}/com.swing.scan"

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

    assert runner.subcommands == ["bootout", "bootstrap", "load", "print"] * 2
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

    assert runner.subcommands == ["bootout", "bootstrap", "print"] * 2  # no fallback attempted
    assert "Installed com.swing.scan" in capsys.readouterr().out


def test_install_explains_a_launchd_refusal_without_crashing(cfg, agents_dir: Path, capsys) -> None:
    runner = Runner(
        {
            "bootstrap": subprocess.CompletedProcess([], 64, stdout="", stderr="Bootstrap failed"),
            "load": subprocess.CompletedProcess([], 1, stdout="", stderr="Load failed: nope"),
            **NOT_LOADED,
        }
    )
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "FAILED to install com.swing.scan" in out
    assert "Load failed: nope" in out
    assert (agents_dir / "com.swing.scan.plist").is_file()  # still written for manual loading


def test_install_does_not_claim_success_when_bootstrap_exits_five(
    cfg, agents_dir: Path, capsys
) -> None:
    """Audit BUG-023: exit 5 is EIO, not "already loaded" — macOS uses it for real failures."""
    runner = Runner(
        {
            "bootstrap": subprocess.CompletedProcess(
                [], 5, stdout="", stderr="Bootstrap failed: 5: Input/output error"
            ),
            **NOT_LOADED,
        }
    )
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "Installed" not in out
    assert "FAILED to install com.swing.scan" in out
    assert "FAILED to install com.swing.confirm" in out
    assert "Input/output error" in out  # launchctl's own words reach the user
    assert "Nothing was installed" in out


def test_install_verifies_with_launchctl_before_claiming_success(
    cfg, agents_dir: Path, capsys
) -> None:
    """A clean exit code is not evidence: launchd is asked whether the job is there."""
    runner = Runner(dict(NOT_LOADED))  # bootstrap succeeds, the job still is not loaded
    launchd.install(cfg, runner=runner, target_dir=agents_dir)

    assert runner.subcommands == ["bootout", "bootstrap", "print", "list"] * 2
    out = capsys.readouterr().out
    assert "Installed" not in out
    assert "Could not find service" in out


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
    assert "Monday to Friday at 17:30 local machine time" in out
    assert "Monday to Friday at 09:00 local machine time" in out
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


# ---------------------------------------------------------------------------
# the nightly wrapper — scan, then the forward paper-trading arms
# ---------------------------------------------------------------------------


def stub_swing(tmp_path: Path) -> Path:
    """A stand-in ``swing`` that records its arguments and obeys two env vars.

    Every subcommand appends its argument list to ``trace.txt`` and writes a
    line to each of stdout and stderr, so a test can prove both *what* ran, in
    *what order*, and *where its output went*.
    """
    binary = tmp_path / "bin" / "swing"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/bin/sh\n"
        'echo "$*" >> "$TRACE"\n'
        'echo "stdout: $*"\n'
        'echo "stderr: $*" >&2\n'
        'if [ "$1" = "scan" ]; then exit "${SCAN_STATUS:-0}"; fi\n'
        'exit "${SHADOW_STATUS:-0}"\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def run_wrapper(
    cfg, tmp_path: Path, *, scan_status: int = 0, shadow_status: int = 0
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Generate the wrapper, run it for real against the stub, return (result, trace).

    The trace is reset each time so a test may call this more than once and
    still read one run's argument list.
    """
    trace = tmp_path / "trace.txt"
    trace.unlink(missing_ok=True)
    binary = stub_swing(tmp_path)
    script = launchd.write_nightly_script(cfg, root=tmp_path, executable=binary)
    result = REAL_RUN(
        ["/bin/sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "TRACE": str(trace),
            "SCAN_STATUS": str(scan_status),
            "SHADOW_STATUS": str(shadow_status),
        },
    )
    lines = trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []
    return result, lines


def test_the_evening_job_runs_the_generated_wrapper(cfg, agents_dir: Path) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    script = launchd.nightly_script_path(cfg)

    assert script.is_file()
    assert os.access(script, os.X_OK)
    assert script.read_text().startswith("#!/bin/sh")


def test_the_morning_job_still_runs_confirm_directly(cfg) -> None:
    """Nothing is chained to the confirm, so it keeps the simplest possible job."""
    plist = launchd.build_plist(cfg, launchd.LABEL_CONFIRM)
    assert plist["ProgramArguments"][0].endswith("/swing")
    assert plist["ProgramArguments"][1] == "confirm"


def test_the_wrapper_scans_first_then_records_and_scores(cfg, tmp_path: Path) -> None:
    """Ordering is the whole reason this is one job and not two.

    Shadow scoring reads the bars the scan just refreshed; a second launchd job
    "a few minutes later" would be a guess about how long tonight's scan takes.
    """
    result, trace = run_wrapper(cfg, tmp_path)

    assert trace == ["scan", "shadow run", "shadow score"]
    assert result.returncode == 0


def test_a_failing_scan_still_records_the_shadow_arms(cfg, tmp_path: Path) -> None:
    """`;` not `&&`: a missed night of forward evidence cannot be re-lived."""
    result, trace = run_wrapper(cfg, tmp_path, scan_status=2)

    assert trace == ["scan", "shadow run", "shadow score"]
    assert result.returncode == 2  # and the scan's own status still surfaces


def test_a_failing_shadow_never_touches_the_scans_exit_code(cfg, tmp_path: Path) -> None:
    """The scan's exit code is load-bearing: non-zero means nobody heard tonight."""
    healthy, _trace = run_wrapper(cfg, tmp_path, shadow_status=2)
    assert healthy.returncode == 0

    failed, _trace = run_wrapper(cfg, tmp_path, scan_status=2, shadow_status=0)
    assert failed.returncode == 2

    both, trace = run_wrapper(cfg, tmp_path, scan_status=2, shadow_status=70)
    assert both.returncode == 2  # shadow cannot raise it, and cannot clear it
    assert trace == ["scan", "shadow run", "shadow score"]


def test_the_second_shadow_step_runs_even_when_the_first_one_fails(cfg, tmp_path: Path) -> None:
    """A `shadow run` that refuses must not cost the scoring pass for older positions."""
    _result, trace = run_wrapper(cfg, tmp_path, shadow_status=2)
    assert trace == ["scan", "shadow run", "shadow score"]


def test_shadow_output_goes_to_its_own_log_pair(cfg, tmp_path: Path) -> None:
    """A stalled shadow must be visible without reading the scan log, and vice versa."""
    result, _trace = run_wrapper(cfg, tmp_path, shadow_status=2)
    logs = launchd.logs_dir(cfg)

    # The scan's own streams stay on the job's stdout/stderr, which launchd
    # sends to scan.out.log / scan.err.log.
    assert "stdout: scan" in result.stdout
    assert "stderr: scan" in result.stderr
    assert "shadow" not in result.stdout
    assert "shadow" not in result.stderr

    out = (logs / "shadow.out.log").read_text()
    err = (logs / "shadow.err.log").read_text()
    assert "stdout: shadow run" in out
    assert "stdout: shadow score" in out
    assert "shadow run exited 2" in out  # the failure is stated in the log itself
    assert "stderr: shadow run" in err
    assert "scan" not in err


def test_the_shadow_log_appends_rather_than_truncating(cfg, tmp_path: Path) -> None:
    """The series is the evidence; last night's log must survive tonight's run."""
    run_wrapper(cfg, tmp_path)
    run_wrapper(cfg, tmp_path)

    out = (launchd.logs_dir(cfg) / "shadow.out.log").read_text()
    assert out.count("=== shadow ") == 2


def test_the_wrapper_survives_a_deleted_log_directory(cfg, tmp_path: Path) -> None:
    """launchd recreates its own log files; the wrapper's redirect has to too."""
    import shutil

    shutil.rmtree(launchd.logs_dir(cfg), ignore_errors=True)
    result, trace = run_wrapper(cfg, tmp_path)

    assert result.returncode == 0
    assert trace == ["scan", "shadow run", "shadow score"]
    assert (launchd.logs_dir(cfg) / "shadow.out.log").is_file()


def test_the_wrapper_quotes_paths_that_contain_spaces(tmp_path: Path) -> None:
    """A checkout under "~/My Projects" must not silently break the schedule."""
    spaced = tmp_path / "My Projects" / "swing trader"
    spaced.mkdir(parents=True)
    cfg = build_config(spaced)
    binary = spaced / "a dir" / "swing"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "$*" >> "$TRACE"\n', encoding="utf-8")
    binary.chmod(0o755)

    script = launchd.write_nightly_script(cfg, root=spaced, executable=binary)
    trace = spaced / "trace.txt"
    result = REAL_RUN(
        ["/bin/sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["scan", "shadow run", "shadow score"]


def test_install_writes_the_wrapper_and_says_what_it_does(cfg, agents_dir: Path, capsys) -> None:
    launchd.install(cfg, runner=Runner(), target_dir=agents_dir)

    out = capsys.readouterr().out
    assert launchd.nightly_script_path(cfg).is_file()
    assert str(launchd.nightly_script_path(cfg)) in out
    assert "shadow run" in out and "shadow score" in out
    assert "cannot change the scan's exit code" in out


def test_install_is_safe_to_re_run(cfg, agents_dir: Path) -> None:
    """Re-running install rewrites the wrapper in place; nothing accumulates."""
    launchd.install(cfg, runner=Runner(), target_dir=agents_dir)
    first = launchd.nightly_script_path(cfg).read_text()

    launchd.install(cfg, runner=Runner(), target_dir=agents_dir)
    script = launchd.nightly_script_path(cfg)

    assert script.read_text() == first
    assert sorted(p.name for p in script.parent.iterdir()) == [launchd.NIGHTLY_SCRIPT_NAME]
    assert sorted(p.name for p in agents_dir.iterdir()) == [
        "com.swing.confirm.plist",
        "com.swing.scan.plist",
    ]


def test_uninstall_removes_everything_install_wrote(cfg, agents_dir: Path, capsys) -> None:
    launchd.install(cfg, runner=Runner(), target_dir=agents_dir)
    script = launchd.nightly_script_path(cfg)
    assert script.is_file()

    launchd.uninstall(cfg, runner=Runner(), target_dir=agents_dir)

    assert not script.exists()
    assert not script.parent.exists()  # the bin directory goes too when it is empty
    assert not list(agents_dir.glob("*.plist"))
    assert "Removed the nightly wrapper" in capsys.readouterr().out
    # The logs are the record of what happened and are deliberately left alone.
    assert launchd.logs_dir(cfg).is_dir()


def test_uninstall_is_still_safe_when_the_wrapper_is_already_gone(
    cfg, agents_dir: Path, capsys
) -> None:
    launchd.uninstall(cfg, runner=Runner(), target_dir=agents_dir)
    launchd.uninstall(cfg, runner=Runner(), target_dir=agents_dir)
    assert "nightly wrapper was not installed" in capsys.readouterr().out


def test_status_points_at_the_wrapper_and_the_shadow_logs(
    cfg, agents_dir: Path, tmp_path: Path, capsys
) -> None:
    launchd.write_plists(cfg, target_dir=agents_dir)
    runner = Runner({"list": subprocess.CompletedProcess([], 0, stdout=LIST_OUTPUT, stderr="")})
    launchd.status(cfg, runner=runner, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert str(launchd.nightly_script_path(cfg)) in out
    assert "shadow run" in out and "shadow score" in out
    assert str(tmp_path / "state" / "logs" / "shadow.out.log") in out
    assert str(tmp_path / "state" / "logs" / "shadow.err.log") in out


def test_status_flags_a_missing_wrapper(cfg, agents_dir: Path, capsys) -> None:
    """The plist can be loaded while the script it names has been deleted."""
    launchd.write_plists(cfg, target_dir=agents_dir)
    launchd.nightly_script_path(cfg).unlink()
    runner = Runner({"list": subprocess.CompletedProcess([], 0, stdout=LIST_OUTPUT, stderr="")})
    launchd.status(cfg, runner=runner, target_dir=agents_dir)

    assert "(MISSING)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the loaded job must match the plist, not merely exist
# ---------------------------------------------------------------------------


def wrapper_arguments(cfg) -> list[str]:
    return ["/bin/sh", str(launchd.nightly_script_path(cfg))]


def test_install_replaces_a_definition_launchd_is_already_holding(
    cfg, agents_dir: Path, capsys
) -> None:
    """The 24 Aug failure, reproduced: a rewritten plist that launchd ignored.

    The agent was loaded from an older plist that ran `swing scan` directly.
    Install rewrote the file, bootstrapped over the top — a no-op for a label
    already in memory — and reported success, and the scheduler went on running
    the old command for two nights with no shadow record at all.
    """
    stale = ["/Users/tim/Code/swingtrader2/.venv/bin/swing", "scan"]
    fake = FakeLaunchd({launchd.LABEL_SCAN: list(stale)})

    launchd.install(cfg, runner=fake, target_dir=agents_dir)

    assert fake.loaded[launchd.LABEL_SCAN] == wrapper_arguments(cfg)
    assert fake.loaded[launchd.LABEL_SCAN] != stale
    assert "Installed com.swing.scan" in capsys.readouterr().out


def test_install_boots_out_before_bootstrapping(cfg, agents_dir: Path) -> None:
    fake = FakeLaunchd({launchd.LABEL_SCAN: ["/old/swing", "scan"]})
    launchd.install(cfg, runner=fake, target_dir=agents_dir)

    assert fake.subcommands == ["bootout", "bootstrap", "print"] * 2


def test_a_first_install_is_not_confused_by_a_failed_bootout(cfg, agents_dir: Path, capsys) -> None:
    """Nothing to boot out is the normal first install, not an error."""
    fake = FakeLaunchd()
    launchd.install(cfg, runner=fake, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert fake.loaded[launchd.LABEL_SCAN] == wrapper_arguments(cfg)
    assert fake.loaded[launchd.LABEL_CONFIRM][-1] == "confirm"
    assert "Installed com.swing.scan" in out
    assert "Installed com.swing.confirm" in out
    assert "No such process" not in out
    assert "FAILED" not in out


def test_reinstalling_an_identical_job_is_still_a_clean_install(
    cfg, agents_dir: Path, capsys
) -> None:
    fake = FakeLaunchd()
    launchd.install(cfg, runner=fake, target_dir=agents_dir)
    capsys.readouterr()

    launchd.install(cfg, runner=fake, target_dir=agents_dir)

    assert fake.loaded[launchd.LABEL_SCAN] == wrapper_arguments(cfg)
    assert "Installed com.swing.scan" in capsys.readouterr().out


def test_install_refuses_to_claim_success_when_launchd_kept_the_old_command(
    cfg, agents_dir: Path, capsys
) -> None:
    """The check has to compare, not just count.

    This models a launchd that accepts every call and still refuses to update
    the definition — so a fix that boots out but verifies only "something is
    loaded" passes the test above and fails this one.
    """

    class StubbornLaunchd(FakeLaunchd):
        def __call__(self, args, **kwargs):
            if len(args) > 1 and args[1] == "bootout":
                self.calls.append(list(args))  # accepted, but nothing changes
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            return super().__call__(args, **kwargs)

    stale = ["/old/venv/bin/swing", "scan"]
    fake = StubbornLaunchd({launchd.LABEL_SCAN: list(stale)})

    launchd.install(cfg, runner=fake, target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "FAILED to install com.swing.scan" in out
    assert "DIFFERENT command" in out
    assert "/old/venv/bin/swing scan" in out  # what launchd would really run
    assert str(launchd.nightly_script_path(cfg)) in out  # what the plist says
    assert f"launchctl bootout {launchd._domain()}/com.swing.scan" in out
    assert "Installed com.swing.scan" not in out
    # The morning agent is independent and still installs.
    assert "Installed com.swing.confirm" in out


def test_install_says_so_when_it_cannot_read_the_loaded_arguments(
    cfg, agents_dir: Path, capsys
) -> None:
    """An unverifiable install must not read as a verified one."""

    class OldMacLaunchd(FakeLaunchd):
        def _print(self, target: str) -> subprocess.CompletedProcess:
            label = target.rsplit("/", 1)[-1]
            if label not in self.loaded:
                return subprocess.CompletedProcess([], 113, stdout="", stderr="Could not find")
            return subprocess.CompletedProcess(
                [], 0, stdout=f"{target} = {{\n\tstate = not running\n}}\n", stderr=""
            )

    launchd.install(cfg, runner=OldMacLaunchd(), target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "NOT VERIFIED" in out
    assert "did not include an arguments block" in out
    assert f"launchctl print {launchd._domain()}/com.swing.scan" in out


def test_a_legacy_list_only_answer_is_reported_as_unverified(cfg, agents_dir: Path, capsys) -> None:
    """`launchctl list` proves the job exists and nothing about what it runs."""

    class ListOnlyLaunchd(FakeLaunchd):
        def _print(self, target: str) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="Bad request")

    launchd.install(cfg, runner=ListOnlyLaunchd(), target_dir=agents_dir)

    out = capsys.readouterr().out
    assert "NOT VERIFIED" in out
    assert "does not show arguments" in out


def test_the_arguments_parser_reads_real_launchctl_output() -> None:
    """Pinned against the real thing, captured from `launchctl print` on macOS."""
    printed = (
        "gui/501/com.swing.scan = {\n"
        "\tactive count = 0\n"
        "\tpath = /Users/tim/Library/LaunchAgents/com.swing.scan.plist\n"
        "\ttype = LaunchAgent\n"
        "\tstate = not running\n"
        "\n"
        "\tprogram = /bin/sh\n"
        "\targuments = {\n"
        "\t\t/bin/sh\n"
        "\t\t/Users/tim/.swing/bin/swing-nightly.sh\n"
        "\t}\n"
        "\n"
        "\tworking directory = /Users/tim/Code/swingtrader2\n"
        "}\n"
    )
    assert launchd._parse_arguments(printed) == [
        "/bin/sh",
        "/Users/tim/.swing/bin/swing-nightly.sh",
    ]
    assert launchd._parse_arguments("gui/501/x = {\n\tstate = running\n}\n") is None
    assert launchd._parse_arguments("") is None
