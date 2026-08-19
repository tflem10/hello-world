"""Tests for the Schwab auth half of FROZEN CONTRACT 2 (AC16).

Everything here runs offline. schwab-py is never really called: the lazy
``from schwab.auth import ...`` inside each function is the seam, and these
tests replace ``schwab``/``schwab.auth`` in ``sys.modules`` with stand-ins so
the login flow can be observed without a browser, a network, or an account.

The token-age boundaries get particular attention, because that one number
decides whether the system may trade at all: Schwab refresh tokens die seven
days after they are created, so day six warns and day seven stops everything.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from swing.broker import auth
from swing.config import Config

PLACEHOLDER_KEY = "EXAMPLE-APP-KEY"
PLACEHOLDER_SECRET = "EXAMPLE-APP-SECRET"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_with_keys(cfg_factory: Any) -> Config:
    """A config carrying placeholder Schwab credentials."""
    return cfg_factory(schwab={"api_key": PLACEHOLDER_KEY, "app_secret": PLACEHOLDER_SECRET})


def write_token(cfg: Config, *, age_days: float = 0.0, wrapped: bool = True) -> Path:
    """Write a synthetic token file of a given age.

    ``wrapped=True`` writes the schwab-py shape (``creation_timestamp`` plus a
    nested token); ``wrapped=False`` writes a bare token and backdates the
    file's mtime instead, exercising the fallback path.
    """
    path = Path(cfg.schwab.token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = dt.datetime.now(dt.UTC).timestamp() - age_days * 86_400.0
    if wrapped:
        body = {
            "creation_timestamp": int(created),
            "token": {"access_token": "PLACEHOLDER", "refresh_token": "PLACEHOLDER"},
        }
    else:
        body = {"access_token": "PLACEHOLDER"}
    path.write_text(json.dumps(body), encoding="utf-8")
    if not wrapped:
        os.utime(path, (created, created))
    return path


@pytest.fixture
def fake_schwab(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Replace the ``schwab`` package with an empty stand-in for this test."""
    package = types.ModuleType("schwab")
    auth_module = types.ModuleType("schwab.auth")
    package.auth = auth_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "schwab", package)
    monkeypatch.setitem(sys.modules, "schwab.auth", auth_module)
    return auth_module


class FakeResponse:
    """The bit of ``httpx.Response`` that schwab-py callers actually use."""

    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


def fake_client(
    *,
    accounts: list[dict[str, str]] | None = None,
    quote: Any = None,
) -> Mock:
    """A mocked schwab-py client that answers the two calls ``check`` makes."""
    client = Mock()
    client.get_account_numbers.return_value = FakeResponse(
        accounts
        if accounts is not None
        else [{"accountNumber": "123456789", "hashValue": "ABCDEF0123456789"}]
    )
    client.get_quote.return_value = FakeResponse(
        quote if quote is not None else {"SPY": {"quote": {"lastPrice": 512.34}}}
    )
    return client


# ---------------------------------------------------------------------------
# the module must import with schwab-py absent
# ---------------------------------------------------------------------------


