"""Historical earnings calendar loader (user-supplied CSV).

Why this exists
---------------
The live scanner applies the ``[strategy.earnings]`` blackout, but the
backtester cannot: no free data source publishes a historical earnings
calendar reaching back to 2010, so
``swing.backtest.engine.BacktestEngine._earnings_blocked`` returns an
all-``False`` mask and the run carries a loud warning instead (see
docs/indicator-research.md §12). That is a genuine backtest/live divergence —
live takes *fewer* trades than the backtest implies.

If you obtain a calendar — a paid-service export (Nasdaq Data Link,
Zacks, Sharadar SF1/ACTIONS, EOD Historical Data …), a broker download, or a
hand-built file — this module turns it into the mapping the engine wants, so
the blackout can be applied in the backtest too and the divergence closes.

Expected CSV shape
------------------
A header row containing at least ``symbol`` and ``date`` (matched
case-insensitively, any column order); any extra columns are ignored. Dates are
ISO ``YYYY-MM-DD``. Lines starting with ``#`` and blank lines are skipped, the
same convention as ``swing.data.universe.read_symbol_file``::

    # earnings dates exported 2026-01-04
    symbol,date,time,fiscal_quarter
    AAPL,2024-02-01,amc,Q1
    AAPL,2024-05-02,amc,Q2
    MSFT,2024-01-30,amc,Q2

One row per announcement; repeat the symbol for each event. Duplicate rows are
collapsed.

The honest caveat
-----------------
An INCOMPLETE calendar buys only partial protection. Symbols absent from the
file get no blackout at all — exactly the situation today — and the loader has
no way to tell "this symbol never reported" from "this symbol is missing from
my file". A calendar covering 50 of 500 names leaves the other 450 as exposed
to earnings gaps in the backtest as they are now. Coverage is on you: prefer a
calendar spanning the whole universe and the whole backtest window, and treat a
partial file as a partial fix rather than a solved problem.

What has changed is that the shortfall is no longer *silent*. The engine's
"no historical earnings calendar" warning is all-or-nothing — it stops firing
as soon as any calendar is supplied — so
``swing.backtest.runner.load_earnings`` measures this file against the bars
actually loaded and, when coverage is partial, raises its own report-visible
warning naming the exact counts ("covers N of M universe symbols; the other K
get NO earnings blackout ..."), alongside ``earnings_calendar_covered`` and
``earnings_calendar_universe`` in the run manifest. So a thin calendar still
under-protects the backtest, but it can no longer masquerade as a complete one:
the report says how thin it is, and the numbers are on record.
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("swing.data.earnings_calendar")

SYMBOL_COLUMN = "symbol"
DATE_COLUMN = "date"


def load_earnings_calendar(path: str | Path) -> dict[str, list[dt.date]]:
    """Load a user-supplied earnings calendar CSV.

    Returns ``{symbol: [date, ...]}`` with symbols upper-cased and each date
    list sorted and de-duplicated. Symbols that end up with no valid date are
    omitted entirely, so the result never contains an empty list (the engine
    treats "absent" and "no events" identically anyway).

    Malformed rows — unparseable date, missing symbol, too few fields — are
    logged with their line number and skipped, because one bad row in a 5,000
    row export should not cost you the other 4,999. But if *more than half* of
    the data rows are malformed the file is far more likely to be the wrong
    file (a quotes export, a different schema, an HTML error page) than a
    slightly dirty one, and quietly returning a handful of symbols would
    silently un-protect most of the universe — so that raises ``ValueError``.

    Raises:
        FileNotFoundError: if ``path`` does not exist. The caller decides how
            to surface it (the backtest runner can treat a missing calendar as
            "run without the blackout, keep the warning").
        ValueError: if the header lacks ``symbol``/``date``, or if more than
            half of the data rows are malformed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"earnings calendar not found: {path}")

    numbered = _significant_lines(path)
    if not numbered:
        log.warning("earnings calendar %s is empty (no header row); no blackout dates loaded", path)
        return {}

    header_lineno, header_text = numbered[0]
    sym_idx, date_idx = _header_indexes(header_text, path, header_lineno)

    by_symbol: dict[str, set[dt.date]] = {}
    data_rows = 0
    malformed = 0

    for lineno, text in numbered[1:]:
        row = _parse_row(text)
        if row is None:
            # A row that the csv module itself cannot parse (e.g. an unbalanced
            # quote). Still counts against the garbage guard.
            data_rows += 1
            malformed += 1
            log.warning("earnings calendar %s line %d: unparseable CSV row, skipped", path, lineno)
            continue
        if not any(field.strip() for field in row):
            continue  # a line of nothing but commas; not a real data row

        data_rows += 1
        symbol = _field(row, sym_idx).strip().upper()
        raw_date = _field(row, date_idx).strip()

        if not symbol:
            malformed += 1
            log.warning("earnings calendar %s line %d: empty symbol, row skipped", path, lineno)
            continue

        parsed = _parse_date(raw_date)
        if parsed is None:
            malformed += 1
            log.warning(
                "earnings calendar %s line %d: bad date %r for %s (want YYYY-MM-DD), row skipped",
                path, lineno, raw_date, symbol,
            )
            continue

        by_symbol.setdefault(symbol, set()).add(parsed)

    if data_rows and malformed * 2 > data_rows:
        raise ValueError(
            f"earnings calendar {path} looks wrong: {malformed} of {data_rows} data rows are "
            "malformed (more than half). Refusing to load it — a mostly-broken calendar would "
            "silently leave most of the universe without an earnings blackout."
        )

    calendar = {sym: sorted(dates) for sym, dates in by_symbol.items() if dates}
    log.info(
        "earnings calendar %s: %d symbols, %d dates (%d rows skipped)",
        path, len(calendar), sum(len(d) for d in calendar.values()), malformed,
    )
    return calendar


def _significant_lines(path: Path) -> list[tuple[int, str]]:
    """File lines paired with their 1-based number, minus comments and blanks.

    Line numbers are of the *original* file so a warning points at something
    the user can actually find in their editor.
    """
    with path.open(newline="") as fh:
        return [
            (i, line)
            for i, line in enumerate(fh, start=1)
            if line.strip() and not line.lstrip().startswith("#")
        ]


def _header_indexes(header_text: str, path: Path, lineno: int) -> tuple[int, int]:
    """Column positions of ``symbol`` and ``date``, matched case-insensitively."""
    header = _parse_row(header_text) or []
    names = [h.strip().lstrip("\ufeff").strip().lower() for h in header]
    try:
        return names.index(SYMBOL_COLUMN), names.index(DATE_COLUMN)
    except ValueError:
        missing = [c for c in (SYMBOL_COLUMN, DATE_COLUMN) if c not in names]
        raise ValueError(
            f"earnings calendar {path} line {lineno}: header is missing required "
            f"column(s) {', '.join(missing)}; found {names or ['(nothing)']}"
        ) from None


def _parse_row(text: str) -> list[str] | None:
    try:
        return next(csv.reader([text]), [])
    except csv.Error:
        return None


def _field(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _parse_date(raw: str) -> dt.date | None:
    """Parse an ISO date, tolerating an appended time from timestamped exports."""
    if not raw:
        return None
    candidates = [raw]
    for sep in ("T", " "):
        if sep in raw:
            candidates.append(raw.split(sep, 1)[0])
    for candidate in candidates:
        try:
            return dt.date.fromisoformat(candidate)
        except ValueError:
            continue
    return None
