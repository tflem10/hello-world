"""Schwab authentication — the broker half of FROZEN CONTRACT 2.

Two commands live here, ``swing auth`` (:func:`login`) and ``swing auth
--check`` (:func:`check`), plus the :func:`get_client` factory that the
executor and the Schwab data provider both build on.

Three rules shape this module:

1. **Every ``schwab`` import is lazy.** ``import swing.broker.auth`` must work
   on a machine where schwab-py is missing or broken, because the rest of the
   system (yfinance data, scans, backtests) does not need a broker at all.
   It also means tests can patch the import site instead of the network.
2. **Refusals are sentences, not tracebacks.** Every failure path prints one
   plain-English line that names the problem and the fix, then exits nonzero
   via :class:`SystemExit` so scripts and launchd can see it.
3. **Token age is a first-class fact.** Schwab refresh tokens die seven days
   after they are created — not seven days after last use — so the age of
   ``schwab_token.json`` decides whether trading can happen at all. The nightly
   scan warns from day six; day seven is a hard stop until you log in again.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "AuthError",
    "REAUTH_WARN_DAYS",
    "REFRESH_TOKEN_LIFETIME_DAYS",
    "SETUP_GUIDE",
    "TokenStatus",
    "check",
    "credentials_problem",
    "get_client",
    "login",
    "quote_price",
    "response_json",
    "short_hash",
    "token_age_days",
    "token_path",
    "token_status",
]

log = logging.getLogger(__name__)

#: Schwab refresh tokens expire seven days after they are first created.
REFRESH_TOKEN_LIFETIME_DAYS = 7.0
#: Start nagging one day before that, so a nightly scan always gets a warning.
REAUTH_WARN_DAYS = 6.0
#: Where a confused human should be sent.
SETUP_GUIDE = "docs/schwab-setup.md"

_SECONDS_PER_DAY = 86_400.0


class AuthError(RuntimeError):
    """Raised when Schwab credentials or the stored token cannot be used.

    The message is always one complete sentence fit to print straight to a
    terminal: what is wrong, and what to do about it.
    """


# --------------------------------------------------------------------------
# credentials and token files
# --------------------------------------------------------------------------


def token_path(cfg: Config) -> Path:
    """Return the absolute path of the stored Schwab token for ``cfg``."""
    return Path(cfg.schwab.token_path).expanduser()


def credentials_problem(cfg: Config) -> str | None:
    """Return a plain-English sentence if the app credentials are unusable.

    Returns ``None`` when both the key and the secret are present. Nothing is
    printed and nothing is validated against Schwab — that only happens when a
    login is actually attempted.
    """
    missing = [
        name
        for name, value in (
            ("api_key", cfg.schwab.api_key),
            ("app_secret", cfg.schwab.app_secret),
        )
        if not str(value).strip()
    ]
    if not missing:
        return None
    joined = " and ".join(missing)
    verb = "is" if len(missing) == 1 else "are"
    return (
        f"Schwab {joined} {verb} empty, so there is nothing to log in with: paste the Key and "
        f"Secret from your developer.schwab.com app into the [schwab] section of your "
        f"config.toml ({SETUP_GUIDE} walks through it step by step)."
    )


def _token_creation_epoch(path: Path) -> float | None:
    """Return the epoch second at which this token was created, if knowable.

    schwab-py wraps every token it writes as ``{"creation_timestamp": ...,
    "token": {...}}`` and deliberately keeps that timestamp fixed across
    refreshes, which is exactly the number the seven-day clock runs on. When
    the file is not in that shape (hand-written fixture, older schwab-py) we
    fall back to the file's modification time, which is a pessimistic but
    honest approximation.
    """
    raw: Any = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        stamp = raw.get("creation_timestamp")
        if isinstance(stamp, int | float) and not isinstance(stamp, bool):
            return float(stamp)
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - the exists() check above makes this rare
        return None


def token_age_days(cfg: Config, *, now: _dt.datetime | None = None) -> float | None:
    """Return the age of the stored token in days, or ``None`` if there is none.

    Args:
        cfg: the loaded configuration (only ``schwab.token_path`` is used).
        now: the moment to measure from. Pass it explicitly in tests; naive
            datetimes are interpreted in local time, matching file timestamps.

    Returns:
        Age in fractional days (never negative), or ``None`` when no token file
        exists or its creation time cannot be established.
    """
    path = token_path(cfg)
    if not path.exists():
        return None
    created = _token_creation_epoch(path)
    if created is None:
        return None
    moment = now or _dt.datetime.now(_dt.UTC)
    return max(0.0, (moment.timestamp() - created) / _SECONDS_PER_DAY)


@dataclass(frozen=True)
class TokenStatus:
    """What we know about the stored token right now."""

    path: Path
    age_days: float | None
    state: str  # "missing" | "fresh" | "warn" | "expired"
    message: str

    @property
    def usable(self) -> bool:
        """True while Schwab would still renew this token."""
        return self.state in {"fresh", "warn"}


def token_status(cfg: Config, *, now: _dt.datetime | None = None) -> TokenStatus:
    """Classify the stored token as missing, fresh, due for renewal, or dead."""
    path = token_path(cfg)
    age = token_age_days(cfg, now=now)
    if age is None:
        return TokenStatus(
            path=path,
            age_days=None,
            state="missing",
            message=(
                f"There is no Schwab token at {path}, so nothing is logged in: run `swing auth` "
                f"to open the browser login flow ({SETUP_GUIDE})."
            ),
        )
    if age >= REFRESH_TOKEN_LIFETIME_DAYS:
        return TokenStatus(
            path=path,
            age_days=age,
            state="expired",
            message=(
                f"The Schwab token at {path} is {age:.1f} days old and Schwab refresh tokens die "
                f"after {REFRESH_TOKEN_LIFETIME_DAYS:.0f} days, so it can no longer be renewed: "
                f"run `swing auth` to log in again."
            ),
        )
    if age >= REAUTH_WARN_DAYS:
        return TokenStatus(
            path=path,
            age_days=age,
            state="warn",
            message=(
                f"The Schwab token is {age:.1f} days old — re-auth soon: run `swing auth`, "
                f"because Schwab refresh tokens stop working at "
                f"{REFRESH_TOKEN_LIFETIME_DAYS:.0f} days."
            ),
        )
    return TokenStatus(
        path=path,
        age_days=age,
        state="fresh",
        message=(
            f"The Schwab token is {age:.1f} days old; "
            f"{REFRESH_TOKEN_LIFETIME_DAYS - age:.1f} days left before it must be renewed."
        ),
    )


def _secure_token_file(path: Path) -> None:
    """Make the token readable only by its owner. Best effort, never fatal."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover - filesystem dependent
        log.warning("Could not chmod 600 the Schwab token at %s: %s", path, exc)


