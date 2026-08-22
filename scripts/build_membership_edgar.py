#!/usr/bin/env python3
"""Point-in-time S&P index membership from SEC EDGAR fund filings.

``docs/survivorship.md`` §2.5 left one free source unexhausted: index-tracking ETFs are registered
funds, so they must file their complete holdings with the SEC, and those filings are dated,
primary-source, licence-clean rosters. This script turns that spot-check into coverage. It reads the
three iShares trackers that hold the indices this repo backtests

============  ===============  ============  ===================================================
index         tracker          series id     forms carrying a Schedule of Investments
============  ===============  ============  ===================================================
S&P 500       IVV              S000004310    ``N-Q`` 2006-2019, ``N-CSR``/``N-CSRS``, ``NPORT-P``
S&P 400       IJH              S000004307    same
S&P 600       IJR              S000004313    same
============  ===============  ============  ===================================================

turns each filing into a dated roster, diffs consecutive rosters into add/remove events, and merges
those events into ``src/swing/assets/universe/sp{500,400,600}-membership.csv`` beside the Wikipedia
dates that are already there.

Two properties of the result matter more than the coverage number:

*Dates from a diff are bounds, not events.* Filings are quarterly at best, so "the first roster this
symbol appears in is dated 2014-06-30" means *joined no later than 2014-06-30*, not *joined on*
2014-06-30. Every EDGAR-derived date is written with ``added_bound``/``removed_bound`` =
``no_later_than``; Wikipedia's dates keep ``exact``. Nothing merges a bound into a column that
claims to be exact without saying so in the adjacent column.

*Holdings are keyed by CUSIP or by name, never by ticker.* That crosswalk is the engineering problem
and it is solved in five tiers, each recorded per symbol so the weak ones can be discounted:

``cusip``   ``NPORT-P`` gives a CUSIP/ISIN per position; SEC's own fails-to-deliver files give
            CUSIP -> ticker for the same fortnight. Point-in-time on both sides, so a ticker that
            was reassigned later cannot leak backwards.
``nport``   The pre-2019 HTML schedules give a *name* and nothing else. Names seen in the
            CUSIP-keyed NPORT era carry their resolved ticker back by exact normalised equality.
``former``  EDGAR's record of what each company used to be called. Without it a rename reads as a
            departure plus a fresh arrival — see ``renames`` for what that is worth.
``sec``     SEC ``company_tickers.json``, exact normalised title equality. Current filers only.
``ftd``     Fails-to-deliver *descriptions*, matched on a 12-character normalised prefix and
            accepted only when exactly one ticker answers. Weakest tier; reported separately.

An unmatched holding is counted and reported, never guessed into a ticker.

Usage::

    uv run python scripts/build_membership_edgar.py filings
    uv run python scripts/build_membership_edgar.py snapshots
    uv run python scripts/build_membership_edgar.py renames
    uv run python scripts/build_membership_edgar.py merge --dry-run
    uv run python scripts/build_membership_edgar.py merge

Writes only to ``src/swing/assets/universe/*-membership.csv`` (``merge`` without ``--dry-run``) and
to a cache directory that defaults to a system temp path. Responses are cached gzipped, so a re-run
costs nothing; delete the cache to force a re-fetch. SEC is polled with the descriptive User-Agent
its access rules require and at a quarter of its documented 10 requests/second limit, with
exponential backoff on 403/429. Importing this module does nothing.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import re
import sys
import tempfile
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

import requests

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
UNIVERSE_DIR: Final = REPO_ROOT / "src" / "swing" / "assets" / "universe"

#: SEC requires a descriptive User-Agent carrying a contact address. Requests without one are
#: refused with 403, and the refusal is per-IP rather than per-request.
USER_AGENT: Final = "swingtrader research tflem10@gmail.com"
SEC: Final = "https://www.sec.gov"
#: SEC documents 10 requests/second. A quarter of that is polite and still fast enough.
MIN_REQUEST_INTERVAL: Final = 0.25
RETRYABLE_STATUS: Final = frozenset({403, 429, 500, 502, 503, 504})
MAX_RETRIES: Final = 5

#: The backtest window this study is about (``backtest.start`` in config.example.toml).
WINDOW_START: Final = dt.date(2010, 1, 1)
WINDOW_END: Final = dt.date(2025, 12, 31)

#: Sentinels shared with ``scripts/build_membership.py`` and with the membership CSVs.
UNKNOWN: Final = "unknown"
BOUND_EXACT: Final = "exact"
BOUND_NO_LATER: Final = "no_later_than"
SOURCE_WIKI: Final = "wikipedia"
SOURCE_EDGAR: Final = "edgar"

#: Additive, optional columns. The first four are the pre-existing schema and keep their meaning:
#: a consumer reading ``symbol,name,added,removed`` positionally is unaffected.
CSV_HEADER: Final = [
    "symbol",
    "name",
    "added",
    "removed",
    "added_bound",
    "removed_bound",
    "added_source",
    "removed_source",
]

#: Forms that carry a Schedule of Investments. ``NPORT-P`` is structured XML; the rest are HTML
#: reports covering every fund in the trust, from which one fund's section has to be cut out.
HOLDING_FORMS: Final = ("NPORT-P", "N-Q", "N-CSR", "N-CSRS")

ISHARES_CIK: Final = "1100663"


@dataclass(frozen=True)
class FundSpec:
    """One tracker: how to find its filings and how to spot its section in a trust report."""

    index: str
    etf: str
    series_id: str
    #: Any of these must appear in the fund name printed above a Schedule of Investments. The fund
    #: was renamed twice ("iShares S&P SmallCap 600 Index Fund" -> "iShares Core S&P Small-Cap").
    include: tuple[str, ...]
    #: ...and none of these, which is what separates the plain tracker from its Growth/Value/ESG
    #: siblings inside the same 30 MB document.
    exclude: tuple[str, ...]
    expected: int
    tolerance: int


FUNDS: Final[tuple[FundSpec, ...]] = (
    FundSpec(
        index="sp500",
        etf="IVV",
        series_id="S000004310",
        include=("S&P 500",),
        exclude=("GROWTH", "VALUE", "EQUAL", "ESG", "MINIMUM", "HEDGED", "SECTOR", "DIVIDEND"),
        expected=503,
        tolerance=40,
    ),
    FundSpec(
        index="sp400",
        etf="IJH",
        series_id="S000004307",
        include=("MIDCAP 400", "MID-CAP 400", "CORE S&P MID-CAP", "S&P MID-CAP"),
        exclude=("GROWTH", "VALUE", "EQUAL", "ESG", "MINIMUM", "HEDGED", "SECTOR", "DIVIDEND"),
        expected=400,
        tolerance=40,
    ),
    FundSpec(
        index="sp600",
        etf="IJR",
        series_id="S000004313",
        include=("SMALLCAP 600", "SMALL-CAP 600", "CORE S&P SMALL-CAP", "S&P SMALL-CAP"),
        exclude=("GROWTH", "VALUE", "EQUAL", "ESG", "MINIMUM", "HEDGED", "SECTOR", "DIVIDEND"),
        expected=600,
        tolerance=40,
    ),
)

FUND_BY_INDEX: Final = {spec.index: spec for spec in FUNDS}

#: A holding worth less than this share of its fund is dropped. Index weights bottom out around
#: 2 bp in the S&P 600; 0.1 bp is twenty times below that, so this only sweeps out rounding dust and
#: stray line items, never a constituent.
MIN_WEIGHT: Final = 0.00001

_ISO: Final = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MONTH_DAY_YEAR: Final = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})")
_NUMERIC_CELL: Final = re.compile(r"^\(?\$?\s*[\d,]+(?:\.\d+)?\)?$")
#: Trailing words that say what kind of company it is rather than which company it is.
_LEGAL_FORMS: Final = frozenset(
    {
        "CO",
        "COS",
        "CORP",
        "INC",
        "LTD",
        "PLC",
        "LP",
        "LLC",
        "SA",
        "NV",
        "AG",
        "AB",
        "ASA",
        "SE",
        "COMPANIES",
        "INTERNATIONAL",
        "HOLDINGS",
        "HOLDING",
        "GROUP",
    }
)
_FOOTNOTE_TAIL: Final = re.compile(r"\((?:[a-z]{1,3}|\d{1,2})\)\s*$")
_COMMON_STOCKS: Final = re.compile(r"^COMMON STOCKS\b(?!\s*\()", re.I)
_END_OF_STOCKS: Final = re.compile(r"^TOTAL (?:COMMON STOCKS|INVESTMENTS|LONG-TERM)", re.I)
_TICKER: Final = re.compile(r"^[A-Z][A-Z0-9]{0,4}$")
_CUSIP: Final = re.compile(r"^[0-9A-Z]{9}$")

#: Name normalisation. Legal-form suffixes are spelled a dozen ways across three sources, so they
#: are folded rather than trusted.
_SUFFIX_FOLD: Final[dict[str, str]] = {
    "CORPORATION": "CORP",
    "INCORPORATED": "INC",
    "COMPANY": "CO",
    "LIMITED": "LTD",
    "COMPANIES": "COS",
}
#: Tokens a fails-to-deliver description tacks on after the company name ("AAR CORP COM PAR $1.00").
#: Everything from the first of these onwards is security boilerplate, not identity.
_DESC_CUT: Final = frozenset(
    {
        "COM",
        "CL",
        "PAR",
        "SHS",
        "ORD",
        "UNIT",
        "UNITS",
        "NOTE",
        "NOTES",
        "PFD",
        "ADR",
        "ADS",
        "SPONSORED",
        "REIT",
        "USD",
        "STK",
        "NEW",
    }
)
#: How many normalised characters of a fails-to-deliver description must agree. The descriptions are
#: truncated at 30 raw characters, so an exact match is not available; 12 is long enough that a
#: collision is a real collision rather than a shared first word, and collisions are rejected.
_DESC_PREFIX: Final = 12


# --------------------------------------------------------------------------------------
# Cached, rate-limited SEC client
# --------------------------------------------------------------------------------------


class SecClient:
    """Every SEC request goes through here: on-disk gzip cache, rate limit, honest User-Agent."""

    def __init__(self, cache_dir: Path, *, offline: bool = False) -> None:
        self.cache_dir = cache_dir
        (self.cache_dir / "raw").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "parsed").mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.live_requests = 0
        self.blocked = False
        self._last = 0.0
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})

    def _cache_path(self, url: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", url)[-150:]
        return self.cache_dir / "raw" / f"{slug}.gz"

    def get(self, url: str) -> bytes:
        """Return the body of ``url``, from the gzip cache when it is already there."""
        path = self._cache_path(url)
        if path.exists():
            return gzip.decompress(path.read_bytes())
        if self.offline:
            raise RuntimeError(f"--offline set but not cached: {url}")
        body = self._fetch(url)
        path.write_bytes(gzip.compress(body, 6))
        return body

    def _fetch(self, url: str) -> bytes:
        delay = MIN_REQUEST_INTERVAL
        for attempt in range(MAX_RETRIES):
            wait = delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            response = self._session.get(url, timeout=180)
            self._last = time.monotonic()
            if response.status_code == 200:
                self.live_requests += 1
                return response.content
            if response.status_code not in RETRYABLE_STATUS or attempt == MAX_RETRIES - 1:
                if response.status_code in {403, 429}:
                    self.blocked = True
                raise RuntimeError(f"HTTP {response.status_code} from {url}")
            delay = min(delay * 4, 60.0)
            print(
                f"  HTTP {response.status_code} from SEC; backing off {delay:.0f}s",
                file=sys.stderr,
            )
        raise RuntimeError(f"gave up on {url}")

    def json(self, url: str) -> Any:
        return json.loads(self.get(url))

    # -- small parsed-artefact cache, so a 30 MB HTML report is walked once ------------------

    def cached_json(self, key: str, build: Any) -> Any:
        path = self.cache_dir / "parsed" / f"{key}.json.gz"
        if path.exists():
            return json.loads(gzip.decompress(path.read_bytes()))
        value = build()
        path.write_bytes(gzip.compress(json.dumps(value).encode("utf-8"), 6))
        return value


# --------------------------------------------------------------------------------------
# Filing discovery
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filing:
    form: str
    filed: str
    accession: str

    @property
    def folder(self) -> str:
        return self.accession.replace("-", "")


_ATOM: Final = "{http://www.w3.org/2005/Atom}"


def list_filings(client: SecClient, series_id: str, form: str) -> list[Filing]:
    """Every ``form`` filed under one fund series, oldest first.

    EDGAR's browse endpoint accepts a series id where it normally wants a CIK, which is the only
    free way to ask "what did *this fund* file" rather than "what did its 380-fund trust file".
    """
    import xml.etree.ElementTree as ET

    out: list[Filing] = []
    start = 0
    while True:
        url = (
            f"{SEC}/cgi-bin/browse-edgar?action=getcompany&CIK={series_id}&type={form}"
            f"&dateb=&owner=include&count=100&start={start}&output=atom"
        )
        root = ET.fromstring(client.get(url))
        entries = root.findall(f"{_ATOM}entry")
        for entry in entries:
            content = entry.find(f"{_ATOM}content")
            if content is None:
                continue

            def cell(tag: str, node: Any = content) -> str:
                found = node.find(f"{_ATOM}{tag}")
                return (found.text or "").strip() if found is not None else ""

            filed_form = cell("filing-type")
            if filed_form != form:  # browse-edgar's `type` is a prefix match: N-CSR matches N-CSRS
                continue
            out.append(
                Filing(
                    form=filed_form,
                    filed=cell("filing-date"),
                    accession=cell("accession-number"),
                )
            )
        if len(entries) < 100:
            break
        start += 100
        if start > 2000:
            break
    out.sort(key=lambda f: (f.filed, f.accession))
    return out


def primary_document(client: SecClient, filing: Filing) -> str:
    """URL of the document carrying the holdings: the XML for NPORT, the biggest HTML otherwise."""
    base = f"{SEC}/Archives/edgar/data/{ISHARES_CIK}/{filing.folder}"
    if filing.form == "NPORT-P":
        return f"{base}/primary_doc.xml"
    index = client.json(f"{base}/index.json")
    candidates = [
        (int(item.get("size") or 0), item["name"])
        for item in index["directory"]["item"]
        if item["name"].lower().endswith((".htm", ".html", ".txt"))
        and "index" not in item["name"].lower()
        and "cert" not in item["name"].lower()
    ]
    if not candidates:
        raise RuntimeError(f"no report document in {filing.accession}")
    return f"{base}/{max(candidates)[1]}"


# --------------------------------------------------------------------------------------
# Holdings extraction
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    """One line of a Schedule of Investments, before any ticker is attached."""

    name: str
    value: float
    cusip: str = ""
    isin: str = ""


@dataclass
class Snapshot:
    """One fund's roster on one date, with the identifier work already done."""

    index: str
    as_of: str
    form: str
    accession: str
    holdings: int
    #: ticker -> issuer name, both as the filing writes them.
    members: dict[str, str] = field(default_factory=dict)
    #: how each ticker was resolved, for the honesty column in the report.
    tiers: dict[str, str] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


