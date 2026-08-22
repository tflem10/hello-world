"""REPORT-1 — the three renderings of a dependency's actual coverage.

``summary.json`` learnt to say how much of the earnings blackout really ran
(commit 3a6c833). ``report.py`` did not: all three surfaces still read the old
``earnings_blackout_simulated`` boolean, so **a run at 40% coverage rendered
byte-identically to one at 99.7%** — the same defect the boolean had, moved one
file downstream into the artefact a human actually opens.

:func:`test_the_defect_a_thin_run_no_longer_looks_like_a_complete_one` is that
sentence as an assertion, and it fails against ``report.py`` as it stood at
3a6c833.

The rest of this module is the surrounding contract:

* every state the ``earnings`` block can be in, including the two degraded
  sources, the vacuous ETF-only universe, and blocks that are half-written or
  hostile — a report that fails to render is worse than one with a gap in it;
* the thirty-six reports **already on disk**, which predate the block entirely
  and must keep rendering exactly as their readers know them;
* the threshold, held to the reasoning in :data:`EARNINGS_COVERAGE_FLOOR`
  rather than to a number typed twice;
* no new CSS. The stylesheet went through a WCAG AA pass with an automated
  guard (``tests/test_html_contrast.py``); these notes reuse two classes that
  were already there, and
  :func:`test_the_notes_introduce_no_new_css_class` fails if that stops
  being true.

Report tests otherwise live in ``tests/test_backtest_runner.py``, which drives
the real runner end to end. This module is deliberately the other half: the
renderers alone, over summaries constructed to sit exactly on the boundaries.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import pandas as pd
import pytest

from conftest import build_config
from swing.backtest import report
from swing.backtest.gate import latest_path
from swing.backtest.report import (
    EARNINGS_COVERAGE_FLOOR,
    STYLESHEET,
    earnings_note,
    legacy_blackout_banner,
    membership_note,
    print_latest,
    render_html,
    render_markdown,
    report_notes,
)

STAMP = datetime(2026, 1, 1, 12, 0, 0)

# ---------------------------------------------------------------------------
# fixtures: the shapes a summary really comes in
# ---------------------------------------------------------------------------

#: A report written **before** 3a6c833 — no ``earnings`` block, no
#: ``earnings_hash``, no ``membership``. Thirty-six of these are on disk right
#: now, and `reports/` is gitignored, so the shape is reproduced here rather
#: than read from one.
LEGACY: dict[str, Any] = {
    "label": "full-walkforward-r1",
    "universe": "full",
    "n_symbols": 1643,
    "walkforward": True,
    "start": "2009-01-02",
    "end": "2025-08-15",
    "oos_start": "2014-01-02",
    "oos_end": "2025-08-15",
    "config_hash": "c6782f8db70ba7e6" * 4,
    "code_ref": "3a6c83393fa66f55f6d2eb638830c4c3f9086a3e",
    "data_hash": "8b500dbf57760da1" * 4,
    "earnings_blackout_simulated": True,
    "oos": {"cagr": 6.5, "trades": 685, "profit_factor": 1.07},
    "costs": {"slippage_bps": 5.0, "spread_atr_frac": 0.05},
}

#: The shipping state at 3a6c833: 1,501 of 1,506 announcing symbols covered.
HEALTHY_EARNINGS: dict[str, Any] = {
    "source": "history",
    "symbols_requested": 1643,
    "symbols_exempt": 137,
    "symbols_applicable": 1506,
    "symbols_with_dates": 1501,
    "symbols_without_dates": 5,
    "coverage_pct": 99.667994,
    "announcements": 115045,
}

#: The shipping membership block: most stints rest on an approximation.
MEMBERSHIP: dict[str, Any] = {
    "mode": "off",
    "applied": False,
    "unknown_policy": "exclude",
    "symbols_gated": 1506,
    "symbols_ungated": 137,
    "symbols_excluded": 0,
    "bounded_policy": "unknown",
    "symbols_bounded_join": 350,
    "join_date_coverage_pct": 76.759628,
    "stint_date_quality": {
        "stints": 4912,
        "exact": 2019,
        "bounded": 2801,
        "undated": 92,
        "approximate_pct": 58.895765,
    },
    "member_years": 24096.0,
    "member_years_point_in_time": 14882.3,
}

EARNINGS_HASH = "4e1b0c9d2a7f5183b6c04ea9d7f21c8350ab9e6f4d2c11b78a03e59fc6d84b27"


def summary(**overrides: Any) -> dict[str, Any]:
    """A modern summary: the legacy skeleton plus whichever blocks are wanted."""
    out = dict(LEGACY)
    out["earnings"] = dict(HEALTHY_EARNINGS)
    out["earnings_hash"] = EARNINGS_HASH
    out["membership"] = dict(MEMBERSHIP)
    for key, value in overrides.items():
        if value is None and key in out:
            del out[key]
        else:
            out[key] = value
    return out


def covering(with_dates: int, applicable: int = 1506, **extra: Any) -> dict[str, Any]:
    """An ``earnings`` block at a chosen coverage, arithmetically consistent."""
    return {
        **HEALTHY_EARNINGS,
        "symbols_applicable": applicable,
        "symbols_with_dates": with_dates,
        "symbols_without_dates": applicable - with_dates,
        "coverage_pct": 100.0 * with_dates / applicable if applicable else 0.0,
        **extra,
    }


def surfaces(data: dict[str, Any]) -> tuple[str, str]:
    """Markdown and HTML for one summary, with the clock pinned."""
    return (
        render_markdown(data),
        render_html(data, pd.DataFrame(), pd.DataFrame(), generated_at=STAMP),
    )


def terminal(tmp_path, data: dict[str, Any], capsys) -> str:
    """``swing report`` for one summary, via the gate's real ``latest.json``."""
    cfg = build_config(tmp_path, gates={"min_trades": 1})
    path = latest_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    print_latest(cfg)
    return capsys.readouterr().out


