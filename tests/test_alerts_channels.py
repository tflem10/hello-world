"""Notification channels — every one of them fully mocked.

Nothing in this file may touch the network, an SMTP server or ``osascript``.
The autouse socket block in ``conftest`` enforces the first; ``monkeypatch``
handles the rest. The most important test here is the isolation one: a dead
channel must never take another channel down with it, because the message these
alerts most need to deliver is often "something is broken".
"""

from __future__ import annotations

import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

from conftest import build_config
from swing.alerts import channels

ALL_CHANNELS = {
    "ntfy_topic": "swing-test",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_user": "me@example.com",
    "smtp_password": "hunter2",
    "email_to": "me@example.com",
    "sms_gateway_address": "5551234567@txt.example.net",
    "macos_notify": True,
}


def cfg_with(tmp_path: Path, **alerts) -> object:
    settings = {"macos_notify": False}
    settings.update(alerts)
    return build_config(tmp_path, alerts=settings)


@pytest.fixture
def note() -> channels.Notification:
    return channels.Notification(
        title="SWING 2026-08-18: 2 picks, 1 watch",
        text="**Picks (2)**\n- ABC 2sh @ $45.10",
        html="<p>ABC</p>",
        sms="SWING 2026-08-18: 2 picks: ABC 2sh@45.10 stop 41.80.",
        attachments=(("ABC.json", '{"oto_stop": {}}'),),
    )


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSMTP:
    """Records everything an SMTP session was asked to do."""

    sessions: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        self.closed = False
        FakeSMTP.sessions.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.closed = True

    def ehlo(self) -> None:
        pass

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def quit(self) -> None:  # noqa: A003 - smtplib's own name
        self.closed = True


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.sessions = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


@pytest.fixture
def posts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    return calls


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# which channels are on
# ---------------------------------------------------------------------------


def test_nothing_is_configured_by_default(tmp_path: Path) -> None:
    active = channels.configured_channels(cfg_with(tmp_path))
    assert active == {"ntfy": False, "email": False, "sms_gateway": False, "macos": False}


def test_each_channel_switches_on_independently(tmp_path: Path) -> None:
    assert channels.configured_channels(cfg_with(tmp_path, ntfy_topic="t"))["ntfy"] is True
    email_cfg = cfg_with(tmp_path, smtp_host="h", email_to="a@b.c")
    assert channels.configured_channels(email_cfg)["email"] is True
    sms_cfg = cfg_with(tmp_path, smtp_host="h", sms_gateway_address="1@txt")
    assert channels.configured_channels(sms_cfg)["sms_gateway"] is True


def test_email_needs_both_a_host_and_a_recipient(tmp_path: Path) -> None:
    only_host = cfg_with(tmp_path, smtp_host="smtp.example.com")
    assert channels.configured_channels(only_host)["email"] is False


def test_macos_is_only_offered_on_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = cfg_with(tmp_path, macos_notify=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert channels.configured_channels(cfg)["macos"] is True
    monkeypatch.setattr(sys, "platform", "linux")
    assert channels.configured_channels(cfg)["macos"] is False


# ---------------------------------------------------------------------------
# ntfy
# ---------------------------------------------------------------------------


def test_ntfy_posts_title_body_and_priority(tmp_path: Path, posts, note) -> None:
    cfg = cfg_with(tmp_path, ntfy_topic="swing-test")
    assert channels.send_ntfy(cfg, note) is True

    (call,) = posts
    assert call["url"] == "https://ntfy.sh/swing-test"
    assert call["data"] == note.text.encode("utf-8")
    assert call["headers"]["Title"] == note.title
    assert call["headers"]["Priority"] == "default"
    assert call["headers"]["Markdown"] == "yes"
    assert call["timeout"] == channels.NETWORK_TIMEOUT


def test_ntfy_accepts_a_full_url_as_the_topic(tmp_path: Path, posts, note) -> None:
    cfg = cfg_with(tmp_path, ntfy_topic="https://ntfy.example.com/private")
    channels.send_ntfy(cfg, note)
    assert posts[0]["url"] == "https://ntfy.example.com/private"


def test_ntfy_title_is_header_safe(tmp_path: Path, posts) -> None:
    cfg = cfg_with(tmp_path, ntfy_topic="t")
    channels.send_ntfy(cfg, channels.Notification(title="picks — 2 ✅", text="body"))
    posts[0]["headers"]["Title"].encode("ascii")  # must not raise


def test_ntfy_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, note
) -> None:
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: (_ for _ in ()).throw(OSError("network down"))
    )
    results = channels.deliver(cfg_with(tmp_path, ntfy_topic="t"), note)
    assert results == {"ntfy": False}


