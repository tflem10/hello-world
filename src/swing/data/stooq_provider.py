"""Free daily data via Stooq's CSV endpoint — the fallback when yfinance breaks.

yfinance is the default, and it is the least reliable link in the chain: Yahoo's
endpoints are undocumented and change without notice. Stooq publishes a plain,
stable CSV download (``/q/d/l/?s=aapl.us&i=d``) with no API key and no approval
wait, which makes it a good second source to fail over to.

What you give up, stated rather than hidden
-------------------------------------------
Stooq daily prices are **split-adjusted but NOT dividend-adjusted**. The
:mod:`swing.data.provider` contract asks for split *and* dividend adjusted
prices, and this provider cannot deliver the second half. Consequences:

* long-horizon backtests will differ slightly from yfinance's
  ``auto_adjust=True`` series — the gap is roughly the compounded dividend
  yield over the window, so it is largest for high-yield names and multi-year
  runs, and negligible for the few-week swing horizon the strategy trades;
* momentum/trend signals are computed on price, not total return, so ranking
  is mildly biased against high-yield names.

That is an acceptable trade for a fallback whose job is to keep the pipeline
running when Yahoo is down. It is not an acceptable substitute for the primary
source in a decade-long backtest; re-run the backtest on yfinance or Schwab
data before trusting numbers produced from Stooq bars.

Stooq also carries no earnings calendar and no fundamentals, so the soft
filters that consume them fail *open* (see :meth:`earnings_dates` and
:meth:`fundamentals`). Everything else degrades the same way yfinance does: a
symbol that cannot be fetched or parsed is simply absent from the result, never
an exception that takes the whole run down.
"""

from __future__ import annotations

import io
import time
from datetime import date, timedelta

import pandas as pd

from ..logging_setup import get_logger
from .provider import Fundamentals, Quote, normalize_bars

log = get_logger("swing.data.stooq")

CSV_URL = "https://stooq.com/q/d/l/"

#: Stooq answers with this plain-text body once you have hammered it too hard.
#: It is a 200 with a human sentence in it, not an HTTP error, so it has to be
#: sniffed out of the body.
RATE_LIMIT_MARKER = "exceeded the daily hits limit"

#: Body a valid-looking but empty symbol/date window comes back with.
NO_DATA_MARKERS = ("no data", "brak danych")

#: How many calendar days of history :meth:`quotes` asks for. Long enough to
#: clear a long weekend plus a holiday, short enough to stay cheap.
QUOTE_LOOKBACK_DAYS = 7