# ---------------------------------------------------------------------------
# 1. the defect
# ---------------------------------------------------------------------------


def test_the_defect_a_thin_run_no_longer_looks_like_a_complete_one(tmp_path, capsys) -> None:
    """REPORT-1 in one assertion, on all three surfaces at once.

    Both runs below set ``earnings_blackout_simulated: true`` — legitimately,
    because the mechanism did operate — and before this change that boolean was
    the only thing any renderer read. One blackout reached 1,501 of 1,506
    announcing symbols; the other reached 602. The reports were the same file.
    """
    complete = summary(earnings=covering(1501))
    thin = summary(earnings=covering(602))
    assert complete["earnings_blackout_simulated"] is thin["earnings_blackout_simulated"] is True

    complete_md, complete_html = surfaces(complete)
    thin_md, thin_html = surfaces(thin)
    assert complete_md != thin_md
    assert complete_html != thin_html

    complete_term = terminal(tmp_path, complete, capsys)
    thin_term = terminal(tmp_path, thin, capsys)
    assert complete_term != thin_term

    # And not merely different — the difference is the number a reader needs.
    for rendered in (thin_md, thin_html, thin_term):
        assert "602" in rendered and "1,506" in rendered and "40.0%" in rendered
        assert "904" in rendered, "the symbols that escaped the blackout are the point"
    for rendered in (complete_md, complete_html, complete_term):
        assert "1,501" in rendered and "99.7%" in rendered
        assert "115,045" in rendered, "the announcement total distinguishes one date from sixty"


@pytest.mark.parametrize("surface", ["md", "html", "term"])
def test_coverage_reaches_every_surface_as_a_count_and_a_percentage(
    tmp_path, capsys, surface: str
) -> None:
    data = summary()
    rendered = {
        "md": lambda: surfaces(data)[0],
        "html": lambda: surfaces(data)[1],
        "term": lambda: terminal(tmp_path, data, capsys),
    }[surface]()
    assert "1,501" in rendered
    assert "1,506" in rendered
    assert "99.7%" in rendered
    assert "115,045" in rendered


# ---------------------------------------------------------------------------
# 2. the threshold, and the two volumes either side of it
# ---------------------------------------------------------------------------