# --------------------------------------------------------------------------
# the client factory
# --------------------------------------------------------------------------


def get_client(cfg: Config) -> Any:
    """Build a schwab-py client from the stored token.

    This is the single place the rest of the system asks for a broker
    connection, so every caller gets the same refusals. The token's age is
    *reported* here but not enforced: refusing to trade on an old token is the
    ``token_age`` guardrail's job, and ``swing auth --check`` needs to be able
    to build a client precisely when things are broken.

    Raises:
        AuthError: with a one-sentence explanation when credentials are
            missing, the token file is absent or unreadable, or schwab-py
            cannot be imported.
    """
    problem = credentials_problem(cfg)
    if problem is not None:
        raise AuthError(problem)

    status = token_status(cfg)
    if status.state == "missing":
        raise AuthError(status.message)
    if status.state != "fresh":
        log.warning("%s", status.message)

    try:
        from schwab.auth import client_from_token_file
    except ImportError as exc:  # pragma: no cover - schwab-py is a hard dependency
        raise AuthError(
            f"schwab-py is not installed, so no broker connection can be made ({exc}): run "
            f"`uv sync` in the project directory to install it."
        ) from exc

    try:
        return client_from_token_file(
            str(status.path),
            cfg.schwab.api_key,
            cfg.schwab.app_secret,
            enforce_enums=False,
        )
    except Exception as exc:
        raise AuthError(
            f"The Schwab token at {status.path} could not be loaded ({exc}): delete that file "
            f"and run `swing auth` to log in again."
        ) from exc