_NPORT: Final = "{http://www.sec.gov/edgar/nport}"


def parse_nport(raw: bytes) -> tuple[str, list[Holding]]:
    """``NPORT-P`` primary document -> (as-of date, long equity positions)."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(raw)
    as_of = ""
    node = root.find(f".//{_NPORT}genInfo/{_NPORT}repPdDate")
    if node is not None and node.text:
        as_of = node.text.strip()
    holdings: list[Holding] = []
    for sec in root.iter(f"{_NPORT}invstOrSec"):
        fields = {child.tag.replace(_NPORT, ""): (child.text or "").strip() for child in sec}
        if fields.get("assetCat") != "EC" or fields.get("payoffProfile", "Long") != "Long":
            continue
        isin = ""
        identifiers = sec.find(f"{_NPORT}identifiers")
        if identifiers is not None:
            element = identifiers.find(f"{_NPORT}isin")
            if element is not None:
                isin = (element.get("value") or "").strip().upper()
        try:
            value = float(fields.get("valUSD") or 0.0)
        except ValueError:
            value = 0.0
        holdings.append(
            Holding(
                name=fields.get("name", "").strip(),
                value=value,
                cusip=fields.get("cusip", "").strip().upper(),
                isin=isin,
            )
        )
    return as_of, holdings


class _TableRows(HTMLParser):
    """Flatten a filing's HTML into blocks: one per ``<tr>``, one per free-standing ``<p>``.

    Both are needed. Reports from 2019 onwards put the "Schedule of Investments <date>" banner and
    the fund's name in cells of a little two-column table; reports before that put them in bare
    paragraphs between the tables. A table-only reader finds nothing at all in the older half of
    the archive, which is the half worth having.

    ``<sup>`` is dropped throughout so footnote markers and the iShares registered-trademark sign do
    not end up glued to a company name.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._para: list[str] | None = None
        self._suppress = 0

    def _flush_para(self) -> None:
        if self._para is None:
            return
        text = re.sub(r"\s+", " ", "".join(self._para)).strip()
        if text:
            self.rows.append([text])
        self._para = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name == "tr":
            self._flush_para()
            self._row = []
        elif name in {"td", "th"}:
            if self._row is None:
                self._row = []
            self._cell = []
        elif name == "sup":
            self._suppress += 1
        elif name == "table":
            self._flush_para()
        elif name == "br":
            if self._cell is not None:
                self._cell.append(" ")
        elif name == "p":
            if self._cell is not None:
                self._cell.append(" ")
            else:
                self._flush_para()
                self._para = []

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif name == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif name == "p" and self._cell is None:
            self._flush_para()
        elif name == "sup" and self._suppress:
            self._suppress -= 1

    def handle_data(self, data: str) -> None:
        if self._suppress:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._para is not None:
            self._para.append(data)


