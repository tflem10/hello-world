"""Notification channels — ntfy push, SMTP email, carrier SMS gateway, macOS banner.

Four rules hold for every channel here:

1. **A channel is active only when it is configured.** An empty ``ntfy_topic``
   is not an error, it is a switched-off channel.
2. **Channels are isolated.** Each send runs inside its own ``try/except``, so a
   dead SMTP server can never stop the push notification that would have told
   you the SMTP server is dead.
3. **Every send returns a bool.** Never an exception, never ``None``. Failures
   are logged with the reason and reported as ``False``.
4. **Nothing here decides anything.** The pipeline decides; these functions
   format and transmit.
"""

from __future__ import annotations

import logging
import smtplib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from swing.alerts import render

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "CHANNELS",
    "Notification",
    "configured_channels",
    "deliver",
    "deliver_confirm",
    "deliver_scan",
    "notify_test",
]

log = logging.getLogger(__name__)

#: Every channel name, in the order they are attempted.
CHANNELS: tuple[str, ...] = ("ntfy", "email", "sms_gateway", "macos")

NTFY_BASE_URL = "https://ntfy.sh"
NETWORK_TIMEOUT = 15.0
OSASCRIPT_TIMEOUT = 10.0


@dataclass(frozen=True)
class Notification:
    """One message, in every shape the channels need it.

    Attributes:
        title: short headline — the ntfy title, the email subject, the macOS
            banner title.
        text: the plain-text/Markdown body. Used by ntfy and as the email's
            text alternative.
        html: optional HTML body for email. Empty means text-only.
        sms: the short body for a carrier SMS gateway. Empty falls back to
            ``title``.
        priority: ntfy priority (``min``/``low``/``default``/``high``/``urgent``).
        attachments: ``(filename, text)`` pairs attached to the email.
    """

    title: str
    text: str
    html: str = ""
    sms: str = ""
    priority: str = "default"
    attachments: tuple[tuple[str, str], ...] = field(default=())


# --------------------------------------------------------------------------
# which channels are switched on
# --------------------------------------------------------------------------


def configured_channels(cfg: Config) -> dict[str, bool]:
    """Say which channels this configuration has switched on.

    ``macos`` is only offered on macOS itself: ``osascript`` does not exist
    anywhere else, and a channel that can only fail is worse than no channel.
    """
    import sys

    alerts = cfg.alerts
    smtp_ready = bool(alerts.smtp_host.strip())
    return {
        "ntfy": bool(alerts.ntfy_topic.strip()),
        "email": smtp_ready and bool(alerts.email_to.strip()),
        "sms_gateway": smtp_ready and bool(alerts.sms_gateway_address.strip()),
        "macos": bool(alerts.macos_notify) and sys.platform == "darwin",
    }


# --------------------------------------------------------------------------
# individual channels — each returns True/False and never raises
# --------------------------------------------------------------------------


def _ntfy_url(topic: str) -> str:
    topic = topic.strip()
    if topic.startswith(("http://", "https://")):
        return topic
    return f"{NTFY_BASE_URL}/{topic.lstrip('/')}"


def _ascii_header(value: str) -> str:
    """ntfy headers must be latin-1 safe; drop anything that is not."""
    return value.encode("ascii", "ignore").decode("ascii").strip() or "swing"


def send_ntfy(cfg: Config, note: Notification) -> bool:
    """POST the notification to the configured ntfy topic."""
    import requests

    url = _ntfy_url(cfg.alerts.ntfy_topic)
    response = requests.post(
        url,
        data=note.text.encode("utf-8"),
        headers={
            "Title": _ascii_header(note.title),
            "Priority": note.priority,
            "Markdown": "yes",
        },
        timeout=NETWORK_TIMEOUT,
    )
    response.raise_for_status()
    return True


