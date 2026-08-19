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

import inspect
import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.alerts import channels, render
from swing.alerts import orders as orders_mod
from swing.state import Journal, PickRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "CONFIRM_DRIFT_ATR_MULT",
    "DEDUPE_WITHIN_DAYS",
    "SCAN_LOOKBACK_DAYS",
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

#: A pick is invalidated when the morning price is more than this many ATRs
#: above the planned entry — the move happened without us.
CONFIRM_DRIFT_ATR_MULT = 1.0

#: Statuses a pick may still be confirmed from.
CONFIRMABLE_STATUSES = frozenset({"drafted", "confirmed"})

SCAN_DIR_PREFIX = "scan-"
_SCAN_DIR_RE = re.compile(r"^scan-\d{4}-\d{2}-\d{2}$")


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


def _recently_picked(journal: Any, symbol: str, within_days: int, asof: date) -> bool:
    """Ask the journal about a recent pick, passing ``asof`` when it accepts one."""
    try:
        accepts_asof = "asof" in inspect.signature(journal.recently_picked).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables only
        accepts_asof = False
    if accepts_asof:
        return bool(journal.recently_picked(symbol, within_days, asof=asof))
    return bool(journal.recently_picked(symbol, within_days))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def latest_scan_dir(cfg: Config) -> Path | None:
    """The most recent ``scan-YYYY-MM-DD`` directory holding a ``picks.json``."""
    reports = Path(cfg.paths.reports_dir).expanduser()
    if not reports.is_dir():
        return None
    candidates = [
        child
        for child in reports.iterdir()
        if child.is_dir() and _SCAN_DIR_RE.match(child.name) and (child / "picks.json").is_file()
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.name)[-1]


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


def _regime_ok(deps: _Deps, cfg: Config, spy_bars: Any, asof: pd.Timestamp) -> bool:
    """Whether the market regime allows new entries as of ``asof``."""
    if spy_bars is None or len(spy_bars) == 0:
        return False
    try:
        allowed = deps.regime.entries_allowed(spy_bars, cfg)
    except Exception as exc:  # noqa: BLE001 - no regime answer means no entries
        log.warning("The regime filter failed: %s", exc)
        return False
    position = _row_position(allowed.index, asof)
    return False if position < 0 else _flag_at(allowed, position)


