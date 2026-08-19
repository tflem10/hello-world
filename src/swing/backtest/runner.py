"""FROZEN CONTRACT 2 + 11 — ``swing backtest``: load data, simulate, write the report.

The runner is the only place in this package that touches the outside world. It
loads the universe, asks the data layer for bars, drives the walk-forward search
and the full-period reference run, and writes a directory that a human (or a
future you, or an auditor) can read without re-running anything::

    <reports_dir>/backtest/<label>/
        summary.json   every headline number, plus the provenance hashes
        report.md      the same thing, diffable
        report.html    the same thing, with charts
        trades.csv     every trade, one row each
        equity.csv     the daily equity curve
    <reports_dir>/backtest/latest.json   copy of the newest summary.json

DETERMINISM (Contract 11, AC9)
-------------------------------
``trades.csv``, ``equity.csv`` and ``summary.json`` contain no wall-clock value
of any kind, all floats are rounded to six decimal places on the way out, and
JSON keys are sorted. Re-running the same inputs produces byte-identical files.
The only timestamp anywhere is ``generated_at`` in ``report.html``, and the only
wall-clock-derived *name* is the default run label — pass ``label=`` explicitly
when you want two runs to be comparable byte for byte.

PROVENANCE
----------
Three hashes answer "what exactly produced this?":

* ``config_hash`` — SHA-256 over the strategy, backtest and gates settings. Two
  reports with the same hash were run with the same rules.
* ``code_ref``    — ``git rev-parse HEAD``, or ``"unknown"`` outside a checkout.
* ``data_hash``   — SHA-256 over each symbol's (last bar date, row count). Cheap
  to compute, and it changes the moment the underlying data does.

ABLATIONS
---------
Contract 11's amendment: a run whose label starts with ``ablate`` writes its own
directory but does **not** update ``latest.json``. An ablation deliberately
cripples the strategy to measure a component's contribution; letting one become
the gate's reference would be the most quietly destructive bug this system could
have.

LABELS ARE NAMES, NOT PATHS (audit BUG-042)
--------------------------------------------
``--label`` names one directory under ``<reports_dir>/backtest`` and nothing
else, so it is validated against :data:`LABEL_PATTERN`. A label containing a
separator used to relocate the run directory — leaving the real ``latest.json``
stale while the user believed they had refreshed it, and slipping an
``ablate``-prefixed run past the guard by burying the prefix in a subdirectory.
``latest.json`` is now always located by :func:`swing.backtest.gate.latest_path`,
never derived from the run directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.backtest.engine import run_engine
from swing.backtest.gate import ABLATION_PREFIX, latest_path
from swing.backtest.metrics import by_year_table, compute_metrics
from swing.backtest.walkforward import (
    OBJECTIVE_DESCRIPTION,
    run_walkforward,
    sensitivity_table,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "ABLATION_PREFIX",
    "FLOAT_PRECISION",
    "LABEL_PATTERN",
    "UNIVERSE_CHOICES",
    "config_hash",
    "data_hash",
    "run_backtest",
    "write_report",
]

log = logging.getLogger(__name__)

#: Decimal places every float is rounded to before being written.
FLOAT_PRECISION = 6

#: A run label is one directory name: letters, digits, dot, dash, underscore.
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

UNIVERSE_CHOICES: tuple[str, ...] = ("full", "etf", "stocks")


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Convert config values into something ``json.dumps`` accepts, stably."""
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in sorted(asdict(value).items())}
    return value


