"""Backtest reporting — Markdown, HTML and the terminal summary.

Three audiences, three renderings of the same ``summary.json``:

* :func:`render_markdown` — diffable, greppable, and the thing you paste into a
  research log.
* :func:`render_html` — the one a human reads, with the equity curve, the
  drawdown, and a monthly heat table drawn by matplotlib and embedded as
  base64 PNGs so the file is self-contained and survives being emailed.
* :func:`print_latest` — Contract 2's ``swing report``: the headline numbers
  and, more importantly, the gate verdict.

``generated_at`` appears in the HTML and **nowhere else**. Contract 11 requires
byte-identical reruns of ``summary.json``, ``trades.csv`` and ``equity.csv``,
and a timestamp in any of them would break that for no benefit.

COVERAGE IS A DEGREE, SO IT IS RENDERED AS ONE (audit REPORT-1)
----------------------------------------------------------------
Every surface here used to turn ``earnings_blackout_simulated`` into an
all-or-nothing banner, which meant a run whose blackout reached 40% of the
universe rendered *identically* to one that reached 99.7%. That is the same
defect the boolean itself had, moved one file downstream: ``summary.json``
learnt to say how much of the blackout ran, and the artefact a human actually
opens still could not repeat it.

So the three renderers share :func:`report_notes` — a list of :class:`Note`,
each one line, each with exactly **two** volumes. A note is emphasised when the
mechanism did not do what the rest of the report implies (see
:data:`EARNINGS_COVERAGE_FLOOR` for the threshold and its arithmetic), and
otherwise it is a quiet line of provenance. Two volumes rather than a palette
of severities, because a scale invented here would carry no information that
the numbers on the line do not already carry.

Everything about these notes is best-effort. A summary predating the coverage
block renders byte-for-byte as it always did (the constants below are frozen
copies of that rendering, and there are thirty-six such reports on disk); a
half-written or hand-edited block prints what it can and stays silent about the
rest. A report that fails to render is worse than a report with a gap in it, so
nothing in here is allowed to raise.

WHICH PERIOD THE HEADLINE NAMES (audit BUG-043)
-------------------------------------------------
A walk-forward run loads years of warm-up data and often stops measuring months
before the last bar, so ``start``/``end`` describe the *data*, not the record.
Every headline here names ``oos_start``/``oos_end`` — the stretch the numbers
directly above it actually cover — and the data span is demoted to provenance,
where it belongs.

LEGIBILITY IN A DARK-MODE BROWSER (audit BUG-051)
-------------------------------------------------
This stylesheet used to set ``color: #222`` on ``body`` and no background at
all. A browser in dark mode painted its own near-black canvas behind that
near-black text and the whole report vanished — every heading, every table,
everything except the two banners, which happened to declare backgrounds of
their own. Three rules now prevent that from ever recurring, and
``tests/test_html_contrast.py`` enforces all three:

1. ``:root`` declares ``color-scheme`` and the whole palette as custom
   properties, with a ``prefers-color-scheme: dark`` block redefining them.
2. ``body`` sets **both** ``background`` and ``color`` — the canvas is never
   inherited from the browser.
3. Every rule that sets a background sets a foreground too. A background-only
   rule is the exact shape of the original bug.

The palette is deliberately the same one as
``swing/alerts/templates/picks.html.j2``: the pick sheet and the backtest
report are two artefacts of one system and should look like it.
"""

from __future__ import annotations

import base64
import html
import io
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.backtest.metrics import PROFIT_FACTOR_CAP

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "CHART_COLORS",
    "EARNINGS_COVERAGE_FLOOR",
    "METRIC_LABELS",
    "STYLESHEET",
    "Note",
    "earnings_note",
    "format_metric",
    "legacy_blackout_banner",
    "measured_period",
    "membership_note",
    "print_latest",
    "render_html",
    "render_markdown",
    "report_notes",
]

#: The colours baked into the matplotlib PNGs.
#:
#: A PNG cannot answer the viewer's ``prefers-color-scheme``, so the charts
#: commit to one deliberate plate rather than inheriting matplotlib's defaults
#: and turning into glaring white rectangles in dark mode. The stylesheet paints
#: ``--plate`` — the same colour, in *both* themes — behind every ``<img>``, so
#: the chart reads as a printed figure laid on the page instead of a hole
#: punched through it. Keep ``--plate`` and ``CHART_COLORS["plate"]`` equal;
#: ``tests/test_html_contrast.py`` asserts they are.
#:
#: ``equity`` and ``drawdown`` are the chart's ink and clear 3:1 against the
#: plate (7.9:1 and 6.6:1). ``drawdown_fill`` is a pre-blended tint rather than
#: an alpha, so the rendered colour is exactly the colour the test measures.
CHART_COLORS: dict[str, str] = {
    "plate": "#f2f4f7",
    "ink": "#14181d",
    "muted": "#5b6572",
    "grid": "#c3cad4",
    "equity": "#1f4e79",
    "drawdown": "#a4262c",
    "drawdown_fill": "#e7c3c5",
}

#: Human labels and units for the Contract 11 metric keys.
METRIC_LABELS: dict[str, tuple[str, str]] = {
    "cagr": ("CAGR", "%"),
    "sharpe": ("Sharpe", ""),
    "sortino": ("Sortino", ""),
    "max_drawdown_pct": ("Max drawdown", "%"),
    "max_dd_duration_days": ("Longest drawdown", " days"),
    "win_rate": ("Win rate", "%"),
    "profit_factor": ("Profit factor", ""),
    "avg_win": ("Average win", "$"),
    "avg_loss": ("Average loss", "$"),
    "avg_hold_days": ("Average hold", " days"),
    "exposure_pct": ("Exposure", "%"),
    "trades": ("Trades", ""),
}


def format_metric(key: str, value: Any) -> str:
    """Render one metric the way a human wants to read it."""
    unit = METRIC_LABELS.get(key, (key, ""))[1]
    if value is None:
        return "n/a"
    if key == "trades" or key == "max_dd_duration_days":
        return f"{int(value)}{unit}"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if key == "profit_factor" and number >= PROFIT_FACTOR_CAP:
        return "no losing trades"
    if unit == "$":
        return f"${number:,.2f}"
    return f"{number:,.2f}{unit}"


# ---------------------------------------------------------------------------
# dependency coverage notes
# ---------------------------------------------------------------------------