# --------------------------------------------------------------------------
# `swing auth`
# --------------------------------------------------------------------------


def _print_login_instructions(cfg: Config, path: Path) -> None:
    print("Schwab login — here is what is about to happen:")
    print("  1. A browser window opens on Schwab's own login page.")
    print("  2. Sign in with your normal Schwab.com username and password.")
    print("  3. Tick the accounts swing may see, continue, then press Done.")
    print(
        f"  4. Schwab sends the browser back to {cfg.schwab.callback_url}. Your browser will warn "
        f"that the certificate is not trusted — that is expected, because this program is the "
        f"server it is talking to and nothing leaves your Mac. Choose Advanced, then Proceed."
    )
    print("  5. The page will look blank or broken. That is fine; the token has been captured.")
    print(f"The token will be written to {path} and locked to your user account (chmod 600).")
    print("Waiting for the browser now — this gives up after five minutes.")
    print()


def login(cfg: Config) -> None:
    """Run the interactive Schwab browser login and store the token.

    Raises:
        SystemExit: code 2 when credentials are missing or the flow fails, so
            that `swing auth` reports failure to whatever invoked it.
    """
    problem = credentials_problem(cfg)
    if problem is not None:
        print(problem)
        raise SystemExit(2)

    path = token_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    _print_login_instructions(cfg, path)

    try:
        from schwab.auth import client_from_login_flow
    except ImportError as exc:  # pragma: no cover - schwab-py is a hard dependency
        print(
            f"schwab-py is not installed, so the login flow cannot start ({exc}): run `uv sync` "
            f"in the project directory and try `swing auth` again."
        )
        raise SystemExit(2) from exc

    try:
        client_from_login_flow(
            cfg.schwab.api_key,
            cfg.schwab.app_secret,
            cfg.schwab.callback_url,
            str(path),
        )
    except Exception as exc:
        print(
            f"The Schwab login did not complete ({exc}). The usual causes are: the callback URL "
            f"in your developer.schwab.com app is not exactly {cfg.schwab.callback_url}, the app "
            f"is not yet in the 'Ready for use' state, or the browser window was closed before "
            f"Schwab finished. Check {SETUP_GUIDE} and run `swing auth` again."
        )
        raise SystemExit(2) from exc

    _secure_token_file(path)
    print()
    print(f"Logged in. Token saved to {path} (readable only by you).")
    print(
        f"That token dies {REFRESH_TOKEN_LIFETIME_DAYS:.0f} days from now, whatever you do with "
        f"it, so re-run `swing auth` weekly — the nightly scan starts reminding you on day "
        f"{REAUTH_WARN_DAYS:.0f}."
    )
    print("Check it any time with `swing auth --check`.")


# --------------------------------------------------------------------------
# `swing auth --check`
# --------------------------------------------------------------------------


def response_json(response: Any) -> Any:
    """Return the JSON body of a schwab-py response, or the object itself.

    schwab-py hands back ``httpx.Response`` objects; tests hand back plain
    dicts and lists. Both are welcome here.
    """
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise AuthError(
            f"Schwab answered with HTTP {status}. If that is a 401 your token has expired — run "
            f"`swing auth` to log in again."
        )
    getter = getattr(response, "json", None)
    if callable(getter):
        return getter()
    return response


