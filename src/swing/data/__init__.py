"""FROZEN CONTRACT 3 — the data layer.

One import gives the rest of the system everything it may know about the
outside world::

    from swing.data import get_provider
    provider = get_provider(cfg)
    bars = provider.daily_bars(["AAPL", "MSFT"], date(2020, 1, 1), date(2026, 8, 18))

Which provider you get is a config setting, not a code change, and choosing it
never fails: if ``data.provider = "schwab"`` but the token is expired, the app
is not approved, or the broker package is not installed yet, you get a loud
warning and free Yahoo data instead. A degraded scan beats no scan.

Neither ``yfinance`` nor ``schwab-py`` is imported until a provider actually
needs the network, so importing this package stays cheap and offline.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from swing.data.cache import BarCache, CacheMeta, TtlJsonCache, earnings_fingerprint
from swing.data.provider import (
    BAR_COLUMNS,
    DataProvider,
    Fundamentals,
    Quote,
    empty_bars,
    normalize_bars,
)
from swing.data.schwab_provider import SchwabProvider, SchwabUnavailable
from swing.data.yf_provider import YFinanceProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "BAR_COLUMNS",
    "BarCache",
    "CacheMeta",
    "DataProvider",
    "Fundamentals",
    "Quote",
    "SchwabProvider",
    "SchwabUnavailable",
    "TtlJsonCache",
    "YFinanceProvider",
    "earnings_fingerprint",
    "empty_bars",
    "get_provider",
    "normalize_bars",
]

log = logging.getLogger(__name__)


def get_provider(cfg: Config, *, client_factory: Callable[[], Any] | None = None) -> DataProvider:
    """Return the data provider named by ``cfg.data.provider``.

    Args:
        cfg: the loaded configuration. ``data.provider`` is already validated
            to be ``"yfinance"`` or ``"schwab"`` by Contract 1.
        client_factory: zero-argument callable returning a ``schwab-py``
            client, used only for the Schwab provider (tests inject one here).

    Returns:
        A :class:`~swing.data.provider.DataProvider`. Yahoo unless Schwab was
        asked for *and* a Schwab client could actually be built.

    Warns:
        UserWarning: when Schwab was requested but is unusable, explaining why
            and that Yahoo is being used instead.
    """
    provider = str(getattr(cfg.data, "provider", "yfinance")).strip().lower()

    if provider == "schwab":
        try:
            schwab = SchwabProvider(cfg, client_factory=client_factory)
            schwab.ensure_client()
        except Exception as exc:  # noqa: BLE001 - never let provider choice kill a run
            warnings.warn(
                f"data.provider is set to 'schwab' but the Schwab connection could not be set up "
                f"({exc}) — falling back to free Yahoo data for this run. Run `swing auth` to fix "
                f'the connection, or set provider = "yfinance" in the [data] section of your '
                f"config.toml to silence this warning.",
                UserWarning,
                stacklevel=2,
            )
        else:
            log.info("Using Schwab market data (cache: %s).", schwab.cache.root)
            return schwab
    elif provider != "yfinance":  # pragma: no cover - Contract 1 validates this
        warnings.warn(
            f"Unknown data.provider {provider!r}; using free Yahoo data instead. Valid values are "
            f"'yfinance' and 'schwab'.",
            UserWarning,
            stacklevel=2,
        )

    return YFinanceProvider(cfg)