def _smtp_send(cfg: Config, message: EmailMessage) -> bool:
    """Deliver one message over STARTTLS, logging in only when credentials exist."""
    alerts = cfg.alerts
    with smtplib.SMTP(alerts.smtp_host, alerts.smtp_port, timeout=NETWORK_TIMEOUT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        if alerts.smtp_user and alerts.smtp_password:
            smtp.login(alerts.smtp_user, alerts.smtp_password)
        smtp.send_message(message)
    return True


def _from_address(cfg: Config) -> str:
    return cfg.alerts.smtp_user.strip() or cfg.alerts.email_to.strip() or "swing@localhost"


def send_email(cfg: Config, note: Notification) -> bool:
    """Send the full report by email: text body, HTML alternative, JSON attachments."""
    message = EmailMessage()
    message["Subject"] = note.title
    message["From"] = _from_address(cfg)
    message["To"] = cfg.alerts.email_to
    message.set_content(note.text)
    if note.html:
        message.add_alternative(note.html, subtype="html")
    for filename, body in note.attachments:
        message.add_attachment(
            body.encode("utf-8"),
            maintype="application",
            subtype="json",
            filename=filename,
        )
    return _smtp_send(cfg, message)


def send_sms(cfg: Config, note: Notification) -> bool:
    """Send the short line to a carrier email-to-SMS gateway address."""
    body = render.clip(note.sms or note.title)
    message = EmailMessage()
    message["Subject"] = ""
    message["From"] = _from_address(cfg)
    message["To"] = cfg.alerts.sms_gateway_address
    message.set_content(body)
    return _smtp_send(cfg, message)


def _applescript_quote(value: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal."""
    flattened = " ".join(value.split())
    return flattened.replace("\\", "\\\\").replace('"', '\\"')


def send_macos(cfg: Config, note: Notification) -> bool:
    """Post a macOS notification-centre banner via ``osascript``."""
    lines = [line for line in note.text.splitlines() if line.strip()]
    body = note.sms or (lines[0] if lines else note.title)
    script = (
        f'display notification "{_applescript_quote(body)}" '
        f'with title "{_applescript_quote(note.title)}"'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=OSASCRIPT_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"osascript exited {result.returncode}: {result.stderr.strip()}")
    return True


_SENDERS = {
    "ntfy": send_ntfy,
    "email": send_email,
    "sms_gateway": send_sms,
    "macos": send_macos,
}


# --------------------------------------------------------------------------
# delivery
# --------------------------------------------------------------------------


def deliver(cfg: Config, note: Notification) -> dict[str, bool]:
    """Send ``note`` through every configured channel, isolating each failure.

    Returns:
        ``{channel: succeeded}`` for the configured channels only. Channels that
        are switched off are simply absent, so an empty dict means "nothing is
        configured" rather than "everything failed".
    """
    active = configured_channels(cfg)
    results: dict[str, bool] = {}
    for name in CHANNELS:
        if not active.get(name):
            continue
        try:
            results[name] = bool(_SENDERS[name](cfg, note))
        except Exception as exc:  # noqa: BLE001 - one dead channel must not stop the rest
            log.warning("Alert channel %s failed: %s", name, exc)
            results[name] = False
    return results


def scan_notification(
    report: Mapping[str, Any],
    *,
    notes: Sequence[str] = (),
    orders: Mapping[str, Any] | None = None,
) -> Notification:
    """Build the notification for a completed scan."""
    import json

    picks = report.get("picks") or []
    gate_passed = bool((report.get("gate") or {}).get("passed"))
    attachments = tuple(
        (f"{symbol}.json", json.dumps(draft, indent=2))
        for symbol, draft in sorted((orders or {}).items())
    )
    return Notification(
        title=render.summary_title(report),
        text=render.summary_text(report, notes=notes),
        html=render.render_html(report, notes=notes, orders=orders),
        sms=render.render_sms(report),
        priority="high" if (picks and gate_passed) else "default",
        attachments=attachments,
    )


def deliver_scan(
    cfg: Config,
    report: Mapping[str, Any],
    *,
    notes: Sequence[str] = (),
    orders: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """Send the nightly scan summary through every configured channel."""
    return deliver(cfg, scan_notification(report, notes=notes, orders=orders))


def confirm_notification(payload: Mapping[str, Any]) -> Notification:
    """Build the notification for a completed confirmation run."""
    markdown = render.render_confirm_markdown(payload)
    counts = {
        name: sum(
            1
            for entry in (payload.get("results") or {}).values()
            if isinstance(entry, Mapping) and entry.get("status") == name
        )
        for name in ("confirmed", "invalidated")
    }
    return Notification(
        title=render.render_confirm_title(payload),
        text=markdown,
        html="",
        sms=render.render_confirm_sms(payload),
        priority="high" if counts["invalidated"] else "default",
    )


def deliver_confirm(cfg: Config, payload: Mapping[str, Any]) -> dict[str, bool]:
    """Send the morning confirmation summary through every configured channel."""
    return deliver(cfg, confirm_notification(payload))


# --------------------------------------------------------------------------
# `swing notify-test`
# --------------------------------------------------------------------------

_TEST_NOTE = Notification(
    title="swing: test notification",
    text=(
        "This is a **test** from `swing notify-test`.\n\n"
        "If you are reading it, this channel is wired up correctly and the nightly "
        "scan can reach you."
    ),
    html=(
        "<p>This is a <strong>test</strong> from <code>swing notify-test</code>.</p>"
        "<p>If you are reading it, this channel is wired up correctly and the nightly "
        "scan can reach you.</p>"
    ),
    sms="swing: test notification. This channel works.",
)


def notify_test(cfg: Config) -> dict[str, bool]:
    """Send a test message through every configured channel and report per channel.

    Prints one line per channel — including the ones that are switched off, so
    the answer to "why did I not get a text?" is on screen rather than implied.

    Returns:
        ``{channel: succeeded}`` for the configured channels only, which is what
        the CLI turns into its exit code.
    """
    active = configured_channels(cfg)
    results = deliver(cfg, _TEST_NOTE)
    for name in CHANNELS:
        if not active.get(name):
            print(f"{name:<12} skipped (not configured)")
        elif results.get(name):
            print(f"{name:<12} sent")
        else:
            print(f"{name:<12} FAILED — run with --verbose to see the underlying error")
    if not results:
        print(
            "No alert channels are configured. Fill in [alerts] in your config.toml "
            "(ntfy_topic is the quickest one to set up)."
        )
    return results
