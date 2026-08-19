"""FROZEN CONTRACT 9 — the nightly scan and the morning confirmation.

``run_scan`` is the whole system in one function: gate, universe, bars, rules,
regime, ranking, dedupe, slots, sizing, report, orders, notifications. It is
written so every one of those steps is *visible* in the report even when it
produces nothing, because a scan that silently emits no picks is indistinguishable
from a scan that crashed.

Three design rules hold throughout:

**The gate is fail-closed.** If ``swing.backtest.gate`` cannot be imported, or
``check()`` raises, or the gate simply has not passed, no picks are emitted. The
report is still written and the notifications are still sent — "the scan ran and
the gate is failing" is exactly the message a user needs. ``force=True`` is the
only override, and it says so in the report.

**Nothing reads the clock except the entry points.** ``asof`` is resolved once,
from ``cfg.schedule.timezone``, and passed down. Everything below is a pure
function of (config, bars, journal, asof).

**Every consumed module is imported lazily**, inside :func:`_load_deps`, so this
package stays importable — and testable — while its siblings are being built.

Fetch window: bars are requested from ``max(cfg.data.start_date, asof - 600
calendar days)``. 600 calendar days is roughly 413 trading days, which covers
the 252-day rolling windows in the trend template, the 200-day regime SMA plus
its 21-day rising check, and the 260-bar minimum ``rank_candidates`` insists on,
with room to spare for holidays and halts.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.alerts import channels, render
from swing.alerts import orders as orders_mod
from swing.reports import SCAN_DIR_PREFIX, latest_scan_dir, prune_scan_dirs
from swing.state import Journal, PickRecord, atomic_write_text, file_lock

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "CONFIRMABLE_STATUSES",
    "CONFIRM_DRIFT_ATR_MULT",
    "DEDUPE_WITHIN_DAYS",
    "SCAN_DIR_PREFIX",
    "SCAN_LOOKBACK_DAYS",
    "TERMINAL_PICK_STATUSES",
    "ScanError",
    "latest_scan_dir",
    "run_confirm",
    "run_scan",
]

log = logging.getLogger(__name__)

#: Calendar days of history requested for every symbol (~413 trading days).
SCAN_LOOKBACK_DAYS = 600

#: A symbol picked within this many calendar days is skipped. Frozen at 7.
DEDUPE_WITHIN_DAYS = 7

#: The default invalidation threshold: a pick dies when the morning price is
#: more than this many ATRs above the planned entry — the move happened without
#: us. The live value is ``execution.max_quote_drift_atr``, which the executor
#: has always used; having the same rule in two places let a retune desynchronise
#: the confirm verdict from the execute verdict (audit DEBT-006). This constant
#: is now only the documented default of that setting.
CONFIRM_DRIFT_ATR_MULT = 1.0

#: Statuses a pick may still be confirmed from.
CONFIRMABLE_STATUSES = frozenset({"drafted", "confirmed"})

#: Journal statuses that end a pick's life. A confirm rerun must not touch one
#: of these: re-quoting an invalidated pick used to *resurrect* it the moment
#: the price came back (audit BUG-018).
TERMINAL_PICK_STATUSES = frozenset({"invalidated", "ordered", "filled", "closed"})


class ScanError(RuntimeError):
    """Raised when a scan or confirmation cannot run at all.

    The message is a complete plain-English sentence naming what is missing and
    what to do about it.
    """


# --------------------------------------------------------------------------
# lazy dependency seam
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """Everything this module consumes from its sibling packages.

    Bundled into one object with one loader so tests can replace the whole set
    at a single seam (``monkeypatch.setattr(pipeline, "_load_deps", ...)``)
    without importing — or waiting for — the real implementations.
    """

    get_provider: Callable[..., Any]
    rules: Any
    scoring: Any
    regime: Any
    sizing: Any
    indicators: Any


def _load_deps() -> _Deps:
    """Import the strategy and data layers, explaining any absence in English."""
    try:
        from swing import indicators
        from swing.data import get_provider
        from swing.strategy import regime, rules, scoring, sizing
    except ImportError as exc:
        raise ScanError(
            f"The scan cannot run because part of the system is not installed in this "
            f"checkout ({exc}). Reinstall with `make install`, or run `uv sync`."
        ) from exc
    return _Deps(
        get_provider=get_provider,
        rules=rules,
        scoring=scoring,
        regime=regime,
        sizing=sizing,
        indicators=indicators,
    )


def _gate_status(cfg: Config) -> dict[str, Any]:
    """Ask the backtest gate whether picks may be emitted at all.

    Fail-closed in every direction: a missing module, a broken gate and a failing
    gate are all reported the same way — ``passed`` is False and the reason says
    which it was.
    """
    try:
        from swing.backtest.gate import check
    except ImportError:
        return {
            "passed": False,
            "reasons": [
                "The backtest gate is not available in this checkout, so there is no evidence "
                "that this strategy works. Run `swing backtest` once it is installed."
            ],
        }
    try:
        result = check(cfg)
    except Exception as exc:  # noqa: BLE001 - any gate failure must fail closed
        log.warning("The backtest gate raised: %s", exc)
        return {
            "passed": False,
            "reasons": [
                f"The backtest gate could not be evaluated ({exc}), so no picks are emitted. "
                f"Run `swing backtest --walkforward` and try again."
            ],
        }
    return {
        "passed": bool(getattr(result, "passed", False)),
        "reasons": [str(reason) for reason in (getattr(result, "reasons", None) or [])],
    }


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _today(cfg: Config) -> date:
    """Today's date in the configured market timezone."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(cfg.schedule.timezone)).date()


