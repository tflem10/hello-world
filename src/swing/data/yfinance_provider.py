"""Free daily data via yfinance.

This is the bootstrap/default provider: no API key, no approval wait, daily
OHLCV back to the 1990s for most listed names. It is also the least reliable
link in the chain — Yahoo's undocumented endpoints change without notice — so
every call is defensive and every failure degrades to "this symbol is absent"
rather than taking the whole run down. The cache is what makes that survivable.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pandas as pd

from ..logging_setup import get_logger
from .provider import Fundamentals, Quote, normalize_bars

log = get_logger("swing.data.yfinance")


class YFinanceProvider:
    name = "yfinance"

    def __init__(self, cfg):
        self.cfg = cfg
        self.batch_size = int(cfg.data.get("request_batch_size", 50))
        self.pause = float(cfg.data.get("request_pause_sec", 1.0))

    # -- bars --------------------------------------------------------------
    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        symbols = [s.upper() for s in symbols]
        out: dict[str, pd.DataFrame] = {}
        # yfinance's `end` is exclusive; add a day so today's bar is included.
        end_exclusive = end + timedelta(days=1)

        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i : i + self.batch_size]
            log.debug("yfinance batch %d-%d of %d", i, i + len(batch), len(symbols))
            try:
                raw = yf.download(
                    tickers=batch,
                    start=start.isoformat(),
                    end=end_exclusive.isoformat(),
                    interval="1d",
                    auto_adjust=True,   # split & dividend adjusted -> backtest-safe
                    actions=False,
                    progress=False,
                    threads=True,
                    group_by="ticker",
                )
            except Exception as exc:
                log.warning("batch download failed (%s); retrying symbol by symbol", exc)
                raw = None

            if raw is None or len(raw) == 0:
                out.update(self._download_individually(batch, start, end_exclusive))
            else:
                out.update(self._split_batch(raw, batch))

            if i + self.batch_size < len(symbols) and self.pause:
                time.sleep(self.pause)

        return out

    def _split_batch(self, raw: pd.DataFrame, batch: list[str]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        multi = isinstance(raw.columns, pd.MultiIndex)
        for sym in batch:
            try:
                sub = raw[sym] if multi else raw
                frame = normalize_bars(sub)
            except (KeyError, ValueError) as exc:
                log.debug("no usable bars for %s (%s)", sym, exc)
                continue
            if len(frame):
                out[sym] = frame
        return out

    def _download_individually(
        self, batch: list[str], start: date, end_exclusive: date
    ) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        out: dict[str, pd.DataFrame] = {}
        for sym in batch:
            try:
                raw = yf.Ticker(sym).history(
                    start=start.isoformat(),
                    end=end_exclusive.isoformat(),
                    interval="1d",
                    auto_adjust=True,
                )
                frame = normalize_bars(raw)
                if len(frame):
                    out[sym] = frame
            except Exception as exc:
                log.warning("giving up on %s: %s", sym, exc)
        return out

    # -- quotes ------------------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        import yfinance as yf

        out: dict[str, Quote] = {}
        for sym in [s.upper() for s in symbols]:
            try:
                tkr = yf.Ticker(sym)
                info = getattr(tkr, "fast_info", None) or {}
                price = _first_float(
                    info, ("last_price", "lastPrice", "regular_market_price")
                )
                if price is None:
                    hist = tkr.history(period="2d", interval="1d", auto_adjust=False)
                    if len(hist):
                        price = float(hist["Close"].iloc[-1])
                if price is None:
                    continue
                out[sym] = Quote(
                    symbol=sym,
                    price=float(price),
                    bid=_first_float(info, ("bid",)),
                    ask=_first_float(info, ("ask",)),
                    timestamp=datetime.now(),
                    # yfinance quotes are delayed ~15 minutes; the confirm step
                    # treats that as a reason to widen tolerances, not to trust
                    # the number as a live NBBO.
                    stale=True,
                )
            except Exception as exc:
                log.warning("quote failed for %s: %s", sym, exc)
        return out

    # -- earnings ----------------------------------------------------------
    def earnings_dates(self, symbols: list[str]) -> dict[str, date | None]:
        import yfinance as yf

        today = date.today()
        out: dict[str, date | None] = {}
        for sym in [s.upper() for s in symbols]:
            out[sym] = None
            try:
                tkr = yf.Ticker(sym)
                df = None
                try:
                    df = tkr.get_earnings_dates(limit=12)
                except Exception:
                    df = getattr(tkr, "earnings_dates", None)
                if df is not None and len(df):
                    idx = pd.to_datetime(pd.Index(df.index))
                    if getattr(idx, "tz", None) is not None:
                        idx = idx.tz_localize(None)
                    future = sorted(d.date() for d in idx if d.date() >= today)
                    if future:
                        out[sym] = future[0]
                        continue
                cal = getattr(tkr, "calendar", None)
                out[sym] = _calendar_earnings_date(cal, today)
            except Exception as exc:
                log.debug("no earnings date for %s: %s", sym, exc)
            if self.pause:
                time.sleep(min(self.pause, 0.2))
        return out

    # -- fundamentals ------------------------------------------------------
    def fundamentals(self, symbols: list[str]) -> dict[str, Fundamentals]:
        import yfinance as yf

        out: dict[str, Fundamentals] = {}
        for sym in [s.upper() for s in symbols]:
            try:
                info = yf.Ticker(sym).get_info() or {}
            except Exception as exc:
                log.debug("no fundamentals for %s: %s", sym, exc)
                out[sym] = Fundamentals(symbol=sym)
                continue
            quote_type = str(info.get("quoteType", "")).upper()
            out[sym] = Fundamentals(
                symbol=sym,
                trailing_eps=_as_float(info.get("trailingEps")),
                revenue_growth=_as_float(info.get("revenueGrowth")),
                earnings_growth=_as_float(info.get("earningsGrowth")),
                market_cap=_as_float(info.get("marketCap")),
                sector=info.get("sector"),
                is_etf=quote_type in ("ETF", "MUTUALFUND"),
            )
            if self.pause:
                time.sleep(min(self.pause, 0.2))
        return out


def _as_float(value) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _first_float(info, keys) -> float | None:
    for key in keys:
        try:
            value = info[key] if not hasattr(info, "get") else info.get(key)
        except Exception:
            value = None
        f = _as_float(value)
        if f is not None and f > 0:
            return f
    return None


def _calendar_earnings_date(cal, today: date) -> date | None:
    """yfinance returns `calendar` as a dict or a DataFrame depending on version."""
    if cal is None:
        return None
    values = []
    if isinstance(cal, dict):
        values = cal.get("Earnings Date") or cal.get("earningsDate") or []
        if not isinstance(values, (list, tuple)):
            values = [values]
    elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
        values = list(cal.loc["Earnings Date"].dropna().values)
    for value in values:
        try:
            d = pd.to_datetime(value).date()
        except (TypeError, ValueError):
            continue
        if d >= today:
            return d
    return None