def test_the_shipping_coverage_reads_as_unremarkable() -> None:
    """99.67% is the practical ceiling: five names have no free history at all.

    A threshold that fired here would fire on every stocks run forever, and a
    banner that is always on is one nobody reads.
    """
    note = earnings_note(summary())
    assert note is not None and not note.emphasise

    markdown, rendered_html = surfaces(summary())
    # Quiet notes join the header bullets; they do not become blockquotes.
    assert "- **Earnings blackout**: covered 1,501 of the 1,506 announcing symbols" in markdown
    assert "> **Earnings blackout" not in markdown
    assert '<p class="sub"><strong>Earnings blackout:</strong>' in rendered_html
    assert 'banner warn"><strong>Earnings' not in rendered_html


@pytest.mark.parametrize(
    ("coverage", "loud"),
    [
        (8.0, True),  # the cold cache this whole block exists to expose
        (40.0, True),
        (94.9, True),
        (EARNINGS_COVERAGE_FLOOR, False),  # the boundary belongs to the quiet side
        (99.667994, False),  # the shipping state
        (100.0, False),
    ],
)
def test_the_threshold_is_the_one_the_constant_documents(coverage: float, loud: bool) -> None:
    """Read off :data:`EARNINGS_COVERAGE_FLOOR`, never restated as a literal."""
    block = {**HEALTHY_EARNINGS, "coverage_pct": coverage}
    note = earnings_note(summary(earnings=block))
    assert note is not None
    assert note.emphasise is loud, f"{coverage}% should be {'loud' if loud else 'quiet'}"


def test_a_loud_note_is_a_banner_and_a_quiet_one_is_not() -> None:
    """The volumes are two existing styles, not two shades of one."""
    loud_md, loud_html = surfaces(summary(earnings=covering(602)))
    quiet_md, quiet_html = surfaces(summary(earnings=covering(1501)))

    assert "> **Earnings blackout:**" in loud_md
    assert '<div class="banner warn"><strong>Earnings blackout:</strong>' in loud_html
    assert "- **Earnings blackout**:" in quiet_md
    assert '<p class="sub"><strong>Earnings blackout:</strong>' in quiet_html


# ---------------------------------------------------------------------------
# 3. every state the block can be in
# ---------------------------------------------------------------------------


def test_upcoming_only_is_loud_even_at_full_coverage() -> None:
    """A12: the next announcement per symbol cannot block a single historical bar.

    This is why coverage alone is not enough — the block would score 100% and
    the blackout would still be entirely absent from the simulation.
    """
    block = {**HEALTHY_EARNINGS, "source": "upcoming_only", "coverage_pct": 100.0}
    note = earnings_note(summary(earnings=block, earnings_blackout_simulated=False))
    assert note is not None and note.emphasise and note.blackout_absent
    assert "cannot block a historical bar" in note.text


def test_an_unavailable_lookup_is_loud_and_says_so() -> None:
    block = {**covering(0), "source": "unavailable"}
    note = earnings_note(summary(earnings=block, earnings_blackout_simulated=False))
    assert note is not None and note.emphasise
    assert "lookup failed" in note.text


def test_a_cold_history_cache_keeps_the_wording_its_readers_know() -> None:
    """``source: history`` with nothing back. Both legacy spellings survive.

    ``tests/test_backtest_runner.py`` asserts these exact strings against a real
    run; they are load-bearing, and the coverage numbers arrive attached to them
    rather than in a second banner underneath saying the same thing.
    """
    data = summary(earnings=covering(0), earnings_blackout_simulated=False)
    markdown, rendered_html = surfaces(data)

    assert "Earnings blackout not simulated" in markdown
    assert "Earnings blackout NOT simulated" in rendered_html
    assert markdown.count("Earnings blackout not simulated") == 1
    assert rendered_html.count("Earnings blackout NOT simulated") == 1
    assert "1,506" in markdown and "1,506" in rendered_html


def test_an_etf_only_universe_is_not_warned_about_earnings_it_cannot_have() -> None:
    """``symbols_applicable: 0`` reports ``coverage_pct: 0.0``, and 0% is not a hole.

    The ETF-only run is the survivorship lower bound (methodology 7.4) — the one
    run in this repo free of the biases such a warning is about. Reading its
    empty denominator as "0% covered" would put a red banner on it forever.
    """
    block = {
        "source": "history",
        "symbols_requested": 137,
        "symbols_exempt": 137,
        "symbols_applicable": 0,
        "symbols_with_dates": 0,
        "symbols_without_dates": 0,
        "coverage_pct": 0.0,
        "announcements": 0,
    }
    note = earnings_note(summary(earnings=block))
    assert note is not None
    assert not note.emphasise
    assert "not applicable" in note.text

    markdown, rendered_html = surfaces(summary(earnings=block))
    assert "0.0%" not in markdown
    assert "Earnings blackout not simulated" not in markdown
    assert "Earnings blackout NOT simulated" not in rendered_html


