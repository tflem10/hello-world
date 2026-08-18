"""The nightly scan: cache refresh -> rules -> sizing -> pick sheet -> alerts.

This runs after the close. It uses **exactly** the same
:mod:`swing.strategy.rules` and :mod:`swing.strategy.sizing` code the
backtester uses, evaluated on the most recent bar, so a pick here is the same
decision the backtest would have made on that bar.

Order of operations, and why:

1. **Check the gate first.** Refusing to trade an unvalidated strategy is the
   whole point, and there is no reason to spend ten minutes downloading data
   to produce picks that are going to be withheld anyway. ``--force`` overrides
   it and stamps the override on the sheet, in the alert, and in the log.
2. **Refresh the cache**, tolerating failure. A stale cache with a loud warning
   beats no picks at all.
3. **Score the cross-section**, rank, size, draft orders.
4. **Write the sheet to disk before sending anything.** If every alert channel
   is down, the picks still exist.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from . import __version__
from .backtest.gate import check_gate
from .config import Config
from .data.cache import BarCache
from .data.pipeline import (
    load_bars,
    refresh_earnings,
    refresh_fundamentals,
    update,
)
from .data.universe import Symbol, build_universe
from .logging_setup import get_logger
from .orders import OrderValidationError, draft_orders, validate_order
from .picks import (
    STATUS_TRADABLE,
    STATUS_UNAFFORDABLE,
    HoldingAction,
    Pick,
    PickSheet,
    now_stamp,
    write_sheet,
)
from .strategy import rules
from .strategy.rules import SymbolMeta
from .strategy.sizing import size_position

log = get_logger("swing.scan")


def run_scan(
    cfg: Config,
    dry_run: bool = False,
    force: bool = False,
    as_of: str | None = None,
    refresh: bool = True,
) -> int:
    warnings: list[str] = []

    # -- 1. the gate -------------------------------------------------------
    gate = check_gate(cfg)
    log.info("%s", gate.describe())
    if not gate.passed and not force:
        print(gate.describe())
        print(
            "\nNo picks were generated. This is deliberate: the strategy has not been\n"
            "validated against the configuration you are about to trade.\n"
            "  swing backtest --walk-forward     # validate\n"
            "  swing scan --force                # override (and own the consequences)"
        )
        return 3
    if not gate.passed and force:
        warnings.append(
            "GATE OVERRIDDEN with --force: these picks come from a strategy that has "
            "NOT passed out-of-sample validation for this config. "
            + "; ".join(gate.reasons)
        )
        log.warning("%s", warnings[-1])

    # -- 2. data -----------------------------------------------------------
    universe = build_universe(cfg)
    if refresh:
        try:
            update(cfg)
        except Exception as exc:
            warnings.append(
                f"data refresh failed ({exc}); scanning the cache as it stands. "
                "Prices may be stale."
            )
            log.warning("%s", warnings[-1])
        for name, fn in (("earnings", refresh_earnings), ("fundamentals", refresh_fundamentals)):
            try:
                fn(cfg)
            except Exception as exc:
                warnings.append(f"{name} refresh failed ({exc}); using cached values")
                log.warning("%s", warnings[-1])

    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    bars = load_bars(cfg, universe, min_bars=260)
    if not bars:
        print(
            "the price cache has no usable history.\n"
            "Run `swing data --backfill` (needs network access) and try again."
        )
        return 4

    as_of_ts = _resolve_as_of(bars, as_of)
    stale_warning = _staleness_warning(cfg, as_of_ts)
    if stale_warning:
        warnings.append(stale_warning)
        log.warning("%s", stale_warning)

    earnings = cache.read_earnings()
    fundamentals = cache.read_fundamentals()

    # -- 3. regime ---------------------------------------------------------
    regime_symbol = str(cfg.strategy.regime.get("symbol", "SPY")).upper()
    benchmark = bars.get(regime_symbol) or load_bars(cfg, [regime_symbol]).get(regime_symbol)
    regime_ok, regime_note = _evaluate_regime(cfg, benchmark, as_of_ts, warnings)

    # -- 4. holdings -------------------------------------------------------
    from .execution.journal import open_positions

    positions = open_positions(cfg)
    holdings = _holding_actions(cfg, positions, bars, earnings, as_of_ts)
    equity = float(cfg.account.equity)
    committed = sum(p.cost_basis for p in positions.values())
    available_cash = max(equity - committed, 0.0)
    slots = int(cfg.account.max_concurrent_positions) - len(positions)
    if slots <= 0:
        warnings.append(
            f"{len(positions)} position(s) already open, which is the configured "
            f"maximum ({cfg.account.max_concurrent_positions}). No new entries."
        )

    # -- 5. score the cross-section ---------------------------------------
    candidates = _score_universe(
        cfg, bars, universe, fundamentals, earnings, as_of_ts, warnings
    )
    log.info("%d candidates passed every filter", len(candidates))

    picks: list[Pick] = []
    watch: list[Pick] = []
    if regime_ok and slots > 0:
        picks, watch = _size_and_draft(
            cfg, candidates, positions, equity, available_cash, slots
        )
    elif candidates:
        # Still show what the filters found, but say plainly why none were
        # taken. Reporting a regime block as "too expensive" would send the
        # user off to fund the account for no reason.
        block_reason = (
            "market regime is risk-off — no new entries tonight"
            if not regime_ok
            else f"no free position slot ({len(positions)} already open)"
        )
        _, watch = _size_and_draft(
            cfg, candidates, positions, equity, available_cash, 0,
            block_reason=block_reason,
        )

    sheet = PickSheet(
        as_of=str(as_of_ts.date()),
        generated_at=now_stamp(),
        equity=equity,
        available_cash=available_cash,
        regime_ok=regime_ok,
        regime_note=regime_note,
        gate_passed=gate.passed,
        gate_note=_gate_note(gate, force),
        config_hash=cfg.hash,
        universe_size=len(bars),
        candidates_considered=len(candidates),
        picks=picks,
        watch=watch,
        holdings=holdings,
        warnings=warnings,
        data_as_of=str(as_of_ts.date()),
        swing_version=__version__,
    )

    # -- 6. write, then send ----------------------------------------------
    out_dir = write_sheet(cfg, sheet)
    log.info("pick sheet written to %s", out_dir)
    print(sheet.to_text())
    print(f"\nsheet: {out_dir / 'picks.html'}")

    if dry_run:
        print("\n--dry-run: no alerts were sent.")
        return 0

    from .alerts.dispatch import deliver

    results = deliver(
        cfg,
        title=sheet.headline(),
        text=sheet.to_text(),
        html=sheet.to_html(),
        attachments=sorted((out_dir / "orders").glob("*.json")),
    )
    print()
    for result in results:
        print(result.line())
    return 0


# ---------------------------------------------------------------------------
# pieces
# ---------------------------------------------------------------------------
def _resolve_as_of(bars: dict[str, pd.DataFrame], as_of: str | None) -> pd.Timestamp:
    latest = max(df.index[-1] for df in bars.values())
    if not as_of:
        return latest
    requested = pd.Timestamp(as_of)
    if requested > latest:
        log.warning(
            "requested as-of %s is after the newest cached bar %s; using %s",
            requested.date(), latest.date(), latest.date(),
        )
        return latest
    return requested


def _staleness_warning(cfg: Config, as_of_ts: pd.Timestamp) -> str:
    max_stale = int(cfg.data.get("max_stale_days", 5))
    age = (date.today() - as_of_ts.date()).days
    if age > max_stale:
        return (
            f"the newest bar in the cache is {as_of_ts.date()} ({age} days old). "
            "These picks are based on stale prices — run `swing data --update`."
        )
    return ""


def _evaluate_regime(cfg, benchmark, as_of_ts, warnings) -> tuple[bool, str]:
    regime_cfg = cfg.strategy.regime
    symbol = str(regime_cfg.get("symbol", "SPY")).upper()
    if not bool(regime_cfg.get("enabled", True)):
        return True, "regime filter disabled in config"
    if benchmark is None or not len(benchmark):
        warnings.append(
            f"no bars for the regime benchmark {symbol}; the regime filter was skipped "
            "and new entries were allowed"
        )
        return True, f"{symbol} unavailable — filter skipped"

    series = rules.regime_series(benchmark, cfg)
    usable = series[series.index <= as_of_ts]
    if not len(usable):
        return True, f"{symbol} has no bars on or before {as_of_ts.date()}"

    ok = bool(usable.iloc[-1])
    ma_len = int(regime_cfg.ma_len)
    from . import indicators as ind

    ma = ind.sma(benchmark["close"], ma_len)
    ma = ma[ma.index <= as_of_ts]
    close = float(benchmark["close"][benchmark.index <= as_of_ts].iloc[-1])
    ma_value = float(ma.iloc[-1]) if len(ma) and pd.notna(ma.iloc[-1]) else float("nan")
    distance = (close / ma_value - 1.0) if ma_value == ma_value and ma_value else float("nan")
    note = (
        f"{symbol} {close:,.2f} vs {ma_len}-SMA {ma_value:,.2f} "
        f"({distance:+.1%}) — {'above' if ok else 'below'}"
    )
    return ok, note


def _score_universe(
    cfg, bars, universe: list[Symbol], fundamentals, earnings, as_of_ts, warnings
) -> list[dict]:
    """Evaluate every symbol on the as-of bar and return the ones that pass."""
    is_etf = {s.symbol: s.is_etf for s in universe}
    names = {s.symbol: s.name for s in universe}
    candidates: list[dict] = []
    unknown_earnings = 0
    blocked_earnings = 0

    for symbol, history in bars.items():
        history = history[history.index <= as_of_ts]
        if len(history) < 260:
            continue
        meta = SymbolMeta(
            symbol=symbol,
            is_etf=is_etf.get(symbol, False),
            fundamentals_ok=rules.fundamentals_ok(fundamentals.get(symbol), cfg.strategy),
        )
        try:
            features = rules.compute_features(history, cfg, meta)
        except Exception as exc:
            log.debug("skipping %s: %s", symbol, exc)
            continue

        row = features.iloc[-1]
        if not (bool(row["eligible"]) and bool(row["entry_signal"])):
            continue

        earnings_date = earnings.get(symbol)
        blocked, note = _earnings_status(cfg, earnings_date, as_of_ts.date())
        if blocked:
            blocked_earnings += 1
            continue
        if earnings_date is None:
            unknown_earnings += 1

        score = float(row["rank_score"])
        if score != score:                       # NaN
            continue

        candidates.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "is_etf": is_etf.get(symbol, False),
                "close": float(row["close"]),
                "atr": float(row["atr"]),
                "rank_score": score,
                "dollar_volume": float(row["dollar_volume"]),
                "adx": _adx_value(cfg, history),
                "earnings_date": earnings_date.isoformat() if earnings_date else None,
                "earnings_note": note,
                "features": features,
            }
        )

    if blocked_earnings:
        log.info("%d candidates were blocked by the earnings blackout", blocked_earnings)
    if unknown_earnings:
        warnings.append(
            f"{unknown_earnings} candidate(s) have no known earnings date. Policy is "
            f"'{cfg.strategy.earnings.get('unknown_date_policy')}' — they were allowed "
            "through and are tagged in the sheet. Check them yourself."
        )

    candidates.sort(key=lambda c: c["rank_score"], reverse=True)
    return candidates


def _adx_value(cfg, history: pd.DataFrame) -> float:
    from . import indicators as ind

    series = ind.adx(
        history["high"], history["low"], history["close"],
        int(cfg.strategy.trend_template.adx_len),
    )
    value = series.iloc[-1] if len(series) else float("nan")
    return float(value) if pd.notna(value) else float("nan")


def _earnings_status(cfg, earnings_date, today: date) -> tuple[bool, str]:
    """(blocked, human note) for one symbol's earnings situation."""
    conf = cfg.strategy.earnings
    if earnings_date is None:
        policy = str(conf.get("unknown_date_policy", "allow_with_warning"))
        if policy == "block":
            return True, "unknown earnings date (policy: block)"
        return False, "date UNKNOWN — verify before buying"

    days = (earnings_date - today).days
    before = int(conf.get("blackout_days_before", 0))
    after = int(conf.get("blackout_days_after", 0))
    if -after <= days <= before:
        return True, f"blackout: earnings in {days} day(s)"
    if 0 <= days <= before + 15:
        return False, f"{earnings_date} (in {days} days)"
    return False, f"{earnings_date}"