#: ``earnings.source`` values, mirrored from :mod:`swing.backtest.runner`.
#:
#: Copied rather than imported because the runner imports *this* module to
#: write its reports, so the arrow only points one way. ``tests/
#: test_backtest_report.py::test_the_earnings_source_names_match_the_runners``
#: fails the moment a spelling drifts, which is the same bargain the contrast
#: suite strikes with the stylesheet: never restate a fact without a test that
#: re-reads the original.
EARNINGS_FROM_HISTORY = "history"
EARNINGS_FROM_UPCOMING = "upcoming_only"
EARNINGS_UNAVAILABLE = "unavailable"

#: Below this percentage of *announcing* symbols, the earnings note is loud.
#:
#: The number has to sit inside a window fixed at both ends, and the window is
#: narrow enough that the choice is nearly made for you:
#:
#: * **It must not fire on a healthy stocks run.** Five S&P names (``CWEN-A``,
#:   ``MCRI``, ``MFP``, ``PAYX``, ``SEI``) have no free announcement history and
#:   are unlikely ever to get one, so the practical ceiling on this universe is
#:   99.67%, not 100%. A threshold above that warns on every stocks run forever,
#:   and a banner that is always on is one nobody reads.
#: * **It must fire on the states this note exists to separate.** The repo's own
#:   cache went 8% -> 99.7% populated in an afternoon, and both states recorded
#:   the identical boolean. 8% and 40% have to look different from 99.7%.
#:
#: That brackets it to (40, 99.67). Within the bracket, pick by the size of the
#: distortion. ``strategy.earnings_blackout_days = 10`` blocks entries from ten
#: calendar days before an announcement through the day itself — about eight
#: trading days — and a US company reports four times a year, so the blackout
#: removes on the order of 32 of ~252 trading days: roughly **13% of an
#: announcing symbol's entry opportunities**. An uncovered symbol runs an entry
#: gate that differs from production on those bars, so the run-wide share of
#: mis-gated entry opportunities is about ``(100 - coverage) x 0.13``:
#:
#: ===========  ===============================
#: coverage     entry opportunities mis-gated
#: ===========  ===============================
#: 99.7%        0.04%
#: 95%          0.6%
#: 90%          1.3%
#: 40%          7.6%
#: 8%           11.7%
#: ===========  ===============================
#:
#: 95% is the round number that keeps the mis-gated share under 1% while
#: leaving about fifteen times the irreducible gap (5 names in 1,506) as slack,
#: so ordinary cache churn cannot trip it and a genuinely thin cache cannot
#: hide behind it.
EARNINGS_COVERAGE_FLOOR = 95.0

#: The pre-coverage earnings banner, frozen per surface.
#:
#: These are the exact bytes every report written before the coverage block
#: carries, and there are thirty-six of them on disk. They are reproduced
#: verbatim — line breaks included — so re-rendering an old ``summary.json``
#: yields the file its reader already knows, and so the two spellings (Markdown
#: sentence case, HTML shouting) survive unchanged.
_LEGACY_MD_BANNER: tuple[str, ...] = (
    "> **Earnings blackout not simulated.** No historical announcement dates were",
    "> available, so the backtest took entries the live scanner would have blocked.",
    "> Results are slightly optimistic against the strategy as it is actually run.",
    "",
)
_LEGACY_HTML_BANNER = (
    '<div class="banner warn">Earnings blackout NOT simulated: no historical '
    "announcement dates were available, so this run took entries the live scanner "
    "would have blocked.</div>"
)
_LEGACY_TERM_BANNER = (
    "  note       earnings blackout NOT simulated — results are slightly optimistic"
)

#: The same headline, for a run that *does* carry a coverage block, in each
#: surface's own voice. The note's predicate continues the sentence, so the
#: numbers arrive attached to the warning rather than in a second banner beside
#: it saying the same thing in fewer words.
_ABSENT_MD_LEAD = "**Earnings blackout not simulated.**"
_ABSENT_HTML_LEAD = "<strong>Earnings blackout NOT simulated.</strong>"
_ABSENT_TERM_LEAD = "NOT simulated —"


@dataclass(frozen=True)
class Note:
    """One line about a dependency the simulation leaned on.

    Split into ``subject`` and ``text`` because the three surfaces need the same
    fact assembled three ways: Markdown wants it as a header bullet next to
    **Data hash**, HTML wants a sentence, and the terminal has already printed
    the subject as a label column and would otherwise say it twice.
    ``text`` is therefore a lowercase predicate with no trailing stop, and every
    surface adds its own.

    ``emphasise`` is the point of the type. The renderers used to have one
    volume and a boolean to trigger it, so 40% coverage and 99.7% coverage
    produced the same page. A note carries the degree in ``text`` and gets
    exactly two volumes: loud when the mechanism did not do what the rest of the
    report implies, quiet otherwise. There is deliberately no third level —
    a severity scale invented in a renderer would encode nothing the numbers on
    the line do not already say.
    """

    #: Which dependency this is about, as a terminal label column. It lives on
    #: the note rather than being inferred from position: a list that drops its
    #: earnings note must not relabel the membership one.
    label: str
    #: The same thing, spelled for prose — ``"Earnings blackout"``.
    subject: str
    #: The predicate: lowercase, no trailing full stop.
    text: str
    emphasise: bool = False
    #: The pre-coverage headline still applies: the blackout did not operate at
    #: all. Each surface spells it in its own established voice, because that
    #: wording predates the coverage block and both readers and tests know it.
    blackout_absent: bool = False

    def sentence(self) -> str:
        """The whole note as one plain sentence — ``"Subject: predicate."``"""
        return f"{self.subject}: {self.text}."