def _now_iso(cfg: Config) -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(cfg.schedule.timezone)).isoformat(timespec="seconds")


def _row_position(index: Any, asof: pd.Timestamp) -> int:
    """Index position of the last bar at or before ``asof``; ``-1`` when there is none."""
    return int(pd.DatetimeIndex(index).searchsorted(asof, side="right")) - 1


def _value_at(series: Any, position: int) -> float:
    """Read one float out of a Series by position, tolerating anything odd."""
    try:
        return float(pd.Series(series).iloc[position])
    except (IndexError, KeyError, TypeError, ValueError):
        return float("nan")


def _flag_at(series: Any, position: int) -> bool:
    try:
        return bool(pd.Series(series).iloc[position])
    except (IndexError, KeyError, TypeError, ValueError):
        return False


def _finite(value: float) -> bool:
    return isinstance(value, float) and math.isfinite(value)


def _safe_call(func: Callable[..., Any], *args: Any, default: Any) -> Any:
    """Call a provider method, degrading to ``default`` instead of failing the scan."""
    try:
        result = func(*args)
    except Exception as exc:  # noqa: BLE001 - a vendor outage must not stop the report
        log.warning("Provider call %s failed: %s", getattr(func, "__name__", func), exc)
        return default
    return default if result is None else result


def _not_recently_picked(
    journal: Any, symbols: Sequence[str], *, within_days: int, asof: date
) -> list[str]:
    """Drop the symbols the journal says we already committed capital to.

    The journal owns the dedupe *rule* (``kinds=("pick",)`` and the ``0 < delta``
    boundary — audit BUG-010/BUG-011); this owns the *cost*. Asking the journal
    about every candidate was O(candidates x journal rows) and measurably slow
    on a journal that only ever grows (audit PERF-008), so the journal's picks
    are indexed by symbol once per scan and only the candidates that actually
    appear in it are asked.
    """
    if within_days <= 0:
        return list(symbols)
    journalled: set[str] = {pick.symbol.strip().upper() for pick in journal.picks}
    return [
        symbol
        for symbol in symbols
        if symbol.strip().upper() not in journalled
        or not journal.recently_picked(symbol, within_days, asof=asof)
    ]


def _write(path: Path, text: str) -> None:
    """Write one report file atomically (audit BUG-020).

    ``write_text`` truncates in place, so a reader — the 07:00 confirm, the
    executor — could see half a ``picks.json``. Every report file therefore
    goes through the same temp-file-and-rename helper the journal uses.
    """
    atomic_write_text(path, text)


def _report_dir(cfg: Config) -> Path:
    return Path(cfg.paths.reports_dir).expanduser()


def _clear_orders_dir(orders_dir: Path) -> None:
    """Empty ``orders/`` so it can never describe a report that no longer exists.

    A second scan on the same evening rewrites ``picks.json`` in place; without
    this, order drafts from the first run survived beside it and the directory
    disagreed with the report (audit BUG-010).
    """
    try:
        stale = sorted(orders_dir.glob("*.json"))
    except OSError:  # pragma: no cover - unreadable directory; the write reports it
        return
    for path in stale:
        try:
            path.unlink()
        except OSError as exc:  # pragma: no cover - permissions only
            log.warning("The stale order draft %s could not be removed (%s)", path, exc)


def _check_delivery(results: Mapping[str, bool], *, strict: bool, what: str) -> None:
    """Refuse a run whose every configured notification channel failed (audit BUG-021).

    An empty ``results`` means nothing is configured, which is a choice rather
    than a failure; the report is still on disk either way. Only a run where at
    least one channel was switched on and *all* of them failed is an error, and
    it is one worth an exit code: launchd showing "last exit 0" for a night the
    user never heard about is indistinguishable from success.
    """
    if not strict or not results or any(results.values()):
        return
    names = ", ".join(sorted(results))
    raise ScanError(
        f"The {what} finished and its report was written, but every notification channel "
        f"configured for it failed ({names}), so nothing reached you. Run `swing notify-test` "
        f"with --verbose to see why, then re-send by re-running this command."
    )


# --------------------------------------------------------------------------
# the scan
# --------------------------------------------------------------------------


def _fetch_start(cfg: Config, asof: date) -> date:
    """First bar date to request — see the module docstring for the arithmetic."""
    return max(cfg.data.start_date, asof - timedelta(days=SCAN_LOOKBACK_DAYS))