def test_an_unrecognised_source_is_quoted_rather_than_guessed_at() -> None:
    note = earnings_note(summary(earnings={**HEALTHY_EARNINGS, "source": "from_a_new_vendor"}))
    assert note is not None
    assert 'recorded source: "from_a_new_vendor"' in note.text


def test_the_earnings_source_names_match_the_runners() -> None:
    """``report.py`` cannot import the runner — the runner imports it.

    So the three source spellings are mirrored, and mirrored constants go stale
    silently. This re-reads the originals rather than restating them.
    """
    from swing.backtest import runner

    assert report.EARNINGS_FROM_HISTORY == runner.EARNINGS_FROM_HISTORY
    assert report.EARNINGS_FROM_UPCOMING == runner.EARNINGS_FROM_UPCOMING
    assert report.EARNINGS_UNAVAILABLE == runner.EARNINGS_UNAVAILABLE


# ---------------------------------------------------------------------------
# 4. the reports already on disk
# ---------------------------------------------------------------------------


def test_a_pre_coverage_report_gains_nothing_and_loses_nothing() -> None:
    """No block, no note, no new row. These files are on disk and get opened."""
    assert report_notes(LEGACY) == []
    assert earnings_note(LEGACY) is None
    assert membership_note(LEGACY) is None

    markdown, rendered_html = surfaces(LEGACY)
    assert "Earnings blackout" not in markdown
    assert "Index membership" not in markdown
    assert "Earnings hash" not in markdown
    assert "Earnings hash" not in rendered_html
    # Provenance keeps the five rows it has always had.
    provenance = rendered_html.split("<h2>Provenance</h2>")[1].split("</table>")[0]
    assert provenance.count("<tr><td>") == 5


def test_the_pre_coverage_banner_is_reproduced_verbatim() -> None:
    """The exact bytes, line breaks included, that these reports carry."""
    data = {**LEGACY, "earnings_blackout_simulated": False}
    assert legacy_blackout_banner(data)
    markdown, rendered_html = surfaces(data)

    assert (
        "> **Earnings blackout not simulated.** No historical announcement dates were\n"
        "> available, so the backtest took entries the live scanner would have blocked.\n"
        "> Results are slightly optimistic against the strategy as it is actually run.\n"
    ) in markdown
    assert (
        '<div class="banner warn">Earnings blackout NOT simulated: no historical '
        "announcement dates were available, so this run took entries the live scanner "
        "would have blocked.</div>"
    ) in rendered_html


def test_the_two_earnings_banners_never_appear_together() -> None:
    """A summary with a block gets the headline folded into the coverage line."""
    data = summary(earnings=covering(0), earnings_blackout_simulated=False)
    assert not legacy_blackout_banner(data)
    markdown, rendered_html = surfaces(data)
    assert "No historical announcement dates were" not in markdown
    assert "no historical announcement dates were available" not in rendered_html


def test_the_legacy_banner_survives_a_membership_only_summary() -> None:
    """A block for one dependency must not silence the other's fallback."""
    data = {**LEGACY, "earnings_blackout_simulated": False, "membership": dict(MEMBERSHIP)}
    assert legacy_blackout_banner(data)
    markdown, _ = surfaces(data)
    assert "No historical announcement dates were" in markdown
    assert "Index membership" in markdown


# ---------------------------------------------------------------------------
# 5. earnings_hash in provenance
# ---------------------------------------------------------------------------


def test_the_earnings_hash_sits_with_the_other_three() -> None:
    markdown, rendered_html = surfaces(summary())
    assert f"- **Earnings hash**: `{EARNINGS_HASH[:16]}`" in markdown
    assert markdown.index("Config hash") < markdown.index("Earnings hash")
    assert f"<tr><td>Earnings hash</td><td><code>{EARNINGS_HASH}</code></td></tr>" in rendered_html
    assert rendered_html.index("Data hash") < rendered_html.index("Earnings hash")


