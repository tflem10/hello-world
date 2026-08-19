"""The optional data provider: Schwab's market-data endpoints via ``schwab-py``.

Schwab is a real, supported, authenticated API — but a retail brokerage one. It
gives us good price history and quotes, and nothing usable for earnings dates
or fundamentals, so those two calls simply delegate to the yfinance provider.
That is the "schwab falls back to yfinance for what it lacks" clause of
Contract 3.

Two deliberate design choices:

* **The client is built lazily.** Constructing this provider must never require
  a token, an approved developer app, or even the broker package to exist yet —
  the data layer ships before the broker layer does. The first call that needs
  the network builds the client, and a plain-English error explains what to do
  when it cannot.
* **Bars live in their own cache directory.** Schwab daily candles are split-
  adjusted but *not* dividend-adjusted, so they are not interchangeable with
  yfinance's auto-adjusted series. Mixing both in one parquet file would create
  exactly the silent discontinuity the overlap check exists to prevent.

Symbology (audit BUG-039, amendment A13): the universe speaks Yahoo — ``BRK-B``
— and Schwab writes share classes with a slash. Requests are translated on the
way out by :func:`swing.universe.to_schwab_symbol` and the answers are keyed
back to the Yahoo form, so nothing outside this module ever sees ``BRK/B``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pandas as pd

from swing.data.cache import BarCache, utcnow
from swing.data.provider import (
    DEFAULT_WORKERS,
    Fundamentals,
    Quote,
    RetryPolicy,
    as_date,
    as_utc,
    chunked,
    clean_symbols,
    coerce_float,
    empty_bars,
    map_concurrent,
    normalize_bars,
    with_retry,
)
from swing.data.yf_provider import YFinanceProvider
from swing.universe import to_schwab_symbol

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = ["SCHWAB_CACHE_SUBDIR", "SchwabProvider", "SchwabUnavailable", "default_client_factory"]

log = logging.getLogger(__name__)

#: Schwab candles are stamped in exchange-local terms; daily bars belong to the
#: New York trading date, not to a UTC calendar day.
EXCHANGE_TZ = "America/New_York"
#: Kept apart from the yfinance cache on purpose — see the module docstring.
SCHWAB_CACHE_SUBDIR = "daily-schwab"
#: Schwab's quote endpoint accepts a few hundred symbols per request.
QUOTE_BATCH = 250
#: Epoch values below this are seconds, above it milliseconds: 1e11 ms is 1973
#: and 1e11 s is the year 5138, so nothing real is ambiguous (audit BUG-038).
SECONDS_CUTOFF = 1e11

_DATETIME_KEYS = ("datetime", "datetime_ms", "datetimeMillis", "time", "timestamp")
_OHLCV_AGGREGATION = (
    ("open", "first"),
    ("high", "max"),
    ("low", "min"),
    ("close", "last"),
    ("volume", "sum"),
)
_PRICE_KEYS = (
    "lastPrice",
    "last_price",
    "mark",
    "regularMarketLastPrice",
    "closePrice",
    "lastPriceInDouble",
)
_QUOTE_SECTIONS = ("quote", "regular", "extended")

#: The names WP-G's broker package might use for "give me a logged-in client".
_CLIENT_FACTORY_NAMES = ("client_from_config", "get_client", "make_client", "build_client")


class SchwabUnavailable(RuntimeError):
    """Raised when a Schwab client cannot be built, with advice on what to do.

    The message is always a complete sentence meant for a terminal, because the
    usual cause is a human problem: not logged in, token expired, app not
    approved yet.
    """


def default_client_factory(cfg: Config) -> Any:
    """Build an authenticated Schwab client from the broker package.

    Imported lazily and defensively: :mod:`swing.broker.auth` is another work
    package's file and may not exist yet, and importing it must never be the
    reason a scan cannot start.

    Raises:
        SchwabUnavailable: when the broker package is missing, exposes no way
            to build a client, or cannot log in.
    """
    try:
        from swing.broker import auth as broker_auth
    except Exception as exc:  # noqa: BLE001 - missing module, import error, anything
        raise SchwabUnavailable(
            "Schwab data needs the broker login module (swing.broker.auth), which is not "
            'available yet. Set provider = "yfinance" in the [data] section of your config.toml '
            "to use free Yahoo data instead."
        ) from exc

    for name in _CLIENT_FACTORY_NAMES:
        factory = getattr(broker_auth, name, None)
        if callable(factory):
            try:
                client = factory(cfg)
            except Exception as exc:  # noqa: BLE001 - any auth failure is the same story
                raise SchwabUnavailable(
                    f"Could not log in to Schwab ({exc}). Run `swing auth` to sign in again, or "
                    f'set provider = "yfinance" in the [data] section of your config.toml.'
                ) from exc
            if client is None:
                raise SchwabUnavailable(
                    "Schwab returned no client, which usually means the saved token has expired. "
                    "Run `swing auth` to sign in again."
                )
            return client

    raise SchwabUnavailable(
        "swing.broker.auth does not offer a way to build a Schwab client (looked for "
        f"{', '.join(_CLIENT_FACTORY_NAMES)}). Run `swing auth` to set up the broker, or set "
        'provider = "yfinance" in the [data] section of your config.toml.'
    )


class SchwabProvider:
    """A :class:`~swing.data.provider.DataProvider` backed by Schwab market data.

    Args:
        cfg: the loaded configuration.
        client_factory: zero-argument callable returning a ``schwab-py`` client.
            Defaults to building one from :mod:`swing.broker.auth`, lazily.
        fallback: provider used for earnings and fundamentals; a
            :class:`~swing.data.yf_provider.YFinanceProvider` by default.
        cache: injected :class:`BarCache`; built from ``cfg`` when omitted.
        retries / retry_backoff: bounded retry policy for client calls;
            both default to the ``[data]`` section of the config.
        workers: upper bound on concurrent price-history calls.
        quote_batch: symbols per quote request.
    """

    def __init__(
        self,
        cfg: Config,
        *,
        client_factory: Callable[[], Any] | None = None,
        fallback: Any | None = None,
        cache: BarCache | None = None,
        retries: int | None = None,
        retry_backoff: float | None = None,
        workers: int = DEFAULT_WORKERS,
        quote_batch: int = QUOTE_BATCH,
    ) -> None:
        if quote_batch < 1:
            raise ValueError("quote_batch must be at least 1.")
        self._policy = RetryPolicy.from_config(cfg, retries=retries, backoff=retry_backoff)
        self._cfg = cfg
        self._client_factory = client_factory or (lambda: default_client_factory(cfg))
        self._client: Any | None = None
        self._fallback = fallback
        self._cache = (
            cache if cache is not None else BarCache.from_config(cfg, subdir=SCHWAB_CACHE_SUBDIR)
        )
        self._workers = max(1, workers)
        self._quote_batch = quote_batch

    @property
    def cache(self) -> BarCache:
        """The parquet cache these bars are stored in."""
        return self._cache

    # -- client -----------------------------------------------------------

    def ensure_client(self) -> Any:
        """Build the Schwab client if needed and return it.

        Called by :func:`swing.data.get_provider` at selection time so that a
        dead token degrades to yfinance *before* a scan starts, rather than
        halfway through one.

        Raises:
            SchwabUnavailable: if no client can be built.
        """
        if self._client is None:
            try:
                client = self._client_factory()
            except SchwabUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - normalise every failure
                raise SchwabUnavailable(
                    f"Could not connect to Schwab ({exc}). Run `swing auth` to sign in again, or "
                    f'set provider = "yfinance" in the [data] section of your config.toml.'
                ) from exc
            if client is None:
                raise SchwabUnavailable(
                    "Could not connect to Schwab: no client was returned. Run `swing auth` to "
                    "sign in again."
                )
            self._client = client
        return self._client

    def _yf(self) -> YFinanceProvider:
        if self._fallback is None:
            # The fallback inherits this provider's retry policy: it used to be
            # built with the hardcoded defaults, so turning retries down in the
            # config left half the data layer ignoring it (audit DEBT-014).
            self._fallback = YFinanceProvider(
                self._cfg, retries=self._policy.retries, retry_backoff=self._policy.backoff
            )
        return self._fallback

    def _with_retry(self, what: str, call: Callable[[], Any]) -> Any:
        """Run ``call`` under the configured retry policy (audit DEBT-013)."""
        return with_retry(what, call, policy=self._policy)

    # -- Contract 3 -------------------------------------------------------

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date, *, now: datetime | None = None
    ) -> dict[str, pd.DataFrame]:
        """Daily bars from Schwab's price-history endpoint, parquet-cached."""
        return self._cache.get_bars(
            symbols, as_date(start), as_date(end), self._fetch_bars, now=now
        )

    def latest_quotes(
        self, symbols: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, Quote]:
        """Latest price per symbol from Schwab's quote endpoint."""
        stamp = as_utc(now) if now is not None else utcnow()
        wanted = clean_symbols(symbols)
        out: dict[str, Quote] = {}
        for batch in chunked(wanted, self._quote_batch):
            payload = self._with_retry(
                f"quotes for {len(batch)} symbols", lambda b=batch: self._quotes(b)
            )
            if not isinstance(payload, dict):
                continue
            for symbol in batch:
                price = _quote_price(payload.get(symbol))
                if price is None:
                    log.warning("Schwab returned no usable price for %s, so it is skipped.", symbol)
                    continue
                out[symbol] = Quote(symbol=symbol, price=price, asof=stamp)
        return out

    def earnings_dates(self, symbols: Sequence[str], **kwargs: Any) -> dict[str, date | None]:
        """Delegated to Yahoo — Schwab's retail API has no earnings calendar."""
        return self._yf().earnings_dates(symbols, **kwargs)

    def earnings_history(
        self, symbols: Sequence[str], start: date, end: date, **kwargs: Any
    ) -> dict[str, tuple[date, ...]]:
        """Delegated to Yahoo — Schwab's retail API has no earnings calendar."""
        return self._yf().earnings_history(symbols, start, end, **kwargs)

    def fundamentals(self, symbols: Sequence[str], **kwargs: Any) -> dict[str, Fundamentals]:
        """Delegated to Yahoo — Schwab's retail API has no growth fundamentals."""
        return self._yf().fundamentals(symbols, **kwargs)

    # -- internals --------------------------------------------------------

    def _fetch_bars(
        self, symbols: Sequence[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """The cache's fetch callback: one price-history call per symbol.

        Schwab has no bulk history endpoint, so the only lever is concurrency —
        1,500 strictly serial round trips is a cold fetch measured in tens of
        minutes (audit PERF-007). Results are collected in request order, so
        the output is identical whatever order the calls finish in.
        """
        client = self.ensure_client()
        wanted = clean_symbols(symbols)
        payloads = map_concurrent(
            wanted,
            lambda symbol: self._history(client, symbol, start, end),
            workers=self._workers,
        )
        out: dict[str, pd.DataFrame] = {}
        for symbol in wanted:
            payload = payloads.get(symbol)
            if payload is None:
                continue
            try:
                out[symbol] = candles_to_bars(payload)
            except ValueError as exc:
                log.warning("Skipping %s: its price history was unreadable (%s).", symbol, exc)
        blank = sum(1 for symbol in wanted if symbol not in out or out[symbol].empty)
        if blank:
            # One line per symbol is invisible in a 1,500-symbol run; the count
            # is what tells you the symbology or the token is wrong (BUG-039).
            log.warning("%d of %d symbols returned no history from Schwab.", blank, len(wanted))
        return out

    def _history(self, client: Any, symbol: str, start: date, end: date) -> Any:
        """One price-history call, with the Schwab-form symbol and ET bounds."""
        requested = to_schwab_symbol(symbol)
        # Aware bounds: schwab-py epoch-encodes whatever it is given using the
        # host's local zone, while the response is decoded as exchange time —
        # so a naive midnight on a Denver laptop asked for 02:00 ET and lost the
        # first bar of every cold fetch (audit BUG-034).
        exchange = ZoneInfo(EXCHANGE_TZ)
        return self._with_retry(
            f"the price history for {symbol}",
            lambda: _payload(
                client.get_price_history_every_day(
                    requested,
                    start_datetime=datetime.combine(start, time.min, tzinfo=exchange),
                    end_datetime=datetime.combine(end, time.max, tzinfo=exchange),
                )
            ),
        )

    def _quotes(self, batch: Sequence[str]) -> dict[str, Any]:
        """Quote one batch, keyed back to the Yahoo symbols the caller used."""
        client = self.ensure_client()
        requests = {symbol: to_schwab_symbol(symbol) for symbol in batch}
        payload = _payload(client.get_quotes(list(requests.values())))
        if not isinstance(payload, dict):
            return {}
        out: dict[str, Any] = {}
        for symbol, requested in requests.items():
            entry = payload.get(requested)
            if entry is None and requested != symbol:
                entry = payload.get(symbol)  # some endpoints echo the plain form
            if entry is not None:
                out[symbol] = entry
        return out


# ---------------------------------------------------------------------------
# payload helpers
# ---------------------------------------------------------------------------


def _payload(response: Any) -> Any:
    """Turn a ``schwab-py`` response into plain JSON, raising on HTTP errors."""
    if isinstance(response, dict | list):
        return response
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise ValueError(f"Schwab replied with HTTP {status}.")
    to_json = getattr(response, "json", None)
    if callable(to_json):
        return to_json()
    raise ValueError(f"Schwab returned something unreadable ({type(response).__name__}).")


def candles_to_bars(payload: Any) -> pd.DataFrame:
    """Convert a Schwab price-history payload into Contract 3 bars.

    Args:
        payload: ``{"candles": [{"datetime": <epoch ms>, "open": .., ...}], ...}``.

    Returns:
        A normalised bars frame; empty when Schwab reports no candles. Candles
        sharing a trading date are aggregated into one bar (open first, high
        max, low min, close last, volume summed) rather than deduplicated —
        keeping only the last one would halve the day's volume the first time
        the endpoint returns intraday rows (audit BUG-049).

    Raises:
        ValueError: if the candles have no timestamp or no price columns.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a Schwab price-history record, got {type(payload).__name__}.")
    candles = payload.get("candles")
    if not candles:
        return empty_bars()
    if not isinstance(candles, list | tuple):
        raise ValueError("The candles field of this Schwab response is not a list.")

    frame = pd.DataFrame(list(candles))
    stamp_key = next((key for key in _DATETIME_KEYS if key in frame.columns), None)
    if stamp_key is None:
        raise ValueError(
            "These Schwab candles carry no timestamp field "
            f"(looked for {', '.join(_DATETIME_KEYS)})."
        )
    stamps = (
        pd.to_datetime(_epoch_millis(frame[stamp_key]), unit="ms", utc=True)
        .dt.tz_convert(EXCHANGE_TZ)
        .dt.tz_localize(None)
        .dt.normalize()
    )
    frame = frame.drop(columns=[stamp_key])
    frame.index = pd.DatetimeIndex(stamps).rename(None)
    frame = frame.loc[frame.index.notna()]
    if not frame.index.is_unique:
        frame = _combine_same_day(frame)
    return normalize_bars(frame)


def _epoch_millis(values: Any) -> pd.Series:
    """Read a Schwab timestamp column as epoch milliseconds.

    ``datetime`` is documented as milliseconds, but the ambiguous ``time`` and
    ``timestamp`` keys have been seen carrying seconds — decoded as
    milliseconds they land in 1970, normalise cleanly, cache cleanly, and then
    the symbol simply vanishes from every window slice with no diagnostic
    anywhere (audit BUG-038). The magnitude settles it.
    """
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.mask(numeric.abs() < SECONDS_CUTOFF, numeric * 1000)


def _combine_same_day(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate rows sharing a trading date into one OHLCV bar."""
    lowered = frame.rename(columns=lambda name: str(name).strip().lower())
    lowered = lowered.loc[:, ~lowered.columns.duplicated(keep="first")]
    how = {name: rule for name, rule in _OHLCV_AGGREGATION if name in lowered.columns}
    if not how:
        return frame
    return lowered.sort_index(kind="stable").groupby(level=0, sort=True).agg(how)


def _quote_price(entry: Any) -> float | None:
    """Dig the last traded price out of one symbol's quote record."""
    if not isinstance(entry, dict):
        return None
    # Nested sections first: a real payload keeps the traded price in "quote",
    # and a stale "closePrice" sitting at the top level must not win over it.
    sections: list[dict[str, Any]] = []
    for name in _QUOTE_SECTIONS:
        section = entry.get(name)
        if isinstance(section, dict):
            sections.append(section)
    sections.append(entry)
    for section in sections:
        for key in _PRICE_KEYS:
            price = coerce_float(section.get(key))
            if price is not None and price > 0:
                return price
    return None