def _thesis(
    deps: _Deps,
    cfg: Config,
    bars: pd.DataFrame,
    position: int,
    *,
    rank: int,
    total: int,
    entry: float,
    stop: float,
) -> str:
    """One honest sentence explaining why this name is on the sheet."""
    window = cfg.strategy.donchian_window
    close = _value_at(bars["close"], position)
    trigger = f"Entry signal fired at the {window}d level"
    try:
        breakout = _value_at(deps.indicators.donchian_high(bars, window), position)
        if _finite(breakout) and _finite(close):
            if close >= breakout:
                trigger = f"Broke the {window}d high"
            elif close >= breakout * (1.0 - cfg.strategy.breakout_proximity_pct / 100.0):
                trigger = f"Within {cfg.strategy.breakout_proximity_pct:g}% of the {window}d high"
            else:
                trigger = f"Pullback entry above the {cfg.strategy.sma_fast}d average"
    except Exception as exc:  # noqa: BLE001 - the thesis is prose, never a blocker
        log.debug("Could not classify the entry trigger: %s", exc)

    volume_clause = ""
    try:
        average = (
            bars["volume"]
            .rolling(cfg.strategy.volume_avg_window, min_periods=cfg.strategy.volume_avg_window)
            .mean()
        )
        ratio = _value_at(bars["volume"], position) / _value_at(average, position)
        if _finite(ratio) and ratio > 0:
            volume_clause = f" on {ratio:.1f}x average volume"
    except Exception as exc:  # noqa: BLE001 - same
        log.debug("Could not compute the volume ratio: %s", exc)

    return (
        f"{trigger}{volume_clause}; trend template intact; momentum rank {rank}/{total}; "
        f"risk ${entry - stop:.2f}/share."
    )


def _regime_bars_missing(bars: Any) -> bool:
    """True when the regime symbol came back with no history at all."""
    return bars is None or len(bars) == 0


def _regime_missing_note(cfg: Config) -> str:
    """The note for "we do not know the regime", which is not "the regime is off".

    A data outage used to be reported as the confident market statement "SPY is
    not above its 200-day average" (audit BUG-015). The two need different
    words because they need different reactions: one is a bear market, the other
    is a broken feed.
    """
    symbol = cfg.regime.symbol.strip().upper()
    return (
        f"No price history came back for {symbol}, the market regime symbol, so this scan "
        f"could not tell whether the regime is ON or OFF and proposed no new entries. This is "
        f"a DATA problem, not a market signal: check the data provider and the cache, then "
        f"re-run `swing scan`."
    )


def _regime_ok(deps: _Deps, cfg: Config, spy_bars: Any, asof: pd.Timestamp) -> bool:
    """Whether the market regime allows new entries as of ``asof``."""
    if _regime_bars_missing(spy_bars):
        return False
    try:
        allowed = deps.regime.entries_allowed(spy_bars, cfg)
    except Exception as exc:  # noqa: BLE001 - no regime answer means no entries
        log.warning("The regime filter failed: %s", exc)
        return False
    position = _row_position(allowed.index, asof)
    return False if position < 0 else _flag_at(allowed, position)


def _regime_only(deps: _Deps, cfg: Config, provider: Any, asof: date) -> tuple[bool, list[str]]:
    """Fetch just the regime symbol — used when picks are blocked anyway.

    Returns ``(regime_ok, notes)``; the notes carry the missing-data case so the
    report never presents an outage as a market reading (audit BUG-015).
    """
    symbol = cfg.regime.symbol.strip().upper()
    bars = _safe_call(provider.daily_bars, [symbol], _fetch_start(cfg, asof), asof, default={}).get(
        symbol
    )
    if _regime_bars_missing(bars):
        log.warning("No history came back for the regime symbol %s", symbol)
        return False, [_regime_missing_note(cfg)]
    return _regime_ok(deps, cfg, bars, pd.Timestamp(asof)), []


def _technical_survivors(
    deps: _Deps,
    cfg: Config,
    bars_by_symbol: dict[str, pd.DataFrame],
    kinds: dict[str, str],
    symbols: Sequence[str],
    asof: pd.Timestamp,
) -> tuple[list[str], dict[str, int]]:
    """Symbols passing liquidity, trend template and the entry signal at ``asof``."""
    survivors: list[str] = []
    positions: dict[str, int] = {}
    for symbol in symbols:
        bars = bars_by_symbol.get(symbol)
        if bars is None or len(bars) == 0:
            continue
        position = _row_position(bars.index, asof)
        if position < 0:
            continue
        is_etf = kinds.get(symbol) == "etf"
        try:
            passed = (
                _flag_at(deps.rules.liquidity_ok(bars, cfg, is_etf=is_etf), position)
                and _flag_at(deps.rules.trend_template(bars, cfg, is_etf=is_etf), position)
                and _flag_at(deps.rules.entry_signal(bars, cfg), position)
            )
        except Exception as exc:  # noqa: BLE001 - one bad frame must not end the scan
            log.warning("Skipping %s: the rules could not be evaluated (%s)", symbol, exc)
            continue
        if passed:
            survivors.append(symbol)
            positions[symbol] = position
    return survivors, positions