def _fund_label(row: list[str]) -> str:
    for cell in row:
        if cell[:7].upper() == "ISHARES":
            return cell.split("(Percentages")[0].strip()
    return ""


def _banner(rows: list[list[str]], position: int) -> tuple[str, str]:
    """``(fund label, as-of date)`` for the Schedule of Investments banner at ``position``.

    Post-2019 reports keep both in the banner row itself; pre-2019 reports put the fund name and
    the date in the two paragraphs immediately after it. Looking a few blocks ahead covers both
    without having to know which era a document belongs to.
    """
    label = _fund_label(rows[position])
    as_of = parse_date(" ".join(rows[position]))
    for row in rows[position + 1 : position + 6]:
        if not label:
            label = _fund_label(row)
        if not as_of:
            as_of = parse_date(" ".join(row))
        if label and as_of:
            break
    return label, as_of


def _matches(spec: FundSpec, label: str) -> bool:
    upper = label.upper()
    if any(bad in upper for bad in spec.exclude):
        return False
    return any(good in upper for good in spec.include)


def parse_date(text: str) -> str:
    iso = _ISO.search(text)
    if iso:
        return iso.group(0)
    match = _MONTH_DAY_YEAR.search(text)
    if match is None:
        return ""
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            parsed = dt.datetime.strptime(
                f"{match.group(1)} {match.group(2)} {match.group(3)}", fmt
            ).date()
        except ValueError:
            continue
        return parsed.isoformat()
    return ""


def _section_holdings(rows: list[list[str]], start: int, end: int) -> list[Holding]:
    """The Common Stocks block of one already-located Schedule of Investments section."""
    holdings: list[Holding] = []
    inside = False
    for row in rows[start:end]:
        cells = [cell for cell in row if cell.strip() and cell.strip() not in {"\xa0", "$"}]
        if not cells:
            continue
        first = cells[0]
        # "Common Stocks" in 2019+, "COMMON STOCKS — 100.00%" before that.
        if len(cells) == 1 and _COMMON_STOCKS.match(first):
            inside = True
            continue
        if _END_OF_STOCKS.match(first):
            inside = False
            continue
        if not inside or "—" in first or first.endswith("%"):
            continue
        numbers = [cell for cell in cells[1:] if _NUMERIC_CELL.match(cell.replace("$", "").strip())]
        # A holding line is name + shares + value. The affiliate-transaction table that follows the
        # schedule repeats every name with five to eight numbers, so the count is what separates
        # them, and getting it wrong would double every roster.
        if len(numbers) != 2:
            continue
        try:
            value = float(numbers[1].replace(",", "").replace("$", "").strip("()"))
        except ValueError:
            value = 0.0
        holdings.append(Holding(name=first, value=value))
    return holdings


def parse_html_schedules(
    raw: bytes, specs: list[FundSpec]
) -> dict[str, tuple[str, list[Holding], str]]:
    """Cut several funds' Schedules of Investments out of one whole-trust report.

    Every ``N-Q``/``N-CSR``/``N-CSRS`` document covers the entire 380-fund trust, so it is walked
    once and sliced three ways rather than downloaded and re-parsed per index.

    Per fund the result is ``(as-of date, holdings, note)``; ``note`` is empty on success and says
    what went wrong otherwise. A section that cannot be located *unambiguously* is refused rather
    than guessed at — picking up the Growth sibling by mistake would silently corrupt every date
    derived from that roster.
    """
    parser = _TableRows()
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    rows = parser.rows

    headers: list[tuple[int, str, str, bool]] = []
    for position, row in enumerate(rows):
        joined = " ".join(row)
        if "Schedule of Investments" not in joined:
            continue
        label, when = _banner(rows, position)
        headers.append((position, label, when, "continued" in joined.lower()))

    out: dict[str, tuple[str, list[Holding], str]] = {}
    for spec in specs:
        openings = [
            (position, label, when)
            for position, label, when, cont in headers
            if not cont and _matches(spec, label)
        ]
        if not openings:
            out[spec.index] = ("", [], "no section for this fund")
            continue
        labels = {label.upper() for _, label, _when in openings}
        if len(labels) > 1:
            out[spec.index] = ("", [], f"ambiguous section: {sorted(labels)}")
            continue

        # An annual report carries the same fund twice: a "Summary Schedule of Investments" listing
        # only the fifty largest positions plus a line called "Other securities", and then the real
        # thing. Taking whichever extraction yields more names picks the real one without having to
        # match on a banner wording that has changed more than once.
        best: tuple[str, list[Holding]] = ("", [])
        for start, _label, as_of in openings:
            end = len(rows)
            for position, other, _when, _cont in headers:
                if position > start and other and other.upper() not in labels:
                    end = position
                    break
            holdings = _section_holdings(rows, start, end)
            if len(holdings) > len(best[1]):
                best = (as_of, holdings)
        note = "" if best[1] else "section found but no Common Stocks block"
        out[spec.index] = (best[0] or openings[0][2], best[1], note)
    return out


# --------------------------------------------------------------------------------------
# Identifier crosswalks
# --------------------------------------------------------------------------------------


def to_yahoo_symbol(symbol: str) -> str:
    """Mirrors ``swing.universe.to_yahoo_symbol``, plus the ``/`` NSCC uses for share classes."""
    return symbol.strip().upper().replace("/", "-").replace(".", "-").replace(" ", "")


@lru_cache(maxsize=1)
def share_class_aliases() -> dict[str, str]:
    """``BRKB -> BRK-B``, learned from the universe files rather than hand-written.

    NSCC — and therefore the fails-to-deliver files — writes Berkshire class B as ``BRKB``, while
    this repo writes ``BRK-B``. Four symbols across the three indices are affected, and left alone
    each one would both fail to fill the row it belongs to and invent a second row beside it. The
    map is derived from the committed snapshots and the membership CSVs, so it stays correct as
    those change instead of rotting in a literal.
    """
    aliases: dict[str, str] = {}
    for index in ("sp500", "sp400", "sp600"):
        for path in (UNIVERSE_DIR / f"{index}.csv", UNIVERSE_DIR / f"{index}-membership.csv"):
            if not path.exists():
                continue
            with path.open(newline="", encoding="utf-8") as handle:
                for record in csv.DictReader(handle):
                    symbol = (record.get("symbol") or "").strip().upper()
                    if "-" in symbol:
                        aliases.setdefault(symbol.replace("-", ""), symbol)
    return aliases


def canonical_symbol(symbol: str) -> str:
    """A resolved ticker, spelled the way the membership CSVs spell it."""
    plain = to_yahoo_symbol(symbol)
    return share_class_aliases().get(plain, plain)


def strip_class(key: str) -> str:
    """Drop a trailing share-class qualifier from an already-normalised name.

    The HTML schedules write "Moog Inc., Class A"; ``NPORT-P`` writes "Moog Inc" and leaves the
    class to the CUSIP. Comparing the two needs a class-free key — but a class-free key is
    ambiguous for a genuinely dual-class issuer, so callers only accept it when exactly one ticker
    answers to it.
    """
    pattern = r"\s+(?:CLASS|CL|SERIES|SER)\s+[A-Z0-9]{1,2}(?:\s+(?:NVS|VTG))?$"
    return re.sub(pattern, "", key).strip()


def normalise_name(name: str, *, cut_boilerplate: bool = False) -> str:
    """Fold an issuer name to a comparison key. Deterministic, and shared by all four tiers."""
    # Older schedules glue their footnote markers straight onto the name — "AeroVironment
    # Inc.(a)(b)" — with no <sup> to strip. Lower-case-in-parentheses is the marker's signature and
    # spares the legitimate "(The)" and "(BEL)" that also show up at the end of a name.
    text = name.strip()
    while True:
        shortened = _FOOTNOTE_TAIL.sub("", text).rstrip()
        if shortened == text:
            break
        text = shortened
    text = text.upper().replace("\xa0", " ")
    text = re.sub(r"\(THE\)|\bTHE\b", " ", text)
    text = re.sub(r"/[A-Z]{2,3}\b", " ", text)  # BlackRock's "/TX", "/IN" domicile tags
    text = text.replace("&", " AND ")
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    tokens = [_SUFFIX_FOLD.get(word, word) for word in text.split()]
    if cut_boilerplate:
        kept: list[str] = []
        for word in tokens:
            # Never on the first token: "New Jersey Resources" would become nothing at all.
            if kept and word in _DESC_CUT:
                break
            kept.append(word)
        tokens = kept
    return " ".join(tokens)


