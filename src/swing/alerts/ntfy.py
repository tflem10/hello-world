"""Push notifications via ntfy.sh — the free "text my phone" channel.

Install the ntfy app, subscribe to a topic, and anything published to that
topic arrives as a push notification. No account, no API key, no cost.

**The topic string is the only secret.** Anyone who knows it can read your
picks and publish to your phone, so use a long random topic
(``swing-$(uuidgen)``) and keep config.toml at chmod 600. If that trade-off is
unacceptable, self-host ntfy or turn this channel off and use email only.
"""

from __future__ import annotations

import requests

from ..logging_setup import get_logger

log = get_logger("swing.alerts.ntfy")

TIMEOUT = 15


def send(
    server: str,
    topic: str,
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
    click_url: str | None = None,
) -> None:
    """Publish one notification. Raises on failure so the dispatcher can report it."""
    if not topic:
        raise ValueError("alerts.ntfy.topic is empty")

    url = f"{server.rstrip('/')}/{topic.lstrip('/')}"
    headers = {
        "Title": _header_safe(title),
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    if click_url:
        headers["Click"] = click_url

    response = requests.post(
        url, data=message.encode("utf-8"), headers=headers, timeout=TIMEOUT
    )
    response.raise_for_status()
    log.debug("ntfy published to %s", url)


def _header_safe(text: str) -> str:
    """HTTP headers are latin-1 and single-line; a stray newline is a request splitter."""
    cleaned = " ".join(str(text).split())
    return cleaned.encode("ascii", "replace").decode("ascii")[:200]