def test_module_imports_even_when_schwab_py_is_unimportable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Everything but a live login has to work on a machine with no schwab-py."""
    monkeypatch.setitem(sys.modules, "schwab", None)
    monkeypatch.setitem(sys.modules, "schwab.auth", None)
    reloaded = importlib.reload(auth)
    assert reloaded.REFRESH_TOKEN_LIFETIME_DAYS == 7.0
    importlib.reload(auth)  # restore for the rest of the session


def test_get_client_explains_itself_when_schwab_py_is_missing(
    cfg_with_keys: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token(cfg_with_keys, age_days=0.0)
    monkeypatch.setitem(sys.modules, "schwab", None)
    monkeypatch.setitem(sys.modules, "schwab.auth", None)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.get_client(cfg_with_keys)
    assert "uv sync" in str(excinfo.value)


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


def test_credentials_problem_names_both_missing_settings(test_cfg: Config) -> None:
    problem = auth.credentials_problem(test_cfg)
    assert problem is not None
    assert "api_key" in problem and "app_secret" in problem
    assert auth.SETUP_GUIDE in problem


def test_credentials_problem_names_only_the_missing_one(cfg_factory: Any) -> None:
    cfg = cfg_factory(schwab={"api_key": PLACEHOLDER_KEY})
    problem = auth.credentials_problem(cfg)
    assert problem is not None
    assert "app_secret is empty" in problem
    assert "api_key" not in problem


def test_credentials_problem_is_none_when_both_are_set(cfg_with_keys: Config) -> None:
    assert auth.credentials_problem(cfg_with_keys) is None


# ---------------------------------------------------------------------------
# token age
# ---------------------------------------------------------------------------


def test_token_age_is_none_when_there_is_no_token(test_cfg: Config) -> None:
    assert auth.token_age_days(test_cfg) is None


def test_token_age_reads_the_embedded_creation_timestamp(test_cfg: Config) -> None:
    write_token(test_cfg, age_days=6.0)
    age = auth.token_age_days(test_cfg)
    assert age is not None
    assert 5.99 < age < 6.01


def test_token_age_ignores_later_writes_to_the_file(test_cfg: Config) -> None:
    """schwab-py rewrites the token on every refresh but keeps the creation stamp."""
    path = write_token(test_cfg, age_days=6.0)
    os.utime(path, None)  # a refresh just touched it
    age = auth.token_age_days(test_cfg)
    assert age is not None and age > 5.9


def test_token_age_falls_back_to_the_file_mtime(test_cfg: Config) -> None:
    write_token(test_cfg, age_days=3.0, wrapped=False)
    age = auth.token_age_days(test_cfg)
    assert age is not None
    assert 2.99 < age < 3.01


def test_token_age_accepts_an_explicit_now(test_cfg: Config) -> None:
    write_token(test_cfg, age_days=0.0)
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=10)
    age = auth.token_age_days(test_cfg, now=later)
    assert age is not None and 9.99 < age < 10.01


def test_token_age_never_goes_negative(test_cfg: Config) -> None:
    write_token(test_cfg, age_days=0.0)
    earlier = dt.datetime.now(dt.UTC) - dt.timedelta(days=5)
    assert auth.token_age_days(test_cfg, now=earlier) == 0.0


# ---------------------------------------------------------------------------
# token status boundaries — the whole point of the seven-day clock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_days", "state"),
    [
        (0.0, "fresh"),
        (5.5, "fresh"),
        (6.0, "warn"),
        (6.9, "warn"),
        (7.0, "expired"),
        (8.0, "expired"),
    ],
)
def test_token_status_boundaries(test_cfg: Config, age_days: float, state: str) -> None:
    write_token(test_cfg, age_days=age_days)
    status = auth.token_status(test_cfg)
    assert status.state == state
    assert status.usable is (state in {"fresh", "warn"})


def test_token_status_at_six_days_says_re_auth_soon(test_cfg: Config) -> None:
    write_token(test_cfg, age_days=6.2)
    status = auth.token_status(test_cfg)
    assert "re-auth soon" in status.message
    assert "swing auth" in status.message


def test_token_status_at_eight_days_says_it_is_dead(test_cfg: Config) -> None:
    write_token(test_cfg, age_days=8.0)
    status = auth.token_status(test_cfg)
    assert status.state == "expired"
    assert "no longer be renewed" in status.message


def test_token_status_missing_points_at_swing_auth(test_cfg: Config) -> None:
    status = auth.token_status(test_cfg)
    assert status.state == "missing"
    assert status.age_days is None
    assert "swing auth" in status.message


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_refuses_without_credentials_and_never_touches_schwab(
    test_cfg: Config, fake_schwab: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = Mock(side_effect=AssertionError("the login flow must not start"))
    fake_schwab.client_from_login_flow = flow  # type: ignore[attr-defined]

    with pytest.raises(SystemExit) as excinfo:
        auth.login(test_cfg)

    assert excinfo.value.code == 2
    assert flow.called is False
    out = capsys.readouterr().out
    assert auth.SETUP_GUIDE in out
    assert "developer.schwab.com" in out


def test_login_runs_the_browser_flow_and_locks_the_token_down(
    cfg_with_keys: Config, fake_schwab: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    token = Path(cfg_with_keys.schwab.token_path)

    def flow(api_key: str, app_secret: str, callback_url: str, token_path: str) -> Mock:
        Path(token_path).write_text('{"creation_timestamp": 1, "token": {}}', encoding="utf-8")
        return Mock()

    spy = Mock(side_effect=flow)
    fake_schwab.client_from_login_flow = spy  # type: ignore[attr-defined]

    auth.login(cfg_with_keys)

    spy.assert_called_once_with(
        PLACEHOLDER_KEY,
        PLACEHOLDER_SECRET,
        cfg_with_keys.schwab.callback_url,
        str(token),
    )
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert cfg_with_keys.schwab.callback_url in out
    assert "7 days" in out
    assert "swing auth --check" in out


def test_login_explains_the_usual_causes_when_the_flow_fails(
    cfg_with_keys: Config, fake_schwab: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_schwab.client_from_login_flow = Mock(  # type: ignore[attr-defined]
        side_effect=RuntimeError("redirect timed out")
    )

    with pytest.raises(SystemExit) as excinfo:
        auth.login(cfg_with_keys)

    assert excinfo.value.code == 2
    out = capsys.readouterr().out
    assert "redirect timed out" in out
    assert cfg_with_keys.schwab.callback_url in out
    assert "Ready for use" in out


def test_login_warns_about_the_self_signed_certificate_before_opening_the_browser(
    cfg_with_keys: Config, fake_schwab: types.ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_schwab.client_from_login_flow = Mock(return_value=Mock())  # type: ignore[attr-defined]
    auth.login(cfg_with_keys)
    out = capsys.readouterr().out
    assert "certificate is not trusted" in out
    assert "nothing leaves your Mac" in out


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


def test_get_client_refuses_without_credentials(test_cfg: Config) -> None:
    write_token(test_cfg)
    with pytest.raises(auth.AuthError) as excinfo:
        auth.get_client(test_cfg)
    assert auth.SETUP_GUIDE in str(excinfo.value)


def test_get_client_refuses_without_a_token(cfg_with_keys: Config) -> None:
    with pytest.raises(auth.AuthError) as excinfo:
        auth.get_client(cfg_with_keys)
    assert "swing auth" in str(excinfo.value)


def test_get_client_builds_from_the_token_file(
    cfg_with_keys: Config, fake_schwab: types.ModuleType
) -> None:
    token = write_token(cfg_with_keys, age_days=1.0)
    sentinel = Mock(name="client")
    builder = Mock(return_value=sentinel)
    fake_schwab.client_from_token_file = builder  # type: ignore[attr-defined]

    client = auth.get_client(cfg_with_keys)

    assert client is sentinel
    builder.assert_called_once_with(
        str(token),
        PLACEHOLDER_KEY,
        PLACEHOLDER_SECRET,
        enforce_enums=False,
    )


def test_get_client_still_builds_on_an_expired_token(
    cfg_with_keys: Config, fake_schwab: types.ModuleType
) -> None:
    """`swing auth --check` needs a client precisely when the token is stale."""
    write_token(cfg_with_keys, age_days=9.0)
    fake_schwab.client_from_token_file = Mock(return_value=Mock())  # type: ignore[attr-defined]
    assert auth.get_client(cfg_with_keys) is not None


def test_get_client_turns_a_broken_token_into_a_sentence(
    cfg_with_keys: Config, fake_schwab: types.ModuleType
) -> None:
    write_token(cfg_with_keys)
    fake_schwab.client_from_token_file = Mock(  # type: ignore[attr-defined]
        side_effect=ValueError("token format has changed")
    )
    with pytest.raises(auth.AuthError) as excinfo:
        auth.get_client(cfg_with_keys)
    message = str(excinfo.value)
    assert "token format has changed" in message
    assert "swing auth" in message


# ---------------------------------------------------------------------------
# response and quote parsing
# ---------------------------------------------------------------------------


def test_response_json_turns_an_http_error_into_a_sentence() -> None:
    with pytest.raises(auth.AuthError) as excinfo:
        auth.response_json(FakeResponse({}, status_code=401))
    assert "401" in str(excinfo.value)
    assert "swing auth" in str(excinfo.value)


def test_response_json_passes_plain_objects_through() -> None:
    assert auth.response_json({"a": 1}) == {"a": 1}


@pytest.mark.parametrize(
    "payload",
    [
        {"SPY": {"quote": {"lastPrice": 512.34}}},
        {"SPY": {"quote": {"mark": 512.34}}},
        {"SPY": {"lastPrice": 512.34}},
        {"quote": {"lastPrice": 512.34}},
    ],
)
def test_quote_price_digs_the_price_out_of_several_shapes(payload: dict[str, Any]) -> None:
    assert auth.quote_price(payload, "SPY") == 512.34


def test_short_hash_abbreviates_for_display() -> None:
    assert auth.short_hash("ABCDEF0123456789") == "ABCDEF..."
    assert auth.short_hash("ABC") == "ABC"
    assert auth.short_hash("") == ""


def test_quote_price_returns_none_when_there_is_no_price() -> None:
    assert auth.quote_price({"SPY": {"quote": {}}}, "SPY") is None
    assert auth.quote_price("not a dict", "SPY") is None


# ---------------------------------------------------------------------------
# account lookup
# ---------------------------------------------------------------------------


def test_account_hash_masks_the_account_number(cfg_with_keys: Config) -> None:
    masked, hash_value = auth.account_hash(cfg_with_keys, fake_client())
    assert masked == "****6789"
    assert hash_value == "ABCDEF0123456789"


def test_account_hash_refuses_when_the_token_sees_no_accounts(cfg_with_keys: Config) -> None:
    with pytest.raises(auth.AuthError) as excinfo:
        auth.account_hash(cfg_with_keys, fake_client(accounts=[]))
    assert "no accounts" in str(excinfo.value)


def test_account_hash_refuses_an_out_of_range_index(cfg_factory: Any) -> None:
    cfg = cfg_factory(
        schwab={
            "api_key": PLACEHOLDER_KEY,
            "app_secret": PLACEHOLDER_SECRET,
            "account_index": 3,
        }
    )
    with pytest.raises(auth.AuthError) as excinfo:
        auth.account_hash(cfg, fake_client())
    assert "account_index" in str(excinfo.value)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_reports_a_healthy_token_account_and_quote(
    cfg_with_keys: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(cfg_with_keys, age_days=1.5)
    monkeypatch.setattr(auth, "get_client", lambda cfg: fake_client())

    auth.check(cfg_with_keys)

    out = capsys.readouterr().out
    assert "1.5 days" in out
    assert "****6789" in out
    assert "ABCDEF..." in out
    assert "ABCDEF0123456789" not in out  # the hash is abbreviated for display
    assert "512.34" in out


def test_check_exits_nonzero_without_a_token(
    cfg_with_keys: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        auth.check(cfg_with_keys)
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "(no token)" in out
    assert "swing auth" in out


def test_check_exits_nonzero_without_credentials(
    test_cfg: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(test_cfg, age_days=1.0)
    with pytest.raises(SystemExit) as excinfo:
        auth.check(test_cfg)
    assert excinfo.value.code == 1
    assert auth.SETUP_GUIDE in capsys.readouterr().out


def test_check_exits_nonzero_on_an_expired_token_even_if_schwab_answers(
    cfg_with_keys: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(cfg_with_keys, age_days=8.0)
    monkeypatch.setattr(auth, "get_client", lambda cfg: fake_client())
    with pytest.raises(SystemExit) as excinfo:
        auth.check(cfg_with_keys)
    assert excinfo.value.code == 1
    assert "no longer be renewed" in capsys.readouterr().out


def test_check_explains_a_failing_quote_as_a_market_data_problem(
    cfg_with_keys: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(cfg_with_keys, age_days=1.0)
    client = fake_client()
    client.get_quote.side_effect = RuntimeError("403 forbidden")
    monkeypatch.setattr(auth, "get_client", lambda cfg: client)

    with pytest.raises(SystemExit) as excinfo:
        auth.check(cfg_with_keys)

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "Market data is a separate product" in out
    assert auth.SETUP_GUIDE in out


def test_check_survives_a_quote_with_no_price_in_it(
    cfg_with_keys: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(cfg_with_keys, age_days=1.0)
    monkeypatch.setattr(auth, "get_client", lambda cfg: fake_client(quote={"SPY": {}}))
    auth.check(cfg_with_keys)
    assert "no price in it" in capsys.readouterr().out


def test_check_reports_an_unusable_client_and_exits(
    cfg_with_keys: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_token(cfg_with_keys, age_days=1.0)

    def boom(cfg: Config) -> Any:
        raise auth.AuthError("The Schwab token could not be loaded: run `swing auth`.")

    monkeypatch.setattr(auth, "get_client", boom)
    with pytest.raises(SystemExit) as excinfo:
        auth.check(cfg_with_keys)
    assert excinfo.value.code == 1
    assert "could not be loaded" in capsys.readouterr().out
