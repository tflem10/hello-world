"""``swing doctor`` — is this install actually able to do its job?

Every check answers one question a first run can fail on, and every failure
carries the command that fixes it. The design rule here is that a check must
*probe*, not assume: "can this machine reach Yahoo?" is answered by asking
Yahoo for a few bars of SPY, not by looking at the config and hoping.

Network probes are the point of this command. The strategy code is hermetic and
well tested; what breaks on a new machine is the environment around it — a
corporate proxy, a captive portal, an IPv6 route that blackholes, a provider
that changed its endpoint. Those failures otherwise surface as "0 symbols
downloaded" forty minutes into a backfill.

Exit codes: 0 all clear (warnings allowed), 1 at least one FAIL.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .logging_setup import get_logger

log = get_logger("swing.doctor")

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"

# Symbols used for probes: one liquid ETF that every provider carries.
PROBE_SYMBOL = "SPY"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""

    def line(self) -> str:
        mark = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", SKIP: "skip"}[self.status]
        return f"  [{mark}] {self.name:<22} {self.detail}"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, fix: str = "") -> Check:
        check = Check(name, status, detail, fix)
        self.checks.append(check)
        return check

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def render(self) -> str:
        lines = ["swing doctor", ""]
        lines += [c.line() for c in self.checks]
        lines.append("")
        if self.failed:
            lines.append(f"{len(self.failed)} check(s) FAILED — fix these first:")
            for check in self.failed:
                lines.append(f"  - {check.name}: {check.detail}")
                if check.fix:
                    for fix_line in check.fix.splitlines():
                        lines.append(f"      {fix_line}")
        elif self.warned:
            lines.append(f"no blockers; {len(self.warned)} warning(s):")
            for check in self.warned:
                lines.append(f"  - {check.name}: {check.detail}")
                if check.fix:
                    for fix_line in check.fix.splitlines():
                        lines.append(f"      {fix_line}")
        else:
            lines.append("everything checks out.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# probes (seams — tests monkeypatch these, they are the only network in here)
# ---------------------------------------------------------------------------
def probe_provider(cfg: Config, name: str) -> tuple[bool, str]:
    """Ask a provider for a few recent bars of SPY. Returns (reachable, detail)."""
    from .config import Config as _Config

    data = cfg.as_dict()
    data["data"] = {**data["data"], "provider": name, "request_pause_sec": 0.0}
    probe_cfg = _Config(data)

    try:
        from .data.provider import get_provider

        provider = get_provider(probe_cfg)
        end = date.today()
        bars = provider.daily_bars([PROBE_SYMBOL], end - timedelta(days=10), end)
    except Exception as exc:                       # noqa: BLE001 - report anything
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    frame = bars.get(PROBE_SYMBOL)
    if frame is None or not len(frame):
        return False, "reachable but returned no bars for " + PROBE_SYMBOL
    return True, f"{len(frame)} bars, latest {frame.index[-1].date()}"


def probe_url(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Plain HTTPS reachability, used for the universe-refresh source."""
    try:
        import requests

        response = requests.get(url, timeout=timeout, headers={"User-Agent": "swing/doctor"})
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        return True, f"HTTP {response.status_code}"
    except Exception as exc:                       # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------
def _check_python(report: Report) -> None:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version < (3, 11):
        report.add("python", FAIL, f"{text} — 3.11+ required",
                   fix="./install.sh   # builds a 3.11 venv with uv")
    else:
        report.add("python", OK, f"{text} ({Path(sys.executable).parent})")


def _check_dependencies(report: Report) -> None:
    required = ("pandas", "numpy", "pyarrow", "requests", "yfinance")
    missing = []
    for module in required:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        report.add("dependencies", FAIL, "missing: " + ", ".join(missing),
                   fix="./install.sh")
    else:
        report.add("dependencies", OK, f"{len(required)} core packages importable")

    try:
        __import__("schwab")
        report.add("schwab-py", OK, "installed (needed only for the Schwab provider)")
    except ImportError:
        report.add("schwab-py", SKIP, "not installed — fine until you use Schwab",
                   fix="uv pip install --python .venv/bin/python -e '.[schwab]'")


def _check_config(cfg: Config, report: Report) -> None:
    if cfg.source is None:
        report.add("config.toml", WARN,
                   "not found — running on the shipped defaults",
                   fix="cp config.example.toml config.toml && chmod 600 config.toml")
    else:
        report.add("config.toml", OK, str(cfg.source))
        try:
            mode = oct(Path(cfg.source).stat().st_mode)[-3:]
            if mode != "600":
                report.add("config perms", WARN,
                           f"mode {mode}; it holds API keys and an SMTP password",
                           fix=f"chmod 600 {cfg.source}")
            else:
                report.add("config perms", OK, "600")
        except OSError:
            pass

    equity = float(cfg.account.equity)
    example_default = 100.0
    if cfg.source is not None and equity == example_default:
        report.add("account.equity", WARN,
                   f"${equity:,.2f} — still the shipped default; every share "
                   "count is sized off this",
                   fix="set [account] equity to your real balance")
    else:
        report.add("account.equity", OK, f"${equity:,.2f}")

    report.add("config hash", OK, f"{cfg.hash}  (the gate is bound to this)")


