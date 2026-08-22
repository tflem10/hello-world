#!/usr/bin/env python3
"""Point-in-time S&P index membership from free sources — reconstruction and honesty check.

Every stock backtest in this repo runs against ``src/swing/assets/universe/sp{500,400,600}.csv``,
which are snapshots of *today's* membership. Applied retroactively to 2010-2025 they omit every
company that failed, was acquired, or was demoted, so the sample is made of survivors
(``docs/backtest-methodology.md`` §7). This script asks how much of that is fixable for free, and
writes down the answer — including the parts that are not fixable.

The findings are written up in ``docs/survivorship.md``; this is the machinery behind them. Five
separable jobs, one per subcommand:

``build``
    Scrape Wikipedia's index-change tables plus the current constituent tables, pair the add and
    remove events into membership intervals, and write ``sp{500,400,600}-membership.csv``
    (``symbol,name,added,removed``) next to the existing snapshots. Every interval boundary is a
    date some source actually states; nothing is interpolated. Where the sources contradict each
    other or fall silent, the date is written as the literal ``unknown`` and the case is counted in
    the gap report rather than being papered over.

    **This command refuses to run against a membership CSV that someone else has extended.** A
    later package merged SEC EDGAR rosters into these files, adding provenance and bound columns
    *and* revising the two date columns this script writes. A plain rebuild would silently undo
    all of it. :func:`preflight_write` checks every target before any of them is opened and raises
    :class:`SchemaConflict` — exit code 3, nothing written — unless ``--force`` is given, which
    overwrites and says exactly what it destroyed. See that function for why the writer refuses
    rather than merging.

``crosscheck``
    Grade those change tables. The constituent table as it stood on 1 January of two consecutive
    years implies how many changes happened in between; the change table says how many it recorded.
    Two independent artefacts of the same wiki, so the ratio is a completeness measurement rather
    than an impression.

``snapshots``
    Feasibility probe for the other point-in-time route: fetching the *article as it existed* on a
    past date via the MediaWiki revision API, which yields a genuine contemporaneous constituent
    list rather than a reconstruction. Reports, for a date grid, whether the article existed, how
    many rows its table had, and whether that row count is plausible for the index.

``probe``
    The number that actually decides the question: for every ticker that left an index during the
    backtest window, ask Yahoo whether it still serves usable daily history. Buckets each symbol
    from the bars alone — recovered, recycled (the series runs on past a delisting, so the bars
    belong to a different company), still listed, thin, or gone.

``verify``
    Re-request a sample of one ``probe`` bucket one symbol at a time, because a headline resting on
    a bulk download of mostly-dead tickers deserves a second opinion.

Usage::

    uv run python scripts/build_membership.py build
    uv run python scripts/build_membership.py crosscheck
    uv run python scripts/build_membership.py snapshots --granularity quarterly
    uv run python scripts/build_membership.py probe
    uv run python scripts/build_membership.py verify --bucket no_data --sample 25

Writes only to ``src/swing/assets/universe/*-membership.csv`` and to a cache directory that
defaults to a system temp path (``--cache-dir`` to move it). Network responses are cached, so a
re-run is free; delete the cache directory to re-fetch. Wikipedia is polled with a descriptive
User-Agent, one request per second, and exponential backoff on 429 — a sustained walk over a few
hundred old revisions does trip its rate limiter.

Importing this module does nothing. All work is behind ``main()``.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
UNIVERSE_DIR: Final = REPO_ROOT / "src" / "swing" / "assets" / "universe"

WIKI_API: Final = "https://en.wikipedia.org/w/api.php"
#: Wikipedia asks for a descriptive agent that identifies the tool and a contact.
USER_AGENT: Final = (
    "swingtrader2-membership-research/0.1 "
    "(https://github.com/; survivorship-bias feasibility study) python-urllib"
)
#: Minimum seconds between live requests. Cached reads do not wait.
MIN_REQUEST_INTERVAL: Final = 1.0
#: Statuses worth waiting out rather than failing on: rate limiting and transient server errors.
RETRYABLE_STATUS: Final = frozenset({429, 502, 503, 504})
MAX_RETRIES: Final = 5

#: The backtest window this study is about (``backtest.start`` in config.example.toml).
WINDOW_START: Final = dt.date(2010, 1, 1)
WINDOW_END: Final = dt.date(2025, 12, 31)

#: Plausible constituent counts, used to sanity-check a scraped table.
INDEX_SIZE: Final[dict[str, int]] = {"sp500": 503, "sp400": 400, "sp600": 600}

#: Where each index's data lives. ``changes_page`` is the article carrying the add/remove table;
#: for the S&P 500 that was split out of the main list article on 2026-08-11.
SOURCES: Final[dict[str, dict[str, str]]] = {
    "sp500": {
        "constituents_page": "List of S&P 500 companies",
        "changes_page": "Historical components of the S&P 500",
    },
    "sp400": {
        "constituents_page": "List of S&P 400 companies",
        "changes_page": "List of S&P 400 companies",
    },
    "sp600": {
        "constituents_page": "List of S&P 600 companies",
        "changes_page": "List of S&P 600 companies",
    },
}

#: Sentinel written into a date column when a boundary provably exists but no source states it.
UNKNOWN: Final = "unknown"

#: The only columns this script is the authority for. A membership CSV carrying anything else has
#: been extended by another package, and this writer must not rewrite it — see `preflight_write`.
OWNED_COLUMNS: Final[tuple[str, ...]] = ("symbol", "name", "added", "removed")
#: Losing more than this share of a file's rows in a rebuild is treated as an accident, not an
#: update. Wikipedia editing a table away costs a handful of rows; a lost merge costs hundreds.
MAX_ROW_SHRINK: Final = 0.05

_MONTH_DAY_YEAR: Final = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})")
_ISO_DATE: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: Reason-text buckets. Heuristic and reported as such — the reasons are free prose written by
#: hundreds of editors. Order matters: the first pattern that matches wins.
_REASON_RULES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "ticker_or_name_change",
        re.compile(r"renam|ticker symbol|name change|changed its name", re.I),
    ),
    (
        "terminal",
        re.compile(
            r"acquir|merg|bankrupt|chapter 11|chapter 7|liquidat|taken private|went private|"
            r"buyout|delist|dissolv|wound down|purchased by",
            re.I,
        ),
    ),
    (
        "index_migration",
        re.compile(
            r"market cap|no longer representative|more representative|moved (?:from|to) the s&p|"
            r"migrat|rebalanc|added to the s&p|join the s&p",
            re.I,
        ),
    ),
    ("spin_off", re.compile(r"spin[- ]?off|spun off", re.I)),
)


# --------------------------------------------------------------------------------------
# Cached, rate-limited Wikipedia client
# --------------------------------------------------------------------------------------


class WikiClient:
    """Minimal MediaWiki API client: on-disk cache, one request per second, honest User-Agent."""

    def __init__(self, cache_dir: Path, *, offline: bool = False) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._last_request = 0.0
        self.live_requests = 0

    def get(self, params: dict[str, str]) -> dict[str, Any]:
        """Return one API response, from cache when possible."""
        query = dict(params)
        query.setdefault("format", "json")
        query.setdefault("formatversion", "2")
        url = f"{WIKI_API}?{urllib.parse.urlencode(query)}"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", url)[-160:]
        path = self.cache_dir / f"wiki_{slug}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if self.offline:
            raise RuntimeError(f"--offline set but {url} is not cached")

        payload = self._fetch(url)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _fetch(self, url: str) -> dict[str, Any]:
        """One live request, backing off when Wikipedia says we are asking too fast.

        A sustained one-per-second walk over a few hundred old revisions does trip Wikipedia's
        rate limiter, which answers 429. Honouring that with an exponential wait is the difference
        between a slow scrape and an abusive one.
        """
        delay = MIN_REQUEST_INTERVAL
        for attempt in range(MAX_RETRIES):
            wait = delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310
                    payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUS or attempt == MAX_RETRIES - 1:
                    raise
                self._last_request = time.monotonic()
                delay = min(delay * 3, 60.0)
                msg = f"  HTTP {exc.code} from Wikipedia; backing off {delay:.0f}s"
                print(msg, file=sys.stderr)
                continue
            self._last_request = time.monotonic()
            self.live_requests += 1
            return payload
        raise RuntimeError(f"gave up on {url} after {MAX_RETRIES} attempts")

    def tables(self, page: str, *, oldid: int | None = None) -> list[tuple[str, list[list[str]]]]:
        """Render a page (or one past revision) and return ``(table id, rows)`` for every table."""
        params = {"action": "parse", "prop": "text"}
        if oldid is not None:
            params["oldid"] = str(oldid)
        else:
            params["page"] = page
        payload = self.get(params)
        if "parse" not in payload:
            raise RuntimeError(f"no parse result for {page!r} oldid={oldid}: {payload!r}")
        parser = HtmlTableParser()
        parser.feed(payload["parse"]["text"])
        parser.close()
        return list(zip(parser.table_ids, parser.tables, strict=True))

    def revision_before(self, page: str, when: dt.date) -> dict[str, Any] | None:
        """Newest revision of ``page`` at or before ``when``; ``None`` if it did not exist yet."""
        payload = self.get(
            {
                "action": "query",
                "prop": "revisions",
                "titles": page,
                "rvlimit": "1",
                "rvdir": "older",
                "rvstart": f"{when.isoformat()}T00:00:00Z",
                "rvprop": "ids|timestamp|size",
            }
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or "revisions" not in pages[0]:
            return None
        return dict(pages[0]["revisions"][0])


# --------------------------------------------------------------------------------------
# HTML table extraction
# --------------------------------------------------------------------------------------


class HtmlTableParser(HTMLParser):
    """Extract every ``<table>`` as a rectangular grid of plain-text cells.

    ``rowspan`` handling is not optional here. Wikipedia's change tables group several changes
    announced on one day under a single ``rowspan=10`` date cell; a parser that ignores it silently
    drops the date from nine rows out of ten, and those rows then look like malformed junk.
    ``<sup>`` is skipped so that footnote markers do not end up inside a ticker.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.table_ids: list[str] = []
        self._grids: list[list[list[str]]] = []
        self._ids: list[str] = []
        #: per open table: column -> (text, rows still to be filled *below* the declaring row)
        self._carried: list[dict[int, tuple[str, int]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span: tuple[int, int] = (1, 1)
        self._consumed: set[int] = set()
        self._new_spans: dict[int, tuple[str, int]] = {}
        self._skip = 0

    @staticmethod
    def _span_of(attrs: dict[str, str | None], key: str) -> int:
        raw = (attrs.get(key) or "1").strip()
        try:
            return max(1, min(100, int(raw)))
        except ValueError:
            return 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "table":
            self._grids.append([])
            self._ids.append(attr.get("id") or "")
            self._carried.append({})
        elif tag == "tr" and self._grids:
            self._row = []
            self._consumed = set()
            self._new_spans = {}
        elif tag in {"td", "th"} and self._grids:
            self._cell = []
            self._span = (self._span_of(attr, "colspan"), self._span_of(attr, "rowspan"))
        elif tag in {"sup", "style", "script"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._grids:
            self.tables.append(self._grids.pop())
            self.table_ids.append(self._ids.pop())
            self._carried.pop()
        elif tag == "tr" and self._grids and self._row is not None:
            self._grids[-1].append(self._finish_row(self._row))
            self._row = None
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._place(re.sub(r"\s+", " ", "".join(self._cell)).strip(), *self._span)
            self._cell = None
        elif tag in {"sup", "style", "script"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0 and self._cell is not None:
            self._cell.append(data)

    def _fill_carried(self, row: list[str]) -> None:
        """Drop in every carried cell that owns the next free column."""
        carried = self._carried[-1]
        while len(row) in carried:
            column = len(row)
            row.append(carried[column][0])
            self._consumed.add(column)

    def _place(self, text: str, colspan: int, rowspan: int) -> None:
        """Append one cell, skipping past columns a previous row's ``rowspan`` already owns."""
        row = self._row
        if row is None:  # pragma: no cover - a <td> outside a <tr>
            return
        self._fill_carried(row)
        start = len(row)
        row.append(text)
        row.extend("" for _ in range(colspan - 1))
        if rowspan > 1:
            for offset in range(colspan):
                self._new_spans[start + offset] = (text if offset == 0 else "", rowspan - 1)

    def _finish_row(self, row: list[str]) -> list[str]:
        """Close the row: trailing carried cells, then age the carry table by one row."""
        self._fill_carried(row)
        carried = self._carried[-1]
        for column in self._consumed:
            text, remaining = carried[column]
            if remaining <= 1:
                del carried[column]
            else:
                carried[column] = (text, remaining - 1)
        carried.update(self._new_spans)
        return row


# --------------------------------------------------------------------------------------
# Parsing the tables into events
# --------------------------------------------------------------------------------------


def parse_date(text: str) -> dt.date | None:
    """Parse the date formats Wikipedia's index tables actually use, or return ``None``."""
    stripped = text.strip()
    if not stripped:
        return None
    iso = _ISO_DATE.search(stripped)
    if iso:
        try:
            return dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    match = _MONTH_DAY_YEAR.search(stripped)
    if match is None:
        return None
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(
                f"{match.group(1)} {match.group(2)} {match.group(3)}", fmt
            ).date()
        except ValueError:
            continue
    return None


def to_yahoo_symbol(symbol: str) -> str:
    """Yahoo writes share classes with a dash. Mirrors ``swing.universe.to_yahoo_symbol``."""
    return symbol.strip().upper().replace(".", "-").replace(" ", "")


def clean_ticker(text: str) -> str:
    """Strip the decoration Wikipedia wraps around tickers and reject anything implausible."""
    candidate = text.strip().split("(")[0].strip().strip("*").strip()
    candidate = candidate.replace("NYSE:", "").replace("NASDAQ:", "").strip()
    if not candidate or len(candidate) > 8:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-]*", candidate):
        return ""
    return to_yahoo_symbol(candidate)


def classify_reason(reason: str) -> str:
    for label, pattern in _REASON_RULES:
        if pattern.search(reason):
            return label
    return "unclassified"


@dataclass(frozen=True)
class ChangeEvent:
    """One row of an index-change table."""

    index: str
    date: dt.date
    added: str
    added_name: str
    removed: str
    removed_name: str
    reason: str

    @property
    def category(self) -> str:
        return classify_reason(self.reason)


@dataclass(frozen=True)
class Constituent:
    """One row of a current-membership table."""

    symbol: str
    name: str
    date_added: dt.date | None


def find_table(tables: list[tuple[str, list[list[str]]]], table_id: str) -> list[list[str]]:
    for tid, rows in tables:
        if tid == table_id:
            return rows
    raise RuntimeError(f"no table with id={table_id!r} on this page")


def parse_change_table(rows: list[list[str]], index: str) -> tuple[list[ChangeEvent], int]:
    """Rows -> events. Returns the events plus the number of rows that could not be read.

    Layout is fixed across all three articles: date, added ticker, added security, removed ticker,
    removed security, reason, [refs]. A row with neither a readable added nor removed ticker is
    counted as unparsed rather than dropped silently.
    """
    events: list[ChangeEvent] = []
    unparsed = 0
    for row in rows:
        if len(row) < 5:
            continue
        when = parse_date(row[0])
        if when is None:
            continue  # header rows and the like
        added = clean_ticker(row[1])
        removed = clean_ticker(row[3])
        if not added and not removed:
            unparsed += 1
            continue
        events.append(
            ChangeEvent(
                index=index,
                date=when,
                added=added,
                added_name=row[2].strip(),
                removed=removed,
                removed_name=row[4].strip(),
                reason=row[5].strip() if len(row) > 5 else "",
            )
        )
    return events, unparsed


def _header_columns(header: list[str]) -> dict[str, int]:
    """Locate the columns we need by name, because the column *order* changed over the years."""
    found: dict[str, int] = {}
    for position, cell in enumerate(header):
        label = cell.strip().lower()
        if "symbol" in label or label in {"ticker", "ticker symbol"}:
            found.setdefault("symbol", position)
        elif label in {"security", "company", "company name"}:
            found.setdefault("name", position)
        elif "date added" in label or label == "date first added":
            found.setdefault("date_added", position)
    return found


def parse_constituents(rows: list[list[str]]) -> list[Constituent]:
    """Rows -> current members, driven by the header rather than by fixed column positions."""
    if not rows:
        return []
    columns = _header_columns(rows[0])
    if "symbol" not in columns:
        raise RuntimeError(f"cannot find a symbol column in header {rows[0]!r}")
    symbol_at = columns["symbol"]
    name_at = columns.get("name", 1 if symbol_at == 0 else 0)
    added_at = columns.get("date_added")

    members: list[Constituent] = []
    for row in rows[1:]:
        if len(row) <= symbol_at:
            continue
        symbol = clean_ticker(row[symbol_at])
        if not symbol:
            continue
        name = row[name_at].strip() if len(row) > name_at else symbol
        added = None
        if added_at is not None and len(row) > added_at:
            added = parse_date(row[added_at])
        members.append(Constituent(symbol=symbol, name=name or symbol, date_added=added))
    return members


# --------------------------------------------------------------------------------------
# Interval reconstruction
# --------------------------------------------------------------------------------------


@dataclass
class GapReport:
    """Everything the sources failed to say, counted rather than smoothed over."""

    index: str
    change_rows: int = 0
    unparsed_rows: int = 0
    earliest_event: str = ""
    latest_event: str = ""
    events_per_year: dict[int, int] = field(default_factory=dict)
    adds_without_removal: int = 0
    removals_without_add: int = 0
    current_member_last_seen_removed: int = 0
    duplicate_adds: int = 0
    date_added_disagreements: int = 0
    ticker_or_name_change_rows: int = 0
    reason_categories: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MembershipRow:
    """One membership interval. ``added``/``removed`` are ISO, ``""`` or :data:`UNKNOWN`."""

    symbol: str
    name: str
    added: str
    removed: str


def _intervals_for(
    events: list[tuple[dt.date, str]], *, is_current: bool, gaps: GapReport
) -> list[tuple[str, str]]:
    """Pair a symbol's add/remove events into intervals. Nothing is invented."""
    ordered = sorted(events, key=lambda item: (item[0], 0 if item[1] == "remove" else 1))
    intervals: list[tuple[str, str]] = []
    open_add: str | None = None

    for when, kind in ordered:
        if kind == "add":
            if open_add is not None:
                gaps.duplicate_adds += 1
                continue
            open_add = when.isoformat()
        elif open_add is None:
            gaps.removals_without_add += 1
            intervals.append(("", when.isoformat()))
        else:
            intervals.append((open_add, when.isoformat()))
            open_add = None

    if open_add is not None:
        if is_current:
            intervals.append((open_add, ""))
        else:
            gaps.adds_without_removal += 1
            intervals.append((open_add, UNKNOWN))
    elif is_current:
        if intervals:
            # Last recorded event was a removal, yet the symbol is on today's list: an unrecorded
            # re-addition happened. Say so instead of pretending the gap is not there.
            gaps.current_member_last_seen_removed += 1
            intervals.append((UNKNOWN, ""))
        else:
            intervals.append(("", ""))
    return intervals


def build_index(
    index: str, events: list[ChangeEvent], members: list[Constituent], unparsed: int
) -> tuple[list[MembershipRow], GapReport]:
    """Turn one index's change events and current roster into membership intervals."""
    gaps = GapReport(index=index, change_rows=len(events), unparsed_rows=unparsed)
    if events:
        gaps.earliest_event = min(e.date for e in events).isoformat()
        gaps.latest_event = max(e.date for e in events).isoformat()
        gaps.events_per_year = dict(sorted(Counter(e.date.year for e in events).items()))
        gaps.reason_categories = dict(Counter(e.category for e in events).most_common())
        gaps.ticker_or_name_change_rows = sum(
            1 for e in events if e.category == "ticker_or_name_change"
        )

    by_symbol: dict[str, list[tuple[dt.date, str]]] = defaultdict(list)
    names: dict[str, str] = {}
    for event in events:
        if event.added:
            by_symbol[event.added].append((event.date, "add"))
            names.setdefault(event.added, event.added_name)
        if event.removed:
            by_symbol[event.removed].append((event.date, "remove"))
            names.setdefault(event.removed, event.removed_name)

    current = {m.symbol: m for m in members}
    for symbol, member in current.items():
        names[symbol] = member.name or names.get(symbol, symbol)
        by_symbol.setdefault(symbol, [])

    rows: list[MembershipRow] = []
    for symbol in sorted(by_symbol):
        member = current.get(symbol)
        intervals = _intervals_for(by_symbol[symbol], is_current=member is not None, gaps=gaps)
        for added, removed in intervals:
            if member is not None and removed == "" and member.date_added is not None:
                stated = member.date_added.isoformat()
                if added in {"", UNKNOWN}:
                    added = stated
                elif added != stated:
                    delta = abs((dt.date.fromisoformat(added) - member.date_added).days)
                    if delta > 7:
                        gaps.date_added_disagreements += 1
                    added = stated
            rows.append(
                MembershipRow(
                    symbol=symbol,
                    name=names.get(symbol, symbol) or symbol,
                    added=added,
                    removed=removed,
                )
            )
    rows.sort(key=lambda r: (r.symbol, r.added or "0000"))
    return rows, gaps


#: Why the guard refuses instead of merging. Stated in the error itself, because the next person
#: to hit this will reasonably wonder why a writer cannot just preserve the columns it does not own.
_WHY_NO_MERGE: Final = """\
This script is the authority for {owned} and nothing else, and it cannot
merge its output into an extended file. A downstream source does not merely add columns — it also
*revises* the two date columns, filling dates this script leaves empty and replacing its 'unknown'
sentinels with real ones. So (symbol, added, removed) is not a stable key between the two versions,
and carrying the extra columns across on a partial match would attach provenance to dates that no
longer match it. A merge here would produce coherent-looking rows that are wrong.

Rebuild the base and re-run the downstream merge that produced those columns, or pass --force to
overwrite and lose them."""


class SchemaConflict(RuntimeError):
    """Raised when a membership CSV on disk holds more than this script is the authority for."""


def read_existing_shape(path: Path) -> tuple[list[str], int]:
    """Header and data-row count of an existing membership CSV. ``([], 0)`` if there is none."""
    if not path.exists():
        return [], 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        return [column.strip() for column in header], sum(1 for _ in reader)


def preflight_write(targets: dict[Path, list[MembershipRow]], *, force: bool) -> list[str]:
    """Refuse to rewrite files this script no longer owns. Returns notes for the caller to print.

    Every path is checked *before* any path is written, so a conflict on the third index cannot
    leave the first two clobbered.

    Two ways a rewrite is rejected:

    * **Foreign columns.** The file carries columns outside :data:`OWNED_COLUMNS`. Something else
      extended the schema and this writer would drop what it added.
    * **Material row loss.** The file has meaningfully more rows than the rebuild produces, which
      is what a schema-stripping edit followed by a rebuild looks like from here.

    ``--force`` converts both into loud notes. Nothing about this is silent in either direction.
    """
    conflicts: list[str] = []
    notes: list[str] = []
    for path, rows in sorted(targets.items()):
        header, existing_rows = read_existing_shape(path)
        if not header:
            continue
        foreign = [column for column in header if column not in OWNED_COLUMNS]
        if foreign:
            conflicts.append(
                f"{path.name} carries {len(foreign)} column(s) this script does not manage "
                f"({', '.join(foreign)}). Rewriting it would discard them."
            )
        shrink = existing_rows - len(rows)
        if existing_rows and shrink > existing_rows * MAX_ROW_SHRINK:
            conflicts.append(
                f"{path.name} has {existing_rows} rows and the rebuild produces {len(rows)} "
                f"— a loss of {shrink} ({shrink / existing_rows:.0%}). Another source has added "
                f"rows this rebuild does not know about."
            )
    if not conflicts:
        return notes
    if not force:
        raise SchemaConflict(
            "Refusing to rewrite the membership CSVs.\n  - "
            + "\n  - ".join(conflicts)
            + "\n\n"
            + _WHY_NO_MERGE.format(owned=", ".join(OWNED_COLUMNS))
        )
    notes.extend(f"--force: OVERWRITING ANYWAY — {conflict}" for conflict in conflicts)
    return notes


def write_membership_csv(path: Path, rows: list[MembershipRow]) -> None:
    """Write the four columns this script owns. Guarded by :func:`preflight_write`."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(list(OWNED_COLUMNS))
        for row in rows:
            writer.writerow([row.symbol, row.name, row.added, row.removed])


# --------------------------------------------------------------------------------------
# Quantifying the hole in the current backtests
# --------------------------------------------------------------------------------------


def _overlap_days(start: dt.date, end: dt.date) -> int:
    lo = max(start, WINDOW_START)
    hi = min(end, WINDOW_END)
    return max(0, (hi - lo).days)


@dataclass
class Departure:
    """A symbol that left an index inside the window and is absent from today's universe."""

    symbol: str
    name: str
    index: str
    added: str
    removed: str
    reason: str
    category: str
    member_days_window: int
    member_days_covered: int


def compute_departures(
    per_index: dict[str, tuple[list[MembershipRow], GapReport]],
    events_by_index: dict[str, list[ChangeEvent]],
    current_universe: set[str],
) -> list[Departure]:
    """Departures that actually cost us sample: gone from the index *and* from today's universe.

    A name that moved from the S&P 500 to the S&P 400 and is still in the 400 today is already in
    the committed CSVs, so it is not a survivorship hole and is excluded here.
    """
    reason_by_key: dict[tuple[str, str, str], ChangeEvent] = {}
    for index, events in events_by_index.items():
        for event in events:
            if event.removed:
                reason_by_key[(index, event.removed, event.date.isoformat())] = event

    departures: list[Departure] = []
    for index, (rows, gaps) in per_index.items():
        coverage_start = (
            dt.date.fromisoformat(gaps.earliest_event) if gaps.earliest_event else WINDOW_START
        )
        for row in rows:
            if row.removed in {"", UNKNOWN}:
                continue
            left = dt.date.fromisoformat(row.removed)
            if not (WINDOW_START <= left <= WINDOW_END):
                continue
            if row.symbol in current_universe:
                continue
            joined = dt.date.fromisoformat(row.added) if row.added not in {"", UNKNOWN} else None
            event = reason_by_key.get((index, row.symbol, row.removed))
            departures.append(
                Departure(
                    symbol=row.symbol,
                    name=row.name,
                    index=index,
                    added=row.added,
                    removed=row.removed,
                    reason=event.reason if event else "",
                    category=event.category if event else "unclassified",
                    member_days_window=_overlap_days(joined or WINDOW_START, left),
                    member_days_covered=_overlap_days(
                        joined or max(WINDOW_START, coverage_start), left
                    ),
                )
            )
    departures.sort(key=lambda d: (d.removed, d.symbol))
    return departures


def lookahead_member_days(rows: list[MembershipRow], current_universe: set[str]) -> dict[str, int]:
    """Member-days the backtest grants a current member *before* it actually joined the index.

    This is the other half of the bias, and unlike the missing-departures half it is measurable
    exactly wherever the source states a join date: a company that entered the S&P 500 in 2019 is
    nevertheless tradable from 2010 in every backtest run so far.
    """
    total = 0
    affected = 0
    for row in rows:
        if row.removed != "" or row.added in {"", UNKNOWN}:
            continue
        if row.symbol not in current_universe:
            continue
        joined = dt.date.fromisoformat(row.added)
        days = _overlap_days(WINDOW_START, min(joined, WINDOW_END))
        if days > 0:
            total += days
            affected += 1
    return {"symbols": affected, "member_days": total}


# --------------------------------------------------------------------------------------
# Subcommand: build
# --------------------------------------------------------------------------------------


def load_current_universe() -> set[str]:
    """Today's committed universe — the thing every existing backtest actually trades."""
    symbols: set[str] = set()
    for stem in ("sp500", "sp400", "sp600"):
        path = UNIVERSE_DIR / f"{stem}.csv"
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for record in csv.DictReader(handle):
                symbol = to_yahoo_symbol(record.get("symbol") or "")
                if symbol:
                    symbols.add(symbol)
    return symbols


def cmd_build(args: argparse.Namespace) -> int:
    client = WikiClient(Path(args.cache_dir), offline=args.offline)
    fetched = dt.date.today().isoformat()

    per_index: dict[str, tuple[list[MembershipRow], GapReport]] = {}
    events_by_index: dict[str, list[ChangeEvent]] = {}
    constituents_by_index: dict[str, list[Constituent]] = {}

    for index, source in SOURCES.items():
        changes_tables = client.tables(source["changes_page"])
        events, unparsed = parse_change_table(find_table(changes_tables, "changes"), index)
        if source["constituents_page"] == source["changes_page"]:
            constituent_tables = changes_tables
        else:
            constituent_tables = client.tables(source["constituents_page"])
        members = parse_constituents(find_table(constituent_tables, "constituents"))
        events_by_index[index] = events
        constituents_by_index[index] = members
        per_index[index] = build_index(index, events, members, unparsed)

    current_universe = load_current_universe()
    departures = compute_departures(per_index, events_by_index, current_universe)

    targets = {
        UNIVERSE_DIR / f"{index}-membership.csv": rows for index, (rows, _) in per_index.items()
    }
    write_notes: list[str] = []
    if not args.dry_run:
        # Check every target before touching any of them: a conflict on sp600 must not leave
        # sp500 and sp400 already clobbered.
        write_notes = preflight_write(targets, force=args.force)
        for path, rows in targets.items():
            write_membership_csv(path, rows)

    summary: dict[str, Any] = {
        "fetched": fetched,
        "window": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "live_requests": client.live_requests,
        "current_universe_symbols": len(current_universe),
        "indices": {},
        "departures": {
            "total": len(departures),
            "by_index": dict(Counter(d.index for d in departures)),
            "by_category": dict(Counter(d.category for d in departures).most_common()),
            "by_year": dict(sorted(Counter(d.removed[:4] for d in departures).items())),
            "member_years_window": round(sum(d.member_days_window for d in departures) / 365.25, 1),
            "member_years_covered": round(
                sum(d.member_days_covered for d in departures) / 365.25, 1
            ),
        },
        "lookahead": {},
    }
    for index, (rows, gaps) in per_index.items():
        summary["indices"][index] = {
            "constituents_scraped": len(constituents_by_index[index]),
            "membership_rows": len(rows),
            "distinct_symbols": len({r.symbol for r in rows}),
            "gaps": vars(gaps),
        }
        summary["lookahead"][index] = lookahead_member_days(rows, current_universe)
        summary["lookahead"][index]["member_years"] = round(
            summary["lookahead"][index]["member_days"] / 365.25, 1
        )

    out = Path(args.json_out) if args.json_out else Path(args.cache_dir) / "build_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    departures_path = Path(args.cache_dir) / "departures.json"
    departures_path.write_text(
        json.dumps([vars(d) for d in departures], indent=2), encoding="utf-8"
    )

    print(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote {out}")
    print(f"wrote {departures_path}  ({len(departures)} departures)")
    if args.dry_run:
        print("(--dry-run: no CSVs written)")
    else:
        for note in write_notes:
            print(note, file=sys.stderr)
        for path in targets:
            print(f"wrote {path}  ({', '.join(OWNED_COLUMNS)})")
    return 0


# --------------------------------------------------------------------------------------
# Subcommand: snapshots
# --------------------------------------------------------------------------------------


def _date_grid(granularity: str) -> list[dt.date]:
    if granularity == "quarterly":
        months = (1, 4, 7, 10)
    elif granularity == "semiannual":
        months = (1, 7)
    else:
        months = (1,)
    return [
        dt.date(year, month, 1)
        for year in range(WINDOW_START.year, WINDOW_END.year + 1)
        for month in months
    ]


def pick_constituent_table(
    tables: list[tuple[str, list[list[str]]]], expected: int
) -> list[list[str]] | None:
    """Choose the constituent table in a revision that predates the ``id="constituents"`` anchor.

    "Biggest table on the page" is not good enough: by 2024 the change table on the S&P 400 article
    had outgrown the constituent table, so size alone picks the wrong one. Prefer the anchor, then
    a table whose header actually has a symbol column, then whichever is nearest the index size.
    """
    for table_id, rows in tables:
        if table_id == "constituents" and len(rows) > 1:
            return rows
    scored: list[tuple[int, list[list[str]]]] = []
    for _, rows in tables:
        if len(rows) < expected * 0.5:
            continue
        if "symbol" not in _header_columns(rows[0]):
            continue
        scored.append((abs(len(rows) - 1 - expected), rows))
    if not scored:
        return None
    return min(scored, key=lambda item: item[0])[1]


def cmd_crosscheck(args: argparse.Namespace) -> int:
    """Measure how complete the change table is, using the article's own past revisions.

    The change table claims to list index changes; the constituent table as it stood on 1 January
    of two consecutive years implies how many changes *actually* happened in between. Those are
    two independent artefacts of the same wiki, so comparing them puts a number on the change
    table's completeness per year instead of leaving it to impression.

    The symmetric difference of two year-end rosters counts one replacement as two changes (one in,
    one out), which is also how a change-table row is counted here, so the two are comparable. A
    name that left and returned inside the same year cancels in the roster diff and shows up only
    in the change table, so the ratio is a floor on completeness, not a point estimate.
    """
    client = WikiClient(Path(args.cache_dir), offline=args.offline)
    years = list(range(WINDOW_START.year, WINDOW_END.year + 2))
    report: dict[str, Any] = {}

    for index, source in SOURCES.items():
        page = source["constituents_page"]
        expected = INDEX_SIZE[index]
        rosters: dict[int, set[str]] = {}
        for year in years:
            revision = client.revision_before(page, dt.date(year, 1, 1))
            if revision is None:
                continue
            try:
                tables = client.tables(page, oldid=int(revision["revid"]))
            except Exception:  # noqa: BLE001 - a bad revision is data, not a crash
                continue
            best = pick_constituent_table(tables, expected)
            if best is None:
                continue
            symbols = {m.symbol for m in parse_constituents(best)}
            if 0.9 <= len(symbols) / expected <= 1.1:
                rosters[year] = symbols

        changes_tables = client.tables(source["changes_page"])
        events, _ = parse_change_table(find_table(changes_tables, "changes"), index)
        recorded: Counter[int] = Counter()
        for event in events:
            if event.added:
                recorded[event.date.year] += 1
            if event.removed:
                recorded[event.date.year] += 1

        rows: list[dict[str, Any]] = []
        for year in years[:-1]:
            if year not in rosters or year + 1 not in rosters:
                continue
            implied = len(rosters[year] ^ rosters[year + 1])
            stated = recorded.get(year, 0)
            rows.append(
                {
                    "year": year,
                    "roster_implied_changes": implied,
                    "change_table_rows": stated,
                    "coverage": round(stated / implied, 2) if implied else None,
                }
            )
        report[index] = rows

    out = Path(args.json_out) if args.json_out else Path(args.cache_dir) / "crosscheck.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    for index, rows in report.items():
        print(f"\n{index}: year  roster-implied  change-table  coverage")
        for row in rows:
            print(
                f"        {row['year']}      {row['roster_implied_changes']:4}"
                f"          {row['change_table_rows']:4}        {row['coverage']}"
            )
        usable = [r for r in rows if r["coverage"] is not None]
        if usable:
            mean = sum(float(r["coverage"]) for r in usable) / len(usable)
            print(f"        mean coverage across {len(usable)} years: {mean:.2f}")
    print(f"\nwrote {out}")
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    """Ask whether past article revisions are a usable point-in-time source."""
    client = WikiClient(Path(args.cache_dir), offline=args.offline)
    grid = _date_grid(args.granularity)
    results: dict[str, list[dict[str, Any]]] = {}

    for index, source in SOURCES.items():
        page = source["constituents_page"]
        expected = INDEX_SIZE[index]
        rows_out: list[dict[str, Any]] = []
        for when in grid:
            revision = client.revision_before(page, when)
            if revision is None:
                rows_out.append({"asof": when.isoformat(), "status": "article_did_not_exist"})
                continue
            entry: dict[str, Any] = {
                "asof": when.isoformat(),
                "revid": revision["revid"],
                "rev_timestamp": revision["timestamp"],
                "lag_days": (when - dt.date.fromisoformat(revision["timestamp"][:10])).days,
            }
            try:
                tables = client.tables(page, oldid=int(revision["revid"]))
                best = pick_constituent_table(tables, expected)
                if best is None:
                    entry["status"] = "no_plausible_table"
                else:
                    members = parse_constituents(best)
                    entry["table_rows"] = len(best) - 1
                    entry["parsed_symbols"] = len(members)
                    entry["distinct_symbols"] = len({m.symbol for m in members})
                    ratio = entry["distinct_symbols"] / expected
                    entry["status"] = "ok" if 0.9 <= ratio <= 1.1 else "implausible_row_count"
            except Exception as exc:  # noqa: BLE001 - feasibility probe: record, do not abort
                entry["status"] = f"error: {type(exc).__name__}: {exc}"
            rows_out.append(entry)
        results[index] = rows_out

    out = Path(args.json_out) if args.json_out else Path(args.cache_dir) / "snapshots.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")

    for index, entries in results.items():
        statuses = Counter(str(e.get("status")).split(":")[0] for e in entries)
        usable = [e for e in entries if e.get("status") == "ok"]
        print(f"\n{index}: {len(entries)} grid points -> {dict(statuses)}")
        if usable:
            print(f"  earliest usable snapshot: {usable[0]['asof']} (rev {usable[0]['revid']})")
            lags = [e["lag_days"] for e in usable]
            print(f"  revision lag behind grid date: median {sorted(lags)[len(lags) // 2]}d")
        for entry in entries:
            if entry.get("status") not in {"ok", "article_did_not_exist"}:
                print(f"  {entry['asof']}: {entry.get('status')} {entry.get('distinct_symbols')}")
    print(f"\nwrote {out}")
    return 0


# --------------------------------------------------------------------------------------
# Subcommand: probe
# --------------------------------------------------------------------------------------

#: Trading past the index exit by more than this is "the series kept going", not rounding.
POST_EXIT_GRACE_DAYS: Final = 45
#: A hole this long around the exit date means the bars either side belong to two companies.
RECYCLE_GAP_DAYS: Final = 250
#: Fewer pre-exit bars than this and the ticker cannot plausibly be the same listing.
RECYCLE_MAX_PRE_BARS: Final = 20
#: Bars needed before the departure date for the strategy's slowest indicator to be warm.
MIN_HISTORY_BARS: Final = 200


def classify_series(removed: str, record: dict[str, Any], *, fetched_to: dt.date) -> str:
    """Bucket one Yahoo response for a departed ticker, using only the bars themselves.

    Deliberately independent of the reason text: the reasons are editor prose and misclassifying
    one would quietly move a symbol between "recovered" and "recycled", which is the one error
    this study cannot afford. Buckets:

    ``no_data``
        Yahoo serves nothing. The overwhelmingly common case.
    ``recycled``
        Bars exist but essentially none of them predate the index exit, and the series runs on
        long past it — the symbol now belongs to a different company.
    ``recycle_gap``
        Bars exist either side of the exit but with a hole of :data:`RECYCLE_GAP_DAYS` or more
        around it — two listings stitched together under one ticker.
    ``still_listed``
        Continuous bars straight through the exit and beyond. The company left the index but kept
        trading. Real data, but a demotion, not a rescued failure — and see the caveat below.
    ``delisted_recovered``
        The series stops at the exit and has enough prior history to trade. This, and only this,
        is a genuine survivorship recovery.
    ``delisted_short``
        Stops at the exit but with too little history to warm the indicators.
    ``truncated``
        Stops well *before* the exit — a partial series Yahoo never finished.
    ``too_recent``
        The exit is so close to the end of the fetch window that "stopped" and "still going" are
        indistinguishable. Excluded from the recovery rate rather than credited to it.

    Caveat that no automated rule can remove: a recycled ticker whose replacement listing has a
    long history produces a *continuous* series across the exit and lands in ``still_listed``.
    Spot-checking prices against known deal terms is the only way to catch those.
    """
    bars = int(record.get("bars", 0))
    if bars == 0 or not record.get("last"):
        return "no_data"
    left = dt.date.fromisoformat(removed)
    if (fetched_to - left).days <= POST_EXIT_GRACE_DAYS:
        return "too_recent"
    last = dt.date.fromisoformat(str(record["last"]))
    pre_bars = int(record.get("bars_before_removal", 0))
    runs_on = (last - left).days > POST_EXIT_GRACE_DAYS

    if runs_on and pre_bars <= RECYCLE_MAX_PRE_BARS:
        return "recycled"
    if runs_on:
        last_before = record.get("last_before_removal")
        first_after = record.get("first_after_removal")
        if last_before and first_after:
            gap = (
                dt.date.fromisoformat(str(first_after)) - dt.date.fromisoformat(str(last_before))
            ).days
            if gap >= RECYCLE_GAP_DAYS:
                return "recycle_gap"
        return "still_listed"
    if (left - last).days > POST_EXIT_GRACE_DAYS:
        return "truncated"
    return "delisted_recovered" if pre_bars >= MIN_HISTORY_BARS else "delisted_short"


def cmd_probe(args: argparse.Namespace) -> int:
    """Ask Yahoo, one departed ticker at a time, whether the history is actually there."""
    departures_path = Path(args.cache_dir) / "departures.json"
    if not departures_path.exists():
        print(f"run `build` first: {departures_path} is missing", file=sys.stderr)
        return 2
    raw = json.loads(departures_path.read_text(encoding="utf-8"))
    departures = [Departure(**item) for item in raw]

    # One row per symbol: the earliest departure is the one whose history we would need.
    unique: dict[str, Departure] = {}
    for departure in departures:
        unique.setdefault(departure.symbol, departure)
    targets = sorted(unique.values(), key=lambda d: d.symbol)
    if args.limit:
        targets = targets[: args.limit]

    import pandas as pd  # noqa: PLC0415 - heavy imports stay out of module import
    import yfinance as yf  # noqa: PLC0415

    # Fetch right up to today, not to the end of the backtest window: a symbol that left the index
    # in the last weeks of the window would otherwise look like it "stopped at the exit" purely
    # because the request stopped there, and would be miscredited as a recovered delisting.
    fetched_to = dt.date.today()
    cache_path = Path(args.cache_dir) / "yahoo_probe.json"
    cached: dict[str, dict[str, Any]] = {}
    if cache_path.exists() and not args.refresh:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))

    todo = [d for d in targets if d.symbol not in cached]
    print(f"{len(targets)} departed symbols; {len(todo)} to fetch, {len(cached)} cached")

    for start in range(0, len(todo), args.batch):
        batch = todo[start : start + args.batch]
        tickers = [d.symbol for d in batch]
        try:
            frame = yf.download(
                tickers,
                start="2008-01-01",
                end=(fetched_to + dt.timedelta(days=1)).isoformat(),
                auto_adjust=False,
                progress=False,
                group_by="ticker",
                threads=True,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001 - a dead batch must not kill the run
            print(f"  batch {start // args.batch}: download failed ({exc})")
            frame = None

        for departure in batch:
            closes = _extract_close(frame, departure.symbol, len(tickers), pd)
            cached[departure.symbol] = summarise_series(closes, departure.removed)
        cache_path.write_text(json.dumps(cached, indent=2), encoding="utf-8")
        print(f"  fetched {min(start + args.batch, len(todo))}/{len(todo)}")
        time.sleep(args.sleep)

    verdicts: list[dict[str, Any]] = []
    for departure in targets:
        record = cached.get(departure.symbol, {"bars": 0})
        verdict = classify_series(departure.removed, record, fetched_to=fetched_to)
        verdicts.append({**vars(departure), **record, "verdict": verdict})

    out = Path(args.json_out) if args.json_out else Path(args.cache_dir) / "probe_summary.json"
    summary = summarise_verdicts(verdicts)
    summary["fetched_to"] = fetched_to.isoformat()
    out.write_text(
        json.dumps({"summary": summary, "symbols": verdicts}, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out}")
    return 0


def summarise_series(closes: Any, removed: str) -> dict[str, Any]:
    """Reduce one price series to the handful of facts the classifier needs."""
    if closes is None or getattr(closes, "empty", True):
        return {"bars": 0}
    left = dt.date.fromisoformat(removed)
    stamps = [value.date() for value in closes.index]
    before = [d for d in stamps if d <= left]
    after = [d for d in stamps if d > left]
    return {
        "bars": len(stamps),
        "first": stamps[0].isoformat(),
        "last": stamps[-1].isoformat(),
        "bars_before_removal": len(before),
        "last_before_removal": before[-1].isoformat() if before else None,
        "first_after_removal": after[0].isoformat() if after else None,
    }


def summarise_verdicts(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll the per-symbol verdicts up into the numbers that answer the feasibility question."""
    counts = Counter(str(v["verdict"]) for v in verdicts)
    # "too_recent" is undecidable, not a failure to recover: keep it out of both denominators.
    verdicts = [v for v in verdicts if v["verdict"] != "too_recent"]
    total = len(verdicts)
    by_index: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_reason: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for verdict in verdicts:
        by_index[str(verdict["index"])][str(verdict["verdict"])] += 1
        by_reason[str(verdict["category"])][str(verdict["verdict"])] += 1

    def days(predicate: str) -> int:
        return sum(int(v["member_days_covered"]) for v in verdicts if v["verdict"] == predicate)

    total_days = sum(int(v["member_days_covered"]) for v in verdicts)
    clean = counts["delisted_recovered"]
    usable = clean + counts["still_listed"]
    usable_days = days("delisted_recovered") + days("still_listed")
    return {
        "probed_symbols": total,
        "excluded_too_recent": counts["too_recent"],
        "verdicts": dict(counts.most_common()),
        "by_index": {k: dict(v) for k, v in by_index.items()},
        "by_reason_category": {k: dict(v) for k, v in by_reason.items()},
        "clean_delisting_recovery_rate": round(clean / total, 4) if total else 0.0,
        "any_usable_recovery_rate": round(usable / total, 4) if total else 0.0,
        "recycling_flagged": counts["recycled"] + counts["recycle_gap"],
        "member_years_missing": round(total_days / 365.25, 1),
        "member_years_recoverable": round(usable_days / 365.25, 1),
        "member_year_recovery_rate": round(usable_days / total_days, 4) if total_days else 0.0,
    }


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-fetch a sample of one verdict bucket one symbol at a time.

    The headline number rests on ``no_data``, and ``no_data`` comes out of a bulk download where
    most tickers are dead. If that bulk call were quietly dropping live symbols the whole study
    would be wrong, so the claim gets checked against single-symbol requests.
    """
    summary_path = Path(args.cache_dir) / "probe_summary.json"
    if not summary_path.exists():
        print(f"run `probe` first: {summary_path} is missing", file=sys.stderr)
        return 2
    import random  # noqa: PLC0415

    import yfinance as yf  # noqa: PLC0415

    symbols = json.loads(summary_path.read_text(encoding="utf-8"))["symbols"]
    bucket = [s for s in symbols if s["verdict"] == args.bucket]
    random.seed(args.seed)
    sample = random.sample(bucket, min(args.sample, len(bucket)))

    disagreements: list[dict[str, Any]] = []
    for entry in sample:
        try:
            frame = yf.Ticker(entry["symbol"]).history(
                start="2008-01-01", end="2026-01-01", auto_adjust=False
            )
        except Exception as exc:  # noqa: BLE001 - a dead symbol must not kill the sweep
            print(f"  {entry['symbol']:6} error {type(exc).__name__}: {exc}")
            continue
        found = frame is not None and not frame.empty
        bars = len(frame) if found else 0
        if found != (args.bucket != "no_data"):
            disagreements.append({"symbol": entry["symbol"], "bars": bars})
            print(f"  {entry['symbol']:6} DISAGREES: single fetch returned {bars} bars")
        time.sleep(args.sleep)

    print(
        f"\nbucket={args.bucket}: {len(sample)} sampled, {len(disagreements)} disagreed with the "
        f"bulk download"
    )
    return 0


def _extract_close(frame: Any, symbol: str, batch_size: int, pd: Any) -> Any:
    """Pull one symbol's Close series out of whatever shape yfinance returned."""
    if frame is None or getattr(frame, "empty", True):
        return None
    try:
        if isinstance(frame.columns, pd.MultiIndex):
            if symbol not in frame.columns.get_level_values(0):
                return None
            closes = frame[symbol]["Close"]
        elif batch_size == 1:
            closes = frame["Close"]
        else:
            return None
    except (KeyError, IndexError):
        return None
    return closes.dropna()


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    default_cache = Path(tempfile.gettempdir()) / "swing-membership-cache"
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cache-dir", default=str(default_cache), help="where fetches are cached")
    parser.add_argument(
        "--offline", action="store_true", help="fail instead of hitting the network"
    )
    parser.add_argument("--json-out", default=None, help="write the machine-readable summary here")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="reconstruct membership CSVs from Wikipedia")
    build.add_argument("--dry-run", action="store_true", help="compute everything, write no CSVs")
    build.add_argument(
        "--force",
        action="store_true",
        help="overwrite membership CSVs even when they carry columns or rows this script does "
        "not manage (discards them)",
    )
    build.set_defaults(func=cmd_build)

    snapshots = subparsers.add_parser("snapshots", help="probe past revisions as a PIT source")
    snapshots.add_argument(
        "--granularity", choices=("yearly", "semiannual", "quarterly"), default="yearly"
    )
    snapshots.set_defaults(func=cmd_snapshots)

    crosscheck = subparsers.add_parser(
        "crosscheck", help="grade the change table against year-end rosters from past revisions"
    )
    crosscheck.set_defaults(func=cmd_crosscheck)

    probe = subparsers.add_parser("probe", help="test Yahoo recoverability of departed tickers")
    probe.add_argument("--batch", type=int, default=40, help="symbols per yfinance download")
    probe.add_argument("--sleep", type=float, default=1.5, help="seconds between batches")
    probe.add_argument("--limit", type=int, default=0, help="probe only the first N symbols")
    probe.add_argument("--refresh", action="store_true", help="ignore the cached Yahoo results")
    probe.set_defaults(func=cmd_probe)

    verify = subparsers.add_parser("verify", help="re-check one probe bucket with single fetches")
    verify.add_argument("--bucket", default="no_data", help="which verdict bucket to re-check")
    verify.add_argument("--sample", type=int, default=25, help="how many symbols to re-fetch")
    verify.add_argument("--seed", type=int, default=7, help="sampling seed")
    verify.add_argument("--sleep", type=float, default=0.5, help="seconds between symbols")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    Path(args.cache_dir).mkdir(parents=True, exist_ok=True)
    try:
        result: int = args.func(args)
    except SchemaConflict as exc:
        # A refusal is a designed outcome, not a crash: report it as one, without a traceback.
        print(f"\n{exc}\n", file=sys.stderr)
        return 3
    return result


if __name__ == "__main__":
    raise SystemExit(main())