def config_hash(cfg: Config) -> str:
    """SHA-256 over the strategy, backtest and gates settings.

    Deliberately *not* the whole config: account size, alert channels and broker
    credentials do not change what the strategy would have done, and including
    them would make every report look different on a different machine.
    """
    payload = {
        section: {
            f.name: _plain(getattr(getattr(cfg, section), f.name))
            for f in fields(getattr(cfg, section))
        }
        for section in ("strategy", "backtest", "gates")
    }
    payload["account"] = {
        "max_positions": cfg.account.max_positions,
        "risk_pct": cfg.account.risk_pct,
        "max_position_pct": cfg.account.max_position_pct,
    }
    payload["regime"] = {
        "enabled": cfg.regime.enabled,
        "symbol": cfg.regime.symbol,
        "sma_window": cfg.regime.sma_window,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def data_hash(bars_by_symbol: dict[str, pd.DataFrame]) -> str:
    """SHA-256 over each symbol's ``(last bar date, row count)``.

    Cheap (no scan of the price columns) but sensitive to the things that
    actually matter: a symbol appearing, disappearing, or gaining bars.
    """
    parts: list[str] = []
    for symbol in sorted(bars_by_symbol):
        frame = bars_by_symbol[symbol]
        if frame is None or frame.empty:
            parts.append(f"{symbol}:empty:0")
            continue
        parts.append(f"{symbol}:{frame.index[-1].date().isoformat()}:{len(frame)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def code_ref() -> str:
    """``git rev-parse HEAD``, or ``"unknown"`` when git cannot answer."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git missing
        return "unknown"
    ref = result.stdout.strip()
    return ref if result.returncode == 0 and ref else "unknown"


# ---------------------------------------------------------------------------
# stable serialisation
# ---------------------------------------------------------------------------


def _round(value: Any) -> Any:
    """Round floats to :data:`FLOAT_PRECISION`, recursively, leaving the rest alone.

    Float formatting is the usual reason two "identical" runs differ byte for
    byte: the seventeenth decimal place of a sum depends on summation order,
    which depends on things no one intends to depend on. Six places is far more
    precision than any of these numbers deserve.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, FLOAT_PRECISION)
        # Normalise -0.0 to 0.0 so a sign that carries no information cannot
        # make two identical runs differ.
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_round(v) for v in value]
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON with sorted keys, rounded floats and a trailing newline."""
    text = json.dumps(_round(payload), indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _write_trades_csv(path: Path, trades: pd.DataFrame) -> None:
    """Write the trade list with ISO dates and fixed-width floats."""
    frame = trades.copy()
    for column in ("entry_date", "exit_date"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column]).dt.strftime("%Y-%m-%d")
    frame.to_csv(path, index=False, float_format=f"%.{FLOAT_PRECISION}f", lineterminator="\n")


def _write_equity_csv(path: Path, equity: pd.DataFrame) -> None:
    """Write the equity curve with an ISO ``date`` column and fixed-width floats."""
    frame = equity.copy()
    frame.index = pd.to_datetime(frame.index).strftime("%Y-%m-%d")
    frame.index.name = "date"
    frame.to_csv(path, float_format=f"%.{FLOAT_PRECISION}f", lineterminator="\n")


# ---------------------------------------------------------------------------
# universe + data
# ---------------------------------------------------------------------------


def _select_universe(cfg: Config, universe: str) -> list[Any]:
    """Load the universe and filter it to ``full`` / ``etf`` / ``stocks``."""
    from swing import universe as universe_module

    if universe not in UNIVERSE_CHOICES:
        raise ValueError(
            f"universe must be one of {', '.join(UNIVERSE_CHOICES)}, but it is {universe!r}."
        )
    instruments = universe_module.load(cfg)
    if universe == "etf":
        return [i for i in instruments if i.kind == "etf"]
    if universe == "stocks":
        return [i for i in instruments if i.kind != "etf"]
    return instruments


def _load_bars(
    cfg: Config,
    symbols: Sequence[str],
    start: date,
    end: date,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Fetch bars for the universe plus the regime symbol.

    Imported lazily so that importing this module never drags in yfinance, and
    so tests can monkeypatch ``swing.data.get_provider``.
    """
    from swing.data import get_provider

    provider = get_provider(cfg)
    regime_symbol = cfg.regime.symbol
    wanted = sorted({*symbols, regime_symbol})
    bars = provider.daily_bars(wanted, start, end)

    spy = bars.get(regime_symbol)
    if spy is None or spy.empty:
        log.warning(
            "No bars for the regime symbol %s: the market filter will block every entry. "
            "Check the data provider or set regime.enabled = false.",
            regime_symbol,
        )
        spy = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # The regime symbol is an input to the filter, not a tradable candidate,
    # unless it is also a member of the requested universe.
    wanted_set = set(symbols)
    tradable = {
        symbol: frame
        for symbol, frame in bars.items()
        if symbol in wanted_set and frame is not None and not frame.empty
    }
    return tradable, spy


def _load_earnings(
    cfg: Config, symbols: Sequence[str], start: date, end: date
) -> tuple[dict[str, Any], bool]:
    """Best-effort earnings dates; a provider failure degrades to "unknown".

    Returns ``(dates_by_symbol, simulated)``. ``simulated`` is True only when
    the provider could supply *historical* announcement dates (contract
    amendment A12). A provider that only knows the next upcoming date cannot
    block a single historical bar, so the blackout the live scanner applies is
    simply not present in the backtest — the summary carries the flag so that
    divergence is declared rather than assumed away (audit BUG-036).
    """
    from swing.data import get_provider

    try:
        provider = get_provider(cfg)
        history = getattr(provider, "earnings_history", None)
        if callable(history):
            return dict(history(list(symbols), start, end)), True
        return dict(provider.earnings_dates(list(symbols))), False
    except Exception as exc:  # noqa: BLE001 - earnings are optional, never fatal
        log.warning(
            "Could not load earnings dates (%s); the backtest will run without an earnings "
            "blackout, which slightly overstates results.",
            exc,
        )
        return {}, False


