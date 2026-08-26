"""macOS scheduling — two launchd agents, one for the scan and one for the confirm.

``launchd`` is the right tool here and cron is not: a Mac Studio sleeps, and
launchd runs a missed calendar job when the machine wakes while cron simply
loses it. A scan that silently did not happen is the worst possible failure for
this system, so the schedule lives where the OS will catch up.

The evening agent runs a **generated wrapper script** rather than ``swing scan``
directly, because the forward paper-trading record (``swing shadow``) has to
accumulate unattended and its two steps have to happen in a particular order and
with particular failure semantics. See :func:`build_nightly_script`: the scan
runs first (it refreshes the bar cache that shadow scoring then reads), the
steps are chained with ``;`` so a failed scan still records shadow, and the job
exits with the *scan's* status so a shadow failure can never move the exit code
the operator reads as "did tonight's picks reach me?".

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
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "AGENTS",
    "LABEL_CONFIRM",
    "LABEL_SCAN",
    "NIGHTLY_SCRIPT_NAME",
    "SHADOW_LOG_STEM",
    "build_nightly_script",
    "build_plist",
    "install",
    "launch_agents_dir",
    "local_timezone_name",
    "nightly_script_path",
    "plist_path",
    "repo_root",
    "status",
    "swing_executable",
    "timezone_warning",
    "uninstall",
    "write_nightly_script",
    "write_plists",
]

LABEL_SCAN = "com.swing.scan"
LABEL_CONFIRM = "com.swing.confirm"

#: ``label -> (swing subcommand, config attribute holding the time)``.
AGENTS: dict[str, tuple[str, str]] = {
    LABEL_SCAN: ("scan", "scan_time"),
    LABEL_CONFIRM: ("confirm", "confirm_time"),
}

#: The wrapper the evening agent actually executes, written into
#: ``<state_dir>/bin/``. A file rather than an inline ``sh -c`` so a human can
#: read it, run it by hand, and see exactly what launchd runs.
NIGHTLY_SCRIPT_NAME = "swing-nightly.sh"

#: Shadow's own log stem in ``<state_dir>/logs``. Separate from the scan's, so
#: neither stream can bury the other.
SHADOW_LOG_STEM = "shadow"

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


def nightly_script_path(cfg: Config) -> Path:
    """Where the generated evening wrapper lives (``<state_dir>/bin/``)."""
    return Path(cfg.paths.state_dir).expanduser() / "bin" / NIGHTLY_SCRIPT_NAME


def build_nightly_script(
    cfg: Config, *, root: Path | None = None, executable: Path | None = None
) -> str:
    """The shell text the evening agent runs: the scan, then the shadow arms.

    launchd executes exactly one ``ProgramArguments`` list, so chaining needs
    either a wrapper or a second timed job. This is the wrapper, and it exists
    rather than a second job for one reason: **ordering**. Shadow scoring reads
    the bars the scan has just downloaded, and a second job scheduled "a few
    minutes later" is a bet on how long a 1,500-symbol scan takes tonight. On
    the night that bet is wrong the two run concurrently, each pulling the
    universe, and scoring reads a cache mid-refresh. Sequencing removes the
    whole class of problem.

    What a separate job would have given for free — failure isolation — is
    written down here instead:

    * The steps are chained unconditionally (``;``, never ``&&``). A scan that
      fails must not stop the shadow record: a night nobody records is a night
      the forward experiment can never get back, and it is precisely the night
      something was already going wrong.
    * The script exits with the **scan's** status, never shadow's. That code is
      load-bearing — with ``strict_delivery`` a non-zero scan means "nobody
      heard about tonight's picks" — so shadow may not raise it and may not
      clear it either.
    * Shadow's output goes to its own log pair, so neither stream buries the
      other and a stalled shadow is visible without reading the scan log.

    Re-running it is safe at any time: recording is idempotent per
    ``(config, date)`` and scoring is a full replay, so a launchd double-fire
    rewrites the same day rather than double-counting it.
    """
    working = Path(root or repo_root())
    binary = Path(executable) if executable is not None else swing_executable(working)
    logs = logs_dir(cfg)
    quoted_binary = shlex.quote(str(binary))
    quoted_logs = shlex.quote(str(logs))
    return f"""#!/bin/sh
# swing nightly job — GENERATED by `swing schedule install`. Do not edit: the
# next install overwrites it. Change config.toml and re-run install instead.
#
# Runs the real scan, then records and scores the forward paper-trading arms.
# Three properties are deliberate:
#
#   1. The scan runs FIRST. Shadow scoring reads the bars the scan just
#      refreshed, so the order is a requirement, not a preference.
#   2. The steps are chained with `;`, never `&&`. A failed scan must not stop
#      the shadow record — a missed night cannot be re-lived.
#   3. This script exits with the SCAN's status. That exit code is what
#      `launchctl list` reports and what says whether tonight's picks reached
#      anyone; a shadow failure must not be able to set or clear it. Shadow
#      failures land in {logs / f"{SHADOW_LOG_STEM}.err.log"} instead.
#
# Safe to run by hand, and safe to run twice: shadow recording is idempotent
# per (configuration, date) and scoring is a full replay.