def _regime_only(deps: _Deps, cfg: Config, provider: Any, asof: date) -> bool:
    """Fetch just the regime symbol — used when picks are blocked anyway."""
    symbol = cfg.regime.symbol.strip().upper()
    bars = _safe_call(provider.daily_bars, [symbol], _fetch_start(cfg, asof), asof, default={}).get(
        symbol
    )
    return _regime_ok(deps, cfg, bars, pd.Timestamp(asof))


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
            atr_value = _value_at(deps.indicators.atr(bars, cfg.strategy.atr_window), position)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{symbol} was dropped: its stop could not be computed ({exc}).")
            continue

        if not (_finite(entry) and _finite(stop)) or entry - stop <= 0:
            notes.append(
                f"{symbol} was dropped: the stop (${stop:.2f}) is not below the entry "
                f"(${entry:.2f}), so the risk per share is not a positive number."
            )
            continue

        size = deps.sizing.size_position(
            equity=equity, cash=available, entry=entry, stop=stop, cfg=cfg
        )
        shares = int(size.shares)
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

    regime_ok = _regime_ok(deps, cfg, bars_by_symbol.get(regime_symbol), asof_ts)
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
    eligible = [
        symbol
        for symbol in eligible
        if not _recently_picked(journal, symbol, DEDUPE_WITHIN_DAYS, asof)
    ]
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
) -> Path:
    """Run the nightly scan and write ``<reports_dir>/scan-YYYY-MM-DD/``.

    Args:
        cfg: the loaded configuration.
        dry_run: build the report but write nothing to the journal and send no
            notifications.
        force: emit picks even though the backtest gate has not passed. The
            report says so, loudly, in every format.
        asof: pretend today is this date. Defaults to today in
            ``cfg.schedule.timezone``.

    Returns:
        The report directory, which always exists by the time this returns —
        even when there is nothing in it but an explanation.

    Raises:
        ScanError: when the scan genuinely cannot run: a missing data layer
            while picks are allowed, or an unwritable reports directory.
    """
    asof_date = asof or _today(cfg)
    report_dir = Path(cfg.paths.reports_dir).expanduser() / f"{SCAN_DIR_PREFIX}{asof_date}"
    orders_dir = report_dir / "orders"
    try:
        orders_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScanError(
            f"The report directory {report_dir} could not be created ({exc}). Check "
            f"paths.reports_dir in your config.toml."
        ) from exc

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
            regime_ok = _regime_only(deps, cfg, provider, asof_date)

    report: dict[str, Any] = {
        "generated_at": _now_iso(cfg),
        "asof": asof_date.isoformat(),
        "equity": float(cfg.account.equity),
        "regime_ok": bool(regime_ok),
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
        drafts[pick.symbol] = draft
        _write(orders_dir / f"{pick.symbol}.json", json.dumps(draft, indent=2) + "\n")

    _write(report_dir / "picks.json", json.dumps(report, indent=2) + "\n")
    _write(report_dir / "picks.md", render.render_markdown(report, notes=notes))
    _write(
        report_dir / "picks.html",
        render.render_html(report, notes=notes, orders=drafts),
    )

    if dry_run:
        log.info("Dry run: journal untouched and no notifications sent (%s)", report_dir)
        return report_dir

    if picks or watch:
        journal.add_picks([*picks, *watch])
    channels.deliver_scan(cfg, report, notes=notes, orders=drafts)
    return report_dir


# --------------------------------------------------------------------------
# the morning confirmation
# --------------------------------------------------------------------------


def _confirm_result(pick: PickRecord, quote: Any) -> dict[str, Any]:
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
    ceiling = pick.entry + CONFIRM_DRIFT_ATR_MULT * pick.atr
    if price > ceiling:
        return {
            "quote": price,
            "status": "invalidated",
            "reason": (
                f"${price:.2f} is more than {CONFIRM_DRIFT_ATR_MULT:g} ATR (${pick.atr:.2f}) "
                f"above the ${pick.entry:.2f} entry, past the ${ceiling:.2f} limit. The move "
                f"happened without us; chasing it is a different, worse trade."
            ),
        }
    return {
        "quote": price,
        "status": "confirmed",
        "reason": (
            f"${price:.2f} is still within {CONFIRM_DRIFT_ATR_MULT:g} ATR of the ${pick.entry:.2f} "
            f"entry (limit ${ceiling:.2f}), so the plan stands."
        ),
    }


def run_confirm(cfg: Config, *, dry_run: bool = False) -> Path:
    """Re-check the most recent scan's picks against this morning's prices.

    Args:
        cfg: the loaded configuration.
        dry_run: re-check and write ``confirm.json``, but leave the journal
            alone and send no notifications.

    Returns:
        The path of the ``confirm.json`` written inside the scan directory.

    Raises:
        ScanError: when there is no scan to confirm, or its ``picks.json``
            cannot be read.
    """
    scan_dir = latest_scan_dir(cfg)
    if scan_dir is None:
        raise ScanError(
            f"There is no scan to confirm: no scan-YYYY-MM-DD folder with a picks.json was "
            f"found in {Path(cfg.paths.reports_dir).expanduser()}. Run `swing scan` first."
        )

    picks_file = scan_dir / "picks.json"
    try:
        payload = json.loads(picks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanError(
            f"The scan report at {picks_file} could not be read ({exc}). Re-run `swing scan`."
        ) from exc

    picks = [PickRecord.from_dict(raw) for raw in payload.get("picks", []) if isinstance(raw, dict)]
    candidates = sorted(
        (pick for pick in picks if pick.status in CONFIRMABLE_STATUSES),
        key=lambda pick: pick.symbol,
    )

    results: dict[str, Any] = {}
    if candidates:
        deps = _load_deps()
        provider = deps.get_provider(cfg)
        quotes = _safe_call(
            provider.latest_quotes, [pick.symbol for pick in candidates], default={}
        )
        journal = Journal.load(cfg)
        for pick in candidates:
            result = _confirm_result(pick, quotes.get(pick.symbol))
            results[pick.symbol] = result
            if dry_run or result["status"] == "unknown":
                continue
            try:
                journal.update_status(pick.symbol, date.fromisoformat(pick.date), result["status"])
            except (KeyError, ValueError) as exc:
                log.warning("Could not update the journal for %s: %s", pick.symbol, exc)

    confirm = {"asof": _today(cfg).isoformat(), "results": results}
    confirm_path = scan_dir / "confirm.json"
    _write(confirm_path, json.dumps(confirm, indent=2) + "\n")
    _write(scan_dir / "confirm.md", render.render_confirm_markdown(confirm))

    if not dry_run:
        channels.deliver_confirm(cfg, confirm)
    return confirm_path