def test_an_unavailable_digest_is_shown_rather_than_hidden() -> None:
    """``"unavailable"`` is a fact about the run, and it is deliberately not 64 hex."""
    markdown, rendered_html = surfaces(summary(earnings_hash="unavailable"))
    assert "- **Earnings hash**: `unavailable`" in markdown
    assert "<tr><td>Earnings hash</td><td><code>unavailable</code></td></tr>" in rendered_html


@pytest.mark.parametrize("value", [None, "", "   ", 12345, {"a": 1}])
def test_a_missing_or_junk_digest_leaves_the_table_alone(value: Any) -> None:
    data = summary()
    if value is None:
        del data["earnings_hash"]
    else:
        data["earnings_hash"] = value
    markdown, rendered_html = surfaces(data)
    assert "Earnings hash" not in markdown
    assert "Earnings hash" not in rendered_html


# ---------------------------------------------------------------------------
# 6. membership
# ---------------------------------------------------------------------------


def test_membership_shows_the_approximation_the_reader_is_standing_on() -> None:
    """58.9% of stints rest on a bound or a missing date. That belongs on the page."""
    markdown, rendered_html = surfaces(summary())
    for rendered in (markdown, rendered_html):
        assert "58.9%" in rendered
        assert "4,912" in rendered
        assert "350" in rendered
        assert 'read as "unknown"' in rendered
        assert "today's index members are traded through all of history" in rendered


def test_the_default_membership_mode_is_stated_but_not_shouted() -> None:
    """``off`` is the documented default; a banner on every run would be noise."""
    note = membership_note(summary())
    assert note is not None and not note.emphasise


def test_point_in_time_membership_names_what_it_dropped() -> None:
    block = {**MEMBERSHIP, "mode": "point_in_time", "applied": True, "symbols_excluded": 128}
    note = membership_note(summary(membership=block))
    assert note is not None and not note.emphasise
    assert "point-in-time" in note.text
    assert "128 symbols excluded" in note.text


def test_unreadable_membership_files_are_loud_because_the_bias_is_unmeasured() -> None:
    """The one loud membership state: zeros that are not a measurement of no bias."""
    block = {
        "mode": "off",
        "applied": False,
        "unknown_policy": "exclude",
        "bounded_policy": "unknown",
        "error": "sp500.csv: line 41 has 3 fields, expected 4",
    }
    note = membership_note(summary(membership=block))
    assert note is not None and note.emphasise
    assert "line 41 has 3 fields" in note.text

    _markdown, rendered_html = surfaces(summary(membership=block))
    assert '<div class="banner warn"><strong>Index membership:</strong>' in rendered_html


def test_a_zero_bounded_count_is_left_out_rather_than_printed() -> None:
    """ "0 symbols joined on a bounded date" is not a finding, it is noise."""
    block = {**MEMBERSHIP, "symbols_bounded_join": 0}
    note = membership_note(summary(membership=block))
    assert note is not None
    assert "bounded" not in note.text
    assert "58.9%" in note.text


def test_a_membership_block_with_nothing_usable_in_it_says_nothing() -> None:
    assert membership_note(summary(membership={})) is None
    assert membership_note(summary(membership={"applied": False})) is None


# ---------------------------------------------------------------------------
# 7. a diagnostic must never be the thing that kills the report
# ---------------------------------------------------------------------------

MALFORMED: list[Any] = [
    {},
    {"source": "history"},
    {"source": None, "coverage_pct": None},
    {"coverage_pct": "not a number", "symbols_applicable": "lots"},
    {"symbols_applicable": -5, "symbols_with_dates": -1},
    {"symbols_applicable": float("nan"), "coverage_pct": float("inf")},
    {"symbols_applicable": True, "symbols_with_dates": False},
    {"symbols_with_dates": 1501, "symbols_without_dates": 5},  # no applicable: derive it
    {"symbols_applicable": 1506, "symbols_with_dates": 1501},  # no pct: derive it
    {"symbols_applicable": 1506, "coverage_pct": 300.0},  # impossible, clamped
    {"source": ["history"], "announcements": {"a": 1}},
    "history",
    ["history"],
    42,
    None,
]