def _check_cache(cfg: Config, report: Report) -> None:
    from .data.cache import BarCache

    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    symbols = cache.symbols()
    configured = str(cfg.data.provider).lower()

    if not symbols:
        report.add("price cache", WARN, f"empty ({cache.root})",
                   fix="swing data --backfill   # 15-45 min for the full universe")
        return

    stamped = cache.stamped_provider()
    if stamped and stamped != configured:
        report.add("price cache", FAIL,
                   f"written by {stamped!r} but [data] provider is {configured!r}",
                   fix=f"rm -rf {cache.root} && swing data --backfill")
        return

    coverage = cache.coverage()
    newest = coverage["end"].max()
    age = (date.today() - newest).days
    detail = f"{len(symbols)} symbols, newest bar {newest} ({age}d old)"
    limit = int(cfg.data.get("max_stale_days", 5))
    if age > limit:
        report.add("price cache", WARN, detail,
                   fix="swing data --update")
    else:
        report.add("price cache", OK, detail)


def _check_network(cfg: Config, report: Report, offline: bool) -> None:
    configured = str(cfg.data.provider).lower()

    if offline:
        report.add("network", SKIP, "--offline: no probes attempted")
        return

    reachable, detail = probe_provider(cfg, configured)
    if reachable:
        report.add(f"provider:{configured}", OK, detail)
    else:
        fallback = "stooq" if configured != "stooq" else "yfinance"
        report.add(
            f"provider:{configured}", FAIL, detail,
            fix=(
                "check the machine has plain internet (a captive portal or a\n"
                "corporate proxy will fail exactly like this), then retry.\n"
                f"If {configured} itself is broken, switch providers:\n"
                f"  set [data] provider = \"{fallback}\" and note that switching\n"
                "  requires deleting data/cache/ and re-downloading."
            ),
        )

    # The fallback is worth knowing about before you need it at 17:30.
    if configured != "stooq":
        ok_fb, detail_fb = probe_provider(cfg, "stooq")
        report.add("provider:stooq", OK if ok_fb else WARN,
                   detail_fb + ("" if ok_fb else "  (fallback unavailable)"))

    ok_wiki, detail_wiki = probe_url(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    )
    report.add("universe source", OK if ok_wiki else WARN,
               detail_wiki + ("" if ok_wiki else "  — `swing universe --fetch` will fail"))


def _check_gate(cfg: Config, report: Report) -> None:
    from .backtest.gate import check_gate

    status = check_gate(cfg)
    if status.passed:
        report.add("backtest gate", OK, "PASS — scan will emit picks")
        return

    reason = status.reasons[0] if status.reasons else "blocked"
    report.add("backtest gate", WARN, reason[:100],
               fix="swing backtest --walk-forward")


def _check_alerts(cfg: Config, report: Report) -> None:
    enabled = []
    for channel in ("ntfy", "email", "sms", "macos"):
        section = cfg.alerts.get(channel, {})
        if section and bool(section.get("enabled", False)):
            enabled.append(channel)
    if not bool(cfg.alerts.get("enabled", True)):
        report.add("alerts", WARN, "disabled in config — the nightly scan runs silently")
    elif not enabled:
        report.add("alerts", WARN, "no channel enabled",
                   fix="enable [alerts.ntfy] or [alerts.email], then: swing notify-test")
    else:
        report.add("alerts", OK, ", ".join(enabled) + "   (prove with: swing notify-test)")


def _check_schedule(cfg: Config, report: Report) -> None:
    if sys.platform != "darwin":
        report.add("scheduler", SKIP, f"launchd is macOS-only (this is {sys.platform})")
        return
    if shutil.which("launchctl") is None:
        report.add("scheduler", WARN, "launchctl not found")
        return

    import subprocess

    result = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                            check=False)
    jobs = [ln for ln in result.stdout.splitlines() if "com.swing" in ln]
    if jobs:
        report.add("scheduler", OK, f"{len(jobs)} job(s) loaded")
    else:
        report.add("scheduler", WARN, "no swing jobs loaded",
                   fix="swing schedule install")


# ---------------------------------------------------------------------------
def build_report(cfg: Config, offline: bool = False) -> Report:
    report = Report()
    _check_python(report)
    _check_dependencies(report)
    _check_config(cfg, report)
    _check_cache(cfg, report)
    _check_network(cfg, report, offline)
    _check_gate(cfg, report)
    _check_alerts(cfg, report)
    _check_schedule(cfg, report)
    return report


def run_doctor(cfg: Config, offline: bool = False) -> int:
    report = build_report(cfg, offline=offline)
    print(report.render())
    return 1 if report.failed else 0
