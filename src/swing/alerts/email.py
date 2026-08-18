"""SMTP email, and the carrier email-to-SMS gateway that rides on it.

Gmail requires an App Password (a normal account password will not work with
SMTP once 2FA is on). The password lives in config.toml, which is gitignored
and should be chmod 600 — it is still a plaintext credential on disk, which is
the honest cost of not running a secrets manager for a hobby trading system.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("swing.alerts.email")

TIMEOUT = 30


def send(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[Path] | None = None,
) -> None:
    """Send one message. Raises on failure so the dispatcher can report it."""
    if not to_addrs:
        raise ValueError("no recipients configured")

    message = EmailMessage()
    message["From"] = from_addr or username
    message["To"] = ", ".join(to_addrs)
    message["Subject"] = subject
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")

    for path in attachments or []:
        path = Path(path)
        if not path.exists():
            log.warning("attachment missing, skipping: %s", path)
            continue
        message.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename=path.name,
        )

    if int(smtp_port) == 465:
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=TIMEOUT) as server:
            _login_and_send(server, username, password, message)
    else:
        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=TIMEOUT) as server:
            server.starttls()
            _login_and_send(server, username, password, message)
    log.debug("email sent to %s", to_addrs)


def _login_and_send(server, username: str, password: str, message: EmailMessage) -> None:
    if username:
        server.login(username, password)
    server.send_message(message)


def send_sms_via_gateway(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    from_addr: str,
    to_addrs: list[str],
    body_text: str,
) -> None:
    """Carrier email-to-SMS. Plain text only, and aggressively truncated.

    Gateways silently split or drop long messages, so this sends a short
    summary and expects the reader to open the email for detail.
    """
    send(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        username=username,
        password=password,
        from_addr=from_addr,
        to_addrs=to_addrs,
        subject="",                       # carriers prepend the subject; keep it empty
        body_text=body_text[:300],
    )