def _size_and_draft(
    cfg,
    candidates: list[dict],
    positions,
    equity: float,
    available_cash: float,
    slots: int,
    block_reason: str = "",
) -> tuple[list[Pick], list[Pick]]:
    """Size each candidate in rank order; split into tradable picks and watch list."""
    s = cfg.strategy
    picks: list[Pick] = []
    watch: list[Pick] = []
    cash_left = available_cash
    taken = 0

    for rank, candidate in enumerate(candidates, start=1):
        symbol = candidate["symbol"]
        if symbol in positions:
            continue

        close = candidate["close"]
        atr_value = candidate["atr"]
        stop = rules.initial_stop(close, atr_value, s)
        trail_offset = float(s.exit.chandelier_atr) * atr_value

        size = size_position(
            entry=close,
            stop=stop,
            equity=equity,
            risk_pct=float(cfg.account.risk_pct),
            max_position_pct=float(cfg.account.max_position_pct),
            available_cash=cash_left,
        )

        affordable_slot = size.affordable and taken < slots
        pick = Pick(
            symbol=symbol,
            rank=rank,
            status=STATUS_TRADABLE if affordable_slot else STATUS_UNAFFORDABLE,
            close=close,
            atr=atr_value,
            stop=round(stop, 2),
            trail_offset=round(trail_offset, 2),
            shares=size.shares if affordable_slot else 0,
            notional=round(size.notional, 2) if affordable_slot else 0.0,
            risk_dollars=round(size.risk_dollars, 2) if affordable_slot else 0.0,
            risk_pct=size.risk_pct(equity) if affordable_slot else 0.0,
            equity_pct=size.equity_pct(equity) if affordable_slot else 0.0,
            sizing_limit=size.limit.value,
            sizing_note=_watch_reason(size, affordable_slot, block_reason),
            rank_score=candidate["rank_score"],
            adx=candidate["adx"],
            dollar_volume=candidate["dollar_volume"],
            is_etf=candidate["is_etf"],
            name=candidate["name"],
            earnings_date=candidate["earnings_date"],
            earnings_note=candidate["earnings_note"],
            thesis=_thesis(candidate),
            stop_limit_price=round(rules.stop_limit_price(stop, s), 2),
        )

        if affordable_slot:
            try:
                pick.orders = draft_orders(
                    symbol=symbol,
                    shares=size.shares,
                    reference_price=close,
                    stop=round(stop, 2),
                    trail_offset=round(trail_offset, 2),
                    stop_limit_offset_pct=float(s.exit.stop_limit_offset_pct),
                    limit_slippage_pct=float(cfg.execution.get("limit_slippage_pct", 0.003)),
                )
                for name, order in pick.orders.items():
                    validate_order(order, path=f"{symbol}.{name}")
            except OrderValidationError as exc:
                log.error("dropping %s: drafted order failed validation (%s)", symbol, exc)
                pick.status = STATUS_UNAFFORDABLE
                pick.sizing_note = f"order validation failed: {exc}"
                pick.orders = {}
                watch.append(pick)
                continue

            picks.append(pick)
            cash_left -= size.notional
            taken += 1
        else:
            watch.append(pick)

    return picks, watch