def strip_legal_form(key: str) -> str:
    """Drop trailing legal-form words: "BEMIS CO INC" and "BEMIS INC" become "BEMIS"."""
    words = key.split()
    while len(words) > 1 and words[-1] in _LEGAL_FORMS:
        words.pop()
    return " ".join(words)


def comparison_keys(name: str, *, cut_boilerplate: bool = False) -> tuple[str, ...]:
    """Keys for one issuer name, most specific first, all with spaces removed.

    Three things differ between sources for what is plainly the same company, and each costs
    matches if it is not folded away:

    *Punctuation.* "D.R. Horton", "DR Horton" and "D R HORTON" are one company in three filings, so
    word boundaries are dropped entirely. A collision still has to agree letter for letter.

    *Share class.* The HTML schedules write "Moog Inc., Class A" where ``NPORT-P`` writes "Moog Inc"
    and leaves the class to the CUSIP.

    *Legal form.* The fund writes "Monsanto Co."; the fails-to-deliver description says "MONSANTO
    COM", which cleans to "MONSANTO". Neither is wrong and neither is a prefix of the other.

    Callers try the keys in order and take the first that exactly one ticker answers to. Every
    variant of every company goes into the lookup tables, so a loose key that two companies share
    collapses to nothing rather than to whichever was inserted first.
    """
    normalised = normalise_name(name, cut_boilerplate=cut_boilerplate)
    base = strip_class(normalised)
    ordered: list[str] = []
    for candidate in (normalised, base, strip_legal_form(base)):
        compact = candidate.replace(" ", "")
        if compact and compact not in ordered:
            ordered.append(compact)
    return tuple(ordered)


class FailsToDeliver:
    """SEC's fails-to-deliver files, read as the CUSIP/name -> ticker crosswalk they happen to be.

    They are published twice a month from 2009-07 onwards as ``SETTLEMENT
    DATE|CUSIP|SYMBOL|QUANTITY|DESCRIPTION|PRICE``. Nothing else free and SEC-published maps a CUSIP
    to a ticker, and because each file is dated, the mapping is point-in-time: PJC and PIPR are both
    correct answers for 724078100, in 2019 and 2020 respectively.
    """

    def __init__(self, client: SecClient) -> None:
        self.client = client
        self._index: dict[tuple[str, str], str] | None = None
        self._loaded: set[str] = set()
        #: cusip -> {yyyymm: ticker}
        self.by_cusip: dict[str, dict[str, str]] = defaultdict(dict)
        #: 12-char normalised description prefix -> tickers ever seen under it
        self.by_prefix: dict[str, set[str]] = defaultdict(set)
        #: whole normalised description -> tickers, for names too short to have a 12-char prefix
        self.by_name: dict[str, set[str]] = defaultdict(set)

    def _links(self) -> dict[tuple[str, str], str]:
        if self._index is None:
            page = self.client.get(f"{SEC}/data/foiadocsfailsdatahtm").decode("utf-8", "replace")
            found = re.findall(r'href="([^"]*cnsfails(\d{6})([ab])\.zip)"', page)
            self._index = {(month, half): href for href, month, half in found}
        return self._index

    def _derive(self, month: str) -> dict[str, Any]:
        """Both halves of one ``YYYYMM``, reduced to the three lookups. Missing halves are fine."""
        cusips: dict[str, str] = {}
        prefixes: dict[str, set[str]] = defaultdict(set)
        names: dict[str, set[str]] = defaultdict(set)
        for half in ("a", "b"):
            href = self._links().get((month, half))
            if href is None:
                continue
            blob = self.client.get(SEC + href)
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                text = archive.read(archive.namelist()[0]).decode("latin-1")
            for line in text.splitlines()[1:]:
                parts = line.split("|")
                if len(parts) < 6:
                    continue
                cusip = parts[1].strip().upper()
                symbol = parts[2].strip().upper()
                if not _TICKER.match(symbol):
                    continue
                if _CUSIP.match(cusip):
                    cusips[cusip] = symbol
                for key in comparison_keys(parts[4], cut_boilerplate=True):
                    if len(key) >= _DESC_PREFIX:
                        prefixes[key[:_DESC_PREFIX]].add(symbol)
                    else:
                        names[key].add(symbol)
        return {
            "cusip": cusips,
            "prefix": {k: sorted(v) for k, v in prefixes.items()},
            "name": {k: sorted(v) for k, v in names.items()},
        }

    def load_month(self, month: str) -> None:
        """One ``YYYYMM`` of the crosswalk.

        The reduction is cached, not just the download: a fortnight's file is 50,000 lines and
        normalising every description on every run is what made a re-run cost minutes instead of
        seconds.
        """
        if month in self._loaded:
            return
        self._loaded.add(month)
        derived = self.client.cached_json(f"ftd_{month}", lambda: self._derive(month))
        for cusip, symbol in derived["cusip"].items():
            self.by_cusip[cusip][month] = symbol
        for prefix, symbols in derived["prefix"].items():
            self.by_prefix[prefix].update(symbols)
        for name, symbols in derived["name"].items():
            self.by_name[name].update(symbols)

    def ticker_for_cusip(self, cusip: str, month: str) -> str:
        """The ticker this CUSIP carried nearest in time to ``month``."""
        seen = self.by_cusip.get(cusip)
        if not seen:
            return ""
        best = min(seen, key=lambda observed: abs(int(observed) - int(month)))
        return seen[best]

    def ticker_for_name(self, name: str) -> tuple[str, bool]:
        """``(ticker, ambiguous)`` from a description; ``("", False)`` when nothing matches.

        Descriptions are cut off at 30 raw characters, so long names can only be compared on a
        prefix and short ones only in full. Both are exact rules with a uniqueness test, and a
        prefix shared by two tickers is reported as ambiguous rather than resolved to either.
        """
        ambiguous = False
        for key in comparison_keys(name, cut_boilerplate=True):
            found = (
                self.by_prefix.get(key[:_DESC_PREFIX])
                if len(key) >= _DESC_PREFIX
                else self.by_name.get(key)
            )
            if not found:
                continue
            if len(found) > 1:
                ambiguous = True
                continue
            return next(iter(found)), False
        return "", ambiguous


def _collapse(table: dict[str, set[str]]) -> dict[str, str]:
    """Keep only the names one ticker answers to. A name with two answers is a gap, not a guess."""
    return {name: next(iter(t)) for name, t in table.items() if len(t) == 1 and name}