def _validate_label(label: str) -> str:
    """Return ``label`` if it names one directory, else refuse in plain English (BUG-042).

    ``.`` and ``..`` satisfy the character pattern but are not names — they are
    the current and parent directory, and ``..`` is exactly the escape the
    finding is about.
    """
    if not LABEL_PATTERN.match(label) or label in {".", ".."}:
        raise ValueError(
            f"The run label {label!r} is not usable as a directory name. A label may contain "
            f"only letters, digits, dots, dashes and underscores — no slashes, spaces or "
            f"'..' — because it names one directory inside reports/backtest and nothing else."
        )
    return label


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def write_report(
    cfg: Config,
    directory: Path,
    summary: dict[str, Any],
    trades: pd.DataFrame,
    equity: pd.DataFrame,
    *,
    update_latest: bool = True,
) -> Path:
    """Write the five report files (and optionally ``latest.json``).

    Args:
        cfg: used only to locate ``<reports_dir>/backtest``.
        directory: the run directory; created if needed.
        summary: the summary payload, written as ``summary.json``.
        trades: headline trades, written as ``trades.csv``.
        equity: headline equity curve, written as ``equity.csv``.
        update_latest: when False, ``latest.json`` is left alone (ablations).

    Returns:
        ``directory``.
    """
    from swing.backtest.report import render_html, render_markdown

    directory.mkdir(parents=True, exist_ok=True)
    _write_json(directory / "summary.json", summary)
    _write_trades_csv(directory / "trades.csv", trades)
    _write_equity_csv(directory / "equity.csv", equity)
    (directory / "report.md").write_text(render_markdown(summary), encoding="utf-8")
    (directory / "report.html").write_text(render_html(summary, equity, trades), encoding="utf-8")

    if update_latest:
        # BUG-042: the gate's file is wherever the gate looks for it, never
        # "one level up from wherever this run happened to land".
        reference = latest_path(cfg)
        reference.parent.mkdir(parents=True, exist_ok=True)
        _write_json(reference, summary)
    else:
        log.info(
            "Label %r starts with %r, so reports/backtest/latest.json was left untouched: "
            "ablation runs must never become the gate's reference.",
            summary.get("label"),
            ABLATION_PREFIX,
        )
    return directory


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def run_backtest(
    cfg: Config,
    *,
    universe: str = "full",
    start: date | None = None,
    end: date | None = None,
    walkforward: bool = True,
    label: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """FROZEN CONTRACT 2 — run a backtest and write its report directory.

    Args:
        cfg: the loaded configuration.
        universe: ``"full"``, ``"etf"`` or ``"stocks"``.
        start: first simulated date. Defaults to ``cfg.backtest.start``.
        end: last simulated date. Defaults to ``cfg.backtest.end``, else the
            last bar available.
        walkforward: run the walk-forward search (the default, and the only
            kind of run the gate will accept). ``False`` runs the configured
            parameters over the full period and marks the report ineligible.
        label: run directory name — one path component matching
            :data:`LABEL_PATTERN`. Defaults to ``backtest-YYYYMMDD-HHMMSS``.
            A label starting with ``ablate`` suppresses the ``latest.json``
            update.
        progress: optional sink for progress lines. Defaults to ``print``, so
            the CLI shows life during a long run; pass ``lambda _: None`` to
            silence it.

    Returns:
        The path of the run directory that was written.

    Raises:
        ValueError: on an unknown universe, an empty universe, no price
            history, or a label that is not a single usable directory name.
    """
    emit = print if progress is None else progress

    # Validated before any work happens: a bad label should cost a sentence,
    # not forty minutes of simulation (BUG-042). Only `None` means "no label
    # given" — an empty string is a mistake, and gets said so.
    default_label = f"backtest-{datetime.now():%Y%m%d-%H%M%S}"
    run_label = _validate_label(default_label if label is None else label)

    instruments = _select_universe(cfg, universe)
    symbols = sorted({instrument.symbol for instrument in instruments})
    if not symbols:
        raise ValueError(
            f"The {universe!r} universe is empty, so there is nothing to backtest. Check the "
            f"[universe] section of your config.toml."
        )
    is_etf = {instrument.symbol: instrument.kind == "etf" for instrument in instruments}

    run_start = start or cfg.backtest.start
    run_end = end or cfg.backtest.end or date.today()
    # Data always starts at data.start_date, whatever the simulation window is:
    # indicators need a year of warm-up before the first simulated bar, and a
    # walk-forward fold three years in still needs everything before it.
    emit(f"Loading bars for {len(symbols)} symbols ({universe} universe)...")
    bars, spy = _load_bars(cfg, symbols, cfg.data.start_date, run_end)
    if not bars:
        raise ValueError(
            "No price history was returned for any symbol, so the backtest cannot run. Check "
            "the data provider and the cache directory."
        )
    emit(f"Loaded {len(bars)} symbols with data.")
    earnings, earnings_simulated = _load_earnings(cfg, sorted(bars), cfg.data.start_date, run_end)

    last_bar = max(frame.index[-1].date() for frame in bars.values())
    effective_end = min(run_end, last_bar)

    # A backtest measures the STRATEGY, not the user's current bank balance, so
    # every simulation below runs on the reference capital in
    # ``backtest.initial_equity`` rather than on ``account.equity``. Sized on a
    # real $100 account, whole-share rounding would reject nearly every entry
    # and the report would say more about the account than about the rules —
    # and the same strategy would score differently for two different users.
    # ``account`` still drives sizing in the live scanner; only the backtest is
    # rebased.
    cfg_for_engine = replace(
        cfg, account=replace(cfg.account, equity=float(cfg.backtest.initial_equity))
    )

    summary: dict[str, Any] = {
        "label": run_label,
        "universe": universe,
        "start": run_start.isoformat(),
        "end": effective_end.isoformat(),
        "walkforward": bool(walkforward),
        "earnings_blackout_simulated": bool(earnings_simulated),
        "n_symbols": len(bars),
        "initial_equity": float(cfg.backtest.initial_equity),
        "config_hash": config_hash(cfg),
        "code_ref": code_ref(),
        "data_hash": data_hash(bars),
        "costs": {
            "slippage_bps": float(cfg.backtest.slippage_bps),
            "spread_atr_frac": float(cfg.backtest.spread_atr_frac),
        },
        "objective": OBJECTIVE_DESCRIPTION if walkforward else "",
    }

    # --- full period with the configured parameters (always run) -----------
    emit(f"Simulating the full period {run_start} to {effective_end} with config parameters...")
    full = run_engine(
        bars,
        spy,
        cfg_for_engine,
        earnings=earnings,
        is_etf=is_etf,
        start=run_start,
        end=effective_end,
    )
    full_metrics = compute_metrics(full.trades, full.equity, initial_equity=full.initial_equity)
    summary["full_period"] = full_metrics

    headline_trades = full.trades
    headline_equity = full.equity
    headline_initial = full.initial_equity

    if walkforward:
        emit("Running walk-forward folds (this is the slow part)...")
        wf = run_walkforward(
            bars,
            spy,
            cfg_for_engine,
            earnings=earnings,
            is_etf=is_etf,
            start=run_start,
            end=effective_end,
            progress=emit,
        )
        summary["oos"] = wf.metrics
        summary["windows"] = [fold.as_dict() for fold in wf.folds]
        if wf.folds:
            # BUG-043: the numbers cover the stitched OOS stretches, not the
            # data span. `start`/`end` stay in the summary as provenance, but
            # every headline reads these two.
            summary["oos_start"] = wf.folds[0].window.oos_start.isoformat()
            summary["oos_end"] = wf.folds[-1].window.oos_end.isoformat()
        headline_trades = wf.trades
        headline_equity = wf.equity
        headline_initial = wf.initial_equity
    else:
        # Contract 11: a non-walk-forward run still reports an ``oos`` block so
        # the schema is stable, but ``walkforward: false`` means the gate will
        # refuse it whatever the numbers say.
        summary["oos"] = full_metrics
        summary["windows"] = []

    summary["by_year"] = by_year_table(
        headline_trades, headline_equity, initial_equity=headline_initial
    )

    emit("Running the +/-25% sensitivity table...")
    summary["sensitivity"] = sensitivity_table(
        bars,
        spy,
        cfg_for_engine,
        earnings=earnings,
        is_etf=is_etf,
        start=run_start,
        end=effective_end,
    )

    directory = Path(cfg.paths.reports_dir) / "backtest" / run_label
    write_report(
        cfg,
        directory,
        summary,
        headline_trades,
        headline_equity,
        update_latest=not run_label.startswith(ABLATION_PREFIX),
    )
    emit(f"Wrote {directory}")
    return directory