def _watch_reason(size, affordable_slot: bool, block_reason: str) -> str:
    """Say the *actual* reason a candidate was not taken.

    Order matters: a portfolio-level block (regime off, book full) is the real
    answer even when the position also happens to be capped, and reporting the
    cap instead would send the user off to fund the account for nothing.
    """
    if affordable_slot:
        return size.notes[0] if size.notes else ""
    if block_reason:
        return block_reason
    if size.notes:
        return size.notes[0]
    return "not taken"


def _thesis(candidate: dict) -> str:
    """One line explaining why this symbol is on the list."""
    bits = [f"20d breakout, rank {candidate['rank_score']:.1f}"]
    if candidate["adx"] == candidate["adx"]:
        bits.append(f"ADX {candidate['adx']:.0f}")
    atr_pct = candidate["atr"] / candidate["close"] if candidate["close"] else 0.0
    bits.append(f"ATR {atr_pct:.1%}")
    if candidate["is_etf"]:
        bits.append("ETF")
    return ", ".join(bits)


def _holding_actions(cfg, positions, bars, earnings, as_of_ts) -> list[HoldingAction]:
    """What to do about positions already on the books."""
    actions: list[HoldingAction] = []
    s = cfg.strategy
    tighten_days = int(s.earnings.get("tighten_stop_days_before", 0))
    time_stop = int(s.exit.time_stop_days)
    today = as_of_ts.date()

    for symbol, pos in sorted(positions.items()):
        history = bars.get(symbol)
        if history is None or not len(history):
            actions.append(
                HoldingAction(symbol, "no data",
                              "not in the cache — check the symbol manually")
            )
            continue
        history = history[history.index <= as_of_ts]
        if not len(history):
            continue

        close = float(history["close"].iloc[-1])
        from . import indicators as ind

        atr_series = ind.atr(
            history["high"], history["low"], history["close"], int(s.exit.atr_len)
        )
        atr_value = float(atr_series.iloc[-1]) if len(atr_series) else float("nan")

        if atr_value == atr_value and atr_value > 0:
            entered = _parse_date(pos.entry_date)
            since = history[history.index >= pd.Timestamp(entered)] if entered else history
            highest_close = float(since["close"].max()) if len(since) else close
            candidate_stop = rules.chandelier_stop(highest_close, atr_value, s)
            new_stop = rules.ratchet(pos.stop, candidate_stop)
            if new_stop > pos.stop + 0.005:
                actions.append(
                    HoldingAction(
                        symbol, "raise stop",
                        f"{pos.stop:.2f} -> {new_stop:.2f} "
                        f"(high close {highest_close:.2f} - "
                        f"{s.exit.chandelier_atr}x ATR {atr_value:.2f})",
                    )
                )

        if close <= pos.stop:
            actions.append(
                HoldingAction(symbol, "STOP BREACHED",
                              f"last {close:.2f} is at or below the stop {pos.stop:.2f}")
            )

        earnings_date = earnings.get(symbol)
        if earnings_date and tighten_days > 0:
            days = (earnings_date - today).days
            if 0 <= days <= tighten_days:
                actions.append(
                    HoldingAction(
                        symbol, "earnings soon",
                        f"reports in {days} day(s) on {earnings_date} — tighten the stop "
                        "or close; a stop does not protect you across a gap",
                    )
                )

        entered = _parse_date(pos.entry_date)
        if entered and time_stop > 0:
            held = len(history[history.index >= pd.Timestamp(entered)])
            if held >= time_stop:
                actions.append(
                    HoldingAction(symbol, "time stop",
                                  f"held {held} trading days (limit {time_stop})")
                )
    return actions


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _gate_note(gate, force: bool) -> str:
    if gate.passed:
        checks = ", ".join(f"{name} {actual:.2f}" for name, actual, _, _ in gate.checked)
        return f"cleared ({checks})" if checks else (gate.reasons[0] if gate.reasons else "")
    prefix = "OVERRIDDEN with --force: " if force else ""
    return prefix + "; ".join(gate.reasons)


def next_trading_day(from_date: date) -> date:
    """Next weekday. Deliberately ignores market holidays — the pre-open confirm
    step re-checks against live quotes anyway, and a wrong label on a holiday is
    cosmetic, whereas a bundled holiday calendar is a maintenance burden."""
    nxt = from_date + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt
