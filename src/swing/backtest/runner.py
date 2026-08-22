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

``summary.json`` additionally spells out ``tuning_grid``: the candidate values
the walk-forward was allowed to choose between, whether or not they came from
config. A hash tells you two reports differ; this tells you *how* the search
differed, which is the thing most likely to explain a flattering number.

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

INDEX MEMBERSHIP (``[backtest] membership``)
--------------------------------------------
The universe is *today's* S&P 500/400/600 membership, applied to every
historical day, so a run trades a company during the years before it joined the
index it is being backtested as a member of. That is look-ahead rather than
survivorship, and it flatters a momentum strategy specifically, because index
inclusion is itself an outcome of past growth.

Every run therefore publishes a ``membership`` block in ``summary.json`` saying
which universe it used and how large the untraded-but-claimed exposure is —
``member_years`` against ``member_years_point_in_time``. The default mode is
``off``, which is the behaviour every existing report was written under.

Setting the mode to ``point_in_time`` turns the measurement into a correction.
:func:`_eligibility_masks` turns each symbol's membership windows into a
per-bar boolean mask and hands the lot to every ``run_engine`` call as
``eligible=``; the engine ANDs it into the entry gate. It gates **entries
only** — a position already open when its company leaves an index is managed
out by the normal exit ladder, never force-closed — and both ends of a window
are inclusive. With the mode ``off`` the masks are not built and ``None`` is
passed, so the engine's entry gate is the expression it always was.
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

import numpy as np
import pandas as pd

from swing.backtest.engine import run_engine
from swing.backtest.gate import ABLATION_PREFIX, latest_path
from swing.backtest.metrics import by_year_table, compute_metrics
from swing.backtest.walkforward import (
    OBJECTIVE_DESCRIPTION,
    is_default_grid,
    resolve_grid,
    run_walkforward,
    sensitivity_table,
)
from swing.config import MEMBERSHIP_OFF, MEMBERSHIP_POINT_IN_TIME

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config
    from swing.universe import Instrument, Window

__all__ = [
    "ABLATION_PREFIX",
    "DAYS_PER_YEAR",
    "FLOAT_PRECISION",
    "LABEL_PATTERN",
    "UNIVERSE_CHOICES",
    "config_hash",
    "data_hash",
    "membership_block",
    "run_backtest",
    "write_report",
]

log = logging.getLogger(__name__)

#: Decimal places every float is rounded to before being written.
FLOAT_PRECISION = 6

#: A run label is one directory name: letters, digits, dot, dash, underscore.
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

UNIVERSE_CHOICES: tuple[str, ...] = ("full", "etf", "stocks")

#: Calendar days per year, for turning eligible-day counts into member-years.
#: Calendar days rather than trading days on purpose: ``docs/survivorship.md``
#: counts member-years the same way, and the two figures have to be comparable.
DAYS_PER_YEAR = 365.25


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
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in sorted(value.items())}
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in sorted(asdict(value).items())}
    return value


def config_hash(cfg: Config) -> str:
    """SHA-256 over the strategy, backtest and gates settings.

    Deliberately *not* the whole config: account size, alert channels and broker
    credentials do not change what the strategy would have done, and including
    them would make every report look different on a different machine.

    ``backtest.tuning_grid`` is included only when it differs from the default
    grid. What the hash answers is "were these two reports run under the same
    rules?", and a config that leaves the grid unset, a config that spells the
    default out, and every report written before the knob existed are all the
    same rules — so they keep the same hash. Any other grid changes it, because
    changing what the tuner may choose changes the experiment.
    """
    payload = {
        section: {
            f.name: _plain(getattr(getattr(cfg, section), f.name))
            for f in fields(getattr(cfg, section))
        }
        for section in ("strategy", "backtest", "gates")
    }
    if is_default_grid(cfg.backtest.tuning_grid):
        payload["backtest"].pop("tuning_grid", None)
    if cfg.backtest.membership == MEMBERSHIP_OFF:
        # Same argument as the grid above. A config that leaves the membership
        # knobs alone, a config that spells the defaults out, and every report
        # written before the knobs existed all describe the same experiment —
        # today's index membership applied to all of history — so they keep the
        # same hash. `membership_unknown` goes with it because it is inert
        # while the mode is off: it cannot change what the strategy did.
        # Turning the mode on DOES change the hash, and should: it is a
        # different universe, which is a different experiment.
        payload["backtest"].pop("membership", None)
        payload["backtest"].pop("membership_unknown", None)
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


# ---------------------------------------------------------------------------
# index membership
# ---------------------------------------------------------------------------


def _eligible_days(windows: tuple[Window, ...], start: date, end: date) -> int:
    """How many calendar days of ``[start, end]`` these windows cover.

    Windows arrive disjoint and sorted from
    :func:`swing.universe.membership_windows`, so overlaps cannot double-count.
    """
    total = 0
    for window_start, window_end in windows:
        first = start if window_start is None else max(start, window_start)
        last = end if window_end is None else min(end, window_end)
        if first <= last:
            total += (last - first).days + 1
    return total


def membership_block(
    cfg: Config,
    instruments: Sequence[Instrument],
    start: date,
    end: date,
) -> dict[str, Any]:
    """The ``summary.json`` block saying which universe this run actually traded.

    Every run writes one, including a default ``mode = "off"`` run, because the
    thing a reader needs is not "was a correction applied" but "how big is the
    thing that was not corrected". Two figures answer that:

    * ``member_years`` — the exposure the run really traded. With the mode off
      that is every gated symbol for the whole window, which is the look-ahead
      in full.
    * ``member_years_point_in_time`` — the exposure a membership file can
      actually vouch for, under this config's unknown-date policy. The gap
      between the two is the size of the bias, in the run's own units.

    ETFs and ``extra_symbols`` are not index constituents, so they are counted
    as ``symbols_ungated`` and appear in neither figure; an ETF-only run reports
    zero member-years, which is the correct answer rather than a missing one.

    An unreadable membership file does **not** stop the run — this block is
    provenance, not simulation input, and a diagnostic must never be the thing
    that kills forty minutes of work. It degrades to an ``error`` key instead,
    so the zeros beside it can never be mistaken for a measurement of no bias.
    """
    from swing import universe as universe_module

    mode = cfg.backtest.membership
    policy = cfg.backtest.membership_unknown
    try:
        coverage = universe_module.membership_coverage(cfg, instruments=instruments, unknown=policy)
        windows = universe_module.membership_windows(cfg, instruments=instruments, unknown=policy)
        # How many symbols the unknown-date policy is actually deciding for,
        # measured rather than inferred: the ones the two readings disagree
        # about. Reported whatever the mode, because it is the size of the
        # choice, and a reader of an `off` report should be able to see it.
        other = (
            universe_module.UNKNOWN_INCLUDE
            if policy == universe_module.UNKNOWN_EXCLUDE
            else universe_module.UNKNOWN_EXCLUDE
        )
        alternative = universe_module.membership_windows(
            cfg, instruments=instruments, unknown=other
        )
        policy_sensitive = sum(1 for s, w in windows.items() if alternative.get(s) != w)
    except universe_module.UniverseError as exc:
        log.warning(
            "Could not read the index membership files (%s); this run's summary cannot say how "
            "much of its universe was actually in an index. The simulation itself is unaffected.",
            exc,
        )
        return {
            "mode": mode,
            "applied": False,
            "unknown_policy": policy,
            "error": str(exc),
        }

    window_days = max((end - start).days + 1, 0)
    gated = [i for i in instruments if i.source in universe_module.MEMBERSHIP_SOURCES]
    nominal = coverage.gated * window_days / DAYS_PER_YEAR
    point_in_time = (
        sum(_eligible_days(windows.get(i.symbol, ()), start, end) for i in gated) / DAYS_PER_YEAR
    )
    applied = mode == MEMBERSHIP_POINT_IN_TIME

    return {
        "mode": mode,
        "applied": applied,
        "unknown_policy": policy,
        "symbols_gated": coverage.gated,
        "symbols_ungated": coverage.ungated,
        # What this run dropped. With the mode off nothing is dropped, and
        # saying so is the point: the count is not "how many could have been".
        "symbols_excluded": coverage.excluded if applied else 0,
        # The size of the unknown-date choice: symbols the two policies would
        # treat differently. Under "include" these are the ones being handed a
        # membership no source states, which is the quiet way the bias comes
        # back.
        "symbols_policy_sensitive": policy_sensitive,
        "symbols_unknown_join": coverage.unknown_join,
        "symbols_no_membership_row": coverage.no_membership_row,
        "join_date_coverage_pct": coverage.coverage_pct,
        "join_date_coverage": {
            source: {"members": members, "with_join_date": stated}
            for source, members, stated in coverage.by_source
        },
        "member_years": point_in_time if applied else nominal,
        "member_years_point_in_time": point_in_time,
        "member_years_nominal": nominal,
    }


def _window_mask(windows: tuple[Window, ...], index: pd.DatetimeIndex) -> np.ndarray:
    """Turn one symbol's eligible windows into a per-bar boolean mask.

    Both ends of a window are inclusive, and ``None`` means "open" at that end.
    ``()`` — no window at all — yields all-False, which is the whole point of
    the conservative unknown-date policy: a symbol no source can date is not
    quietly back-dated to the dawn of time.

    The index is made tz-naive before the comparison for the same reason
    :func:`swing.strategy.rules.earnings_blackout` does it (audit BUG-045):
    comparing a tz-aware index against a naive ``Timestamp`` raises, and a
    membership date is a calendar day, not an instant.
    """
    days = index.tz_localize(None) if index.tz is not None else index
    days = days.normalize()
    mask = np.zeros(len(index), dtype=bool)
    for window_start, window_end in windows:
        inside = np.ones(len(index), dtype=bool)
        if window_start is not None:
            inside &= days >= pd.Timestamp(window_start)
        if window_end is not None:
            inside &= days <= pd.Timestamp(window_end)
        mask |= inside
    return mask


def _eligible_windows(
    cfg: Config, instruments: Sequence[Instrument]
) -> dict[str, tuple[Window, ...]] | None:
    """Read the eligible windows an enforced run needs, or ``None`` when off.

    Called **before** the bars are loaded, deliberately. Unlike
    :func:`membership_block`, an unreadable membership file does stop an
    enforced run — there the block is provenance and degrading to an ``error``
    key is the kind thing to do, but here the file *is* simulation input, and a
    run labelled point-in-time that silently fell back to trading the full
    universe would be worse than no run at all. So the ``UniverseError``
    propagates — and it should cost a sentence rather than the forty minutes it
    would if this waited until after the data load.
    """
    if cfg.backtest.membership != MEMBERSHIP_POINT_IN_TIME:
        return None

    from swing import universe as universe_module

    return universe_module.membership_windows(
        cfg, instruments=instruments, unknown=cfg.backtest.membership_unknown
    )


def _eligibility_masks(
    cfg: Config,
    windows: dict[str, tuple[Window, ...]] | None,
    bars: dict[str, pd.DataFrame],
) -> dict[str, np.ndarray] | None:
    """The per-symbol entry gate ``run_engine`` needs, or ``None`` when off.

    ``None`` for every mode but ``point_in_time`` — not an all-True dict. The
    engine's default path has to stay literally the expression it always was,
    and handing it a dict of all-True masks would be a different code path
    reaching the same answer, which is a weaker guarantee than not taking the
    path at all.
    """
    if windows is None:
        return None
    # Explicit rather than `windows.get(symbol, ())`: a symbol with no entry is
    # a mismatched instrument list, and the ()-shaped fallback would delete it
    # from the run without saying so — the one failure mode this whole feature
    # exists to prevent.
    missing = sorted(set(bars) - set(windows))
    if missing:
        raise ValueError(
            f"Point-in-time membership was asked for, but {len(missing)} symbol(s) with bars "
            f"have no membership windows because they are absent from the instrument list "
            f"({', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}). Refusing rather "
            f"than treating them as never-eligible, which would silently shrink the universe."
        )
    masks = {symbol: _window_mask(windows[symbol], frame.index) for symbol, frame in bars.items()}
    eligible_symbols = sum(1 for mask in masks.values() if bool(mask.any()))
    log.info(
        "Point-in-time membership is ON (unknown-date policy %r): %d of %d symbols have at "
        "least one eligible bar, and entries are blocked on every other bar. Positions already "
        "open when a symbol leaves an index are NOT force-closed; the normal exit ladder still "
        "manages them.",
        cfg.backtest.membership_unknown,
        eligible_symbols,
        len(masks),
    )
    return masks


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

    # Same reasoning as the label above: an enforced run whose membership files
    # cannot be read must cost a sentence, not forty minutes of fetching
    # followed by a refusal. Reading them needs the instrument list and nothing
    # else, so it happens here; the masks themselves need the bar indexes and
    # are built further down.
    windows = _eligible_windows(cfg, instruments)

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

    # The candidate values the tuner will actually be offered: whatever
    # [backtest.tuning_grid] asked for, else the default. Recorded in the
    # summary so no report can be read as having searched the default grid when
    # it searched a wider one — a widened grid makes more in-sample choices per
    # fold, which is a reason to trust the out-of-sample number *less*, and that
    # has to be visible next to the number it bought.
    tuning_grid = resolve_grid(cfg.backtest.tuning_grid)

    # The symbols this run actually simulates, as Instruments — the same slice
    # `membership_block` reports on, so the block below and the gate applied to
    # the engine can never describe different universes.
    traded = [instrument for instrument in instruments if instrument.symbol in bars]
    # None unless `[backtest] membership = "point_in_time"`, in which case every
    # run_engine call below is handed a per-symbol, per-bar entry gate.
    eligible = _eligibility_masks(cfg, windows, bars)

    summary: dict[str, Any] = {
        "label": run_label,
        "universe": universe,
        "start": run_start.isoformat(),
        "end": effective_end.isoformat(),
        "walkforward": bool(walkforward),
        "earnings_blackout_simulated": bool(earnings_simulated),
        "n_symbols": len(bars),
        "initial_equity": float(cfg.backtest.initial_equity),
        # Which universe this run really traded, and how much of it a
        # membership file can vouch for. Written for every run, including the
        # default one, so no report can be read as point-in-time when it is not
        # — or as unaware of the gap when it is. Counted over the symbols that
        # actually had bars, not the ones the config asked for, so
        # ``symbols_gated + symbols_ungated == n_symbols`` and the member-year
        # figures describe what was simulated rather than what was requested.
        "membership": membership_block(cfg, traded, run_start, effective_end),
        "config_hash": config_hash(cfg),
        "code_ref": code_ref(),
        "data_hash": data_hash(bars),
        "costs": {
            "slippage_bps": float(cfg.backtest.slippage_bps),
            "spread_atr_frac": float(cfg.backtest.spread_atr_frac),
        },
        "objective": OBJECTIVE_DESCRIPTION if walkforward else "",
        # Empty for a non-walk-forward run, which tunes nothing at all — the
        # same reason ``objective`` is empty there.
        "tuning_grid": (
            {name: list(values) for name, values in tuning_grid.items()} if walkforward else {}
        ),
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
        eligible=eligible,
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
            grid=tuning_grid,
            progress=emit,
            eligible=eligible,
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
        eligible=eligible,
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
