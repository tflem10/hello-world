"""macOS scheduling — two launchd agents, one for the scan and one for the confirm.

``launchd`` is the right tool here and cron is not: a Mac Studio sleeps, and
launchd runs a missed calendar job when the machine wakes while cron simply
loses it. A scan that silently did not happen is the worst possible failure for
this system, so the schedule lives where the OS will catch up.

One sharp edge is unavoidable and is therefore shouted about rather than hidden:
**``StartCalendarInterval`` is interpreted in the machine's local time, not in
``schedule.timezone``.** If the Mac is on Pacific time and the config says
America/New_York, the 17:30 "market close" scan fires at 17:30 Pacific. There is
no launchd key that fixes this, so :func:`install` prints a loud warning and
tells you the two ways out (change the Mac's timezone, or change the times).

Every ``launchctl`` call goes through an injected ``runner`` so the tests can
prove the argument lists without ever touching the real service manager.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "AGENTS",
    "LABEL_CONFIRM",
    "LABEL_SCAN",
    "build_plist",
    "install",
    "launch_agents_dir",
    "local_timezone_name",
    "plist_path",
    "repo_root",
    "status",
    "swing_executable",
    "timezone_warning",
    "uninstall",
    "write_plists",
]

LABEL_SCAN = "com.swing.scan"
LABEL_CONFIRM = "com.swing.confirm"

#: ``label -> (swing subcommand, config attribute holding the time)``.
AGENTS: dict[str, tuple[str, str]] = {
    LABEL_SCAN: ("scan", "scan_time"),
    LABEL_CONFIRM: ("confirm", "confirm_time"),
}

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def launch_agents_dir() -> Path:
    """Where per-user launchd agents live."""
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(label: str, target_dir: Path | None = None) -> Path:
    """Full path of one agent's plist."""
    return (target_dir or launch_agents_dir()) / f"{label}.plist"


def repo_root() -> Path:
    """The checkout root — ``<root>/src/swing/scheduler/launchd.py`` is this file."""
    return Path(__file__).resolve().parents[3]