@pytest.mark.parametrize("block", MALFORMED, ids=[str(b)[:40] for b in MALFORMED])
def test_no_earnings_block_however_broken_can_stop_a_report(block: Any) -> None:
    data = summary(earnings=block) if block is not None else summary(earnings={})
    markdown, rendered_html = surfaces(data)
    assert markdown.startswith("# Backtest")
    assert rendered_html.startswith("<!doctype html>")
    assert "Out-of-sample (headline)" in markdown


@pytest.mark.parametrize(
    "block",
    [
        {},
        {"mode": 7},
        {"mode": "off", "stint_date_quality": "some"},
        {"mode": "off", "stint_date_quality": {"approximate_pct": "x", "stints": "y"}},
        {"mode": "off", "symbols_bounded_join": "many", "bounded_policy": 3},
        {"error": 404},
        {"error": ""},
        "off",
        None,
    ],
    ids=lambda b: str(b)[:40],
)
def test_no_membership_block_however_broken_can_stop_a_report(block: Any) -> None:
    data = summary(membership=block) if block is not None else summary(membership={})
    markdown, rendered_html = surfaces(data)
    assert markdown.startswith("# Backtest")
    assert rendered_html.startswith("<!doctype html>")


def test_a_derivable_block_is_completed_rather_than_abandoned() -> None:
    """Any two of applicable/covered/uncovered give the third."""
    note = earnings_note(
        summary(
            earnings={"source": "history", "symbols_with_dates": 602, "symbols_without_dates": 904}
        )
    )
    assert note is not None and note.emphasise
    assert "602" in note.text and "1,506" in note.text and "40.0%" in note.text


def test_a_block_with_no_coverage_in_it_admits_that_instead_of_inventing_one() -> None:
    note = earnings_note(summary(earnings={"source": "history"}))
    assert note is not None
    assert not note.emphasise, "a writer's bug is not evidence of a bad run"
    assert "does not record" in note.text


def test_a_hostile_summary_cannot_inject_markup_into_the_html() -> None:
    """``summary.json`` is a file on disk; ``source`` and ``error`` are free text."""
    data = summary(
        earnings={**HEALTHY_EARNINGS, "source": "<script>alert(1)</script>"},
        membership={"mode": "off", "error": "<img src=x onerror=alert(1)>"},
    )
    _markdown, rendered_html = surfaces(data)
    # The angle brackets are what make markup; neutering them is the whole job.
    # `onerror=` surviving as literal text inside an escaped tag is inert, and
    # escaping `=` would only make the note unreadable.
    assert "<script>" not in rendered_html
    assert "<img" not in rendered_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered_html
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered_html


# ---------------------------------------------------------------------------
# 8. honesty of the numbers themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (99.667994, "99.7%"),
        (99.96, "just under 100%"),  # NOT "100.0%": five symbols are still missing
        (100.0, "100.0%"),
        (0.0, "0.0%"),
        (0.04, "under 0.1%"),  # NOT "0.0%": one symbol is still covered
        (58.895765, "58.9%"),
    ],
)
def test_a_percentage_never_rounds_the_gap_away(value: float, expected: str) -> None:
    assert report._pct(value) == expected


def test_the_near_miss_reaches_the_page_as_a_near_miss() -> None:
    """One uncovered name in three thousand still rounds to 100.0% at one decimal.

    On the shipping universe the five missing names come to 99.93%, which one
    decimal renders honestly on its own; the guard is for the denominators where
    it would not.
    """
    block = covering(2999, applicable=3000)  # 99.9667%
    markdown, _ = surfaces(summary(earnings=block))
    assert "100.0%" not in markdown
    assert "just under 100%" in markdown

    shipping, _ = surfaces(summary())
    assert "99.7%" in shipping


def test_counts_are_pluralised() -> None:
    one = earnings_note(summary(earnings=covering(1, applicable=1, announcements=1)))
    assert one is not None
    assert "1 announcing symbol (100.0%), from 1 announcement date" in one.text
    assert "symbols" not in one.text and "dates" not in one.text


# ---------------------------------------------------------------------------
# 9. the terminal
# ---------------------------------------------------------------------------


def test_the_terminal_labels_each_note_without_saying_it_twice(tmp_path, capsys) -> None:
    out = terminal(tmp_path, summary(), capsys)
    assert "  earnings   covered 1,501 of the 1,506 announcing symbols (99.7%)" in out
    assert "  membership not applied" in out
    # The label column is the subject; repeating it in the sentence is noise.
    assert "earnings   Earnings blackout" not in out


