"""Schwab OAuth: login, token status, health check.

Schwab's retail Trader API uses OAuth with two tokens:

* an **access token**, valid 30 minutes, refreshed automatically by schwab-py
* a **refresh token**, valid **7 days**, which cannot be renewed programmatically

That 7-day ceiling is not a bug to work around, it is the product. Once a week
you run ``swing auth`` and complete a browser login. The nightly scan warns
from day ``[schwab] token_warn_days`` onward so the expiry is never a surprise,
and it degrades to yfinance rather than failing when the token finally dies.

``schwab-py`` is an optional dependency (``pip install -e '.[schwab]'``) so the
whole system works before the developer app is approved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .logging_setup import get_logger

log = get_logger("swing.auth")

REFRESH_TOKEN_LIFETIME_DAYS = 7


class SchwabNotConfigured(RuntimeError):
    """Raised when Schwab credentials are missing or schwab-py is not installed."""


@dataclass
class TokenStatus:
    exists: bool
    path: Path
    age_days: float | None = None
    days_left: float | None = None
    expired: bool = False
    warn: bool = False
    detail: str = ""

    def describe(self) -> str:
        if not self.exists:
            return (
                f"no Schwab token at {self.path}\n"
                "  run `swing auth` to log in (see docs/schwab-setup.md)"
            )
        if self.expired:
            return (
                f"Schwab refresh token EXPIRED ({self.age_days:.1f} days old, "
                f"limit {REFRESH_TOKEN_LIFETIME_DAYS})\n"
                "  run `swing auth` to log in again"
            )
        line = (
            f"Schwab token: {self.age_days:.1f} days old, "
            f"{self.days_left:.1f} day(s) left"
        )
        if self.warn:
            line += "  <-- re-authenticate soon (`swing auth`)"
        return line


def token_path(cfg: Config) -> Path:
    return cfg.expand_path(cfg.schwab.token_path)


def token_status(cfg: Config, now: datetime | None = None) -> TokenStatus:
    """Age and remaining life of the refresh token, from the file's own metadata.

    schwab-py writes ``creation_timestamp`` into the token file. If that field
    is missing (older versions, or a hand-edited file) we fall back to the
    file's mtime, which is a slight over-estimate of remaining life and is
    therefore flagged.
    """
    path = token_path(cfg)
    now = now or datetime.now()
    if now.tzinfo is not None:
        # Token timestamps are naive local time; callers (the executor) may pass
        # a market-timezone-aware clock. Normalise rather than raising.
        now = now.astimezone().replace(tzinfo=None)
    if not path.exists():
        return TokenStatus(exists=False, path=path)

    created: datetime | None = None
    detail = ""
    try:
        payload = json.loads(path.read_text())
        stamp = payload.get("creation_timestamp")
        if stamp is not None:
            created = datetime.fromtimestamp(float(stamp))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        detail = f"could not parse the token file ({exc})"

    if created is None:
        created = datetime.fromtimestamp(path.stat().st_mtime)
        detail = detail or (
            "token file has no creation_timestamp; using its modification time, "
            "which may over-state the remaining life"
        )

    age_days = (now - created).total_seconds() / 86400.0
    days_left = REFRESH_TOKEN_LIFETIME_DAYS - age_days
    warn_after = float(cfg.schwab.get("token_warn_days", 6))
    return TokenStatus(
        exists=True,
        path=path,
        age_days=age_days,
        days_left=days_left,
        expired=days_left <= 0,
        warn=age_days >= warn_after,
        detail=detail,
    )


def _require_credentials(cfg: Config) -> tuple[str, str, str]:
    api_key = str(cfg.schwab.get("api_key", "") or "")
    app_secret = str(cfg.schwab.get("app_secret", "") or "")
    callback = str(cfg.schwab.get("callback_url", "") or "")
    missing = [
        name for name, value in
        (("api_key", api_key), ("app_secret", app_secret), ("callback_url", callback))
        if not value
    ]
    if missing:
        raise SchwabNotConfigured(
            f"[schwab] {', '.join(missing)} is not set in your config.\n"
            "Create the app at developer.schwab.com and follow docs/schwab-setup.md."
        )
    return api_key, app_secret, callback


def _import_schwab():
    try:
        import schwab
    except ImportError as exc:
        raise SchwabNotConfigured(
            "schwab-py is not installed. Install the optional extra:\n"
            "  uv pip install --python .venv/bin/python -e '.[schwab]'"
        ) from exc
    return schwab


def get_client(cfg: Config, interactive: bool = False):
    """Return an authenticated schwab-py client.

    ``interactive=False`` (the default, and what the nightly jobs use) will
    load an existing token but never open a browser — a scheduled job that
    silently waits for a login prompt is a job that hangs until you notice.
    """
    # Order matters: report the problem the user can act on soonest. A missing
    # api_key or a dead token is a five-second fix; "install schwab-py" is only
    # worth saying once the rest is actually in place.
    api_key, app_secret, callback = _require_credentials(cfg)
    path = token_path(cfg)
    status = token_status(cfg)

    if not status.exists and not interactive:
        raise SchwabNotConfigured(
            f"no Schwab token at {path}. Run `swing auth` from a terminal "
            "where a browser can open."
        )
    if status.exists and status.expired and not interactive:
        raise SchwabNotConfigured(
            f"the Schwab refresh token expired {abs(status.days_left):.1f} days ago. "
            "Run `swing auth`."
        )

    schwab = _import_schwab()
    path.parent.mkdir(parents=True, exist_ok=True)

    if not status.exists:
        return _login(schwab, api_key, app_secret, callback, path)
    if status.expired:
        log.warning("token expired; starting a fresh login")
        return _login(schwab, api_key, app_secret, callback, path)

    try:
        return schwab.auth.client_from_token_file(
            token_path=str(path), api_key=api_key, app_secret=app_secret
        )
    except Exception as exc:
        if not interactive:
            raise SchwabNotConfigured(
                f"could not load the Schwab token ({exc}). Run `swing auth`."
            ) from exc
        log.warning("token load failed (%s); starting a fresh login", exc)
        return _login(schwab, api_key, app_secret, callback, path)


def _login(schwab, api_key: str, app_secret: str, callback: str, path: Path):
    log.info("opening a browser for the Schwab login flow...")
    client = schwab.auth.client_from_login_flow(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback,
        token_path=str(path),
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return client


def run_auth(cfg: Config, check: bool = False, force: bool = False) -> int:
    path = token_path(cfg)

    if force and path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        path.rename(backup)
        print(f"moved the existing token to {backup}")

    if check:
        return _check(cfg)

    try:
        get_client(cfg, interactive=True)
    except SchwabNotConfigured as exc:
        print(str(exc))
        return 2
    except Exception as exc:
        print(f"login failed: {exc}")
        print("\nCommon causes, in order of likelihood:")
        print("  - the app is still 'Approved - Pending' at developer.schwab.com;")
        print("    it must read 'Ready for use' before the API accepts a login")
        print("  - the callback URL in the app does not match [schwab] callback_url")
        print("    exactly (including https:// and the port)")
        print("  - the app secret was regenerated and config.toml still has the old one")
        return 1

    status = token_status(cfg)
    print(f"logged in. Token written to {path}")
    print(
        f"It expires in {REFRESH_TOKEN_LIFETIME_DAYS} days "
        f"({(datetime.now() + timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS)):%Y-%m-%d}). "
        "Re-run `swing auth` before then."
    )
    if status.detail:
        print(f"note: {status.detail}")
    print("\nverify with: swing auth --check")
    return 0


def _check(cfg: Config) -> int:
    """Print token status, the account, and one live quote — proof it works."""
    status = token_status(cfg)
    print(status.describe())
    if status.detail:
        print(f"  note: {status.detail}")
    if not status.exists or status.expired:
        return 1

    try:
        client = get_client(cfg, interactive=False)
    except SchwabNotConfigured as exc:
        print(f"\n{exc}")
        return 2

    ok = True
    try:
        response = client.get_account_numbers()
        response.raise_for_status()
        accounts = response.json()
        print("\naccounts:")
        for account in accounts:
            number = str(account.get("accountNumber", ""))
            masked = f"...{number[-4:]}" if len(number) >= 4 else number
            print(f"  {masked}   hash {account.get('hashValue')}")
        configured = str(cfg.schwab.get("account_hash", "") or "")
        hashes = {a.get("hashValue") for a in accounts}
        if not configured:
            print(
                "\n[schwab] account_hash is empty. Copy the hash above into your "
                "config — `swing execute` needs it to know which account to trade."
            )
            ok = False
        elif configured not in hashes:
            print(
                f"\nWARNING: [schwab] account_hash {configured} is not one of the "
                "hashes above. Orders would target an account you do not have."
            )
            ok = False
    except Exception as exc:
        print(f"\naccount lookup failed: {exc}")
        ok = False

    try:
        response = client.get_quote("SPY")
        response.raise_for_status()
        payload = response.json()
        quote = payload.get("SPY", {}).get("quote", {})
        last = quote.get("lastPrice") or quote.get("closePrice")
        print(f"\nsample quote: SPY {last}  (bid {quote.get('bidPrice')} / "
              f"ask {quote.get('askPrice')})")
    except Exception as exc:
        print(f"\nquote request failed: {exc}")
        print("  market data entitlement may not be enabled on the app")
        ok = False

    if ok:
        print("\nSchwab connection looks healthy.")
    return 0 if ok else 1


def warn_if_token_expiring(cfg: Config) -> str:
    """Called by the nightly jobs. Returns a warning string, or ''."""
    if str(cfg.data.provider).lower() != "schwab" and not bool(
        cfg.execution.get("enabled", False)
    ):
        return ""
    status = token_status(cfg)
    if not status.exists:
        return "no Schwab token; run `swing auth`"
    if status.expired:
        return (
            f"the Schwab refresh token expired {abs(status.days_left):.1f} days ago — "
            "run `swing auth`. Falling back to free data where possible."
        )
    if status.warn:
        return (
            f"the Schwab refresh token expires in {status.days_left:.1f} day(s) — "
            "run `swing auth` before it does"
        )
    return ""
