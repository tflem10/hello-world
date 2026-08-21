"""Schwab market data, behind the same :class:`DataProvider` seam as yfinance.

What Schwab gives you that yfinance does not: real-time quotes with bid/ask
(which is what the pre-open confirm step actually wants) and a vendor
relationship that is not an undocumented scraping endpoint.

What it does not give you: fundamentals. The Trader API is a trading API, so
:meth:`fundamentals` falls back to yfinance rather than returning nothing and
silently switching off the fundamental filter for the whole universe.

Quotes, earnings dates and fundamentals degrade rather than fail: if the token
has expired or schwab-py is not installed, each logs the reason and falls back
to yfinance, so a Friday-night token expiry produces a warning in the pick
sheet instead of a missing pick sheet.

**Bars are the exception, deliberately.** The bar cache is stamped with the
*configured* provider name, so yfinance bars returned from here would be
written under a ``schwab`` stamp and the cache's own provider check would wave
them through — two adjustment bases spliced into one series with nothing left
to detect it. :meth:`daily_bars` therefore returns what Schwab gave it and
nothing else; an empty result degrades to "the scan runs on cached history and
says so", which is recoverable, unlike a corrupted cache.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from ..auth import SchwabNotConfigured, get_client
from ..logging_setup import get_logger
from .provider import Fundamentals, Quote, normalize_bars

log = get_logger("swing.data.schwab")

# Schwab price-history responses use epoch milliseconds.
MS = 1000.0


class SchwabProvider:
    name = "schwab"

    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        self._fallback = None
        self._fallback_reason = ""

    # -- plumbing ----------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            self._client = get_client(self.cfg, interactive=False)
        return self._client

    def _yfinance(self):
        """Lazily construct the fallback provider."""
        if self._fallback is None:
            from .yfinance_provider import YFinanceProvider

            self._fallback = YFinanceProvider(self.cfg)
        return self._fallback

    def _fall_back(self, what: str, exc: Exception):
        reason = f"Schwab {what} unavailable ({exc}); falling back to yfinance"
        if reason != self._fallback_reason:
            log.warning("%s", reason)
            self._fallback_reason = reason
        return self._yfinance()

    # -- bars --------------------------------------------------------------
    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        """Schwab candles only — never the fallback's. See the module docstring."""
        try:
            client = self.client
        except (SchwabNotConfigured, Exception) as exc:
            self._no_bar_fallback(exc, len(symbols), len(symbols))
            return {}

        out: dict[str, pd.DataFrame] = {}
        failures = 0
        for symbol in [s.upper() for s in symbols]:
            try:
                frame = self._history_for(client, symbol, start, end)
            except Exception as exc:
                failures += 1
                log.debug("Schwab history failed for %s: %s", symbol, exc)
                continue
            if frame is not None and len(frame):
                out[symbol] = frame

        if failures:
            self._no_bar_fallback(
                RuntimeError(f"{failures} symbol(s) failed"), failures, len(symbols)
            )
        return out

    def _no_bar_fallback(self, exc: Exception, failed: int, asked: int) -> None:
        """One warning per call: bars are missing, and they will stay missing."""
        log.warning(
            "Schwab price history unavailable for %d of %d symbol(s) (%s). Bars do "
            "NOT fall back to yfinance: this cache is stamped 'schwab' and the two "
            "sources adjust prices differently, so substituted bars would splice "
            "two price series into one undetectably. The scan runs on cached "
            "history instead — fix the source with: swing auth",
            failed, asked, exc,
        )

    def _history_for(self, client, symbol: str, start: date, end: date):
        response = client.get_price_history_every_day(
            symbol,
            start_datetime=datetime.combine(start, datetime.min.time()),
            end_datetime=datetime.combine(end, datetime.max.time()),
            need_extended_hours_data=False,
            need_previous_close=False,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("empty") or not payload.get("candles"):
            return None

        candles = payload["candles"]
        frame = pd.DataFrame(
            {
                "open": [c["open"] for c in candles],
                "high": [c["high"] for c in candles],
                "low": [c["low"] for c in candles],
                "close": [c["close"] for c in candles],
                "volume": [c.get("volume", 0) for c in candles],
            },
            index=pd.to_datetime([c["datetime"] / MS for c in candles], unit="s"),
        )
        return normalize_bars(frame)

    # -- quotes ------------------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Quotes with fallback. Fine for the pre-open confirm; NOT for execution.

        Use :func:`live_quotes` in any code path that is about to transmit an
        order — silently substituting a 15-minute-delayed print for a live NBBO
        is exactly the kind of degradation that must never happen with money
        moving.
        """
        symbols = [s.upper() for s in symbols]
        try:
            return live_quotes(self.client, symbols)
        except Exception as exc:
            return self._fall_back("quotes", exc).quotes(symbols)

    # -- earnings ----------------------------------------------------------
    def earnings_dates(self, symbols: list[str]) -> dict[str, date | None]:
        """The Trader API has no earnings calendar; yfinance supplies it."""
        return self._yfinance().earnings_dates(symbols)

    # -- fundamentals ------------------------------------------------------
    def fundamentals(self, symbols: list[str]) -> dict[str, Fundamentals]:
        """Schwab's instrument search returns some fundamentals; fill gaps from yfinance."""
        symbols = [s.upper() for s in symbols]
        try:
            client = self.client
        except Exception as exc:
            return self._fall_back("fundamentals", exc).fundamentals(symbols)

        out: dict[str, Fundamentals] = {}
        unresolved: list[str] = []
        for symbol in symbols:
            try:
                response = client.get_instruments(
                    [symbol], client.Instrument.Projection.FUNDAMENTAL
                )
                response.raise_for_status()
                payload = response.json()
                record = (payload.get("instruments") or [{}])[0]
                fundamental = record.get("fundamental") or {}
                if not fundamental:
                    unresolved.append(symbol)
                    continue
                out[symbol] = Fundamentals(
                    symbol=symbol,
                    trailing_eps=_as_float(fundamental.get("eps")),
                    revenue_growth=_pct(fundamental.get("totalRevenueChangeInPercent")),
                    earnings_growth=_pct(fundamental.get("epsChangePercentTTM")),
                    market_cap=_as_float(fundamental.get("marketCap")),
                    sector=record.get("assetType"),
                    is_etf=str(record.get("assetType", "")).upper() == "ETF",
                )
            except Exception as exc:
                log.debug("Schwab fundamentals failed for %s: %s", symbol, exc)
                unresolved.append(symbol)

        if unresolved:
            log.info("filling %d fundamental gap(s) from yfinance", len(unresolved))
            out.update(self._yfinance().fundamentals(unresolved))
        return out


def live_quotes(client, symbols: list[str]) -> dict[str, Quote]:
    """Real-time Schwab quotes, with **no fallback**.

    Raises if the request fails. Callers in the execution path want that: no
    quote means the quote-drift guardrail blocks, which is the correct outcome.
    """
    symbols = [s.upper() for s in symbols]
    response = client.get_quotes(symbols)
    response.raise_for_status()
    payload = response.json()

    out: dict[str, Quote] = {}
    for symbol in symbols:
        quote = (payload.get(symbol) or {}).get("quote") or {}
        price = _first(quote, ("lastPrice", "mark", "closePrice"))
        if price is None:
            continue
        out[symbol] = Quote(
            symbol=symbol,
            price=float(price),
            bid=_first(quote, ("bidPrice",)),
            ask=_first(quote, ("askPrice",)),
            timestamp=_quote_time(quote),
            stale=False,              # Schwab quotes are real-time, unlike yfinance
        )
    return out


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        return None if pd.isna(result) else result
    except (TypeError, ValueError):
        return None


def _pct(value) -> float | None:
    """Schwab reports growth as a percentage; the rest of the code wants a fraction."""
    result = _as_float(value)
    return None if result is None else result / 100.0


def _first(mapping: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        result = _as_float(mapping.get(key))
        if result is not None and result > 0:
            return result
    return None


def _quote_time(quote: dict) -> datetime | None:
    stamp = quote.get("quoteTime") or quote.get("tradeTime")
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(float(stamp) / MS)
    except (TypeError, ValueError, OSError):
        return None


def market_is_open(now: datetime | None = None) -> bool:
    """Cheap local check: weekday, 09:30-16:00 US/Eastern.

    Deliberately ignores market holidays. It is used only as a guardrail
    ("do not transmit at 3am"), and a bundled holiday calendar is a
    maintenance burden that would go stale. Schwab rejects orders sent on a
    holiday anyway, which is the authoritative answer.
    """
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    now = (now or datetime.now(tz=eastern)).astimezone(eastern)
    if now.weekday() >= 5:
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now <= close_time


def minutes_until_open(now: datetime | None = None) -> float:
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    now = (now or datetime.now(tz=eastern)).astimezone(eastern)
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now > target:
        target = target + timedelta(days=1)
    return (target - now).total_seconds() / 60.0