def test_the_terminal_wraps_instead_of_running_off_the_screen(tmp_path, capsys) -> None:
    out = terminal(tmp_path, summary(earnings=covering(602)), capsys)
    body = [line for line in out.splitlines() if line.startswith(("  earnings", " " * 13))]
    assert len(body) > 1, "a 200-character diagnostic must wrap"
    assert all(len(line) <= 96 for line in body)
    # Continuations line up under the text, not under the label.
    assert all(line.startswith(" " * 13) for line in body[1:])


def test_the_terminal_keeps_its_own_legacy_line(tmp_path, capsys) -> None:
    out = terminal(tmp_path, {**LEGACY, "earnings_blackout_simulated": False}, capsys)
    assert "  note       earnings blackout NOT simulated — results are slightly optimistic" in out


def test_the_terminal_folds_the_headline_into_the_note_when_there_is_a_block(
    tmp_path, capsys
) -> None:
    data = summary(earnings=covering(0), earnings_blackout_simulated=False)
    out = terminal(tmp_path, data, capsys)
    assert "  note       earnings blackout NOT simulated" not in out
    assert "  earnings   NOT simulated —" in out
    assert "1,506" in out


def test_the_terminal_still_prints_a_gate_verdict_after_all_this(tmp_path, capsys) -> None:
    out = terminal(tmp_path, summary(), capsys)
    assert "GATE:" in out


# ---------------------------------------------------------------------------
# 10. the stylesheet is not part of this change
# ---------------------------------------------------------------------------

_CLASS = re.compile(r'class="([^"]+)"')


def test_the_notes_introduce_no_new_css_class() -> None:
    """Every class the notes use was already in the stylesheet and already measured.

    ``tests/test_html_contrast.py`` parses ``STYLESHEET`` and fails any pair
    below 4.5:1. A note that invented ``.warn-amber`` would sail past that suite
    by never appearing in the stylesheet at all, so the check has to run from
    the other end: the rendered document.
    """
    declared = set(re.findall(r"\.([A-Za-z][\w-]*)", STYLESHEET))
    for data in (
        LEGACY,
        {**LEGACY, "earnings_blackout_simulated": False},
        summary(),
        summary(earnings=covering(602)),
        summary(earnings=covering(0), earnings_blackout_simulated=False),
        summary(membership={"mode": "off", "error": "unreadable"}),
    ):
        _markdown, rendered_html = surfaces(data)
        used = {name for attr in _CLASS.findall(rendered_html) for name in attr.split()}
        assert used <= declared, f"undeclared class(es): {sorted(used - declared)}"


def test_the_stylesheet_is_delivered_unchanged() -> None:
    """The notes ride on existing styles; nothing here edits the palette."""
    _markdown, rendered_html = surfaces(summary(earnings=covering(602)))
    assert STYLESHEET in rendered_html
    assert "--bad-bg" in STYLESHEET and "--bad-ink" in STYLESHEET


# ---------------------------------------------------------------------------
# 11. the rest of the report still works
# ---------------------------------------------------------------------------


def test_the_notes_do_not_displace_anything_that_was_already_there() -> None:
    data = summary()
    data["by_year"] = {"2024": {"return_pct": 4.2, "trades": 51, "max_dd_pct": -6.1}}
    markdown, rendered_html = surfaces(data)

    for rendered in (markdown, rendered_html):
        assert "Out-of-sample" in rendered
        assert "2014-01-02 to 2025-08-15" in rendered
        assert "By year" in rendered
    assert "Generated at 2026-01-01T12:00:00" in rendered_html


def test_a_report_with_charts_still_renders_around_the_notes() -> None:
    equity = pd.DataFrame(
        {"equity": [10_000.0, 10_400.0, 9_900.0], "drawdown": [0.0, 0.0, -0.048]},
        index=pd.bdate_range(date(2024, 1, 1), periods=3),
    )
    rendered = render_html(summary(earnings=covering(602)), equity, pd.DataFrame())
    assert "data:image/png;base64," in rendered
    assert '<div class="banner warn"><strong>Earnings blackout:</strong>' in rendered