# ---------------------------------------------------------------------------
# email
# ---------------------------------------------------------------------------


def test_email_sends_html_and_attaches_the_orders(tmp_path: Path, smtp, note) -> None:
    cfg = cfg_with(
        tmp_path,
        smtp_host="smtp.example.com",
        smtp_user="me@example.com",
        smtp_password="hunter2",
        email_to="me@example.com",
    )
    assert channels.send_email(cfg, note) is True

    (session,) = smtp.sessions
    assert (session.host, session.port) == ("smtp.example.com", 587)
    assert session.started_tls is True
    assert session.login_args == ("me@example.com", "hunter2")
    assert session.closed is True

    (message,) = session.sent
    assert message["Subject"] == note.title
    assert message["To"] == "me@example.com"
    assert message.get_body(("html",)).get_content().strip() == "<p>ABC</p>"
    filenames = [part.get_filename() for part in message.iter_attachments()]
    assert "ABC.json" in filenames


def test_email_skips_login_without_credentials(tmp_path: Path, smtp, note) -> None:
    cfg = cfg_with(tmp_path, smtp_host="smtp.example.com", email_to="me@example.com")
    channels.send_email(cfg, note)
    assert smtp.sessions[0].login_args is None


def test_email_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, note
) -> None:
    def explode(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"nope")

    monkeypatch.setattr(smtplib, "SMTP", explode)
    cfg = cfg_with(tmp_path, smtp_host="smtp.example.com", email_to="me@example.com")
    assert channels.deliver(cfg, note) == {"email": False}


# ---------------------------------------------------------------------------
# sms gateway
# ---------------------------------------------------------------------------


def test_sms_goes_to_the_gateway_as_short_plain_text(tmp_path: Path, smtp, note) -> None:
    cfg = cfg_with(
        tmp_path, smtp_host="smtp.example.com", sms_gateway_address="5551234567@txt.example.net"
    )
    assert channels.send_sms(cfg, note) is True

    (message,) = smtp.sessions[0].sent
    assert message["To"] == "5551234567@txt.example.net"
    body = message.get_content()
    assert body.strip() == note.sms
    assert "<" not in body
    assert len(body) <= 460


def test_sms_is_clipped_to_the_gateway_limit(tmp_path: Path, smtp) -> None:
    cfg = cfg_with(
        tmp_path, smtp_host="smtp.example.com", sms_gateway_address="5551234567@txt.example.net"
    )
    long_note = channels.Notification(title="t", text="x", sms="A" * 900)
    channels.send_sms(cfg, long_note)
    body = smtp.sessions[0].sent[0].get_content().strip()
    assert len(body) <= 450


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def test_macos_calls_osascript(tmp_path: Path, runs, note, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = cfg_with(tmp_path, macos_notify=True)
    assert channels.send_macos(cfg, note) is True

    (call,) = runs
    assert call[0] == "osascript"
    assert call[1] == "-e"
    assert call[2].startswith("display notification ")
    assert note.title in call[2]


def test_macos_escapes_quotes_and_backslashes(tmp_path: Path, runs, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cfg = cfg_with(tmp_path, macos_notify=True)
    hostile = channels.Notification(title='say "hi" \\ now', text="body")
    channels.send_macos(cfg, hostile)
    script = runs[0][2]
    assert '\\"hi\\"' in script
    assert "\\\\" in script


def test_macos_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, note
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="not permitted"),
    )
    assert channels.deliver(cfg_with(tmp_path, macos_notify=True), note) == {"macos": False}


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