class StooqProvider:
    name = "stooq"

    def __init__(self, cfg):
        self.cfg = cfg
        self.pause = float(cfg.data.get("request_pause_sec", 1.0))
        self.timeout = float(cfg.data.get("request_timeout_sec", 15.0))
        # One warning per run, not one per symbol: hitting the daily limit
        # means *every* remaining symbol fails, and 500 identical warnings
        # bury the one line that matters.
        self._limit_warned = False
        self._no_extras_logged = False

    # -- bars --------------------------------------------------------------
    def daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, pd.DataFrame]:
        symbols = [s.upper() for s in symbols]
        out: dict[str, pd.DataFrame] = {}
        self._limit_warned = False

        for sym in symbols:
            try:
                body = self._fetch_csv(sym, start, end)
            except Exception as exc:  # network stack, DNS, TLS, ...
                log.warning("stooq fetch failed for %s: %s", sym, exc)
                continue

            frame = self._parse_csv(sym, body)
            if frame is not None and len(frame):
                out[sym] = frame

        missing = [s for s in symbols if s not in out]
        if missing:
            log.debug("stooq returned no usable bars for %d symbol(s): %s",
                      len(missing), ", ".join(missing[:10]))
        return out

    # -- the single network seam ------------------------------------------
    def _fetch_csv(self, symbol: str, start: date, end: date) -> str | None:
        """Fetch one symbol's daily CSV. The *only* place this module does I/O.

        Tests monkeypatch this with canned CSV strings; everything above it is
        pure parsing, so the failure modes below can be exercised offline.
        """
        import requests

        params = {
            "s": stooq_symbol(symbol),
            "i": "d",
            "d1": start.strftime("%Y%m%d"),
            "d2": end.strftime("%Y%m%d"),
        }
        try:
            resp = requests.get(CSV_URL, params=params, timeout=self.timeout)
        finally:
            # Politeness pause happens whether or not the call succeeded —
            # retrying a rate-limited host at full speed is how you get banned.
            if self.pause:
                time.sleep(self.pause)

        if resp.status_code != 200:
            log.debug("stooq HTTP %s for %s", resp.status_code, symbol)
            return None
        return resp.text

    # -- parsing -----------------------------------------------------------
    def _parse_csv(self, symbol: str, body: str | None) -> pd.DataFrame | None:
        """Turn a CSV body into canonical bars, or ``None`` for "no data".

        Every failure mode returns ``None``. A symbol Stooq does not know about
        is indistinguishable from one it is refusing to serve right now, and
        neither is worth stopping a scan over.
        """
        if body is None:
            return None

        text = body.strip()
        if not text:
            log.debug("stooq returned an empty body for %s", symbol)
            return None

        head = text[:200].lower()
        if RATE_LIMIT_MARKER in head:
            if not self._limit_warned:
                self._limit_warned = True
                log.warning(
                    "stooq daily hits limit reached — remaining symbols this run "
                    "will be absent; cached bars are what keeps the scan usable"
                )
            return None
        if any(marker in head for marker in NO_DATA_MARKERS):
            log.debug("stooq has no data for %s", symbol)
            return None

        try:
            raw = pd.read_csv(io.StringIO(text))
        except Exception as exc:
            log.debug("unparseable stooq CSV for %s: %s", symbol, exc)
            return None

        if raw is None or len(raw) == 0:
            log.debug("stooq CSV for %s has a header but no rows", symbol)
            return None

        date_col = next(
            (c for c in raw.columns if str(c).strip().lower() == "date"), None
        )
        if date_col is None:
            log.debug("stooq CSV for %s has no Date column (got %s)",
                      symbol, list(raw.columns))
            return None

        try:
            raw = raw.set_index(pd.to_datetime(raw[date_col], errors="coerce"))
            raw = raw.drop(columns=[date_col])
            raw = raw[raw.index.notna()]
            frame = normalize_bars(raw)
        except Exception as exc:
            # normalize_bars raises ValueError on a frame that is missing
            # required columns; anything else here is equally malformed.
            log.debug("malformed stooq bars for %s: %s", symbol, exc)
            return None
        return frame

    # -- quotes ------------------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Last daily close, explicitly marked stale.

        Stooq is end-of-day only, so there is no such thing as a live quote
        here. Marking these ``stale=True`` is not a formality: it is what makes
        the pre-open confirm step widen its tolerances instead of treating a
        day-old close as an executable price. No bid/ask exists either, so
        spread checks fall back to their ATR-based estimate.
        """
        end = date.today()
        start = end - timedelta(days=QUOTE_LOOKBACK_DAYS)
        bars = self.daily_bars(list(symbols), start, end)

        out: dict[str, Quote] = {}
        for sym in [s.upper() for s in symbols]:
            frame = bars.get(sym)
            if frame is None or not len(frame):
                continue
            price = float(frame["close"].iloc[-1])
            if price <= 0:
                continue
            out[sym] = Quote(
                symbol=sym,
                price=price,
                bid=None,
                ask=None,
                timestamp=frame.index[-1].to_pydatetime(),
                stale=True,
            )
        return out

    # -- earnings / fundamentals ------------------------------------------
    def earnings_dates(self, symbols: list[str]) -> dict[str, date | None]:
        """Stooq has no earnings calendar: everything is ``None`` (= unknown)."""
        self._log_no_extras()
        return {s.upper(): None for s in symbols}

    def fundamentals(self, symbols: list[str]) -> dict[str, Fundamentals]:
        """Stooq has no fundamentals: empty records, every field ``None``."""
        self._log_no_extras()
        return {s.upper(): Fundamentals(symbol=s.upper()) for s in symbols}

    def _log_no_extras(self) -> None:
        if self._no_extras_logged:
            return
        self._no_extras_logged = True
        log.info(
            "stooq carries no earnings dates or fundamentals; the earnings "
            "blackout and fundamental filters fail open (no opinion, not 'fails')"
        )


def stooq_symbol(symbol: str) -> str:
    """Map a US ticker to Stooq's symbol space.

    Stooq namespaces by exchange suffix and uses dashes where US tickers use
    dots for share classes: ``AAPL -> aapl.us``, ``BRK.B -> brk-b.us``.
    """
    base = str(symbol).strip().lower().replace(".", "-")
    return f"{base}.us"