def _number(value: Any) -> float | None:
    """A finite number out of a JSON value, or ``None`` if it does not hold one.

    Tolerant on purpose: a diagnostic block is the last thing that should be
    allowed to raise, and a summary that has been hand-edited, half-written or
    round-tripped through a tool that stringifies numbers is still worth
    reading. ``bool`` is refused rather than silently read as 0/1, because a
    boolean in a count field means the record is wrong, not that the count is 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):  # NaN / +-inf
        return None
    return number


def _count(value: Any) -> int | None:
    """:func:`_number`, restricted to a non-negative whole count."""
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _pct(value: float) -> str:
    """A percentage to one decimal, without ever rounding a gap away.

    ``f"{99.96:.1f}%"`` is ``"100.0%"``, which would report a universe with
    uncovered symbols in it as fully covered — the precise class of lie this
    whole note exists to stop. Same at the bottom: 0.04% is not "none".
    """
    if 0.0 < value < 0.05:
        return "under 0.1%"
    if 99.95 <= value < 100.0:
        return "just under 100%"
    return f"{value:.1f}%"


def _sentence(text: str) -> str:
    """Capitalise the first character and leave every other one alone."""
    return text[:1].upper() + text[1:] if text else text


def _text(value: Any) -> str | None:
    """A non-empty string out of a JSON value, or ``None``."""
    return value.strip() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class _Earnings:
    """The coverage block, read as tolerantly as it can be read."""

    source: str | None
    applicable: int | None
    covered: int | None
    uncovered: int | None
    coverage_pct: float | None
    announcements: int | None


def _earnings_facts(block: Mapping[str, Any]) -> _Earnings:
    """Derive what the block does not state from what it does.

    Any two of applicable/covered/uncovered give the third, and either the
    counts or ``coverage_pct`` give the percentage, so a block missing one key
    is still worth a full line.
    """
    applicable = _count(block.get("symbols_applicable"))
    covered = _count(block.get("symbols_with_dates"))
    uncovered = _count(block.get("symbols_without_dates"))
    if applicable is None and covered is not None and uncovered is not None:
        applicable = covered + uncovered
    if uncovered is None and applicable is not None and covered is not None:
        uncovered = max(applicable - covered, 0)
    if covered is None and applicable is not None and uncovered is not None:
        covered = max(applicable - uncovered, 0)

    coverage_pct = _number(block.get("coverage_pct"))
    if coverage_pct is None and applicable and covered is not None:
        coverage_pct = 100.0 * covered / applicable
    if coverage_pct is not None:
        coverage_pct = min(max(coverage_pct, 0.0), 100.0)

    return _Earnings(
        source=_text(block.get("source")),
        applicable=applicable,
        covered=covered,
        uncovered=uncovered,
        coverage_pct=coverage_pct,
        announcements=_count(block.get("announcements")),
    )


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """``"1 announcement date"`` / ``"115,045 announcement dates"``."""
    return f"{count:,} {singular if count == 1 else (plural or singular + 's')}"


def _announcing(applicable: int | None) -> str:
    """``"the 1,506 announcing symbols"``, degrading when the count is absent."""
    if applicable is None:
        return "the symbols that announce"
    return f"the {_plural(applicable, 'announcing symbol')}"


def earnings_note(summary: Mapping[str, Any]) -> Note | None:
    """How much of the entry gate's earnings blackout this run actually had.

    Returns ``None`` when the summary carries no usable ``earnings`` block —
    every report written before the block existed, and any report whose block
    has been replaced by something that is not a mapping. The surfaces fall
    back to the frozen banner those reports have always rendered, so nothing on
    disk changes appearance.

    Otherwise returns one line covering every state the block can be in:

    * nothing in the universe announces (an ETF-only run) — quiet, and
      explicitly "not applicable" rather than 0% coverage, because the block
      reports ``coverage_pct: 0.0`` for an empty denominator and a red banner on
      the one run free of these biases would be its own lie (methodology 7.4);
    * ``source: unavailable`` or ``upcoming_only`` — loud. Neither can block a
      single historical bar, and ``upcoming_only`` in particular scores full
      coverage while simulating no blackout at all (amendment A12);
    * ``source: history`` with no dates back — loud, and the reason
      ``earnings_blackout_simulated`` was tightened;
    * ``history`` below :data:`EARNINGS_COVERAGE_FLOOR` — loud, with the count
      that went through the gate unblacked-out;
    * ``history`` at or above it — quiet, with the counts and the announcement
      total, because "1,501 of 1,506 over 115,045 dates" and "one date each"
      both read as "covered" and are not the same run;
    * a partial or malformed block — quiet, and honest that the figure is
      missing rather than inventing one.
    """
    absent = bool(
        "earnings_blackout_simulated" in summary and not summary["earnings_blackout_simulated"]
    )
    block = summary.get("earnings")
    if not isinstance(block, Mapping):
        return None

    facts = _earnings_facts(block)

    # An unrecognised source is quoted rather than guessed at, so a reader can
    # see that this renderer did not understand the record it was handed.
    unknown_source = (
        f' (recorded source: "{facts.source}")'
        if facts.source
        and facts.source
        not in {EARNINGS_FROM_HISTORY, EARNINGS_FROM_UPCOMING, EARNINGS_UNAVAILABLE}
        else ""
    )

    def note(text: str, *, emphasise: bool) -> Note:
        return Note(
            "earnings",
            "Earnings blackout",
            f"{text}{unknown_source}",
            emphasise=emphasise or absent,
            blackout_absent=absent,
        )

    if facts.applicable == 0:
        return note(
            "not applicable — no symbol in this universe announces earnings, so there was "
            "nothing for it to block",
            emphasise=False,
        )

    if facts.source == EARNINGS_UNAVAILABLE:
        why = "the earnings lookup failed and returned nothing"
    elif facts.source == EARNINGS_FROM_UPCOMING:
        why = (
            "the provider knew only each symbol's next announcement date, which cannot "
            "block a historical bar"
        )
    elif facts.covered == 0:
        why = f"no announcement dates came back for any of {_announcing(facts.applicable)}"
    else:
        why = ""

    if why:
        tail = (
            "every entry went through the gate unblacked-out"
            if facts.covered == 0 and facts.source == EARNINGS_FROM_HISTORY
            else f"none of {_announcing(facts.applicable)} was blacked out"
        )
        # "did not run" is the surfaces' own lead when `absent`, so saying it
        # here too would print it twice in the one sentence.
        opener = "" if absent else "did not run — "
        return note(f"{opener}{why}, so {tail}", emphasise=True)

    if facts.coverage_pct is None or facts.applicable is None or facts.covered is None:
        return note(
            "this report does not record how much of the universe the announcement dates covered",
            emphasise=False,
        )

    if facts.coverage_pct < EARNINGS_COVERAGE_FLOOR:
        missed = facts.uncovered if facts.uncovered is not None else 0
        return note(
            f"reached only {facts.covered:,} of {_announcing(facts.applicable)} "
            f"({_pct(facts.coverage_pct)}) — the other {missed:,} went through the entry gate "
            f"with no blackout in it, so this run took entries the live scanner would have "
            f"blocked",
            emphasise=True,
        )

    dates = (
        f", from {_plural(facts.announcements, 'announcement date')}"
        if facts.announcements is not None
        else ""
    )
    return note(
        f"covered {facts.covered:,} of {_announcing(facts.applicable)} "
        f"({_pct(facts.coverage_pct)}){dates}",
        emphasise=False,
    )


def membership_note(summary: Mapping[str, Any]) -> Note | None:
    """Which universe the run traded, and how much of that rests on a guess.

    Returns ``None`` when there is no usable ``membership`` block, which is
    every report written before one was published.

    The mode itself is *not* emphasised, even though ``off`` is the whole
    look-ahead: it is the documented default, the figures on the same line
    quantify it, and a note that fires on every run is a note nobody reads. The
    one loud case is a block carrying ``error`` — the membership files could not
    be read, so the bias is not merely present but unmeasured, and the zeros
    beside it must not be mistaken for a measurement of no bias.

    ``stint_date_quality.approximate_pct`` is the number worth the space. It
    counts rows of the membership files rather than this run's symbols, so it
    does not move when a policy does, and on the shipping files most stints
    rest on an approximation — which a reader should learn from the report
    rather than from a log line.
    """
    block = summary.get("membership")
    if not isinstance(block, Mapping):
        return None

    subject = "Index membership"
    problem = _text(block.get("error"))
    if problem:
        return Note(
            "membership",
            subject,
            f"the membership files could not be read ({problem}), so this report cannot say "
            f"how much of its universe was actually in an index",
            emphasise=True,
        )

    mode = _text(block.get("mode"))
    if mode == "off":
        clauses = ["not applied — today's index members are traded through all of history"]
    elif mode == "point_in_time":
        excluded = _count(block.get("symbols_excluded"))
        dropped = f", {_plural(excluded, 'symbol')} excluded" if excluded else ""
        clauses = [f"point-in-time{dropped}"]
    elif mode:
        clauses = [f'mode "{mode}"']
    else:
        clauses = []

    quality = block.get("stint_date_quality")
    if isinstance(quality, Mapping):
        approximate = _number(quality.get("approximate_pct"))
        stints = _count(quality.get("stints"))
        if approximate is not None and stints:
            clauses.append(
                f"{_pct(approximate)} of {_plural(stints, 'membership stint')} rest on an "
                f"approximate join date"
            )
        elif approximate is not None:
            clauses.append(f"{_pct(approximate)} of membership stints rest on an approximate date")

    # A zero here is not a finding — it means the policy decided nothing — so it
    # is left out rather than printed as `0 symbols have a bounded join date`.
    bounded = _count(block.get("symbols_bounded_join"))
    policy = _text(block.get("bounded_policy"))
    if bounded:
        read_as = f', read as "{policy}"' if policy else ""
        clauses.append(f"{_plural(bounded, 'symbol')} joined on a bounded date{read_as}")

    if not clauses:
        return None
    return Note("membership", subject, "; ".join(clauses))


def report_notes(summary: Mapping[str, Any]) -> list[Note]:
    """Every dependency note this summary supports, in reading order."""
    return [note for note in (earnings_note(summary), membership_note(summary)) if note is not None]


def legacy_blackout_banner(summary: Mapping[str, Any]) -> bool:
    """True when the frozen pre-coverage banner is still the whole earnings story.

    Exactly the old condition, and only on the old shape: a summary with no
    coverage block whose ``earnings_blackout_simulated`` is false. Anything
    carrying a block gets the headline folded into :func:`earnings_note`
    instead, so the two never render one above the other saying the same thing.
    """
    return not isinstance(summary.get("earnings"), Mapping) and bool(
        "earnings_blackout_simulated" in summary and not summary["earnings_blackout_simulated"]
    )


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def _metric_table(metrics: dict[str, Any]) -> list[str]:
    lines = ["| Metric | Value |", "| --- | --- |"]
    for key, (label, _unit) in METRIC_LABELS.items():
        if key in metrics:
            lines.append(f"| {label} | {format_metric(key, metrics[key])} |")
    return lines


def measured_period(summary: dict[str, Any]) -> str:
    """The span the headline numbers actually cover, as ``"<start> to <end>"``.

    For a walk-forward run that is the stitched out-of-sample stretch
    (``oos_start``..``oos_end``); for anything else it is the simulated window.
    Never the data span, which starts years earlier so indicators can warm up
    (audit BUG-043).
    """
    if bool(summary.get("walkforward")) and summary.get("oos_start") and summary.get("oos_end"):
        return f"{summary['oos_start']} to {summary['oos_end']}"
    return f"{summary.get('start', '?')} to {summary.get('end', '?')}"


def _md_quote(note: Note) -> list[str]:
    """One emphasised note as a wrapped Markdown blockquote."""
    text = (
        f"{_ABSENT_MD_LEAD} {_sentence(note.text)}."
        if note.blackout_absent
        else f"**{note.subject}:** {note.text}."
    )
    return [f"> {line}" for line in textwrap.wrap(text, width=88)] + [""]


def render_markdown(summary: dict[str, Any]) -> str:
    """Render ``summary.json`` as a Markdown report.

    Quiet notes join the header bullets, loud ones become blockquotes below it,
    both starting with the same words so one ``grep`` finds either — which is
    the difference a reader is meant to notice at a glance and a diff of two
    runs is meant to show as a moved line.
    """
    walkforward = bool(summary.get("walkforward"))
    notes = report_notes(summary)
    out: list[str] = [
        f"# Backtest — {summary.get('label', 'unlabelled')}",
        "",
        f"- **Universe**: {summary.get('universe', 'unknown')} "
        f"({summary.get('n_symbols', '?')} symbols)",
        f"- **Measured period**: {measured_period(summary)}",
        f"- **Data span**: {summary.get('start', '?')} to {summary.get('end', '?')}",
        f"- **Walk-forward**: {'yes' if walkforward else 'NO — cannot open the trading gate'}",
        f"- **Config hash**: `{summary.get('config_hash', '')[:16]}`",
        f"- **Code ref**: `{summary.get('code_ref', 'unknown')}`",
        f"- **Data hash**: `{summary.get('data_hash', '')[:16]}`",
    ]
    earnings_hash = _text(summary.get("earnings_hash"))
    if earnings_hash:
        out.append(f"- **Earnings hash**: `{earnings_hash[:16]}`")
    out += [f"- **{note.subject}**: {note.text}" for note in notes if not note.emphasise]
    out.append("")

    if legacy_blackout_banner(summary):
        # Pre-coverage-block report: the banner it has always carried, verbatim.
        out += list(_LEGACY_MD_BANNER)
    for note in notes:
        if note.emphasise:
            out += _md_quote(note)

    if walkforward:
        out += [
            "## Out-of-sample (headline)",
            "",
            f"These are the numbers the deployment gate reads, covering "
            f"{measured_period(summary)}. Every parameter used to produce them was chosen",
            "on data that ended before the trade did.",
            "",
        ]
    else:
        out += [
            "## Full period (in-sample — NOT gate-eligible)",
            "",
            f"This run tuned nothing over {measured_period(summary)}, but it also proved",
            "nothing out of sample.",
            "",
        ]
    out += _metric_table(summary.get("oos") or summary.get("full_period") or {})
    out.append("")

    if walkforward and summary.get("full_period"):
        out += ["## Full period, config parameters (reference only)", ""]
        out += _metric_table(summary["full_period"])
        out.append("")

    by_year = summary.get("by_year") or {}
    if by_year:
        out += [
            "## By year",
            "",
            "| Year | Return | Trades | Max drawdown |",
            "| --- | --- | --- | --- |",
        ]
        for year in sorted(by_year):
            row = by_year[year]
            out.append(
                f"| {year} | {float(row.get('return_pct', 0.0)):,.2f}% | "
                f"{int(row.get('trades', 0))} | {float(row.get('max_dd_pct', 0.0)):,.2f}% |"
            )
        out.append("")

    folds = summary.get("windows") or []
    if folds:
        out += [
            "## Walk-forward folds",
            "",
            f"Objective: {summary.get('objective', '')}",
            "",
            "| In-sample | Out-of-sample | Chosen parameters | IS PF | OOS PF | OOS trades |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for fold in folds:
            params = ", ".join(f"{k}={v}" for k, v in sorted((fold.get("params") or {}).items()))
            out.append(
                f"| {fold.get('is_start')}..{fold.get('is_end')} "
                f"| {fold.get('oos_start')}..{fold.get('oos_end')} "
                f"| {params} "
                f"| {float(fold.get('is_profit_factor', 0.0)):.2f} "
                f"| {float(fold.get('oos_profit_factor', 0.0)):.2f} "
                f"| {int(fold.get('oos_trades', 0))} |"
            )
        out.append("")

    sensitivity = summary.get("sensitivity") or []
    if sensitivity:
        out += [
            "## Sensitivity (+/-25%, one parameter at a time, full period)",
            "",
            "A result that only works at one setting is a curve fit. Look for a plateau.",
            "",
            "| Parameter | Variant | Value | Profit factor | CAGR | Max DD | Trades |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in sensitivity:
            out.append(
                f"| {row.get('param')} | {row.get('variant')} | {row.get('value')} "
                f"| {float(row.get('profit_factor', 0.0)):.2f} "
                f"| {float(row.get('cagr', 0.0)):.2f}% "
                f"| {float(row.get('max_drawdown_pct', 0.0)):.2f}% "
                f"| {int(row.get('trades', 0))} |"
            )
        out.append("")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------


def _png_data_uri(figure: Any) -> str:
    """Serialise a matplotlib figure to a base64 ``data:`` URI and close it.

    LEAK-006: the close is in a ``finally`` and the buffer is a context manager,
    so a failing ``savefig`` cannot leave a figure registered with pyplot (and
    therefore alive) for the life of the process.
    """
    import matplotlib.pyplot as plt

    try:
        with io.BytesIO() as buffer:
            # facecolor is passed explicitly: `bbox_inches="tight"` re-renders
            # through a fresh bbox and savefig would otherwise fall back to the
            # rcParam rather than the plate _paint_chart set on the figure.
            figure.savefig(
                buffer,
                format="png",
                dpi=110,
                bbox_inches="tight",
                facecolor=figure.get_facecolor(),
                edgecolor="none",
            )
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    finally:
        plt.close(figure)
    return f"data:image/png;base64,{encoded}"


def _paint_chart(figure: Any, axes: Any, *, title: str, ylabel: str) -> None:
    """Give a chart an explicit, theme-independent colour scheme.

    Matplotlib's defaults are a white figure with black text and no declared
    intent, which is why the old charts turned into glaring white rectangles the
    moment the surrounding page learnt to respect dark mode. Everything with a
    colour is named here instead: face, title, axis label, ticks, spines, grid.
    """
    figure.patch.set_facecolor(CHART_COLORS["plate"])
    axes.set_facecolor(CHART_COLORS["plate"])
    axes.set_title(title, color=CHART_COLORS["ink"])
    axes.set_ylabel(ylabel, color=CHART_COLORS["muted"])
    axes.tick_params(which="both", colors=CHART_COLORS["muted"])
    for spine in axes.spines.values():
        spine.set_color(CHART_COLORS["muted"])
    axes.grid(True, color=CHART_COLORS["grid"], linewidth=0.8)
    axes.set_axisbelow(True)


def _equity_chart(equity: pd.DataFrame, *, title: str = "Equity") -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(10, 4))
    axes.plot(equity.index, equity["equity"], linewidth=1.4, color=CHART_COLORS["equity"])
    _paint_chart(figure, axes, title=title, ylabel="Account equity ($)")
    return _png_data_uri(figure)


def _drawdown_chart(equity: pd.DataFrame) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(10, 2.6))
    drawdown = equity["drawdown"].astype("float64") * 100.0
    # A solid tint plus a solid boundary line, rather than one translucent fill:
    # the line is the ink that has to clear 3:1, and an alpha would make the
    # colour that actually reaches the eye a blend nothing can assert against.
    axes.fill_between(equity.index, drawdown, 0.0, color=CHART_COLORS["drawdown_fill"])
    axes.plot(equity.index, drawdown, linewidth=1.1, color=CHART_COLORS["drawdown"])
    _paint_chart(figure, axes, title="Drawdown", ylabel="%")
    return _png_data_uri(figure)


def _monthly_table_html(equity: pd.DataFrame) -> str:
    """A month-by-year table of returns, coloured green/red by sign."""
    if equity.empty:
        return "<p>No equity history.</p>"
    returns = equity["equity"].astype("float64").pct_change().fillna(0.0)
    monthly = (1.0 + returns).groupby([returns.index.year, returns.index.month]).prod() - 1.0
    if monthly.empty:
        return "<p>No equity history.</p>"

    years = sorted({int(y) for y, _ in monthly.index})
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    head = "".join(f"<th>{name}</th>" for name in months)
    rows: list[str] = []
    for year in years:
        cells: list[str] = []
        for month in range(1, 13):
            try:
                value = float(monthly.loc[(year, month)])
            except KeyError:
                cells.append('<td class="empty"></td>')
                continue
            css = "pos" if value >= 0 else "neg"
            cells.append(f'<td class="{css}">{value * 100:.1f}</td>')
        rows.append(f"<tr><th>{year}</th>{''.join(cells)}</tr>")
    return _scroll(
        '<table class="heat"><thead><tr><th>Year</th>'
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _scroll(table_html: str) -> str:
    """Wrap a wide table so a narrow viewport scrolls it instead of the page.

    Paired with the ``width=device-width`` viewport meta: without the container
    a thirteen-column heat table drags the whole document sideways on a phone
    and every other line of prose goes off-screen with it.
    """
    return f'<div class="scroll">{table_html}</div>'


#: The report's stylesheet — see the module docstring for the three rules it
#: exists to keep. Every colour is a custom property so the dark block can
#: restate the palette and nothing else, and so a test can read the palette out
#: of here rather than being told it a second time.
#:
#: ``--plate`` and ``--plate-ink`` are deliberately theme-INVARIANT: they have
#: to match the colours baked into the chart PNGs, which cannot change with the
#: viewer's theme. ``--plate-line`` is not invariant — in dark mode the plate
#: is a light panel on a dark page and gets a rim strong enough to read as a
#: deliberate frame.
STYLESHEET = """
  :root {
    color-scheme: light dark;
    --bg: #f6f7f9;
    --card: #ffffff;
    --ink: #14181d;
    --muted: #5b6572;
    --line: #dfe3e8;
    --head-bg: #eef1f5; --head-ink: #14181d;
    --code-bg: #eef1f5; --code-ink: #14181d;
    --ok-bg: #e4f6ea; --ok-ink: #10633a;
    --bad-bg: #fdeaea; --bad-ink: #9b1c1c;
    --empty-bg: #f0f2f5; --empty-ink: #5b6572;
    --plate: #f2f4f7; --plate-ink: #14181d;
    --plate-line: #dfe3e8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0e1116;
      --card: #161b22;
      --ink: #e6edf3;
      --muted: #9198a1;
      --line: #2a313a;
      --head-bg: #1d232c; --head-ink: #e6edf3;
      --code-bg: #1d232c; --code-ink: #e6edf3;
      --ok-bg: #12301f; --ok-ink: #6ee7a5;
      --bad-bg: #3a1616; --bad-ink: #ff9b9b;
      --empty-bg: #171c23; --empty-ink: #9198a1;
      --plate-line: #6b7684;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px;
    background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  .wrap { max-width: 1060px; margin: 0 auto; }
  h1 { font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }
  h2 { font-size: 18px; margin: 26px 0 6px; letter-spacing: -0.01em; }
  p { margin: 0 0 10px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 18px; }
  .banner { padding: 12px 14px; border-radius: 10px; margin: 0 0 14px;
            font-weight: 600; font-size: 13.5px; }
  .banner.ok { background: var(--ok-bg); color: var(--ok-ink); }
  .banner.warn { background: var(--bad-bg); color: var(--bad-ink); }
  .scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; width: 100%; margin: 8px 0 4px;
          font-size: 13.5px; background: var(--card); color: var(--ink); }
  th, td { border: 1px solid var(--line); padding: 7px 10px; text-align: right;
           font-variant-numeric: tabular-nums; }
  th { background: var(--head-bg); color: var(--head-ink);
       font-weight: 600; white-space: nowrap; }
  td:first-child, th:first-child { text-align: left; }
  table.heat td { text-align: center; min-width: 3.2rem; }
  table.heat td.pos { background: var(--ok-bg); color: var(--ok-ink); }
  table.heat td.neg { background: var(--bad-bg); color: var(--bad-ink); }
  table.heat td.empty { background: var(--empty-bg); color: var(--empty-ink); }
  .plate { background: var(--plate); color: var(--plate-ink);
           border: 1px solid var(--plate-line); border-radius: 10px;
           padding: 10px; margin: 10px 0 14px; }
  .plate img { display: block; width: 100%; height: auto; }
  code { background: var(--code-bg); color: var(--code-ink);
         padding: 1px 5px; border-radius: 4px; font-size: 12.5px;
         font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  footer { color: var(--muted); font-size: 12px; margin: 26px 0 8px; }
"""

_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest — {label}</title>
<style>{style}</style>
</head>
<body>
<div class="wrap">
<h1>Backtest — {label}</h1>
<p class="sub">{universe} universe ({n_symbols} symbols) &middot; {measured_period}</p>
<div class="banner {banner_class}">{banner}</div>
{notes}
<h2>{headline_title}</h2>
{headline_table}

{full_period_section}

{charts_section}

<h2>Monthly returns (%)</h2>
{monthly_table}

{by_year_section}
{folds_section}
{sensitivity_section}

<h2>Provenance</h2>
<table>
<tr><td>Data span</td><td>{start} to {end}</td></tr>
<tr><td>Measured period</td><td>{measured_period}</td></tr>
<tr><td>Config hash</td><td><code>{config_hash}</code></td></tr>
<tr><td>Code ref</td><td><code>{code_ref}</code></td></tr>
<tr><td>Data hash</td><td><code>{data_hash}</code></td></tr>
{earnings_hash_row}</table>

<footer>Generated at {generated_at}. Costs charged per side:
{slippage_bps} bps slippage plus {spread_atr_frac} x ATR spread.</footer>
</div>
</body>
</html>
"""


def _html_metric_table(metrics: dict[str, Any]) -> str:
    rows = [
        f"<tr><td>{label}</td><td>{format_metric(key, metrics[key])}</td></tr>"
        for key, (label, _unit) in METRIC_LABELS.items()
        if key in metrics
    ]
    return f"<table>{''.join(rows)}</table>"


def _html_section(title: str, body: str) -> str:
    return f"<h2>{title}</h2>\n{body}" if body else ""


def _html_notes(summary: Mapping[str, Any]) -> str:
    """The dependency notes, in the two costumes the stylesheet already owns.

    A loud note reuses ``.banner.warn`` — the same red the report already puts
    on a non-walk-forward run — and a quiet one is a ``.sub`` line, the muted
    13px the subtitle uses. No new selector and no new colour: the palette went
    through a WCAG AA pass with an automated guard
    (``tests/test_html_contrast.py``), and a diagnostic is not worth reopening
    it. The gradient between "obvious" and "unremarkable" is carried by two
    existing styles rather than by a scale invented here.
    """
    out: list[str] = []
    if legacy_blackout_banner(summary):
        out.append(_LEGACY_HTML_BANNER)
    for note in report_notes(summary):
        # Escaped because a summary is a file on disk that anything may have
        # written, and `source`, `bounded_policy` and `error` are free text.
        if note.blackout_absent:
            body = f"{_ABSENT_HTML_LEAD} {html.escape(_sentence(note.text), quote=False)}."
        else:
            body = (
                f"<strong>{html.escape(note.subject, quote=False)}:</strong> "
                f"{html.escape(note.text, quote=False)}."
            )
        if note.emphasise:
            out.append(f'<div class="banner warn">{body}</div>')
        else:
            out.append(f'<p class="sub">{body}</p>')
    return "\n".join(out)


def render_html(
    summary: dict[str, Any],
    equity: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    generated_at: datetime | None = None,
) -> str:
    """Render the self-contained HTML report.

    Args:
        summary: the summary dict about to be written as ``summary.json``.
        equity: the headline equity curve (out-of-sample when walk-forward).
        trades: the headline trade list. Only its length is used here; the full
            list is written to ``trades.csv`` beside this file.
        generated_at: stamped into the footer. Defaults to now — this is the
            ONLY wall-clock value in any report artefact.
    """
    del trades  # written separately; kept in the signature for callers' clarity
    walkforward = bool(summary.get("walkforward"))
    headline = summary.get("oos") if walkforward else summary.get("full_period")
    headline = headline or {}

    period = measured_period(summary)
    if walkforward:
        banner = (
            f"Walk-forward run: the headline numbers below are out-of-sample, cover {period}, "
            f"and are what the trading gate reads."
        )
        banner_class = "ok"
        headline_title = "Out-of-sample (headline)"
        # DEBT-016: only a walk-forward run has an out-of-sample curve to draw.
        chart_title = "Out-of-sample equity"
        equity_alt = "Out-of-sample equity curve"
    else:
        banner = (
            "NOT a walk-forward run. These numbers are in-sample and cannot open the trading gate."
        )
        banner_class = "warn"
        headline_title = "Full period (in-sample)"
        chart_title = "Full-period equity (in-sample)"
        equity_alt = "Full-period in-sample equity curve"

    full_period_section = ""
    if walkforward and summary.get("full_period"):
        full_period_section = _html_section(
            "Full period, config parameters (reference only)",
            _html_metric_table(summary["full_period"]),
        )

    by_year = summary.get("by_year") or {}
    by_year_section = ""
    if by_year:
        rows = "".join(
            f"<tr><td>{year}</td><td>{float(row.get('return_pct', 0.0)):.2f}%</td>"
            f"<td>{int(row.get('trades', 0))}</td>"
            f"<td>{float(row.get('max_dd_pct', 0.0)):.2f}%</td></tr>"
            for year, row in sorted(by_year.items())
        )
        by_year_section = _html_section(
            "By year",
            "<table><thead><tr><th>Year</th><th>Return</th><th>Trades</th>"
            f"<th>Max DD</th></tr></thead><tbody>{rows}</tbody></table>",
        )

    folds = summary.get("windows") or []
    folds_section = ""
    if folds:
        rows = "".join(
            "<tr>"
            f"<td>{fold.get('is_start')} to {fold.get('is_end')}</td>"
            f"<td>{fold.get('oos_start')} to {fold.get('oos_end')}</td>"
            f"<td>{', '.join(f'{k}={v}' for k, v in sorted((fold.get('params') or {}).items()))}"
            "</td>"
            f"<td>{float(fold.get('is_profit_factor', 0.0)):.2f}</td>"
            f"<td>{float(fold.get('oos_profit_factor', 0.0)):.2f}</td>"
            f"<td>{int(fold.get('oos_trades', 0))}</td>"
            "</tr>"
            for fold in folds
        )
        folds_section = _html_section(
            "Walk-forward folds",
            f"<p>{summary.get('objective', '')}</p>"
            + _scroll(
                "<table><thead><tr><th>In-sample</th><th>Out-of-sample</th><th>Chosen parameters"
                "</th><th>IS PF</th><th>OOS PF</th><th>OOS trades</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
            ),
        )

    sensitivity = summary.get("sensitivity") or []
    sensitivity_section = ""
    if sensitivity:
        rows = "".join(
            "<tr>"
            f"<td>{row.get('param')}</td><td>{row.get('variant')}</td><td>{row.get('value')}</td>"
            f"<td>{float(row.get('profit_factor', 0.0)):.2f}</td>"
            f"<td>{float(row.get('cagr', 0.0)):.2f}%</td>"
            f"<td>{float(row.get('max_drawdown_pct', 0.0)):.2f}%</td>"
            f"<td>{int(row.get('trades', 0))}</td>"
            "</tr>"
            for row in sensitivity
        )
        sensitivity_section = _html_section(
            "Sensitivity (+/-25%, one parameter at a time)",
            "<p>A result that only works at one setting is a curve fit. Look for a plateau.</p>"
            + _scroll(
                "<table><thead><tr><th>Parameter</th><th>Variant</th><th>Value</th>"
                "<th>Profit factor</th><th>CAGR</th><th>Max DD</th><th>Trades</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>"
            ),
        )

    charts_section = ""
    if not equity.empty:
        # Each chart sits on a "plate" painted the same colour as the PNG itself,
        # in both themes, so the image never reads as a hole punched in the page.
        charts_section = (
            "<h2>Equity</h2>\n"
            f'<figure class="plate"><img src="{_equity_chart(equity, title=chart_title)}" '
            f'alt="{equity_alt}"></figure>\n'
            f'<figure class="plate"><img src="{_drawdown_chart(equity)}" alt="Drawdown">'
            "</figure>"
        )

    stamp = generated_at or datetime.now()
    costs = summary.get("costs") or {}
    earnings_hash = _text(summary.get("earnings_hash"))
    # Absent on every report written before the hash existed, and left absent
    # rather than rendered blank so those files still produce the exact table
    # their reader knows.
    earnings_hash_row = (
        f"<tr><td>Earnings hash</td><td><code>{html.escape(earnings_hash, quote=False)}"
        "</code></td></tr>\n"
        if earnings_hash
        else ""
    )
    return _HTML_TEMPLATE.format(
        style=STYLESHEET,
        label=summary.get("label", "unlabelled"),
        universe=summary.get("universe", "unknown"),
        n_symbols=summary.get("n_symbols", "?"),
        start=summary.get("start", "?"),
        end=summary.get("end", "?"),
        measured_period=period,
        banner=banner,
        banner_class=banner_class,
        notes=_html_notes(summary),
        headline_title=headline_title,
        headline_table=_html_metric_table(headline),
        full_period_section=full_period_section,
        charts_section=charts_section,
        monthly_table=_monthly_table_html(equity),
        by_year_section=by_year_section,
        folds_section=folds_section,
        sensitivity_section=sensitivity_section,
        config_hash=summary.get("config_hash", ""),
        code_ref=summary.get("code_ref", "unknown"),
        data_hash=summary.get("data_hash", ""),
        earnings_hash_row=earnings_hash_row,
        generated_at=stamp.isoformat(timespec="seconds"),
        slippage_bps=costs.get("slippage_bps", "?"),
        spread_atr_frac=costs.get("spread_atr_frac", "?"),
    )


# ---------------------------------------------------------------------------
# terminal
# ---------------------------------------------------------------------------

#: Width of ``print_latest``'s label column, counting the two-space indent.
#: Set by the rows already there — ``universe``, ``measured``, ``data span``.
_TERM_LABEL = 13
#: Where a wrapped line resumes, and the width it wraps to.
_TERM_WIDTH = 96


def _print_note(note: Note) -> None:
    """One note as a labelled, wrapped terminal row.

    The subject is dropped: the label column already says ``earnings``, and
    printing "Earnings blackout" after it says the same word twice. A terminal
    has no red either, so emphasis is carried by the words — the loud texts all
    lead with what did not happen. What the terminal *can* get wrong is
    legibility: an unwrapped 200-character sentence reflows into the margin and
    stops being read at all, which for a diagnostic is the same as not printing
    it.
    """
    text = f"{_ABSENT_TERM_LEAD} {note.text}." if note.blackout_absent else f"{note.text}."
    label = f"  {note.label:<{_TERM_LABEL - 2}}"
    lines = textwrap.wrap(text, width=_TERM_WIDTH - _TERM_LABEL) or [""]
    print(f"{label}{lines[0]}")
    for line in lines[1:]:
        print(f"{' ' * _TERM_LABEL}{line}")


def print_latest(cfg: Config) -> None:
    """FROZEN CONTRACT 2 — print the most recent backtest summary and the gate verdict.

    Prints (rather than returns) because it is the body of ``swing report``. If
    there is no report yet it says so and explains how to make one, instead of
    raising at a user who has simply not run a backtest.
    """
    from swing.backtest import gate

    summary, problem = gate.read_latest(cfg)
    if summary is None:
        if problem is not None:
            # BUG-017: a hostile or half-written report is a refusal sentence,
            # not an OverflowError traceback out of `swing report`.
            print(f"The backtest report at {gate.latest_path(cfg)} cannot be used: {problem}.")
            print("Re-run `swing backtest` to regenerate it.")
            return
        print(f"No backtest report found at {gate.latest_path(cfg)}.")
        print("Run `swing backtest` first — the scanner will not emit picks without one.")
        return

    walkforward = bool(summary.get("walkforward"))
    print(f"Backtest: {summary.get('label', 'unlabelled')}")
    print(
        f"  universe   {summary.get('universe', 'unknown')} "
        f"({summary.get('n_symbols', '?')} symbols)"
    )
    print(f"  measured   {measured_period(summary)}")
    print(f"  data span  {summary.get('start', '?')} to {summary.get('end', '?')}")
    print(f"  method     {'walk-forward' if walkforward else 'full period (in-sample only)'}")
    print(f"  code_ref   {summary.get('code_ref', 'unknown')}")
    # `earnings_hash` is deliberately not printed here. The other two digests
    # are not either, this header is six lines a human scans rather than a
    # provenance section, and `swing report` shows one run — you cannot compare
    # two hashes on a surface that only ever displays one. It is in `report.md`
    # and `report.html` beside `config_hash` and `data_hash`, where comparing
    # them is the thing you are actually doing.
    if legacy_blackout_banner(summary):
        print(_LEGACY_TERM_BANNER)
    for note in report_notes(summary):
        _print_note(note)
    print()

    headline = (summary.get("oos") if walkforward else summary.get("full_period")) or {}
    heading = (
        f"Out-of-sample results ({measured_period(summary)})"
        if walkforward
        else "Full-period results (in-sample)"
    )
    print(heading)
    for key, (label, _unit) in METRIC_LABELS.items():
        if key in headline:
            print(f"  {label:<18} {format_metric(key, headline[key])}")
    print()

    by_year = summary.get("by_year") or {}
    if by_year:
        print("By year")
        for year in sorted(by_year):
            row = by_year[year]
            print(
                f"  {year}  return {float(row.get('return_pct', 0.0)):>8.2f}%   "
                f"trades {int(row.get('trades', 0)):>4}   "
                f"max DD {float(row.get('max_dd_pct', 0.0)):>6.2f}%"
            )
        print()

    verdict = gate.check(cfg)
    if verdict.passed:
        print("GATE: PASSED — the scanner may emit picks.")
    else:
        print("GATE: FAILED — the scanner will refuse to emit picks (use --force to override).")
        for reason in verdict.reasons:
            print(f"  - {reason}")