def test_one_failing_channel_never_blocks_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, smtp, runs, note
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def broken_post(*args, **kwargs):
        raise ConnectionError("ntfy.sh is unreachable")

    monkeypatch.setattr("requests.post", broken_post)

    cfg = build_config(tmp_path, alerts=ALL_CHANNELS)
    results = channels.deliver(cfg, note)

    assert results == {"ntfy": False, "email": True, "sms_gateway": True, "macos": True}
    # One session, not two: email and SMS share the connection (audit LEAK-005).
    assert len(smtp.sessions) == 1
    assert len(smtp.sessions[0].sent) == 2
    assert runs  # osascript still ran


def test_deliver_reports_only_configured_channels(tmp_path: Path, posts, note) -> None:
    results = channels.deliver(cfg_with(tmp_path, ntfy_topic="t"), note)
    assert set(results) == {"ntfy"}


def test_deliver_with_nothing_configured_sends_nothing(tmp_path: Path, note) -> None:
    assert channels.deliver(cfg_with(tmp_path), note) == {}


# ---------------------------------------------------------------------------
# report deliveries
# ---------------------------------------------------------------------------


def sample_report() -> dict:
    return {
        "generated_at": "2026-08-18T17:30:00-04:00",
        "asof": "2026-08-18",
        "equity": 100.0,
        "regime_ok": True,
        "gate": {"passed": True, "reasons": []},
        "picks": [
            {
                "symbol": "ABC",
                "date": "2026-08-18",
                "kind": "pick",
                "entry": 45.10,
                "stop": 41.80,
                "shares": 2,
                "risk_amount": 6.60,
                "score": 1.0,
                "atr": 1.65,
                "earnings_date": None,
                "earnings_known": False,
                "thesis": "Broke the 20d high.",
                "status": "drafted",
            }
        ],
        "watch": [],
    }


def test_deliver_scan_sends_the_html_sheet_and_order_attachments(
    tmp_path: Path, smtp, posts
) -> None:
    cfg = cfg_with(
        tmp_path, ntfy_topic="t", smtp_host="smtp.example.com", email_to="me@example.com"
    )
    drafts = {"ABC": {"oto_stop": {"orderType": "LIMIT"}}}
    results = channels.deliver_scan(cfg, sample_report(), notes=["a note"], orders=drafts)

    assert results == {"ntfy": True, "email": True}
    message = smtp.sessions[0].sent[0]
    assert "SWING 2026-08-18" in message["Subject"]
    html = message.get_body(("html",)).get_content()
    assert "<!doctype html>" in html
    assert "Drafted Schwab orders" in html
    assert [p.get_filename() for p in message.iter_attachments()] == ["ABC.json"]
    assert b"ABC" in posts[0]["data"]


def test_deliver_scan_raises_priority_only_for_real_picks(tmp_path: Path, posts) -> None:
    cfg = cfg_with(tmp_path, ntfy_topic="t")
    channels.deliver_scan(cfg, sample_report())
    assert posts[0]["headers"]["Priority"] == "high"

    posts.clear()
    empty = sample_report() | {"picks": [], "gate": {"passed": False, "reasons": ["no backtest"]}}
    channels.deliver_scan(cfg, empty)
    assert posts[0]["headers"]["Priority"] == "default"


def test_deliver_confirm_summarises_the_outcomes(tmp_path: Path, posts) -> None:
    cfg = cfg_with(tmp_path, ntfy_topic="t")
    payload = {
        "asof": "2026-08-19",
        "results": {
            "ABC": {"quote": 45.5, "status": "confirmed", "reason": "fine"},
            "XYZ": {"quote": 99.0, "status": "invalidated", "reason": "gapped"},
        },
    }
    assert channels.deliver_confirm(cfg, payload) == {"ntfy": True}
    assert posts[0]["headers"]["Title"] == "SWING confirm 2026-08-19: 1 confirmed, 1 invalidated"
    assert posts[0]["headers"]["Priority"] == "high"


# ---------------------------------------------------------------------------
# notify-test
# ---------------------------------------------------------------------------


def test_notify_test_reports_per_channel(
    tmp_path: Path, posts, smtp, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = cfg_with(tmp_path, ntfy_topic="t")
    results = channels.notify_test(cfg)

    assert results == {"ntfy": True}
    out = capsys.readouterr().out
    assert "ntfy         sent" in out
    assert "email        skipped (not configured)" in out
    assert "sms_gateway  skipped (not configured)" in out
    assert "macos        skipped (not configured)" in out


def test_notify_test_marks_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: (_ for _ in ()).throw(OSError("no route to host"))
    )
    results = channels.notify_test(cfg_with(tmp_path, ntfy_topic="t"))
    assert results == {"ntfy": False}
    assert "ntfy         FAILED" in capsys.readouterr().out