def _drop_earnings_blackout(
    deps: _Deps,
    cfg: Config,
    bars_by_symbol: dict[str, pd.DataFrame],
    positions: dict[str, int],
    survivors: Sequence[str],
    earnings: dict[str, Any],
) -> list[str]:
    """Remove names inside their earnings blackout window."""
    kept: list[str] = []
    for symbol in survivors:
        try:
            blocked = _flag_at(
                deps.rules.earnings_blackout(
                    bars_by_symbol[symbol].index, earnings.get(symbol), cfg
                ),
                positions[symbol],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping the earnings check for %s (%s)", symbol, exc)
            blocked = False
        if not blocked:
            kept.append(symbol)
    return kept


def _apply_fundamentals(
    deps: _Deps,
    cfg: Config,
    ranking: pd.DataFrame,
    kinds: dict[str, str],
    provider: Any,
) -> list[str]:
    """Filter ranked names through the one-sided fundamentals screen (stocks only)."""
    ranked = [str(symbol) for symbol in ranking.index]
    if not ranked:
        return []
    stocks = [symbol for symbol in ranked if kinds.get(symbol) != "etf"]
    fundamentals = _safe_call(provider.fundamentals, stocks, default={}) if stocks else {}
    median_rank = float(ranking["rank"].median())

    eligible: list[str] = []
    for symbol in ranked:
        if kinds.get(symbol) == "etf":
            eligible.append(symbol)
            continue
        below_median = float(ranking.loc[symbol, "rank"]) > median_rank
        try:
            ok = bool(deps.rules.fundamentals_ok(fundamentals.get(symbol), below_median, cfg))
        except Exception as exc:  # noqa: BLE001 - patchy data must never reject a name
            log.warning(
                "The fundamentals screen failed for %s (%s); treating it as ok", symbol, exc
            )
            ok = True
        if ok:
            eligible.append(symbol)
    return eligible


#: ``SizeResult.capped_by`` values that mean "there is not enough money", which
#: the watch list and its one summary note already explain. Anything else is
#: reported per symbol, including values this module has never heard of — the
#: sizing module is free to add reasons (``"risk_floor"`` arrived with contract
#: A6) and a scanner that silently swallowed them would hide the answer to
#: "why is this name on the watch list?" (audit BUG-030).
_AFFORDABILITY_CAPS = frozenset({"cash", "unaffordable", "position_cap"})

#: How a non-affordability cap is put into words.
_CAP_EXPLANATIONS: dict[str, str] = {
    "risk_floor": (
        "its stop is too close to the entry to size a position against, so the risk-per-share "
        "floor refused it"
    ),
}


def _cap_note(symbol: str, capped_by: Any) -> str | None:
    """Explain a zero-share result whose cause is not simply "not enough money"."""
    if capped_by is None:
        return None
    reason = str(capped_by).strip().lower()
    if not reason or reason in _AFFORDABILITY_CAPS:
        return None
    explanation = _CAP_EXPLANATIONS.get(reason)
    if explanation is None:
        return (
            f"{symbol} sized to zero shares and the sizing module gave {reason!r} as the reason. "
            f"It is on the watch list."
        )
    return f"{symbol} sized to zero shares because {explanation}. It is on the watch list."


def _size_candidates(
    deps: _Deps,
    cfg: Config,
    provider: Any,
    bars_by_symbol: dict[str, pd.DataFrame],
    positions: dict[str, int],
    ranking: pd.DataFrame,
    chosen: Sequence[str],
    earnings: dict[str, Any],
    *,
    asof: date,
    cash: float,
) -> tuple[list[PickRecord], list[PickRecord], list[str]]:
    """Turn chosen candidates into pick and watch records."""
    picks: list[PickRecord] = []
    watch: list[PickRecord] = []
    notes: list[str] = []
    equity = float(cfg.account.equity)
    total_ranked = len(ranking)
    available = cash

    for symbol in chosen:
        bars = bars_by_symbol[symbol]
        position = positions[symbol]
        entry = round(_value_at(bars["close"], position), 2)
        try:
            stop = round(_value_at(deps.rules.initial_stop(bars, cfg), position), 2)
            # PERF-009: one ATR series per sized candidate, read once. `chosen`
            # is at most `max_positions` names, so this loop is not the hot one
            # — the per-universe duplication PERF-009 also names lives in
            # `scoring.rank_candidates`.
            atr_series = deps.indicators.atr(bars, cfg.strategy.atr_window)
            atr_value = _value_at(atr_series, position)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{symbol} was dropped: its stop could not be computed ({exc}).")
            continue

        # Everything sizing needs must be a real, positive, ordered pair of
        # prices *before* the strategy sees it: `size_position` is the one
        # strategy call the scanner used to make unguarded (audit BUG-030).
        if not (_finite(entry) and _finite(stop)):
            notes.append(
                f"{symbol} was dropped: its entry or stop could not be computed as a number "
                f"from the price history."
            )
            continue
        if entry <= 0 or stop < 0:
            notes.append(
                f"{symbol} was dropped: the entry (${entry:.2f}) and stop (${stop:.2f}) are not "
                f"a sane pair of prices — an entry has to be positive and a stop cannot be "
                f"below zero."
            )
            continue
        if entry - stop <= 0:
            notes.append(
                f"{symbol} was dropped: the stop (${stop:.2f}) is not below the entry "
                f"(${entry:.2f}), so the risk per share is not a positive number."
            )
            continue

        try:
            size = deps.sizing.size_position(
                equity=equity, cash=available, entry=entry, stop=stop, cfg=cfg
            )
        except Exception as exc:  # noqa: BLE001 - one refusal must not end the scan
            notes.append(f"{symbol} was dropped: the position size could not be computed ({exc}).")
            continue

        try:
            shares = int(size.shares)
        except (TypeError, ValueError) as exc:
            notes.append(
                f"{symbol} was dropped: the sizing module returned {size.shares!r} shares ({exc})."
            )
            continue
        rank = int(ranking.loc[symbol, "rank"])
        record = PickRecord(
            symbol=symbol,
            date=asof.isoformat(),
            kind="pick" if shares >= 1 else "watch",
            entry=entry,
            stop=stop,
            shares=shares,
            risk_amount=round(float(size.risk_amount), 2),
            score=round(float(ranking.loc[symbol, "score"]), 4),
            atr=round(atr_value, 4) if _finite(atr_value) else 0.0,
            earnings_date=(
                earnings[symbol].isoformat() if isinstance(earnings.get(symbol), date) else None
            ),
            earnings_known=isinstance(earnings.get(symbol), date),
            thesis=_thesis(
                deps, cfg, bars, position, rank=rank, total=total_ranked, entry=entry, stop=stop
            ),
            status="drafted",
        )
        if shares >= 1:
            picks.append(record)
            available = max(0.0, available - float(size.notional))
        else:
            watch.append(record)
            explanation = _cap_note(symbol, getattr(size, "capped_by", None))
            if explanation is not None:
                notes.append(explanation)
    return picks, watch, notes


def _scan(
    deps: _Deps,
    cfg: Config,
    provider: Any,
    journal: Any,
    asof: date,
) -> tuple[bool, list[PickRecord], list[PickRecord], list[str]]:
    """The full candidate pipeline. Returns ``(regime_ok, picks, watch, notes)``."""
    from swing import universe as universe_mod

    notes: list[str] = []
    asof_ts = pd.Timestamp(asof)

    instruments = universe_mod.load(cfg)
    kinds = {inst.symbol: inst.kind for inst in instruments}
    symbols = sorted(kinds)
    regime_symbol = cfg.regime.symbol.strip().upper()

    wanted = sorted({*symbols, regime_symbol})
    bars_by_symbol = _safe_call(
        provider.daily_bars, wanted, _fetch_start(cfg, asof), asof, default={}
    )
    if not bars_by_symbol:
        notes.append(
            "No price history came back from the data provider, so nothing could be evaluated. "
            "Check the network and the cache directory."
        )
        return False, [], [], notes

    regime_bars = bars_by_symbol.get(regime_symbol)
    if _regime_bars_missing(regime_bars):
        log.warning("No history came back for the regime symbol %s", regime_symbol)
        notes.append(_regime_missing_note(cfg))
        return False, [], [], notes

    regime_ok = _regime_ok(deps, cfg, regime_bars, asof_ts)
    if not regime_ok:
        notes.append(
            f"The market regime gate is OFF ({regime_symbol} is not above its "
            f"{cfg.regime.sma_window}-day average), so no new entries are proposed today. "
            f"Positions you already hold are unaffected — they are managed by their stops."
        )
        return regime_ok, [], [], notes

    survivors, positions = _technical_survivors(deps, cfg, bars_by_symbol, kinds, symbols, asof_ts)
    if not survivors:
        notes.append(
            f"None of the {len(symbols)} symbols scanned passed liquidity, the trend template "
            f"and the entry signal on {asof.isoformat()}."
        )
        return regime_ok, [], [], notes

    earnings = _safe_call(provider.earnings_dates, survivors, default={})
    survivors = _drop_earnings_blackout(deps, cfg, bars_by_symbol, positions, survivors, earnings)
    if not survivors:
        notes.append(
            "Every candidate is inside its earnings blackout window, so none may be entered."
        )
        return regime_ok, [], [], notes

    ranking = deps.scoring.rank_candidates(
        {symbol: bars_by_symbol[symbol] for symbol in survivors}, asof_ts, cfg
    )
    if len(ranking) == 0:
        notes.append(
            "No candidate had enough price history to be scored, so nothing could be ranked."
        )
        return regime_ok, [], [], notes

    eligible = _apply_fundamentals(deps, cfg, ranking, kinds, provider)
    eligible = _not_recently_picked(journal, eligible, within_days=DEDUPE_WITHIN_DAYS, asof=asof)
    if not eligible:
        notes.append(
            f"Every ranked candidate was either rejected by the fundamentals screen or already "
            f"picked within the last {DEDUPE_WITHIN_DAYS} days."
        )
        return regime_ok, [], [], notes

    open_positions = list(journal.positions())
    slots = int(cfg.account.max_positions) - len(open_positions)
    if slots <= 0:
        notes.append(
            f"All {cfg.account.max_positions} position slots are full "
            f"({len(open_positions)} open), so no new ideas are proposed. "
            f"{len(eligible)} candidates passed every rule."
        )
        return regime_ok, [], [], notes

    open_notional = sum(
        float(position.get("shares", 0) or 0) * float(position.get("entry", 0.0) or 0.0)
        for position in open_positions
    )
    cash = max(0.0, float(cfg.account.equity) - open_notional)

    picks, watch, sizing_notes = _size_candidates(
        deps,
        cfg,
        provider,
        bars_by_symbol,
        positions,
        ranking,
        eligible[:slots],
        earnings,
        asof=asof,
        cash=cash,
    )
    notes.extend(sizing_notes)
    if watch and not picks:
        notes.append(
            f"Every candidate sized to zero shares: a {render.money(cfg.account.equity)} account "
            f"cannot buy one share at the risk these stops require. They are on the watch list."
        )
    return regime_ok, picks, watch, notes


def run_scan(
    cfg: Config,
    *,
    dry_run: bool = False,
    force: bool = False,
    asof: date | None = None,
    strict_delivery: bool = False,
) -> Path:
    """Run the nightly scan and write ``<reports_dir>/scan-YYYY-MM-DD/``.

    Args:
        cfg: the loaded configuration.
        dry_run: build the report but write nothing to the journal and send no
            notifications. The report records that it was a dry run, and
            :func:`run_confirm` refuses to act on one (audit BUG-019).
        force: emit picks even though the backtest gate has not passed. The
            report says so, loudly, in every format.
        asof: pretend today is this date. Defaults to today in
            ``cfg.schedule.timezone``.
        strict_delivery: raise when every configured notification channel
            failed. The CLI passes True: for a system whose entire value is the
            notification, a night nobody heard about must not exit 0 (audit
            BUG-021). Having *no* channel configured is not a failure.

    Returns:
        The report directory, which always exists by the time this returns —
        even when there is nothing in it but an explanation.

    Raises:
        ScanError: when the scan genuinely cannot run — a missing data layer
            while picks are allowed, or an unwritable reports directory — or,
            under ``strict_delivery``, when the finished report reached nobody.
            The report directory is complete on disk either way.
    """
    asof_date = asof or _today(cfg)
    reports_dir = _report_dir(cfg)
    report_dir = reports_dir / f"{SCAN_DIR_PREFIX}{asof_date}"
    orders_dir = report_dir / "orders"
    try:
        orders_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScanError(
            f"The report directory {report_dir} could not be created ({exc}). Check "
            f"paths.reports_dir in your config.toml."
        ) from exc

    # Retention (contract A17 / audit LEAK-002): one directory per calendar day
    # accumulates forever otherwise, and every `latest_scan_dir` walks them all.
    prune_scan_dirs(reports_dir, asof=asof_date)

    # Audit BUG-010: last night's `orders/` must never outlive the report that
    # explains it. Re-running a scan rewrites `picks.json`, so the order drafts
    # beside it are rebuilt from scratch rather than merged with the old set.
    _clear_orders_dir(orders_dir)

    gate = _gate_status(cfg)
    picks_allowed = bool(gate["passed"]) or force
    notes: list[str] = []
    if not gate["passed"]:
        if force:
            notes.append(
                "The backtest gate has NOT passed, but --force was given, so picks below are "
                "UNVALIDATED. There is no evidence this strategy works right now."
            )
        else:
            notes.append(
                "The backtest gate has NOT passed, so no picks are emitted. Run "
                "`swing backtest --walkforward`, or re-run with --force to see the candidates "
                "anyway (they carry no evidence)."
            )

    deps: _Deps | None
    try:
        deps = _load_deps()
    except ScanError as exc:
        if picks_allowed:
            raise
        deps = None
        notes.append(str(exc))

    journal = Journal.load(cfg)
    regime_ok = False
    picks: list[PickRecord] = []
    watch: list[PickRecord] = []

    if deps is not None:
        provider = deps.get_provider(cfg)
        if picks_allowed:
            regime_ok, picks, watch, scan_notes = _scan(deps, cfg, provider, journal, asof_date)
            notes.extend(scan_notes)
        else:
            regime_ok, regime_notes = _regime_only(deps, cfg, provider, asof_date)
            notes.extend(regime_notes)

    if journal.recovered:
        notes.append(
            "The journal could not be read and was reset, so swing currently believes it holds "
            "no positions and has sent no orders. Check the broker before acting on anything "
            "below (audit BUG-024)."
        )

    report: dict[str, Any] = {
        "generated_at": _now_iso(cfg),
        "asof": asof_date.isoformat(),
        "equity": float(cfg.account.equity),
        "regime_ok": bool(regime_ok),
        "dry_run": bool(dry_run),
        "gate": gate,
        "picks": [pick.to_dict() for pick in picks],
        "watch": [entry.to_dict() for entry in watch],
    }

    drafts: dict[str, Any] = {}
    for pick in picks:
        try:
            draft = orders_mod.draft_orders(pick, cfg)
        except orders_mod.OrderDraftError as exc:
            notes.append(f"No order could be drafted for {pick.symbol}: {exc}")
            continue
        # DEBT-002: the validator existed but only tests ever ran it, so the one
        # artefact a human might hand a broker was never actually checked.
        problems = orders_mod.validate_order_draft(draft)
        if problems:
            notes.append(
                f"The drafted order for {pick.symbol} did not pass its own structural check and "
                f"was NOT written: {problems[0]}"
            )
            log.warning("Order draft for %s rejected: %s", pick.symbol, "; ".join(problems))
            continue
        drafts[pick.symbol] = draft
        _write(orders_dir / f"{pick.symbol}.json", json.dumps(draft, indent=2) + "\n")

    # picks.json is written LAST, deliberately: `swing.reports.latest_scan_dir`
    # treats it as this directory's commit point, so a crash mid-report leaves a
    # directory that is ignored rather than a half-built one that is trusted
    # (audit BUG-020).
    _write(report_dir / "picks.md", render.render_markdown(report, notes=notes))
    _write(
        report_dir / "picks.html",
        render.render_html(report, notes=notes, orders=drafts),
    )
    _write(report_dir / "picks.json", json.dumps(report, indent=2) + "\n")

    if dry_run:
        log.info("Dry run: journal untouched and no notifications sent (%s)", report_dir)
        return report_dir

    if picks or watch:
        # Journal first, notify second, and that order is deliberate (contract
        # A15, audit BUG-055): the journal is safety state — the duplicate
        # guardrail and the position count read it — so a crash between these
        # two lines must leave the record of what we decided, not lose it. The
        # accepted residual is that such a crash suppresses those symbols for
        # the dedupe window without having told anyone; `strict_delivery` below
        # is the mitigation, because a delivery that fails now fails loudly.
        journal.add_picks([*picks, *watch])
    delivery = channels.deliver_scan(cfg, report, notes=notes, orders=drafts)
    _check_delivery(delivery, strict=strict_delivery, what=f"scan for {asof_date.isoformat()}")
    return report_dir


# --------------------------------------------------------------------------
# the morning confirmation
# --------------------------------------------------------------------------


def _drift_multiple(cfg: Config) -> float:
    """How many ATRs of adverse drift invalidate a pick.

    Read from ``execution.max_quote_drift_atr`` so the morning confirm and the
    executor's own quote-drift guardrail can never disagree about the same pick
    (audit DEBT-006); :data:`CONFIRM_DRIFT_ATR_MULT` is that setting's
    documented default and nothing else.
    """
    try:
        value = float(cfg.execution.max_quote_drift_atr)
    except (AttributeError, TypeError, ValueError):  # pragma: no cover - config validates it
        return CONFIRM_DRIFT_ATR_MULT
    return value if math.isfinite(value) and value > 0 else CONFIRM_DRIFT_ATR_MULT


def _confirm_result(pick: PickRecord, quote: Any, *, mult: float) -> dict[str, Any]:
    """Decide whether one pick survives this morning's price."""
    if quote is None:
        return {
            "quote": None,
            "status": "unknown",
            "reason": (
                "No quote came back for this symbol, so it was left as it was. Check it by hand "
                "before entering."
            ),
        }
    price = float(getattr(quote, "price", quote))
    ceiling = pick.entry + mult * pick.atr
    if price > ceiling:
        return {
            "quote": price,
            "status": "invalidated",
            "reason": (
                f"${price:.2f} is more than {mult:g} ATR (${pick.atr:.2f}) "
                f"above the ${pick.entry:.2f} entry, past the ${ceiling:.2f} limit. The move "
                f"happened without us; chasing it is a different, worse trade."
            ),
        }
    return {
        "quote": price,
        "status": "confirmed",
        "reason": (
            f"${price:.2f} is still within {mult:g} ATR of the ${pick.entry:.2f} "
            f"entry (limit ${ceiling:.2f}), so the plan stands."
        ),
    }


def _journal_statuses(journal: Any) -> dict[tuple[str, str], str]:
    """``{(symbol, date): status}`` for every pick the journal holds."""
    return {
        (pick.symbol.strip().upper(), pick.date): pick.status.strip().lower()
        for pick in journal.picks
    }


def _rewrite_pick_statuses(picks_file: Path, verdicts: Mapping[str, str]) -> int:
    """Write the confirm verdicts back into ``picks.json`` (contract A7 / audit BUG-018).

    ``picks.json`` used to be written once, always ``"drafted"``, so every
    downstream reader — the executor's skip-invalidated branch, tomorrow's
    confirm, the human reading the sheet — trusted a file that the confirm had
    already contradicted in the journal. Re-quoting an invalidated pick could
    then *resurrect* it.

    The file is re-read under the same lock it is written with, so a concurrent
    scan cannot lose the statuses (and this rewrite cannot lose the scan).

    Returns:
        How many pick records changed status.
    """
    if not verdicts:
        return 0
    wanted = {symbol.strip().upper(): status for symbol, status in verdicts.items()}
    with file_lock(picks_file):
        try:
            payload = json.loads(picks_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                "The confirm verdicts could not be written back into %s (%s); the journal is "
                "still authoritative.",
                picks_file,
                exc,
            )
            return 0
        if not isinstance(payload, dict) or not isinstance(payload.get("picks"), list):
            return 0
        changed = 0
        for raw in payload["picks"]:
            if not isinstance(raw, dict):
                continue
            status = wanted.get(str(raw.get("symbol", "")).strip().upper())
            if status is not None and raw.get("status") != status:
                raw["status"] = status
                changed += 1
        if changed:
            atomic_write_text(picks_file, json.dumps(payload, indent=2) + "\n")
    return changed


def run_confirm(cfg: Config, *, dry_run: bool = False, strict_delivery: bool = False) -> Path:
    """Re-check the most recent scan's picks against this morning's prices.

    The verdicts land in three places, and A7 makes them agree: the journal (the
    safety state), ``picks.json`` itself (what every downstream reader trusts)
    and ``confirm.json`` (what the human is shown). A pick the journal has
    already finished with — invalidated, ordered, filled or closed — is left
    alone, so re-running the confirm can never bring one back to life.

    Args:
        cfg: the loaded configuration.
        dry_run: re-check and write ``confirm.json``, but change no statuses
            anywhere and send no notifications.
        strict_delivery: raise when every configured notification channel
            failed (audit BUG-021). The CLI passes True.

    Returns:
        The path of the ``confirm.json`` written inside the scan directory.

    Raises:
        ScanError: when there is no scan to confirm, when its ``picks.json``
            cannot be read, when the latest scan was a dry run (audit BUG-019),
            or — under ``strict_delivery`` — when the result reached nobody.
    """
    scan_dir = latest_scan_dir(_report_dir(cfg))
    if scan_dir is None:
        raise ScanError(
            f"There is no scan to confirm: no scan-YYYY-MM-DD folder with a readable picks.json "
            f"was found in {_report_dir(cfg)}. Run `swing scan` first."
        )

    picks_file = scan_dir / "picks.json"
    try:
        payload = json.loads(picks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanError(
            f"The scan report at {picks_file} could not be read ({exc}). Re-run `swing scan`."
        ) from exc

    if not isinstance(payload, dict):
        raise ScanError(
            f"The scan report at {picks_file} is not a scan report — its top level is a "
            f"{type(payload).__name__} rather than an object. Re-run `swing scan`."
        )

    if bool(payload.get("dry_run")):
        raise ScanError(
            f"The most recent scan report ({scan_dir.name}) came from a dry run, so its picks "
            f"were never journalled and confirming them would notify you about trades this "
            f"system never proposed. Run `swing scan` without --dry-run, then confirm."
        )

    raw_picks = payload.get("picks")
    picks = [
        PickRecord.from_dict(raw)
        for raw in (raw_picks if isinstance(raw_picks, list) else [])
        if isinstance(raw, dict)
    ]
    journal = Journal.load(cfg)
    journal_status = _journal_statuses(journal)

    candidates: list[PickRecord] = []
    skipped: dict[str, str] = {}
    for pick in sorted(picks, key=lambda p: p.symbol):
        if pick.status not in CONFIRMABLE_STATUSES:
            continue
        settled = journal_status.get((pick.symbol.strip().upper(), pick.date))
        if settled in TERMINAL_PICK_STATUSES:
            # Audit BUG-018: a rerun must not resurrect a pick the journal has
            # already finished with, however friendly this morning's price is.
            skipped[pick.symbol] = (
                f"The journal already records this pick as {settled}, so it was not re-quoted."
            )
            continue
        candidates.append(pick)

    mult = _drift_multiple(cfg)
    results: dict[str, Any] = {}
    verdicts: dict[str, tuple[str, str]] = {}  # symbol -> (pick date, new status)
    if candidates:
        deps = _load_deps()
        provider = deps.get_provider(cfg)
        quotes = _safe_call(
            provider.latest_quotes, [pick.symbol for pick in candidates], default={}
        )
        for pick in candidates:
            result = _confirm_result(pick, quotes.get(pick.symbol), mult=mult)
            results[pick.symbol] = result
            if dry_run or result["status"] == "unknown":
                continue
            verdicts[pick.symbol] = (pick.date, str(result["status"]))

    if verdicts:
        changes: list[tuple[str, str, str]] = []
        for symbol, (day, status) in sorted(verdicts.items()):
            if (symbol.strip().upper(), day) in journal_status:
                changes.append((symbol, day, status))
            else:
                # Audit BUG-019: a verdict the journal cannot record used to be
                # a swallowed KeyError in a log file nobody reads.
                skipped[symbol] = (
                    "This pick is in the report but not in the journal, so only the report was "
                    "updated. The scan that produced it never journalled it."
                )
        if changes:
            journal.update_statuses(changes)
        _rewrite_pick_statuses(picks_file, {s: status for s, (_d, status) in verdicts.items()})

    confirm: dict[str, Any] = {
        "asof": _today(cfg).isoformat(),
        "results": results,
        "skipped": dict(sorted(skipped.items())),
    }
    confirm_path = scan_dir / "confirm.json"
    _write(confirm_path, json.dumps(confirm, indent=2) + "\n")
    _write(scan_dir / "confirm.md", render.render_confirm_markdown(confirm))

    if not dry_run:
        delivery = channels.deliver_confirm(cfg, confirm)
        _check_delivery(delivery, strict=strict_delivery, what=f"confirmation of {scan_dir.name}")
    return confirm_path