class Resolver:
    """Holding -> ticker, in four tiers, refusing to answer rather than guessing."""

    def __init__(self, client: SecClient, ftd: FailsToDeliver) -> None:
        self.ftd = ftd
        payload = client.json(f"{SEC}/files/company_tickers.json")
        titles: dict[str, set[str]] = defaultdict(set)
        self.cik_of: dict[str, str] = {}
        for record in payload.values():
            ticker = str(record["ticker"]).upper()
            for key in comparison_keys(record["title"]):
                titles[key].add(ticker)
            self.cik_of.setdefault(canonical_symbol(ticker), f"{int(record['cik_str']):010d}")
        self.sec_titles = _collapse(titles)
        self.former_names: dict[str, str] = {}
        #: filled in by the NPORT pass; issuer name as BlackRock writes it -> ticker
        self._nport: dict[str, set[str]] = defaultdict(set)
        self.nport_names: dict[str, str] = {}

    def learn(self, name: str, ticker: str) -> None:
        for key in comparison_keys(name):
            self._nport[key].add(ticker)

    def learn_former_names(self, client: SecClient, tickers: set[str]) -> int:
        """Ask EDGAR what each of today's members used to be called.

        This is the fix for the single worst failure mode of a name-keyed diff. Apple's 2006
        schedule says "Apple Computer Inc."; nothing in a 2019-vintage crosswalk answers to that, so
        the 2006 roster looks like a roster without Apple in it, and the diff cheerfully reports
        that Apple joined the S&P 500 in 2007. EDGAR's company record carries the former names with
        the dates they were dropped, which turns the whole class of error into an exact lookup.

        The names are only registered where they do not collide with a *different* company's
        current name, and the tier is reported separately.
        """
        collected: dict[str, set[str]] = defaultdict(set)
        pending = sorted({t for t in tickers if t in self.cik_of})
        for position, ticker in enumerate(pending, start=1):
            if position % 250 == 0:
                print(f"  former names {position}/{len(pending)} ...", flush=True)
            url = (
                f"{SEC}/cgi-bin/browse-edgar?action=getcompany&CIK={self.cik_of[ticker]}"
                "&type=10-K&dateb=&owner=include&count=1&output=atom"
            )
            try:
                page = client.get(url).decode("utf-8", "replace")
            except RuntimeError:
                continue
            head = page[: page.find("</company-info>")]
            for name in re.findall(r"<name>(.*?)</name>", head, re.S):
                for key in comparison_keys(name):
                    collected[key].add(ticker)
        resolved = _collapse(collected)
        self.former_names = {
            key: ticker
            for key, ticker in resolved.items()
            if self.sec_titles.get(key, ticker) == ticker
        }
        return len(self.former_names)

    def finalise(self) -> None:
        """Close the learning phase. Called once every NPORT filing has been seen."""
        self.nport_names = _collapse(self._nport)

    def resolve(self, holding: Holding, month: str) -> tuple[str, str]:
        """``(ticker, tier)``; tier is ``""`` when nothing matched, ``"ambiguous"`` on a clash.

        A dual-class issuer is one name in ``NPORT-P`` and two in the HTML schedules, so the
        class-free key can legitimately have two answers; those entries are dropped rather than
        resolved to whichever class came first.
        """
        for candidate in (holding.cusip, holding.isin[2:11] if len(holding.isin) == 12 else ""):
            if candidate and _CUSIP.match(candidate) and candidate != "000000000":
                ticker = self.ftd.ticker_for_cusip(candidate, month)
                if ticker:
                    return canonical_symbol(ticker), "cusip"
        keys = comparison_keys(holding.name)
        for tier, table in (
            ("nport", self.nport_names),
            ("former", self.former_names),
            ("sec", self.sec_titles),
        ):
            found = next((table[key] for key in keys if key in table), "")
            if found:
                return canonical_symbol(found), tier
        ticker, ambiguous = self.ftd.ticker_for_name(holding.name)
        if ticker:
            return canonical_symbol(ticker), "ftd"
        return "", "ambiguous" if ambiguous else ""


# --------------------------------------------------------------------------------------
# Snapshot construction
# --------------------------------------------------------------------------------------


def collect_filings(client: SecClient, spec: FundSpec) -> list[Filing]:
    filings: list[Filing] = []
    for form in HOLDING_FORMS:
        filings.extend(list_filings(client, spec.series_id, form))
    filings.sort(key=lambda f: (f.filed, f.accession))
    return filings


def _pack(as_of: str, note: str, holdings: list[Holding]) -> dict[str, Any]:
    total = sum(h.value for h in holdings) or 1.0
    kept = [h for h in holdings if h.value / total >= MIN_WEIGHT]
    return {
        "as_of": as_of,
        "note": note,
        "holdings": [
            {"name": h.name, "value": h.value, "cusip": h.cusip, "isin": h.isin} for h in kept
        ],
    }


def raw_snapshots(
    client: SecClient, filing: Filing, specs: list[FundSpec]
) -> dict[str, dict[str, Any]]:
    """One filing -> ``{index: {as_of, holdings, note}}``, cached after the first walk.

    HTML reports cover the whole trust, so all the requested funds come out of a single parse; the
    parsed result is cached, which is what makes a re-run free rather than a ten-minute replay.
    """

    def build() -> dict[str, dict[str, Any]]:
        url = primary_document(client, filing)
        raw = client.get(url)
        if filing.form == "NPORT-P":
            as_of, holdings = parse_nport(raw)
            note = "" if holdings else "no long equity positions"
            return {specs[0].index: _pack(as_of, note, holdings)}
        parsed = parse_html_schedules(raw, specs)
        return {
            index: _pack(as_of, note, holdings) for index, (as_of, holdings, note) in parsed.items()
        }

    key = f"{filing.form.replace('/', '-')}_{filing.accession}"
    return client.cached_json(key, build)


def read_all(client: SecClient, *, verbose: bool) -> dict[str, list[tuple[Filing, dict[str, Any]]]]:
    """Every holdings filing of all three trackers, parsed, grouped by index."""
    wanted: dict[str, tuple[Filing, list[FundSpec]]] = {}
    for spec in FUNDS:
        for filing in collect_filings(client, spec):
            existing = wanted.get(filing.accession)
            if existing is None:
                wanted[filing.accession] = (filing, [spec])
            elif spec not in existing[1]:
                existing[1].append(spec)

    out: dict[str, list[tuple[Filing, dict[str, Any]]]] = defaultdict(list)
    order = sorted(wanted.values(), key=lambda item: (item[0].filed, item[0].accession))
    for position, (filing, specs) in enumerate(order, start=1):
        if verbose:
            print(f"  [{position}/{len(order)}] {filing.form:8s} {filing.filed}", flush=True)
        for index, record in raw_snapshots(client, filing, specs).items():
            out[index].append((filing, record))
    return out


def learn_from_nport(
    ftd: FailsToDeliver, resolver: Resolver, parsed: list[tuple[Filing, dict[str, Any]]]
) -> None:
    """Teach the resolver every issuer name whose ticker a CUSIP already settles.

    The NPORT era is CUSIP-keyed and therefore self-sufficient; walking all of it first is what
    lets the pre-2019 HTML era — which has no identifier, only a name — inherit an issuer-name
    crosswalk built from the same filer's own wording rather than from a fuzzy guess.
    """
    # Load the fails-to-deliver fortnights that sit alongside *every* roster, not just the
    # CUSIP-keyed ones. The pre-2019 rosters have no identifier at all, so their only chance of a
    # ticker is a description written while the company was still trading; matching a 2011 roster
    # against 2019 descriptions finds only the survivors, which is precisely the wrong sample.
    for _filing, record in parsed:
        if record["as_of"] and record["as_of"] >= "2009-07":
            ftd.load_month(record["as_of"][:7].replace("-", ""))
    nport = [(f, r) for f, r in parsed if f.form == "NPORT-P" and r["as_of"]]
    for _filing, record in nport:
        month = record["as_of"][:7].replace("-", "")
        for item in record["holdings"]:
            for candidate in (item["cusip"], item["isin"][2:11] if item["isin"] else ""):
                if not candidate or not _CUSIP.match(candidate) or candidate == "000000000":
                    continue
                ticker = ftd.ticker_for_cusip(candidate, month)
                if ticker:
                    resolver.learn(item["name"], canonical_symbol(ticker))
                    break


def build_snapshots(
    resolver: Resolver,
    spec: FundSpec,
    parsed: list[tuple[Filing, dict[str, Any]]],
    *,
    verbose: bool,
) -> list[Snapshot]:
    """One fund's filings, in date order, with tickers attached."""
    snapshots: list[Snapshot] = []
    for filing, record in parsed:
        if not record["as_of"] or not record["holdings"]:
            if verbose:
                note = record["note"] or "empty"
                print(f"  skip {spec.index} {filing.form:8s} {filing.filed}: {note}")
            continue
        month = record["as_of"][:7].replace("-", "")
        snapshot = Snapshot(
            index=spec.index,
            as_of=record["as_of"],
            form=filing.form,
            accession=filing.accession,
            holdings=len(record["holdings"]),
        )
        for item in record["holdings"]:
            holding = Holding(item["name"], item["value"], item["cusip"], item["isin"])
            ticker, tier = resolver.resolve(holding, month)
            if not ticker:
                (snapshot.ambiguous if tier == "ambiguous" else snapshot.unmatched).append(
                    holding.name
                )
                continue
            snapshot.members.setdefault(ticker, holding.name)
            snapshot.tiers.setdefault(ticker, tier)
        snapshots.append(snapshot)

    snapshots.sort(key=lambda s: (s.as_of, s.accession))
    deduped: list[Snapshot] = []
    for snapshot in snapshots:
        if deduped and deduped[-1].as_of == snapshot.as_of:
            if len(snapshot.members) > len(deduped[-1].members):
                deduped[-1] = snapshot
            continue
        deduped.append(snapshot)
    return deduped


def plausible(spec: FundSpec, snapshot: Snapshot) -> bool:
    return abs(snapshot.holdings - spec.expected) <= spec.tolerance


