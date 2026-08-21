"""launchd scheduling for macOS.

Two jobs:

* **scan** at ``[schedule] scan_time`` (17:30 ET by default) — after the close,
  produce the pick sheet and send the alerts.
* **confirm** at ``confirm_time`` (09:00 ET) — re-quote before the open.

Notes on the design:

* Times in ``config.toml`` are given in ``[schedule] timezone`` (America/New_York
  by default) and converted to the machine's local time here, because launchd's
  ``StartCalendarInterval`` has no timezone field — it fires on local wall
  clock. If you travel with the laptop, re-run ``swing schedule install``.
* ``RunAtLoad`` is deliberately **false**. A job that runs on every login would
  fire a scan at 11pm on a Sunday and mail you Friday's picks.
* stdout and stderr go to ``[schedule] log_dir`` so a failure at 17:30 leaves
  evidence rather than vanishing.
* launchd does not wake a sleeping Mac for a ``StartCalendarInterval`` job; it
  runs it once the machine wakes. If the Mac Studio sleeps at 17:30 you will
  get the picks late, which is stated in ``swing schedule status`` rather than
  discovered in week three.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .logging_setup import get_logger

log = get_logger("swing.schedule")

LABEL_PREFIX = "com.swing"
JOBS = {
    "scan": ("scan_time", ["scan"]),
    "confirm": ("confirm_time", ["confirm"]),
}


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(job: str) -> Path:
    return launch_agents_dir() / f"{LABEL_PREFIX}.{job}.plist"


def swing_executable() -> str:
    """Absolute path to the installed console script, not just 'swing'.

    launchd runs with a minimal PATH, so a bare command name resolves to
    nothing and the job fails silently every day until someone reads the log.
    """
    candidate = Path(sys.argv[0]).resolve()
    if candidate.name == "swing" and candidate.exists():
        return str(candidate)
    found = shutil.which("swing")
    if found:
        return str(Path(found).resolve())
    # Fall back to running the module with this interpreter.
    return sys.executable


def local_time_for(cfg: Config, hhmm: str) -> tuple[int, int]:
    """Convert an ``HH:MM`` in the configured market timezone to local wall clock."""
    hour, minute = (int(part) for part in str(hhmm).split(":"))
    market_tz = ZoneInfo(str(cfg.schedule.get("timezone", "America/New_York")))
    today = datetime.now().date()
    market_dt = datetime(today.year, today.month, today.day, hour, minute, tzinfo=market_tz)
    local_dt = market_dt.astimezone()
    return local_dt.hour, local_dt.minute


def build_plist(cfg: Config, job: str) -> dict:
    time_key, args = JOBS[job]
    hour, minute = local_time_for(cfg, cfg.schedule[time_key])
    log_dir = cfg.expand_path(cfg.schedule.log_dir)
    executable = swing_executable()

    if Path(executable).name == "swing":
        program_args = [executable, *args]
    else:
        program_args = [executable, "-m", "swing.cli", *args]

    config_source = cfg.source
    if config_source:
        program_args = [program_args[0], "-c", str(config_source), *program_args[1:]]

    return {
        "Label": f"{LABEL_PREFIX}.{job}",
        "ProgramArguments": program_args,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "StandardOutPath": str(log_dir / f"{job}.log"),
        "StandardErrorPath": str(log_dir / f"{job}.err.log"),
        "WorkingDirectory": str(Path(__file__).resolve().parents[2]),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"},
        "ProcessType": "Background",
    }


def run_schedule(cfg: Config, action: str) -> int:
    if action == "print":
        for job in JOBS:
            print(f"--- {plist_path(job)} ---")
            sys.stdout.buffer.write(plistlib.dumps(build_plist(cfg, job)))
            print()
        return 0

    if sys.platform != "darwin":
        print(
            f"launchd scheduling is macOS-only (this is {sys.platform}).\n"
            "Use `swing schedule print` to see the job definitions, or run the "
            "equivalent from cron/systemd:\n"
            f"  30 17 * * 1-5  {swing_executable()} scan\n"
            f"  0  9  * * 1-5  {swing_executable()} confirm"
        )
        return 1

    if action == "install":
        return _install(cfg)
    if action == "uninstall":
        return _uninstall()
    if action == "status":
        return _status(cfg)
    print(f"unknown action {action!r}")
    return 2


def _install(cfg: Config) -> int:
    agents = launch_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    log_dir = cfg.expand_path(cfg.schedule.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    for job in JOBS:
        path = plist_path(job)
        payload = build_plist(cfg, job)
        path.write_bytes(plistlib.dumps(payload))
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
        result = subprocess.run(
            ["launchctl", "load", str(path)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            log.error("launchctl load failed for %s: %s", job, result.stderr.strip())
            return 1
        interval = payload["StartCalendarInterval"]
        print(
            f"installed {path.name}: {job} at "
            f"{interval['Hour']:02d}:{interval['Minute']:02d} local"
        )

    print(f"\nlogs: {log_dir}")
    print(
        "note: launchd does not wake a sleeping Mac. If this machine sleeps at the\n"
        "scheduled time the job runs when it next wakes, so the picks arrive late.\n"
        "Either keep it awake (System Settings > Energy) or accept the delay."
    )
    return 0


def _uninstall() -> int:
    removed = 0
    for job in JOBS:
        path = plist_path(job)
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True, check=False)
            path.unlink()
            print(f"removed {path.name}")
            removed += 1
    if not removed:
        print("nothing installed")
    return 0


def _status(cfg: Config) -> int:
    result = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=False
    )
    lines = [ln for ln in result.stdout.splitlines() if LABEL_PREFIX in ln]
    if not lines:
        print("no swing jobs are loaded. Run `swing schedule install`.")
        return 1

    print("loaded launchd jobs (pid, last exit status, label):")
    for line in lines:
        print(f"  {line}")
        parts = line.split()
        if len(parts) >= 2 and parts[1] not in ("0", "-"):
            print(
                f"    ^ last run exited {parts[1]} — check "
                f"{cfg.expand_path(cfg.schedule.log_dir)}"
            )

    for job, (time_key, _) in JOBS.items():
        hour, minute = local_time_for(cfg, cfg.schedule[time_key])
        print(
            f"  {job}: configured {cfg.schedule[time_key]} "
            f"{cfg.schedule.get('timezone')} = {hour:02d}:{minute:02d} local"
        )
    return 0