def test_notify_test_with_nothing_configured_explains_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert channels.notify_test(cfg_with(tmp_path)) == {}
    out = capsys.readouterr().out
    assert "No alert channels are configured" in out
    assert "ntfy_topic" in out


def test_notify_test_sends_a_recognisable_message(tmp_path: Path, posts) -> None:
    channels.notify_test(cfg_with(tmp_path, ntfy_topic="t"))
    assert posts[0]["headers"]["Title"] == "swing: test notification"
    assert b"test" in posts[0]["data"]


# ---------------------------------------------------------------------------
# audit regressions
# ---------------------------------------------------------------------------


def test_email_and_sms_share_one_smtp_connection(tmp_path: Path, smtp, note) -> None:
    """Audit LEAK-005: two connects, two TLS handshakes, two logins per delivery."""
    cfg = cfg_with(
        tmp_path,
        smtp_host="smtp.example.com",
        smtp_user="me@example.com",
        smtp_password="hunter2",
        email_to="me@example.com",
        sms_gateway_address="5551234567@txt.example.net",
    )
    results = channels.deliver(cfg, note)

    assert results == {"email": True, "sms_gateway": True}
    (session,) = smtp.sessions
    assert session.started_tls is True
    assert session.login_args == ("me@example.com", "hunter2")
    assert [message["To"] for message in session.sent] == [
        "me@example.com",
        "5551234567@txt.example.net",
    ]
    assert session.closed is True


def test_only_one_smtp_channel_still_opens_its_own_connection(tmp_path: Path, smtp, note) -> None:
    cfg = cfg_with(tmp_path, smtp_host="smtp.example.com", email_to="me@example.com")
    assert channels.deliver(cfg, note) == {"email": True}
    assert len(smtp.sessions) == 1


def test_a_dead_smtp_server_fails_both_smtp_channels_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, posts, runs, note
) -> None:
    """LEAK-005 must not cost the isolation rule that channels.py exists for."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def refuse(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    cfg = build_config(tmp_path, alerts=ALL_CHANNELS)

    assert channels.deliver(cfg, note) == {
        "ntfy": True,
        "email": False,
        "sms_gateway": False,
        "macos": True,
    }


def test_a_rejected_email_does_not_stop_the_sms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, smtp, note
) -> None:
    def reject_the_first(self, message):
        if message["To"] == "me@example.com":
            raise smtplib.SMTPRecipientsRefused({"me@example.com": (550, b"no such user")})
        self.sent.append(message)

    monkeypatch.setattr(FakeSMTP, "send_message", reject_the_first)
    cfg = cfg_with(
        tmp_path,
        smtp_host="smtp.example.com",
        email_to="me@example.com",
        sms_gateway_address="5551234567@txt.example.net",
    )

    assert channels.deliver(cfg, note) == {"email": False, "sms_gateway": True}
    assert [message["To"] for message in smtp.sessions[0].sent] == ["5551234567@txt.example.net"]


def test_ntfy_titles_collapse_embedded_whitespace(tmp_path: Path, posts) -> None:
    """Audit BUG-047: an embedded newline reaches requests, which rejects it."""
    cfg = cfg_with(tmp_path, ntfy_topic="t")
    hostile = channels.Notification(title="SWING 2026-08-18\n2 picks\r\n\tand a tab", text="body")
    assert channels.send_ntfy(cfg, hostile) is True

    title = posts[0]["headers"]["Title"]
    assert title == "SWING 2026-08-18 2 picks and a tab"
    assert "\n" not in title and "\r" not in title and "\t" not in title


def test_an_entirely_unprintable_title_falls_back_to_a_usable_one(tmp_path: Path, posts) -> None:
    channels.send_ntfy(
        cfg_with(tmp_path, ntfy_topic="t"), channels.Notification(title="✅\n✅", text="body")
    )
    assert posts[0]["headers"]["Title"] == "swing"