def _account_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("accounts", payload.get("accountNumbers", []))
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def short_hash(value: str, *, keep: int = 6) -> str:
    """Abbreviate an account hash for display.

    The hash identifies an account in API calls but is useless without the
    token, so this is tidiness rather than secrecy: the full string is long,
    meaningless to read, and ends up pasted into screenshots and issues.
    """
    text = str(value)
    if len(text) <= keep:
        return text
    return f"{text[:keep]}..."


def account_hash(cfg: Config, client: Any) -> tuple[str, str]:
    """Return ``(masked_account_number, account_hash)`` for the configured account.

    Raises:
        AuthError: when the token can see no accounts, or ``schwab.account_index``
            points past the end of the list.
    """
    rows = _account_rows(response_json(client.get_account_numbers()))
    if not rows:
        raise AuthError(
            "Schwab returned no accounts for this token, so there is nothing to trade: check "
            "that you ticked at least one account during `swing auth`."
        )
    index = int(cfg.schwab.account_index)
    if index >= len(rows):
        raise AuthError(
            f"schwab.account_index is {index} but this token can only see {len(rows)} account(s), "
            f"so pick a number between 0 and {len(rows) - 1} in the [schwab] section of your "
            f"config.toml."
        )
    row = rows[index]
    number = str(row.get("accountNumber", ""))
    masked = f"****{number[-4:]}" if len(number) >= 4 else "****"
    return masked, str(row.get("hashValue", ""))


def quote_price(payload: Any, symbol: str) -> float | None:
    """Dig the last traded price out of a Schwab quote payload."""
    if not isinstance(payload, dict):
        return None
    block = payload.get(symbol, payload)
    if isinstance(block, dict):
        for key in ("quote", "regular", "extended"):
            inner = block.get(key)
            if isinstance(inner, dict):
                for field in ("lastPrice", "mark", "regularMarketLastPrice", "closePrice"):
                    value = inner.get(field)
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        return float(value)
        for field in ("lastPrice", "mark", "closePrice"):
            value = block.get(field)
            if isinstance(value, int | float) and not isinstance(value, bool):
                return float(value)
    return None


def check(cfg: Config) -> None:
    """Report on the stored token: its age, the account it sees, a live quote.

    Raises:
        SystemExit: code 1 when the token is missing, dead, or Schwab will not
            answer — so `swing auth --check` can be used in a script.
    """
    status = token_status(cfg)
    print(f"Token file : {status.path}")
    if status.age_days is None:
        print("Token age  : (no token)")
    else:
        print(f"Token age  : {status.age_days:.1f} days")
    print(f"Status     : {status.message}")

    problem = credentials_problem(cfg)
    if problem is not None:
        print(problem)
        raise SystemExit(1)
    if status.state == "missing":
        raise SystemExit(1)

    try:
        client = get_client(cfg)
    except AuthError as exc:
        print(str(exc))
        raise SystemExit(1) from exc

    try:
        masked, hash_value = account_hash(cfg, client)
    except AuthError as exc:
        print(str(exc))
        raise SystemExit(1) from exc
    except Exception as exc:
        print(
            f"Schwab would not list your accounts ({exc}). If the token has expired, run "
            f"`swing auth`; otherwise check your internet connection and try again."
        )
        raise SystemExit(1) from exc
    print(f"Account    : {masked} (hash {short_hash(hash_value)})")

    try:
        price = quote_price(response_json(client.get_quote("SPY")), "SPY")
    except Exception as exc:
        print(
            f"The account looks fine but the SPY test quote failed ({exc}). Market data is a "
            f"separate product on your developer.schwab.com app — check it was added and "
            f"approved ({SETUP_GUIDE})."
        )
        raise SystemExit(1) from exc
    if price is None:
        print("SPY quote  : (Schwab answered, but with no price in it)")
    else:
        print(f"SPY quote  : {price:.2f}")

    if status.state == "expired":
        raise SystemExit(1)