def swing_executable(root: Path | None = None) -> Path:
    """Best guess at the ``swing`` console script to run from launchd.

    launchd starts with a bare environment and no ``PATH`` worth trusting, so the
    plist has to name an absolute executable. The project's own virtualenv is
    the answer whenever it exists.
    """
    root = root or repo_root()
    candidates = [
        root / ".venv" / "bin" / "swing",
        Path(sys.executable).with_name("swing"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    from shutil import which

    found = which("swing")
    return Path(found) if found else candidates[0]


def logs_dir(cfg: Config) -> Path:
    """Where launchd writes each agent's stdout and stderr (``~/.swing/logs``)."""
    return Path(cfg.paths.state_dir).expanduser() / "logs"


# --------------------------------------------------------------------------
# timezone sanity
# --------------------------------------------------------------------------


def local_timezone_name() -> str | None:
    """The system timezone name, or None when it cannot be determined."""
    name = os.environ.get("TZ", "").strip()
    if name:
        return name
    try:
        resolved = Path("/etc/localtime").resolve()
    except OSError:  # pragma: no cover - unreadable /etc only
        return None
    parts = resolved.parts
    if "zoneinfo" in parts:
        index = len(parts) - 1 - parts[::-1].index("zoneinfo")
        return "/".join(parts[index + 1 :]) or None
    return None


def _offsets_differ(timezone_name: str) -> bool:
    """True when the configured timezone is at a different UTC offset than the Mac."""
    from zoneinfo import ZoneInfo

    now = datetime.now(UTC)
    try:
        configured = now.astimezone(ZoneInfo(timezone_name)).utcoffset()
    except Exception:  # noqa: BLE001 - an unknown zone is already a config error
        return False
    return configured != now.astimezone().utcoffset()


def timezone_warning(cfg: Config) -> str | None:
    """The warning to print when the Mac's clock disagrees with the config, else None."""
    configured = cfg.schedule.timezone
    local = local_timezone_name()
    mismatch = local != configured if local else _offsets_differ(configured)
    if not mismatch:
        return None
    seen = local or "unknown (offsets differ)"
    return (
        "\n"
        "  ****************************** WARNING ******************************\n"
        f"  launchd runs these jobs in the MAC'S LOCAL TIME, which is {seen}.\n"
        f"  Your config says schedule.timezone = {configured}.\n"
        f"  The scan will therefore fire at {cfg.schedule.scan_time} {seen}, NOT at\n"
        f"  {cfg.schedule.scan_time} {configured}. launchd has no timezone key, so there\n"
        "  is no way to express this in the plist.\n"
        "  Fix it by setting the Mac's timezone to match, or by changing\n"
        "  schedule.scan_time / schedule.confirm_time to local-clock times.\n"
        "  *********************************************************************\n"
    )


# --------------------------------------------------------------------------
# plist generation
# --------------------------------------------------------------------------


def _hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


#: Monday to Friday, in launchd's numbering (0 and 7 are both Sunday).
TRADING_WEEKDAYS: tuple[int, ...] = (1, 2, 3, 4, 5)


def _calendar_intervals(hour: int, minute: int) -> list[dict[str, int]]:
    """When the job fires: this time, Monday to Friday only (audit BUG-012).

    Without a weekday restriction both agents fired seven days a week, and a
    Saturday scan — which re-derives Friday's candidates and dedupes them all
    away — wrote an empty report that then shadowed Friday's real picks on
    Monday morning.

    ``launchd.plist(5)`` documents every ``StartCalendarInterval`` field as a
    single *integer* and the key itself as "a dictionary of integers **or an
    array of dictionaries** of integers", so Monday-to-Friday is five entries
    rather than one entry holding a list. A list inside one dictionary is not
    something launchd promises to parse, and a plist launchd will not parse is
    a schedule that silently never runs — the exact failure this fix exists to
    prevent.
    """
    return [{"Hour": hour, "Minute": minute, "Weekday": day} for day in TRADING_WEEKDAYS]


def build_plist(
    cfg: Config,
    label: str,
    *,
    root: Path | None = None,
    executable: Path | None = None,
) -> dict[str, Any]:
    """Build the launchd job description for one agent.

    Args:
        cfg: the loaded configuration.
        label: ``com.swing.scan`` or ``com.swing.confirm``.
        root: the working directory for the job; defaults to the checkout root.
        executable: the ``swing`` binary to run; defaults to the project venv's.

    Returns:
        A plain dict ready for :func:`plistlib.dump`.
    """
    if label not in AGENTS:
        raise ValueError(
            f"{label!r} is not a swing launchd agent. Valid labels are: {', '.join(AGENTS)}."
        )
    command, time_attr = AGENTS[label]
    hour, minute = _hhmm(getattr(cfg.schedule, time_attr))
    working = Path(root or repo_root())
    binary = Path(executable) if executable is not None else swing_executable(working)
    logs = logs_dir(cfg)
    return {
        "Label": label,
        "ProgramArguments": [str(binary), command],
        "WorkingDirectory": str(working),
        "StandardOutPath": str(logs / f"{command}.out.log"),
        "StandardErrorPath": str(logs / f"{command}.err.log"),
        "StartCalendarInterval": _calendar_intervals(hour, minute),
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": f"{binary.parent}:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def write_plists(
    cfg: Config,
    *,
    target_dir: Path | None = None,
    root: Path | None = None,
    executable: Path | None = None,
) -> dict[str, Path]:
    """Write both agents' plists and return ``{label: path}``."""
    directory = Path(target_dir or launch_agents_dir())
    directory.mkdir(parents=True, exist_ok=True)
    logs_dir(cfg).mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for label in AGENTS:
        path = plist_path(label, directory)
        with path.open("wb") as handle:
            plistlib.dump(build_plist(cfg, label, root=root, executable=executable), handle)
        written[label] = path
    return written


# --------------------------------------------------------------------------
# launchctl
# --------------------------------------------------------------------------


def _default_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


def _launchctl(runner: Runner, *args: str) -> subprocess.CompletedProcess[str]:
    return runner(["launchctl", *args])


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _already(result: subprocess.CompletedProcess[str]) -> bool:
    """True when launchctl refused because the job is already in the state we want.

    Exit code 5 used to be on this list as "already loaded". It is not: macOS
    returns 5 (``EIO``) for genuine bootstrap failures too — SIP/TCC denial, a
    malformed plist, a missing binary — so "Installed" was printed over real
    failures and the user found out days later when no alert ever arrived
    (audit BUG-023). Only 37 and the word "already" stay; every other refusal
    is now settled by asking launchd whether the job is actually there.
    """
    text = f"{result.stdout or ''} {result.stderr or ''}".lower()
    return "already" in text or result.returncode == 37


def _output(result: subprocess.CompletedProcess[str]) -> str:
    """Whatever launchctl said, as one line, or a stand-in when it said nothing."""
    return (f"{result.stderr or ''} {result.stdout or ''}").strip() or "no output"


def _is_loaded(runner: Runner, label: str) -> tuple[bool, str]:
    """Ask launchd whether ``label`` is really loaded (audit BUG-023).

    ``launchctl print`` is the authoritative answer in the modern (``bootstrap``)
    interface; ``launchctl list <label>`` is the legacy one and is tried second
    so this still works on a Mac where ``print`` is unavailable or refuses.

    Returns:
        ``(loaded, explanation)`` — the explanation is launchctl's own output
        and is empty when the job is loaded.
    """
    printed = _launchctl(runner, "print", f"{_domain()}/{label}")
    if printed.returncode == 0:
        return True, ""
    listed = _launchctl(runner, "list", label)
    if listed.returncode == 0:
        return True, ""
    return False, _output(printed)


def _emit(text: str) -> None:
    print(text)


def install(cfg: Config, *, runner: Runner | None = None, target_dir: Path | None = None) -> None:
    """Write both plists and load them into launchd. Idempotent.

    Nothing is called installed until launchd itself says the job is loaded
    (audit BUG-023): the exit code of ``bootstrap`` alone was not enough
    evidence, and a schedule that was never really installed is invisible until
    the night it fails to fire.

    Args:
        cfg: the loaded configuration.
        runner: injected replacement for :func:`subprocess.run` (tests only).
        target_dir: where to write the plists; defaults to ``~/Library/LaunchAgents``.
    """
    run = runner or _default_runner
    warning = timezone_warning(cfg)
    if warning:
        _emit(warning)

    written = write_plists(cfg, target_dir=target_dir)
    binary = swing_executable()
    if not binary.is_file():
        _emit(
            f"Warning: {binary} does not exist yet, so the scheduled job will fail until you "
            f"run `make install`. The plists have been written anyway."
        )

    installed = 0
    for label, path in written.items():
        attempts: list[str] = []
        result = _launchctl(run, "bootstrap", _domain(), str(path))
        if result.returncode != 0 and not _already(result):
            attempts.append(f"bootstrap: {_output(result)}")
            fallback = _launchctl(run, "load", "-w", str(path))
            if fallback.returncode != 0 and not _already(fallback):
                attempts.append(f"load: {_output(fallback)}")

        loaded, why = _is_loaded(run, label)
        if not loaded:
            attempts.append(f"print: {why}")
            _emit(
                f"FAILED to install {label}. launchd does not have the job loaded and said: "
                f"{'; '.join(attempts)}. The plist is written at {path}; try "
                f"`launchctl bootstrap {_domain()} {path}` by hand to see the full error. "
                f"Until this is fixed the schedule will NOT run."
            )
            continue
        installed += 1
        _emit(f"Installed {label}: {path}")

    if installed:
        _emit(
            f"Scan runs at {cfg.schedule.scan_time} and confirm at {cfg.schedule.confirm_time} "
            f"on weekdays only, local machine time. Logs: {logs_dir(cfg)}"
        )
    else:
        _emit(
            f"Nothing was installed, so no scan or confirm will run. Fix the errors above and "
            f"re-run `swing schedule install`. Logs would go to {logs_dir(cfg)}"
        )


def uninstall(cfg: Config, *, runner: Runner | None = None, target_dir: Path | None = None) -> None:
    """Unload both agents and delete their plists. Safe to run twice."""
    run = runner or _default_runner
    directory = Path(target_dir or launch_agents_dir())
    for label in AGENTS:
        path = plist_path(label, directory)
        result = _launchctl(run, "bootout", f"{_domain()}/{label}")
        if result.returncode != 0 and not _already(result) and path.is_file():
            _launchctl(run, "unload", "-w", str(path))
        if path.is_file():
            path.unlink()
            _emit(f"Removed {label}: {path}")
        else:
            _emit(f"{label} was not installed ({path} does not exist).")


def _list_jobs(runner: Runner) -> dict[str, tuple[str, str]]:
    """Parse ``launchctl list`` into ``{label: (pid, last_exit_status)}``."""
    result = _launchctl(runner, "list")
    jobs: dict[str, tuple[str, str]] = {}
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) != 3 or fields[0] == "PID":
            continue
        pid, exit_status, label = fields
        jobs[label] = (pid, exit_status)
    return jobs


def status(cfg: Config, *, runner: Runner | None = None, target_dir: Path | None = None) -> None:
    """Print what launchd thinks of both agents, and where everything lives."""
    run = runner or _default_runner
    directory = Path(target_dir or launch_agents_dir())
    jobs = _list_jobs(run)
    logs = logs_dir(cfg)

    warning = timezone_warning(cfg)
    if warning:
        _emit(warning)

    for label, (command, time_attr) in AGENTS.items():
        when = getattr(cfg.schedule, time_attr)
        path = plist_path(label, directory)
        _emit(f"{label}")
        if label in jobs:
            pid, exit_status = jobs[label]
            running = "running" if pid not in ("-", "0") else "loaded, idle"
            _emit(f"  state       : {running} (pid {pid}, last exit {exit_status})")
        else:
            _emit("  state       : NOT loaded")
        _emit(f"  plist       : {path}{'' if path.is_file() else '  (missing)'}")
        _emit(f"  next fire   : Monday to Friday at {when} local machine time")
        _emit(f"  stdout log  : {logs / f'{command}.out.log'}")
        _emit(f"  stderr log  : {logs / f'{command}.err.log'}")
