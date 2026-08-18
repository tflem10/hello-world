"""Alert fan-out.

One rule governs this module: **a broken channel must never take down the
run.** If SMTP is down, the push notification still goes out; if ntfy is
unreachable, the email still arrives; if everything fails, the pick sheet is
still on disk and the failure is logged and reported. A nightly job that dies
because a mail server hiccupped is worse than no nightly job, because you will
believe it ran.

Every channel returns an :class:`AlertResult` so ``swing notify-test`` can
print a truthful per-channel status instead of "probably fine".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..logging_setup import get_logger
from . import email as email_channel
from . import macos, ntfy

log = get_logger("swing.alerts")


@dataclass
class AlertResult:
    channel: str
    ok: bool
    detail: str = ""
    skipped: bool = False

    def line(self) -> str:
        if self.skipped:
            return f"  {self.channel:<8} skipped   {self.detail}"
        mark = "ok" if self.ok else "FAILED"
        return f"  {self.channel:<8} {mark:<9} {self.detail}"


def deliver(
    cfg: Config,
    title: str,
    text: str,
    html: str | None = None,
    short: str | None = None,
    attachments: list[Path] | None = None,
    click_url: str | None = None,
) -> list[AlertResult]:
    """Send one message down every enabled channel. Never raises."""
    results: list[AlertResult] = []
    alerts = cfg.alerts

    if not bool(alerts.get("enabled", True)):
        return [AlertResult("all", True, "alerts disabled in config", skipped=True)]

    short = short or _shorten(text)

    results.append(_deliver_ntfy(cfg, title, short, click_url))
    results.append(_deliver_email(cfg, title, text, html, attachments))
    results.append(_deliver_sms(cfg, short))
    results.append(_deliver_macos(cfg, title, short))

    failed = [r for r in results if not r.ok and not r.skipped]
    if failed:
        log.warning(
            "%d alert channel(s) failed: %s",
            len(failed), ", ".join(f"{r.channel} ({r.detail})" for r in failed),
        )
    if not any(r.ok and not r.skipped for r in results):
        log.error(
            "no alert reached you. The pick sheet is still on disk — check it manually."
        )
    return results


def _deliver_ntfy(cfg, title: str, short: str, click_url: str | None) -> AlertResult:
    conf = cfg.alerts.ntfy
    if not bool(conf.get("enabled", False)):
        return AlertResult("ntfy", True, "not enabled", skipped=True)
    topic = str(conf.get("topic", "") or "")
    if not topic:
        return AlertResult("ntfy", False, "alerts.ntfy.topic is empty")
    try:
        ntfy.send(
            server=str(conf.get("server", "https://ntfy.sh")),
            topic=topic,
            title=title,
            message=short,
            priority=str(conf.get("priority", "default")),
            tags=["chart_with_upwards_trend"],
            click_url=click_url,
        )
        return AlertResult("ntfy", True, f"published to {topic[:8]}...")
    except Exception as exc:
        return AlertResult("ntfy", False, str(exc)[:200])


def _deliver_email(cfg, title, text, html, attachments) -> AlertResult:
    conf = cfg.alerts.email
    if not bool(conf.get("enabled", False)):
        return AlertResult("email", True, "not enabled", skipped=True)
    to_addrs = list(conf.get("to_addrs", []) or [])
    if not to_addrs:
        return AlertResult("email", False, "alerts.email.to_addrs is empty")
    try:
        email_channel.send(
            smtp_host=str(conf.smtp_host),
            smtp_port=int(conf.smtp_port),
            username=str(conf.get("username", "")),
            password=str(conf.get("password", "")),
            from_addr=str(conf.get("from_addr", "")),
            to_addrs=to_addrs,
            subject=title,
            body_text=text,
            body_html=html,
            attachments=attachments,
        )
        return AlertResult("email", True, f"sent to {len(to_addrs)} recipient(s)")
    except Exception as exc:
        return AlertResult("email", False, str(exc)[:200])


def _deliver_sms(cfg, short: str) -> AlertResult:
    conf = cfg.alerts.sms
    if not bool(conf.get("enabled", False)):
        return AlertResult("sms", True, "not enabled", skipped=True)
    to_addrs = list(conf.get("to_addrs", []) or [])
    if not to_addrs:
        return AlertResult("sms", False, "alerts.sms.to_addrs is empty")
    email_conf = cfg.alerts.email
    try:
        email_channel.send_sms_via_gateway(
            smtp_host=str(email_conf.smtp_host),
            smtp_port=int(email_conf.smtp_port),
            username=str(email_conf.get("username", "")),
            password=str(email_conf.get("password", "")),
            from_addr=str(email_conf.get("from_addr", "")),
            to_addrs=to_addrs,
            body_text=short,
        )
        return AlertResult("sms", True, f"gateway message to {len(to_addrs)} number(s)")
    except Exception as exc:
        return AlertResult("sms", False, str(exc)[:200])


def _deliver_macos(cfg, title: str, short: str) -> AlertResult:
    conf = cfg.alerts.macos
    if not bool(conf.get("enabled", True)):
        return AlertResult("macos", True, "not enabled", skipped=True)
    if not macos.available():
        return AlertResult("macos", True, "not on macOS", skipped=True)
    try:
        macos.send(title=title, message=short)
        return AlertResult("macos", True, "banner shown")
    except Exception as exc:
        return AlertResult("macos", False, str(exc)[:200])


def _shorten(text: str, limit: int = 400) -> str:
    """First few meaningful lines, for push and SMS."""
    lines = [line for line in text.splitlines() if line.strip()]
    out: list[str] = []
    for line in lines:
        if sum(len(x) + 1 for x in out) + len(line) > limit:
            break
        out.append(line)
    return "\n".join(out) if out else text[:limit]


def notify_test(cfg: Config) -> int:
    """``swing notify-test`` — prove every channel actually works, before you need it."""
    from datetime import datetime

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "swing: notification test"
    text = (
        f"swing notification test at {stamp}\n\n"
        "If you are reading this, this channel works.\n"
        "No picks are attached; this message was sent by `swing notify-test`.\n"
    )
    html = (
        f"<p><b>swing notification test</b> at {stamp}</p>"
        "<p>If you are reading this, this channel works.</p>"
    )

    results = deliver(cfg, title=title, text=text, html=html, short=f"swing test {stamp}")

    print("alert channels:")
    for result in results:
        print(result.line())

    configured = [r for r in results if not r.skipped]
    if not configured:
        print(
            "\nNo channels are enabled. Turn on at least one in [alerts] "
            "or the nightly scan will run silently."
        )
        return 1
    failures = [r for r in configured if not r.ok]
    if failures:
        print(f"\n{len(failures)} channel(s) failed — fix these before relying on the scan.")
        return 1
    print(f"\nall {len(configured)} configured channel(s) delivered.")
    return 0
