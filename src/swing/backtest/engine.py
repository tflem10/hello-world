"""Cross-sectional daily-bar backtest engine.

Why a custom engine
-------------------
vectorbt fights numba on Apple silicon and is awkward for cross-sectional
portfolio logic; backtesting.py is single-asset only. This strategy ranks a
1,000-name universe every day, caps concurrent positions, buys whole shares
with real cash, and trails ATR stops — that is portfolio logic, not a
per-symbol signal loop. Roughly 400 lines of pandas/numpy buys us an engine
that imports :mod:`swing.strategy.rules` **unchanged**, so the backtest and the
nightly scan cannot disagree about what a trade is.

Timing model (no look-ahead)
----------------------------
Signals are evaluated on the close of day *t*; the resulting order fills at the
**open of day t+1**. Nothing that happens on day t+1 is visible when the order
is chosen. The only information used to size the order is ATR as of the close
of day *t*, which is exactly what the drafted Schwab order carries overnight.

Within a day the sequence is:

1. queued exits fill at the open
2. queued entries fill at the open (cash permitting)
3. stops are tested against the day's bar — a gap through the stop fills at the
   **open**, not at the stop price, because that is what actually happens
4. survivors ratchet their Chandelier trail against today's close
5. the time stop and tomorrow's entry signals are evaluated on the close

Costs
-----
Commission (default $0 at Schwab), ``slippage_bps`` per side, and an
ATR-proportional spread proxy per side. Costs are charged on entry *and* exit,
and stop exits pay them too — stops fill worse than mid, not better.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from ..config import Config
from ..data.cache import data_fingerprint
from ..logging_setup import get_logger
from ..strategy import rules
from ..strategy.rules import SymbolMeta
from ..strategy.sizing import size_position

log = get_logger("swing.backtest")

EXIT_STOP = "stop"
EXIT_GAP = "gap_through_stop"
EXIT_TRAIL = "trailing_stop"
EXIT_TIME = "time_stop"
EXIT_EOD = "end_of_backtest"


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    initial_stop: float
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    final_stop: float = 0.0
    rank_score: float = float("nan")
    atr_at_entry: float = float("nan")
    mae: float = 0.0          # worst adverse excursion, as a fraction of entry
    mfe: float = 0.0          # best favourable excursion
    costs: float = 0.0

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price <= 0:
            return 0.0
        return self.exit_price / self.entry_price - 1.0

    @property
    def risk_dollars(self) -> float:
        return (self.entry_price - self.initial_stop) * self.shares

    @property
    def r_multiple(self) -> float:
        """P&L expressed in units of initial risk — the only cross-trade-comparable
        measure of whether a trade worked."""
        risk = self.risk_dollars
        return self.pnl / risk if risk > 0 else float("nan")

    def hold_days(self) -> int:
        if self.exit_date is None:
            return 0
        return int(np.busday_count(self.entry_date.date(), self.exit_date.date()))


@dataclass
class _Position:
    symbol: str
    col: int
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    stop: float
    initial_stop: float
    atr_at_entry: float
    rank_score: float
    highest_close: float
    lowest_low: float
    highest_high: float = 0.0
    bars_held: int = 0
    entry_costs: float = 0.0
    time_stop_queued: bool = False


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    cash: pd.Series
    exposure: pd.Series
    open_positions: pd.Series
    params: dict
    config_hash: str
    data_hash: str
    universe_size: int
    start: date
    end: date
    warnings: list[str] = field(default_factory=list)
    label: str = ""

    @property
    def n_trades(self) -> int:
        return len(self.trades)


class Backtester:
    """Run one backtest over a fixed set of bars."""

    def __init__(
        self,
        cfg: Config,
        bars: dict[str, pd.DataFrame],
        meta: dict[str, SymbolMeta] | None = None,
        benchmark: pd.DataFrame | None = None,
        earnings: dict[str, list[date]] | None = None,
        feature_cache: dict | None = None,
    ):
        self.cfg = cfg
        self.raw_bars = {s: b for s, b in bars.items() if len(b)}
        self.meta = meta or {}
        self.benchmark = benchmark
        self.earnings = earnings or {}
        self.warnings: list[str] = []
        # Walk-forward re-runs the same symbols under many parameter sets, but
        # most of the grid (stop multiples, position caps) does not change a
        # single feature. Keying the cache on only the feature-relevant params
        # turns a 27-combination sweep into 3 feature passes.
        self.feature_cache = feature_cache if feature_cache is not None else {}

    # -- panel construction ------------------------------------------------
    def _build_panel(self, start: pd.Timestamp | None, end: pd.Timestamp | None):
        """Compute features per symbol, then align everything onto one date axis."""
        feature_frames: dict[str, pd.DataFrame] = {}
        fkey = feature_key(self.cfg)
        for sym, bars in self.raw_bars.items():
            meta = self.meta.get(sym, SymbolMeta(symbol=sym))
            cache_key = (sym, fkey, len(bars), bars.index[-1])
            cached = self.feature_cache.get(cache_key)
            if cached is not None:
                feature_frames[sym] = cached
                continue
            try:
                frame = rules.compute_features(bars, self.cfg, meta)
            except Exception as exc:      # one bad symbol must not kill the run
                log.warning("skipping %s: feature computation failed (%s)", sym, exc)
                continue
            self.feature_cache[cache_key] = frame
            feature_frames[sym] = frame

        if not feature_frames:
            raise ValueError("no symbols produced features; is the cache populated?")

        all_dates = sorted(set().union(*(f.index for f in feature_frames.values())))
        dates = pd.DatetimeIndex(all_dates)
        if start is not None:
            dates = dates[dates >= start]
        if end is not None:
            dates = dates[dates <= end]
        if not len(dates):
            raise ValueError("no bars fall inside the requested backtest window")

        symbols = sorted(feature_frames)
        n_d, n_s = len(dates), len(symbols)

        numeric = ["open", "high", "low", "close", "atr", "rank_score"]
        arrays = {name: np.full((n_d, n_s), np.nan) for name in numeric}
        arrays["eligible"] = np.zeros((n_d, n_s), dtype=bool)
        arrays["entry_signal"] = np.zeros((n_d, n_s), dtype=bool)

        for j, sym in enumerate(symbols):
            frame = feature_frames[sym].reindex(dates)
            for name in numeric:
                arrays[name][:, j] = frame[name].to_numpy(dtype="float64")
            arrays["eligible"][:, j] = frame["eligible"].fillna(False).to_numpy(dtype=bool)
            arrays["entry_signal"][:, j] = (
                frame["entry_signal"].fillna(False).to_numpy(dtype=bool)
            )

        return dates, symbols, arrays

    def _regime_mask(self, dates: pd.DatetimeIndex) -> np.ndarray:
        bench = self.benchmark
        if bench is None:
            sym = str(self.cfg.strategy.regime.get("symbol", "SPY")).upper()
            bench = self.raw_bars.get(sym)
        if bench is None or not len(bench):
            if bool(self.cfg.strategy.regime.get("enabled", True)):
                self.warnings.append(
                    f"regime benchmark {self.cfg.strategy.regime.get('symbol')} has no bars; "
                    "the regime filter was DISABLED for this run"
                )
            return np.ones(len(dates), dtype=bool)
        series = rules.regime_series(bench, self.cfg).reindex(dates).ffill().fillna(False)
        return series.to_numpy(dtype=bool)

    def _earnings_blocked(self, dates: pd.DatetimeIndex, symbols: list[str]) -> np.ndarray:
        """Per-(date, symbol) earnings blackout mask.

        Free data has no historical earnings calendar going back to 2010, so
        this is all-``False`` unless a calendar was supplied. That is a real
        difference between backtest and live and it is recorded as a warning
        rather than papered over.

        The warning below is deliberately **all-or-nothing**: it fires only
        when no calendar at all was supplied. It says nothing about how much of
        the universe a supplied calendar actually covers, and a calendar
        covering three symbols out of a thousand silences it for the whole run.
        Coverage accounting lives one level up, in
        :func:`swing.backtest.runner.load_earnings`, because that is where the
        universe being traded is known alongside the file it came from — every
        path reached through ``swing backtest`` therefore gets a second warning
        naming the covered-of-universe counts.

        The consequence worth knowing: calling :func:`run_backtest` directly
        with a hand-built ``earnings`` dict bypasses the runner, and so gets
        this all-or-nothing warning and no coverage check. If you drive the
        engine yourself, check coverage yourself.
        """
        blocked = np.zeros((len(dates), len(symbols)), dtype=bool)
        if not self.earnings:
            if int(self.cfg.strategy.earnings.get("blackout_days_before", 0)) > 0:
                self.warnings.append(
                    "no historical earnings calendar was supplied, so the earnings "
                    "blackout was NOT applied in this backtest. The live scanner does "
                    "apply it, which removes some entries — expect live to take fewer "
                    "trades than these results imply."
                )
            return blocked

        before = int(self.cfg.strategy.earnings.get("blackout_days_before", 0))
        after = int(self.cfg.strategy.earnings.get("blackout_days_after", 0))
        date_values = dates.to_numpy(dtype="datetime64[D]")
        for j, sym in enumerate(symbols):
            for event in self.earnings.get(sym, []):
                ev = np.datetime64(event, "D")
                blocked[:, j] |= (date_values >= ev - np.timedelta64(before, "D")) & (
                    date_values <= ev + np.timedelta64(after, "D")
                )
        return blocked

    # -- costs -------------------------------------------------------------
    def _buy_fill(self, price: float, atr_value: float) -> float:
        bt = self.cfg.backtest
        slip = price * float(bt.slippage_bps) / 10_000.0
        spread = float(bt.get("spread_atr_frac", 0.0)) * (atr_value if atr_value > 0 else 0.0)
        return price + slip + spread

    def _sell_fill(self, price: float, atr_value: float) -> float:
        bt = self.cfg.backtest
        slip = price * float(bt.slippage_bps) / 10_000.0
        spread = float(bt.get("spread_atr_frac", 0.0)) * (atr_value if atr_value > 0 else 0.0)
        return max(price - slip - spread, 0.01)

    # -- the loop ----------------------------------------------------------
    def run(
        self,
        start: date | None = None,
        end: date | None = None,
        label: str = "",
    ) -> BacktestResult:
        cfg = self.cfg
        s = cfg.strategy
        start_ts = pd.Timestamp(start) if start else None
        end_ts = pd.Timestamp(end) if end else None

        dates, symbols, panel = self._build_panel(start_ts, end_ts)
        regime = self._regime_mask(dates)
        blocked = self._earnings_blocked(dates, symbols)
        col_of = {sym: j for j, sym in enumerate(symbols)}

        op, hi, lo, cl = panel["open"], panel["high"], panel["low"], panel["close"]
        atr_a, score_a = panel["atr"], panel["rank_score"]
        eligible, signal = panel["eligible"], panel["entry_signal"]

        commission = float(cfg.backtest.get("commission_per_trade", 0.0))
        max_positions = int(cfg.account.max_concurrent_positions)
        risk_pct = float(cfg.account.risk_pct)
        max_position_pct = float(cfg.account.max_position_pct)
        time_stop = int(s.exit.time_stop_days)

        cash = float(cfg.backtest.initial_equity)
        starting_equity = cash
        positions: dict[str, _Position] = {}
        pending_entries: list[tuple[str, float, float, float]] = []   # sym, stop, atr, score
        pending_exits: list[tuple[str, str]] = []                     # sym, reason
        trades: list[Trade] = []

        equity_curve = np.full(len(dates), np.nan)
        cash_curve = np.full(len(dates), np.nan)
        exposure_curve = np.zeros(len(dates))
        open_count_curve = np.zeros(len(dates), dtype=int)

        for i, today in enumerate(dates):
            # -- 1. queued exits fill at the open --------------------------
            for sym, reason in pending_exits:
                pos = positions.get(sym)
                if pos is None:
                    continue
                price = op[i, pos.col]
                if not math.isfinite(price):
                    continue        # no bar today (halt); carry the position
                fill = self._sell_fill(price, pos.atr_at_entry)
                cash += pos.shares * fill - commission
                trades.append(self._close_trade(pos, today, fill, reason, commission))
                del positions[sym]
            pending_exits = []

            # -- 2. queued entries fill at the open ------------------------
            for sym, stop, atr_value, score in pending_entries:
                if sym in positions or len(positions) >= max_positions:
                    continue
                j = col_of[sym]
                price = op[i, j]
                if not math.isfinite(price) or price <= 0:
                    continue
                fill = self._buy_fill(price, atr_value)
                if stop >= fill:
                    continue        # gapped up through the stop distance; skip
                equity_now = cash + sum(
                    p.shares * _last_price(cl, i, p.col) for p in positions.values()
                )
                size = size_position(
                    entry=fill,
                    stop=stop,
                    equity=equity_now,
                    risk_pct=risk_pct,
                    max_position_pct=max_position_pct,
                    available_cash=cash - commission,
                )
                if not size.affordable:
                    continue
                cost = size.shares * fill + commission
                if cost > cash:
                    continue
                cash -= cost
                positions[sym] = _Position(
                    symbol=sym,
                    col=j,
                    entry_date=today,
                    entry_price=fill,
                    shares=size.shares,
                    stop=stop,
                    initial_stop=stop,
                    atr_at_entry=atr_value,
                    rank_score=score,
                    highest_close=fill,
                    lowest_low=fill,
                    entry_costs=commission + size.shares * (fill - price),
                    highest_high=fill,
                )
            pending_entries = []

            # -- 3. stops against today's bar ------------------------------
            for sym in list(positions):
                pos = positions[sym]
                day_open, day_low, day_high = op[i, pos.col], lo[i, pos.col], hi[i, pos.col]
                if not math.isfinite(day_low):
                    continue
                pos.lowest_low = min(pos.lowest_low, day_low)
                if math.isfinite(day_high):
                    pos.highest_high = max(pos.highest_high, day_high)

                if math.isfinite(day_open) and day_open <= pos.stop:
                    # Gapped through the stop overnight: you get the open, not
                    # the stop price. Modelling this as a stop-price fill is the
                    # single most common way a backtest flatters itself.
                    fill = self._sell_fill(day_open, pos.atr_at_entry)
                    cash += pos.shares * fill - commission
                    trades.append(self._close_trade(pos, today, fill, EXIT_GAP, commission))
                    del positions[sym]
                elif day_low <= pos.stop:
                    fill = self._sell_fill(pos.stop, pos.atr_at_entry)
                    cash += pos.shares * fill - commission
                    reason = EXIT_TRAIL if pos.stop > pos.initial_stop else EXIT_STOP
                    trades.append(self._close_trade(pos, today, fill, reason, commission))
                    del positions[sym]

            # -- 4. ratchet trails on today's close; queue time stops ------
            for sym, pos in positions.items():
                today_close = cl[i, pos.col]
                today_atr = atr_a[i, pos.col]
                if math.isfinite(today_close):
                    pos.highest_close = max(pos.highest_close, today_close)
                if math.isfinite(today_close) and math.isfinite(today_atr) and today_atr > 0:
                    pos.stop = rules.ratchet(
                        pos.stop, rules.chandelier_stop(pos.highest_close, today_atr, s)
                    )
                pos.bars_held += 1
                if time_stop > 0 and pos.bars_held >= time_stop and not pos.time_stop_queued:
                    pos.time_stop_queued = True
                    pending_exits.append((sym, EXIT_TIME))

            # -- 5. mark to market -----------------------------------------
            held_value = sum(
                p.shares * _last_price(cl, i, p.col) for p in positions.values()
            )
            equity = cash + held_value
            equity_curve[i] = equity
            cash_curve[i] = cash
            exposure_curve[i] = held_value / equity if equity > 0 else 0.0
            open_count_curve[i] = len(positions)

            # -- 6. tonight's signals -> tomorrow's orders -----------------
            if i + 1 >= len(dates):
                continue
            # Positions queued to exit tomorrow do not free a slot today: with
            # cash accounting you cannot buy before you sell.
            slots = max_positions - len(positions)
            if slots <= 0 or not regime[i]:
                continue

            candidates = np.where(
                eligible[i] & signal[i] & ~blocked[i] & np.isfinite(score_a[i])
            )[0]
            if not len(candidates):
                continue
            # Rank the cross-section: best risk-adjusted momentum first.
            candidates = candidates[np.argsort(-score_a[i, candidates])]

            taken = 0
            for j in candidates:
                if taken >= slots:
                    break
                sym = symbols[j]
                if sym in positions:
                    continue
                atr_value = atr_a[i, j]
                close_now = cl[i, j]
                if not (math.isfinite(atr_value) and atr_value > 0 and math.isfinite(close_now)):
                    continue
                stop = rules.initial_stop(close_now, atr_value, s)
                if stop <= 0:
                    continue
                pending_entries.append((sym, stop, float(atr_value), float(score_a[i, j])))
                taken += 1

        # -- close whatever is still open at the end -----------------------
        last = len(dates) - 1
        for _sym, pos in list(positions.items()):
            price = _last_price(cl, last, pos.col)
            fill = self._sell_fill(price, pos.atr_at_entry)
            cash += pos.shares * fill - commission
            trades.append(self._close_trade(pos, dates[last], fill, EXIT_EOD, commission))
        if positions:
            equity_curve[last] = cash
            positions.clear()

        equity = pd.Series(equity_curve, index=dates, name="equity").ffill().fillna(
            starting_equity
        )
        return BacktestResult(
            trades=_trades_frame(trades),
            equity=equity,
            cash=pd.Series(cash_curve, index=dates, name="cash").ffill(),
            exposure=pd.Series(exposure_curve, index=dates, name="exposure"),
            open_positions=pd.Series(open_count_curve, index=dates, name="open_positions"),
            params=_flatten_params(cfg),
            config_hash=cfg.hash,
            data_hash=data_fingerprint(self.raw_bars),
            universe_size=len(symbols),
            start=dates[0].date(),
            end=dates[-1].date(),
            warnings=list(self.warnings),
            label=label,
        )

    def _close_trade(
        self, pos: _Position, when: pd.Timestamp, fill: float, reason: str, commission: float
    ) -> Trade:
        mae = (pos.lowest_low / pos.entry_price - 1.0) if pos.entry_price > 0 else 0.0
        best = max(pos.highest_high, pos.highest_close)
        mfe = (best / pos.entry_price - 1.0) if pos.entry_price > 0 else 0.0
        return Trade(
            symbol=pos.symbol,
            entry_date=pos.entry_date,
            entry_price=pos.entry_price,
            shares=pos.shares,
            initial_stop=pos.initial_stop,
            exit_date=when,
            exit_price=fill,
            exit_reason=reason,
            final_stop=pos.stop,
            rank_score=pos.rank_score,
            atr_at_entry=pos.atr_at_entry,
            mae=mae,
            mfe=mfe,
            costs=pos.entry_costs + commission,
        )


def _last_price(closes: np.ndarray, i: int, col: int) -> float:
    """Most recent finite close at or before row ``i`` (handles halts/late listings)."""
    value = closes[i, col]
    if math.isfinite(value):
        return float(value)
    column = closes[: i + 1, col]
    finite = column[np.isfinite(column)]
    return float(finite[-1]) if len(finite) else 0.0


def _trades_frame(trades: list[Trade]) -> pd.DataFrame:
    columns = [
        "symbol", "entry_date", "entry_price", "shares", "initial_stop",
        "exit_date", "exit_price", "exit_reason", "final_stop", "pnl",
        "return_pct", "r_multiple", "hold_days", "mae", "mfe",
        "rank_score", "atr_at_entry", "risk_dollars",
    ]
    if not trades:
        return pd.DataFrame(columns=columns)
    rows = []
    for t in trades:
        rows.append(
            {
                "symbol": t.symbol,
                "entry_date": t.entry_date,
                "entry_price": t.entry_price,
                "shares": t.shares,
                "initial_stop": t.initial_stop,
                "exit_date": t.exit_date,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "final_stop": t.final_stop,
                "pnl": t.pnl,
                "return_pct": t.return_pct,
                "r_multiple": t.r_multiple,
                "hold_days": t.hold_days(),
                "mae": t.mae,
                "mfe": t.mfe,
                "rank_score": t.rank_score,
                "atr_at_entry": t.atr_at_entry,
                "risk_dollars": t.risk_dollars,
            }
        )
    return pd.DataFrame(rows, columns=columns).sort_values("entry_date").reset_index(drop=True)


def feature_key(cfg: Config) -> str:
    """Hash of only those config values that change computed features.

    Position caps, risk percentages, costs and stop multiples are deliberately
    excluded: they affect sizing and exits, not the indicator frames.
    """
    s = cfg.strategy.as_dict()
    relevant = {
        "trend_template": s.get("trend_template"),
        "entry": s.get("entry"),
        "rank": s.get("rank"),
        "atr_len": s.get("exit", {}).get("atr_len"),
        "fundamentals": s.get("fundamentals"),
        "min_price": cfg.universe.get("min_price"),
        "min_dollar_volume": cfg.universe.get("min_dollar_volume"),
    }
    return hashlib.sha256(
        json.dumps(relevant, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _flatten_params(cfg: Config) -> dict:
    keep = ("account", "universe", "strategy", "backtest")
    return {k: cfg.as_dict().get(k) for k in keep}


def run_backtest(
    cfg: Config,
    bars: dict[str, pd.DataFrame],
    meta: dict[str, SymbolMeta] | None = None,
    benchmark: pd.DataFrame | None = None,
    earnings: dict[str, list[date]] | None = None,
    start: date | None = None,
    end: date | None = None,
    label: str = "",
    feature_cache: dict | None = None,
) -> BacktestResult:
    """Convenience wrapper around :class:`Backtester`."""
    return Backtester(
        cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
        feature_cache=feature_cache,
    ).run(start=start, end=end, label=label)