# --------------------------------------------------------------------------------------
# Snapshots -> bounded membership events
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A membership interval as a diff can know it: both ends bracketed, never pinpointed."""

    symbol: str
    name: str
    #: last roster before the symbol appeared; ``""`` if it was in the first roster
    join_after: str
    #: first roster the symbol appears in
    join_by: str
    #: last roster the symbol appears in
    last_seen: str
    #: first roster it is decisively missing from; ``""`` when no roster settles the question
    left_by: str
    #: is ``last_seen`` the newest roster there is?
    is_final: bool = False


def decisive_flags(snapshots: list[Snapshot], threshold: float) -> list[bool]:
    """Which rosters are complete enough for *absence* to mean anything.

    "Symbol S is missing from the 2011 roster" is only evidence that S was not a member if a
    membership would have been recognised. In the pre-2019 era it often would not have been: the
    filings name issuers and nothing else, so a name the crosswalk cannot place is indistinguishable
    from a name that is not there. A roster where too many holdings went unresolved can still
    witness presence, but it cannot witness absence, and treating it otherwise would manufacture
    join dates out of crosswalk failures.
    """
    return [(len(s.members) / s.holdings if s.holdings else 0.0) >= threshold for s in snapshots]


def spans_from(snapshots: list[Snapshot], decisive: list[bool]) -> dict[str, list[Span]]:
    """Diff consecutive rosters into per-symbol spans, bracketed by what the rosters can prove."""
    dates = [s.as_of for s in snapshots]
    present: dict[str, list[int]] = defaultdict(list)
    names: dict[str, str] = {}
    for position, snapshot in enumerate(snapshots):
        for symbol, name in snapshot.members.items():
            present[symbol].append(position)
            names.setdefault(symbol, name)

    out: dict[str, list[Span]] = {}
    for symbol, positions in present.items():
        seen = set(positions)

        runs: list[list[int]] = []
        for position in positions:
            if runs and position == runs[-1][1] + 1:
                runs[-1][1] = position
            else:
                runs.append([position, position])

        # A gap with no decisive roster in it is not a departure, it is a crosswalk outage; the two
        # runs on either side are one membership and get merged.
        merged: list[list[int]] = []
        for run in runs:
            if merged and not any(
                decisive[j] for j in range(merged[-1][1] + 1, run[0]) if j not in seen
            ):
                merged[-1][1] = run[1]
            else:
                merged.append(run)

        spans: list[Span] = []
        for index, (first, last) in enumerate(merged):
            floor = merged[index - 1][1] if index else -1
            join_after = ""
            for j in range(first - 1, floor, -1):
                if j not in seen and decisive[j]:
                    join_after = dates[j]
                    break
            ceiling = merged[index + 1][0] if index + 1 < len(merged) else len(dates)
            left_by = ""
            for j in range(last + 1, ceiling):
                if j not in seen and decisive[j]:
                    left_by = dates[j]
                    break
            spans.append(
                Span(
                    symbol=symbol,
                    name=names[symbol],
                    join_after=join_after,
                    join_by=dates[first],
                    last_seen=dates[last],
                    left_by=left_by,
                    is_final=last == len(dates) - 1,
                )
            )
        out[symbol] = spans
    return out


# --------------------------------------------------------------------------------------
# The existing CSVs
# --------------------------------------------------------------------------------------


@dataclass
class Row:
    symbol: str
    name: str
    added: str
    removed: str
    added_bound: str = ""
    removed_bound: str = ""
    added_source: str = ""
    removed_source: str = ""

    @property
    def has_join_date(self) -> bool:
        return bool(_ISO.fullmatch(self.added))

    @property
    def is_current(self) -> bool:
        return self.removed == ""


def read_rows(path: Path) -> list[Row]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return [
            Row(
                symbol=record["symbol"],
                name=record.get("name", ""),
                added=record.get("added", "") or "",
                removed=record.get("removed", "") or "",
                added_bound=record.get("added_bound", "") or "",
                removed_bound=record.get("removed_bound", "") or "",
                added_source=record.get("added_source", "") or "",
                removed_source=record.get("removed_source", "") or "",
            )
            for record in reader
        ]


def write_rows(path: Path, rows: list[Row]) -> None:
    ordered = sorted(rows, key=lambda r: (r.symbol, r.added or "0000"))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        for row in ordered:
            writer.writerow(
                [
                    row.symbol,
                    row.name,
                    row.added,
                    row.removed,
                    row.added_bound,
                    row.removed_bound,
                    row.added_source,
                    row.removed_source,
                ]
            )


def backfill_provenance(rows: list[Row]) -> None:
    """Label what is already there before anything new is merged in."""
    for row in rows:
        if row.has_join_date and not row.added_source:
            row.added_bound = BOUND_EXACT
            row.added_source = SOURCE_WIKI
        if _ISO.fullmatch(row.removed) and not row.removed_source:
            row.removed_bound = BOUND_EXACT
            row.removed_source = SOURCE_WIKI


# --------------------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------------------


def _overlap_days(row: Row, span: Span) -> int:
    """How much of one CSV interval and one EDGAR span describe the same membership.

    Zero means they cannot be the same stint. Anything above zero is a candidate, and the caller
    takes the largest — matching on merely "overlaps at all" would let the second stint of a company
    that left and rejoined match the row belonging to the first.
    """
    row_start = row.added if _ISO.fullmatch(row.added) else "0001-01-01"
    row_end = row.removed if _ISO.fullmatch(row.removed) else "9999-12-31"
    span_start = span.join_after or "0001-01-01"
    span_end = span.left_by or "9999-12-31"
    low = max(row_start, span_start)
    high = min(row_end, span_end)
    if low > high:
        return 0
    return max((dt.date.fromisoformat(high) - dt.date.fromisoformat(low)).days, 1)


@dataclass
class MergeStats:
    index: str
    join_before: int = 0
    join_after: int = 0
    current_members: int = 0
    removal_before: int = 0
    removal_after: int = 0
    rows_before: int = 0
    rows_after: int = 0
    new_rows: int = 0
    agree_join: int = 0
    disagree_join: int = 0
    untestable_join: int = 0
    #: wiki date outside the strict bracket but within --bracket-tolerance days of it
    near_join: int = 0
    agree_removal: int = 0
    disagree_removal: int = 0
    near_removal: int = 0
    lookahead_years_before: float = 0.0
    lookahead_years_after: float = 0.0
    #: rows the CSV calls current that EDGAR's newest roster does not contain
    current_but_absent: int = 0
    disagreements: list[str] = field(default_factory=list)


def _lookahead_years(rows: list[Row]) -> float:
    """Member-years each current member is backtested as a member before it actually joined."""
    total = 0
    for row in rows:
        if not row.is_current or not row.has_join_date:
            continue
        joined = dt.date.fromisoformat(row.added)
        if joined <= WINDOW_START:
            continue
        total += (min(joined, WINDOW_END) - WINDOW_START).days
    return total / 365.25


def union_lookahead_years(by_index: dict[str, list[Row]]) -> tuple[float, int]:
    """Look-ahead member-years counting a symbol's stints across *all* three indices as one.

    Counting per index scores a company that moved from the S&P 500 to the S&P 400 as a brand-new
    arrival, when in truth it never left the tradable universe. The union rule — earliest known
    stint start across every index — is the one that matches how the backtest actually selects
    names, and it is the figure ``docs/survivorship.md`` §5.2 now quotes.
    """
    earliest: dict[str, dt.date] = {}
    current: set[str] = set()
    for rows in by_index.values():
        for row in rows:
            if row.is_current:
                current.add(row.symbol)
            if row.has_join_date:
                joined = dt.date.fromisoformat(row.added)
            elif row.added == "":
                joined = WINDOW_START
            else:
                continue
            if row.symbol not in earliest or joined < earliest[row.symbol]:
                earliest[row.symbol] = joined
    total = 0
    affected = 0
    for symbol in current:
        joined = earliest.get(symbol)
        if joined is None or joined <= WINDOW_START:
            continue
        total += (min(joined, WINDOW_END) - WINDOW_START).days
        affected += 1
    return total / 365.25, affected


def _within(low: str, value: str, high: str, tolerance: int) -> bool:
    """Is ``value`` inside ``(low, high]`` once both ends are loosened by ``tolerance`` days?

    Index changes take effect before the open on a stated date, while a fund's schedule is dated at
    a quarter end, so the two can straddle by a day or two without either being wrong. CBRL joined
    the S&P 400 on 2015-06-29 and appears in the roster dated 2015-06-30; scored strictly that is a
    disagreement, which tells the reader nothing useful.
    """
    lower = dt.date.fromisoformat(low) - dt.timedelta(days=tolerance)
    upper = dt.date.fromisoformat(high) + dt.timedelta(days=tolerance)
    return lower < dt.date.fromisoformat(value) <= upper


def merge_index(
    rows: list[Row], spans: dict[str, list[Span]], index: str, *, tolerance: int = 7
) -> MergeStats:
    """Fold EDGAR spans into the Wikipedia rows. Wikipedia wins wherever it speaks."""
    stats = MergeStats(index=index)
    stats.rows_before = len(rows)
    current = [r for r in rows if r.is_current]
    stats.current_members = len(current)
    stats.join_before = sum(1 for r in current if r.has_join_date)
    stats.removal_before = sum(1 for r in rows if _ISO.fullmatch(r.removed))
    stats.lookahead_years_before = _lookahead_years(rows)

    by_symbol: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_symbol[row.symbol].append(row)

    used: set[tuple[str, str]] = set()
    for symbol, symbol_spans in spans.items():
        candidates = by_symbol.get(symbol, [])
        # One row per span and one span per row. Without the consumption bookkeeping a company that
        # left and rejoined has both of its spans match its first row, the second span then looks
        # unrecorded, and a re-run of the merge appends a duplicate interval every time.
        claimed: set[int] = set()
        for span in symbol_spans:
            scored = [
                (_overlap_days(row, span), position)
                for position, row in enumerate(candidates)
                if position not in claimed and _overlap_days(row, span) > 0
            ]
            if not scored:
                continue
            match = candidates[max(scored)[1]]
            claimed.add(max(scored)[1])
            used.add((symbol, span.join_by))

            # --- join date -----------------------------------------------------------------
            if match.has_join_date and match.added_source == SOURCE_WIKI:
                if span.join_after:  # EDGAR brackets the join: (join_after, join_by]
                    if span.join_after < match.added <= span.join_by:
                        stats.agree_join += 1
                    elif _within(span.join_after, match.added, span.join_by, tolerance):
                        stats.near_join += 1
                    else:
                        stats.disagree_join += 1
                        stats.disagreements.append(
                            f"{symbol}: wiki {match.added}, edgar"
                            f" ({span.join_after}, {span.join_by}]"
                        )
                else:
                    stats.untestable_join += 1
            elif not match.has_join_date and span.join_after:
                # Only claim a join where a decisive earlier roster shows the symbol absent.
                # Without one, "first seen in 2014" says nothing about 2013.
                match.added = span.join_by
                match.added_bound = BOUND_NO_LATER
                match.added_source = SOURCE_EDGAR

            # --- removal date --------------------------------------------------------------
            if span.left_by:
                if _ISO.fullmatch(match.removed) and match.removed_source == SOURCE_WIKI:
                    if span.last_seen <= match.removed <= span.left_by:
                        stats.agree_removal += 1
                    elif _within(span.last_seen, match.removed, span.left_by, tolerance):
                        stats.near_removal += 1
                    else:
                        stats.disagree_removal += 1
                elif match.removed == UNKNOWN:
                    match.removed = span.left_by
                    match.removed_bound = BOUND_NO_LATER
                    match.removed_source = SOURCE_EDGAR
                elif match.is_current:
                    # The CSV says still a member; the tracker's newest rosters disagree. Left as
                    # the CSV has it — the committed snapshots are the authority on who is in the
                    # index today — but counted, because a silent contradiction is worse than a
                    # noisy one.
                    stats.current_but_absent += 1

    # --- memberships the Wikipedia reconstruction never saw at all -------------------------
    seen = {(row.symbol, row.added, row.removed) for row in rows}
    for symbol, symbol_spans in spans.items():
        for span in symbol_spans:
            if (symbol, span.join_by) in used or span.is_final or not span.left_by:
                continue
            new = Row(
                symbol=symbol,
                name=span.name,
                added=span.join_by if span.join_after else "",
                removed=span.left_by,
                added_bound=BOUND_NO_LATER if span.join_after else "",
                removed_bound=BOUND_NO_LATER,
                added_source=SOURCE_EDGAR if span.join_after else "",
                removed_source=SOURCE_EDGAR,
            )
            # Belt and braces on top of the matching above, so that running the merge twice is a
            # no-op rather than a slow accumulation of near-duplicate intervals.
            if (new.symbol, new.added, new.removed) in seen:
                continue
            seen.add((new.symbol, new.added, new.removed))
            rows.append(new)
            stats.new_rows += 1

    stats.rows_after = len(rows)
    current = [r for r in rows if r.is_current]
    stats.join_after = sum(1 for r in current if r.has_join_date)
    stats.removal_after = sum(1 for r in rows if _ISO.fullmatch(r.removed))
    stats.lookahead_years_after = _lookahead_years(rows)
    return stats


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def _client(args: argparse.Namespace) -> SecClient:
    return SecClient(Path(args.cache_dir), offline=args.offline)


def _current_members(
    resolver: Resolver, parsed: dict[str, list[tuple[Filing, dict[str, Any]]]]
) -> set[str]:
    """Tickers in the newest roster of each fund — the members whose join dates are the point."""
    out: set[str] = set()
    for records in parsed.values():
        newest = max(
            (r for _f, r in records if r["as_of"] and r["holdings"]),
            key=lambda r: str(r["as_of"]),
            default=None,
        )
        if newest is None:
            continue
        month = str(newest["as_of"])[:7].replace("-", "")
        for item in newest["holdings"]:
            holding = Holding(item["name"], item["value"], item["cusip"], item["isin"])
            ticker, _tier = resolver.resolve(holding, month)
            if ticker:
                out.add(ticker)
    return out


def cmd_filings(args: argparse.Namespace) -> int:
    client = _client(args)
    for spec in FUNDS:
        filings = collect_filings(client, spec)
        by_form = Counter(f.form for f in filings)
        first = filings[0].filed if filings else "-"
        last = filings[-1].filed if filings else "-"
        print(f"{spec.index} ({spec.etf}, {spec.series_id}): {len(filings)} holdings filings")
        print(f"  {first} .. {last}   {dict(sorted(by_form.items()))}")
        per_year = Counter(f.filed[:4] for f in filings)
        print("  per year: " + " ".join(f"{y}:{n}" for y, n in sorted(per_year.items())))
    print(f"\nlive requests: {client.live_requests}")
    return 0


def _prepare(args: argparse.Namespace) -> tuple[SecClient, dict[str, list[Snapshot]]]:
    client = _client(args)
    ftd = FailsToDeliver(client)
    resolver = Resolver(client, ftd)
    print("reading filings (each trust report is parsed once for all three funds) ...", flush=True)
    parsed = read_all(client, verbose=args.verbose)
    for spec in FUNDS:
        learn_from_nport(ftd, resolver, parsed.get(spec.index, []))
    resolver.finalise()
    if not args.no_former_names:
        today = _current_members(resolver, parsed)
        print(f"asking EDGAR for the former names of {len(today)} current members ...", flush=True)
        print(f"  usable former names: {resolver.learn_former_names(client, today)}")
    out: dict[str, list[Snapshot]] = {}
    for spec in FUNDS:
        out[spec.index] = build_snapshots(
            resolver, spec, parsed.get(spec.index, []), verbose=args.verbose
        )
    return client, out


def _snapshot_table(spec: FundSpec, snapshots: list[Snapshot]) -> None:
    print(f"\n{spec.index} ({spec.etf}) — {len(snapshots)} snapshots")
    print("  as-of       form      holdings  matched   rate    tiers")
    for snapshot in snapshots:
        matched = len(snapshot.members)
        rate = matched / snapshot.holdings if snapshot.holdings else 0.0
        tiers = Counter(snapshot.tiers.values())
        flag = "" if plausible(spec, snapshot) else "  <-- implausible size"
        order = " ".join(f"{k}={tiers[k]}" for k in ("cusip", "nport", "sec", "ftd") if tiers[k])
        print(
            f"  {snapshot.as_of}  {snapshot.form:8s}  {snapshot.holdings:7d}  {matched:7d}"
            f"  {rate:5.1%}   {order}{flag}"
        )


def cmd_snapshots(args: argparse.Namespace) -> int:
    client, snapshots = _prepare(args)
    grand_holdings = 0
    grand_matched = 0
    grand_tiers: Counter[str] = Counter()
    for spec in FUNDS:
        series = snapshots[spec.index]
        _snapshot_table(spec, series)
        for snapshot in series:
            grand_holdings += snapshot.holdings
            grand_matched += len(snapshot.members)
            grand_tiers.update(snapshot.tiers.values())
    print("\n--- crosswalk, all snapshots pooled ---")
    share = grand_matched / max(grand_holdings, 1)
    print(f"holding-lines: {grand_holdings}   resolved: {grand_matched} ({share:.1%})")
    for tier, count in grand_tiers.most_common():
        print(f"  {tier:6s} {count:7d}  {count / max(grand_matched, 1):5.1%} of resolved")
    print(f"\nlive requests: {client.live_requests}")
    return 0


def cmd_renames(args: argparse.Namespace) -> int:
    """Measure what the former-name crosswalk actually buys, by building the series both ways.

    A corporate rename is the one event a name-keyed diff cannot see for what it is. "Apple Computer
    Inc." leaves the roster and "Apple Inc." arrives, and unless something tells the diff those are
    the same company it records a departure and a fresh arrival — a manufactured join date, in the
    one direction that would corrupt a look-ahead fix. This runs the whole pipeline with and without
    EDGAR's former-name records and reports the difference.
    """
    args.no_former_names = True
    _client_off, without = _prepare(args)
    args.no_former_names = False
    client, with_former = _prepare(args)

    print("\n=== what the former-name tier changed ===")
    total_stitched = 0
    total_earlier = 0
    for spec in FUNDS:
        plain = [s for s in without[spec.index] if plausible(spec, s)]
        rich = [s for s in with_former[spec.index] if plausible(spec, s)]
        before = spans_from(plain, decisive_flags(plain, args.min_resolution))
        after = spans_from(rich, decisive_flags(rich, args.min_resolution))
        stitched = sum(len(v) for v in before.values()) - sum(len(v) for v in after.values())
        earlier = 0
        moved: list[str] = []
        for symbol, spans in after.items():
            was = before.get(symbol)
            if not was:
                continue
            if min(s.join_by for s in spans) < min(s.join_by for s in was):
                earlier += 1
                moved.append(symbol)
        resolved_by_former = sum(
            sum(1 for tier in s.tiers.values() if tier == "former") for s in rich
        )
        total_stitched += max(stitched, 0)
        total_earlier += earlier
        print(f"\n{spec.index}:")
        print(f"  holding-lines resolved only by a former name: {resolved_by_former}")
        print(
            f"  membership spans: {sum(len(v) for v in before.values())}"
            f" -> {sum(len(v) for v in after.values())} ({stitched} spurious breaks stitched)"
        )
        print(f"  symbols whose first appearance moved earlier: {earlier}")
        print(f"    {', '.join(sorted(moved)[: args.show_disagreements])}")
        unresolved = Counter()
        for snapshot in rich:
            unresolved.update(snapshot.unmatched)
        print(f"  names still unresolved in any roster: {len(unresolved)} distinct")
        print(f"    most persistent: {[n for n, _ in unresolved.most_common(5)]}")
    print(
        f"\ntotal: {total_stitched} spurious membership breaks removed,"
        f" {total_earlier} symbols dated earlier"
    )
    print(f"\nlive requests: {client.live_requests}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    client, snapshots = _prepare(args)
    all_stats: list[MergeStats] = []
    before_rows: dict[str, list[Row]] = {}
    after_rows: dict[str, list[Row]] = {}
    for spec in FUNDS:
        series = [s for s in snapshots[spec.index] if plausible(spec, s)]
        dropped = len(snapshots[spec.index]) - len(series)
        decisive = decisive_flags(series, args.min_resolution)
        path = UNIVERSE_DIR / f"{spec.index}-membership.csv"
        rows = read_rows(path)
        backfill_provenance(rows)
        before_rows[spec.index] = [Row(**vars(r)) for r in rows]
        stats = merge_index(
            rows, spans_from(series, decisive), spec.index, tolerance=args.bracket_tolerance
        )
        after_rows[spec.index] = rows
        all_stats.append(stats)
        print(f"\n=== {spec.index} ===")
        print(f"snapshots used: {len(series)} ({dropped} dropped as implausible)")
        first_decisive = next((s.as_of for s, ok in zip(series, decisive, strict=True) if ok), "-")
        print(
            f"decisive (>= {args.min_resolution:.0%} of holdings resolved):"
            f" {sum(decisive)}/{len(series)}, earliest {first_decisive}"
        )
        if series:
            print(f"span: {series[0].as_of} .. {series[-1].as_of}")
        print(f"rows {stats.rows_before} -> {stats.rows_after} (+{stats.new_rows} EDGAR-only)")
        print(
            f"current members with a join date: {stats.join_before}/{stats.current_members}"
            f" -> {stats.join_after}/{stats.current_members}"
            f"  ({stats.join_before / max(stats.current_members, 1):.0%}"
            f" -> {stats.join_after / max(stats.current_members, 1):.0%})"
        )
        print(f"rows with a removal date: {stats.removal_before} -> {stats.removal_after}")
        print(
            "rows the CSV calls current that EDGAR's newest rosters lack:"
            f" {stats.current_but_absent} (left as the CSV has them)"
        )
        print(
            f"look-ahead member-years (current members): {stats.lookahead_years_before:,.0f}"
            f" -> {stats.lookahead_years_after:,.0f}"
        )
        testable = stats.agree_join + stats.disagree_join
        if testable:
            print(
                f"wiki join dates cross-checked: {testable} testable,"
                f" {stats.disagree_join} outside the EDGAR bracket"
                f" ({stats.disagree_join / testable:.1%});"
                f" {stats.untestable_join} untestable (member since the first snapshot)"
            )
        testable_removal = stats.agree_removal + stats.disagree_removal
        if testable_removal:
            print(
                f"wiki removal dates cross-checked: {testable_removal} testable,"
                f" {stats.disagree_removal} outside the bracket"
                f" ({stats.disagree_removal / testable_removal:.1%})"
            )
        for line in stats.disagreements[: args.show_disagreements]:
            print(f"    {line}")
        if not args.dry_run:
            write_rows(path, rows)
            print(f"wrote {path}")

    print("\n=== totals ===")
    members = sum(s.current_members for s in all_stats)
    before = sum(s.join_before for s in all_stats)
    after = sum(s.join_after for s in all_stats)
    print(f"current members with a join date: {before}/{members} -> {after}/{members}")
    print(
        "look-ahead member-years, per index (double-counts index-to-index moves): "
        f"{sum(s.lookahead_years_before for s in all_stats):,.0f}"
        f" -> {sum(s.lookahead_years_after for s in all_stats):,.0f}"
    )
    union_before, affected_before = union_lookahead_years(before_rows)
    union_after, affected_after = union_lookahead_years(after_rows)
    print(
        f"look-ahead member-years, union across indices: {union_before:,.0f}"
        f" ({affected_before} symbols) -> {union_after:,.0f} ({affected_after} symbols)"
    )
    testable = sum(s.agree_join + s.near_join + s.disagree_join for s in all_stats)
    disagree = sum(s.disagree_join for s in all_stats)
    near = sum(s.near_join for s in all_stats)
    if testable:
        print(
            f"wiki-vs-EDGAR join disagreement: {disagree}/{testable} = {disagree / testable:.1%}"
            f" (+{near} within {args.bracket_tolerance}d of the edge ="
            f" {(disagree + near) / testable:.1%} scored strictly)"
        )
    testable_removal = sum(s.agree_removal + s.near_removal + s.disagree_removal for s in all_stats)
    disagree_removal = sum(s.disagree_removal for s in all_stats)
    if testable_removal:
        print(
            f"wiki-vs-EDGAR removal disagreement: {disagree_removal}/{testable_removal}"
            f" = {disagree_removal / testable_removal:.1%}"
        )
    if args.dry_run:
        print("\n--dry-run: no CSV was written")
    print(f"\nlive requests: {client.live_requests}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--cache-dir",
        default=str(Path(tempfile.gettempdir()) / "swing-edgar-cache"),
        help="where SEC responses and parsed snapshots are cached",
    )
    parser.add_argument("--offline", action="store_true", help="fail instead of making a request")
    parser.add_argument("-v", "--verbose", action="store_true", help="say why a filing was skipped")
    parser.add_argument(
        "--no-former-names",
        action="store_true",
        help="skip the per-company former-name lookup (~1,500 small requests)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("filings", help="what the three trackers filed, by form and year")
    sub.add_parser("snapshots", help="build every roster and report the crosswalk match rate")

    renames = sub.add_parser("renames", help="A/B the former-name crosswalk against going without")
    renames.add_argument("--min-resolution", type=float, default=0.60)
    renames.add_argument("--show-disagreements", type=int, default=10)

    merge = sub.add_parser("merge", help="fold EDGAR dates into the membership CSVs")
    merge.add_argument("--dry-run", action="store_true", help="report but do not write")
    merge.add_argument(
        "--min-resolution",
        type=float,
        default=0.60,
        help="a roster below this resolved share can witness presence but not absence",
    )
    merge.add_argument(
        "--bracket-tolerance",
        type=int,
        default=7,
        help="days of slack at a bracket edge before a Wikipedia date counts as a disagreement",
    )
    merge.add_argument(
        "--show-disagreements", type=int, default=10, help="how many disagreeing rows to print"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "filings": cmd_filings,
        "snapshots": cmd_snapshots,
        "renames": cmd_renames,
        "merge": cmd_merge,
    }
    try:
        return handlers[args.command](args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if "403" in str(exc) or "429" in str(exc):
            print(
                "SEC refused the request. Back off, leave the cache in place, and retry later;"
                " do not remove the User-Agent.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