set -u

SWING={quoted_binary}
LOGS={quoted_logs}

cd {shlex.quote(str(working))} || exit 1
mkdir -p "$LOGS" || exit 1

"$SWING" scan
scan_status=$?

{{
    echo "=== shadow $(date -u '+%Y-%m-%dT%H:%M:%SZ') (scan exited $scan_status) ==="
    "$SWING" shadow run
    echo "shadow run exited $?"
    "$SWING" shadow score
    echo "shadow score exited $?"
}} >>"$LOGS/{SHADOW_LOG_STEM}.out.log" 2>>"$LOGS/{SHADOW_LOG_STEM}.err.log"

exit "$scan_status"
"""


def write_nightly_script(
    cfg: Config, *, root: Path | None = None, executable: Path | None = None
) -> Path:
    """Write the evening wrapper and make it executable. Returns its path."""
    from swing.state import atomic_write_text

    path = nightly_script_path(cfg)
    atomic_write_text(path, build_nightly_script(cfg, root=root, executable=executable))
    path.chmod(0o755)
    return path


def build_plist(
    cfg: Config,
    label: str,
    *,
    root: Path | None = None,
    executable: Path | None = None,
) -> dict[str, Any]:
    """Build the launchd job description for one agent.

    The evening agent runs the generated wrapper (:func:`build_nightly_script`)
    so the shadow record accumulates with the scan; the morning agent runs
    ``swing confirm`` directly, because nothing is chained to it.

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
    arguments = (
        ["/bin/sh", str(nightly_script_path(cfg))]
        if label == LABEL_SCAN
        else [str(binary), command]
    )
    return {
        "Label": label,
        "ProgramArguments": arguments,
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
    """Write both agents' plists — and the evening wrapper — returning ``{label: path}``.

    The wrapper is written before the plists that point at it, so there is no
    window in which launchd holds a job naming a script that does not exist.
    """
    directory = Path(target_dir or launch_agents_dir())
    directory.mkdir(parents=True, exist_ok=True)
    logs_dir(cfg).mkdir(parents=True, exist_ok=True)
    write_nightly_script(cfg, root=root, executable=executable)
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


def _parse_arguments(text: str) -> list[str] | None:
    """Pull the ``arguments = { ... }`` vector out of ``launchctl print`` output.

    The block looks like this, one argument per line, tab-indented::

        program = /bin/sh
        arguments = {
            /bin/sh
            /Users/tim/.swing/bin/swing-nightly.sh
        }

    Arguments here are always paths and flags, so the first ``}`` ends the
    block. Returns ``None`` when there is no such block to read — a macOS whose
    ``print`` is shaped differently is a thing to *report*, not to guess about.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "arguments = {":
            continue
        arguments: list[str] = []
        for entry in lines[index + 1 :]:
            stripped = entry.strip()
            if stripped == "}":
                return arguments
            if stripped:
                arguments.append(stripped)
        return None  # an unterminated block is not an answer
    return None


def _plist_arguments(path: Path) -> list[str]:
    """The ``ProgramArguments`` the plist on disk specifies."""
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except (OSError, ValueError):  # pragma: no cover - we wrote it moments ago
        return []
    return [str(item) for item in loaded.get("ProgramArguments", [])]


def _loaded_arguments(runner: Runner, label: str) -> tuple[bool, list[str] | None, str]:
    """What launchd is actually holding for ``label`` right now.

    Returns:
        ``(loaded, arguments, explanation)``. ``arguments`` is what launchd
        would execute; ``None`` means the job is loaded but this macOS did not
        show its argument vector, which is a different answer from "the job is
        not there" and is reported differently.
    """
    printed = _launchctl(runner, "print", f"{_domain()}/{label}")
    if printed.returncode == 0:
        arguments = _parse_arguments(printed.stdout or "")
        if arguments is None:
            return True, None, "`launchctl print` did not include an arguments block"
        return True, arguments, ""
    listed = _launchctl(runner, "list", label)
    if listed.returncode == 0:
        return (
            True,
            None,
            "only the legacy `launchctl list` answered, and it does not show arguments",
        )
    return False, None, _output(printed)


def _emit(text: str) -> None:
    print(text)


def install(cfg: Config, *, runner: Runner | None = None, target_dir: Path | None = None) -> None:
    """Write both plists and load them into launchd. Idempotent.

    Nothing is called installed until launchd has been asked what it is
    *actually going to run* and its answer matches the plist on disk. Two
    weaker checks have already failed here, and both failed the same way — by
    verifying a property that was true while the thing they claimed was false:

    * the exit code of ``bootstrap`` (audit BUG-023), which macOS returns as 5
      for genuine failures;
    * the mere presence of a loaded job, which stays true across a rewritten
      plist that launchd has ignored.

    So :func:`install` boots the old definition out before bootstrapping the new
    one, and then compares ``launchctl print``'s argument vector against the
    file's ``ProgramArguments``.

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
        # Boot the old definition out first, every time. launchd holds a job's
        # definition in memory: bootstrapping a label it already has is a no-op,
        # so a rewritten plist is simply ignored and the *previous* command
        # keeps running on schedule. That is not hypothetical — it shipped: the
        # evening agent went on running `swing scan` directly for two nights
        # after an install that had already rewritten the plist to run the
        # nightly wrapper, and the install reported success because something
        # was indeed loaded. A momentary unload of a calendar job costs nothing.
        _launchctl(run, "bootout", f"{_domain()}/{label}")
        # Its failure is deliberately not inspected: on a first install there is
        # nothing to boot out, which is success, not an error.

        result = _launchctl(run, "bootstrap", _domain(), str(path))
        if result.returncode != 0 and not _already(result):
            attempts.append(f"bootstrap: {_output(result)}")
            fallback = _launchctl(run, "load", "-w", str(path))
            if fallback.returncode != 0 and not _already(fallback):
                attempts.append(f"load: {_output(fallback)}")

        loaded, arguments, why = _loaded_arguments(run, label)
        if not loaded:
            attempts.append(f"print: {why}")
            _emit(
                f"FAILED to install {label}. launchd does not have the job loaded and said: "
                f"{'; '.join(attempts)}. The plist is written at {path}; try "
                f"`launchctl bootstrap {_domain()} {path}` by hand to see the full error. "
                f"Until this is fixed the schedule will NOT run."
            )
            continue

        wanted = _plist_arguments(path)
        if arguments is not None and wanted and arguments != wanted:
            _emit(
                f"FAILED to install {label}. launchd has the job loaded, but it is running a "
                f"DIFFERENT command from the one in {path}:\n"
                f"    launchd runs : {' '.join(arguments)}\n"
                f"    the plist says: {' '.join(wanted)}\n"
                f"Repair it by hand with `launchctl bootout {_domain()}/{label}` followed by "
                f"`launchctl bootstrap {_domain()} {path}`, then re-run this command. Until then "
                f"the schedule runs the OLD command."
            )
            continue

        installed += 1
        if arguments is None:
            _emit(
                f"Installed {label}: {path} — but NOT VERIFIED: {why}, so this command could not "
                f"confirm that launchd is running what the plist says. Check it with "
                f"`launchctl print {_domain()}/{label}`."
            )
        else:
            _emit(f"Installed {label}: {path}")

    if installed:
        _emit(
            f"Scan runs at {cfg.schedule.scan_time} and confirm at {cfg.schedule.confirm_time} "
            f"on weekdays only, local machine time. Logs: {logs_dir(cfg)}"
        )
        _emit(
            f"The evening job runs {nightly_script_path(cfg)}: `swing scan`, then "
            f"`swing shadow run` and `swing shadow score`. Shadow cannot change the scan's exit "
            f"code; its output goes to {logs_dir(cfg) / f'{SHADOW_LOG_STEM}.out.log'} and its "
            f"failures to {logs_dir(cfg) / f'{SHADOW_LOG_STEM}.err.log'}. `swing shadow report` "
            f"leads with a warning if the record ever stops accumulating."
        )
    else:
        _emit(
            f"Nothing was installed, so no scan or confirm will run. Fix the errors above and "
            f"re-run `swing schedule install`. Logs would go to {logs_dir(cfg)}"
        )


def uninstall(cfg: Config, *, runner: Runner | None = None, target_dir: Path | None = None) -> None:
    """Unload both agents and delete everything install wrote. Safe to run twice.

    "Everything" now includes the generated evening wrapper: leaving an orphan
    script behind that still names ``swing scan`` would be a booby trap for
    whoever finds it in ``<state_dir>/bin`` a year from now. The logs are left
    alone — they are the record of what happened, not part of the installation.
    """
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

    script = nightly_script_path(cfg)
    if script.is_file():
        script.unlink()
        _emit(f"Removed the nightly wrapper: {script}")
        with suppress(OSError):
            script.parent.rmdir()  # only when nothing else lives there
    else:
        _emit(f"The nightly wrapper was not installed ({script} does not exist).")


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
        if label == LABEL_SCAN:
            script = nightly_script_path(cfg)
            _emit(f"  runs        : {script}{'' if script.is_file() else '  (MISSING)'}")
            _emit("                scan, then `shadow run` and `shadow score`")
            _emit(f"  shadow log  : {logs / f'{SHADOW_LOG_STEM}.out.log'}")
            _emit(f"  shadow errs : {logs / f'{SHADOW_LOG_STEM}.err.log'}")
