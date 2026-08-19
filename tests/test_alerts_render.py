"""Rendering the pick sheet: Markdown, HTML, push summary and SMS.

Two things get the most attention here because they are the two ways this
report can quietly lie to its user: the watch list must be prominent (on a $100
account it *is* the report), and an unknown earnings date must be tagged rather
than silently treated as "no earnings".
"""

from __future__ import annotations

import pytest

from swing.alerts import render


def record(**overrides) -> dict:
    values = {
        "symbol": "ABC",
        "date": "2026-08-18",
        "kind": "pick",
        "entry": 45.10,
        "stop": 41.80,
        "shares": 2,
        "risk_amount": 6.60,
        "score": 1.234,
        "atr": 1.65,
        "earnings_date": "2026-09-02",
        "earnings_known": True,
        "thesis": "Broke the 20d high on 1.6x average volume.",
        "status": "drafted",
    }
    values.update(overrides)
    return values


def report(**overrides) -> dict:
    values = {
        "generated_at": "2026-08-18T17:30:00-04:00",
        "asof": "2026-08-18",
        "equity": 100.0,
        "regime_ok": True,
        "gate": {"passed": True, "reasons": []},
        "picks": [record()],
        "watch": [
            record(
                symbol="XYZ",
                kind="watch",
                entry=310.0,
                stop=295.0,
                shares=0,
                risk_amount=0.0,
                earnings_date=None,
                earnings_known=False,
            )
        ],
    }
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------


def test_markdown_lists_picks_with_the_numbers_that_matter() -> None:
    text = render.render_markdown(report())
    assert "# Swing scan — 2026-08-18" in text
    assert "**ABC**" in text
    assert "$45.10" in text  # entry
    assert "$41.80" in text  # stop
    assert "$6.60" in text  # risk dollars
    assert "6.6%" in text  # risk as a percent of the account
    for header in ("Entry", "Stop", "Shares", "Risk $", "Risk % of account", "Earnings"):
        assert header in text


def test_markdown_gives_the_watch_list_its_own_heading_and_reason() -> None:
    text = render.render_markdown(report())
    assert "## Watch — passed every rule, sized to zero shares (1)" in text
    assert "XYZ" in text
    # The watch section explains itself rather than being a bare table.
    assert "single share" in text
    # It comes after picks but is a peer heading, not a nested aside.
    assert text.index("## Picks") < text.index("## Watch")


def test_markdown_tags_unknown_earnings_dates() -> None:
    text = render.render_markdown(report())
    assert "UNKNOWN" in text
    assert "earnings blackout could not be applied" in text


def test_markdown_states_a_failing_gate_first() -> None:
    text = render.render_markdown(
        report(
            picks=[],
            watch=[],
            gate={"passed": False, "reasons": ["No walk-forward backtest has been run."]},
        )
    )
    assert "NOT PASSED" in text
    assert "No walk-forward backtest has been run." in text
    assert "Nothing is tradable tonight." in text


def test_markdown_says_when_the_regime_is_off() -> None:
    text = render.render_markdown(report(regime_ok=False, picks=[], watch=[]))
    assert "entries BLOCKED" in text


def test_markdown_includes_notes() -> None:
    text = render.render_markdown(report(), notes=["All four position slots are full."])
    assert "## Notes" in text
    assert "All four position slots are full." in text


def test_markdown_handles_a_completely_empty_report() -> None:
    text = render.render_markdown(
        {
            "generated_at": "",
            "asof": "2026-08-18",
            "equity": 100.0,
            "regime_ok": False,
            "gate": {"passed": False, "reasons": []},
            "picks": [],
            "watch": [],
        }
    )
    assert "Nothing is tradable tonight." in text
    assert "Nothing reached the watch list either." in text


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------


def test_html_is_a_complete_self_contained_document() -> None:
    html = render.render_html(report())
    assert html.lstrip().startswith("<!doctype html>")
    assert "</html>" in html.strip()[-20:]
    assert "<style>" in html
    # Self-contained: no external stylesheets, scripts, images or fonts.
    for forbidden in ("<script", 'src="http', 'href="http', "@import", "cdn."):
        assert forbidden not in html


def test_html_is_dark_friendly() -> None:
    html = render.render_html(report())
    assert "prefers-color-scheme: dark" in html
    assert "color-scheme: light dark" in html


def test_html_tables_carry_entry_stop_shares_risk_and_earnings() -> None:
    html = render.render_html(report())
    for header in ("Entry", "Stop", "Shares", "Risk $", "Risk % acct", "Earnings"):
        assert f">{header}<" in html
    assert "$45.10" in html
    assert "2026-09-02" in html


def test_html_flags_unknown_earnings_with_a_visible_tag() -> None:
    html = render.render_html(report())
    assert '<span class="tag">UNKNOWN</span>' in html
    assert "Check the date yourself before entering." in html


def test_html_watch_section_is_a_peer_of_picks() -> None:
    html = render.render_html(report())
    assert "Watch — passed every rule, sized to zero shares (1)" in html
    assert 'class="watch"' in html
    assert html.index("Picks (1)") < html.index("Watch —")


def test_html_embeds_order_drafts_when_given() -> None:
    orders = {"ABC": {"oto_stop": {"orderType": "LIMIT"}}}
    html = render.render_html(report(), orders=orders)
    assert "Drafted Schwab orders" in html
    assert "<summary>ABC</summary>" in html
    assert "&#34;orderType&#34;: &#34;LIMIT&#34;" in html or '"orderType": "LIMIT"' in html


def test_html_escapes_hostile_content() -> None:
    hostile = record(symbol="ABC", thesis="<script>alert(1)</script>")
    html = render.render_html(report(picks=[hostile]))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_shows_gate_chips() -> None:
    passing = render.render_html(report())
    assert "Backtest gate PASSED" in passing
    failing = render.render_html(report(gate={"passed": False, "reasons": ["nope"]}))
    assert "Backtest gate NOT PASSED" in failing
    assert "nope" in failing


# ---------------------------------------------------------------------------
# short forms
# ---------------------------------------------------------------------------


def test_summary_title_counts_picks_and_watch() -> None:
    assert render.summary_title(report()) == "SWING 2026-08-18: 1 picks, 1 watch"


def test_summary_title_leads_with_the_blocking_reason() -> None:
    blocked = report(picks=[], watch=[], gate={"passed": False, "reasons": []})
    assert "gate not passed" in render.summary_title(blocked)
    regime_off = report(picks=[], watch=[], regime_ok=False)
    assert "regime off" in render.summary_title(regime_off)


def test_summary_text_is_short_and_lists_both_sections() -> None:
    text = render.summary_text(report(), notes=["a note"])
    assert "**Picks (1)**" in text
    assert "ABC 2sh @ $45.10 stop $41.80" in text
    assert "**Watch — 0 shares affordable (1)**" in text
    assert "a note" in text
    assert len(text.splitlines()) < 20


def test_sms_matches_the_documented_shape_and_length() -> None:
    line = render.render_sms(report())
    assert line.startswith("SWING 2026-08-18: 1 picks: ABC 2sh@45.10 stop 41.80")
    assert "Watch (1): XYZ" in line
    assert "\n" not in line
    assert len(line) <= render.SMS_MAX_CHARS


def test_sms_explains_a_blocked_scan() -> None:
    assert "gate NOT passed" in render.render_sms(
        report(picks=[], watch=[], gate={"passed": False, "reasons": []})
    )
    assert "regime off" in render.render_sms(report(picks=[], watch=[], regime_ok=False))


def test_sms_is_clipped_to_the_gateway_limit() -> None:
    many = [record(symbol=f"SYM{i:03d}") for i in range(60)]
    line = render.render_sms(report(picks=many, watch=[]))
    assert len(line) <= render.SMS_MAX_CHARS
    assert line.endswith("…")


def test_clip_leaves_short_text_alone() -> None:
    assert render.clip("short") == "short"


# ---------------------------------------------------------------------------
# confirmation
# ---------------------------------------------------------------------------


def confirm_payload() -> dict:
    return {
        "asof": "2026-08-19",
        "results": {
            "ABC": {"quote": 45.50, "status": "confirmed", "reason": "still within one ATR."},
            "XYZ": {"quote": 60.00, "status": "invalidated", "reason": "gapped past the entry."},
            "QQQ": {"quote": None, "status": "unknown", "reason": "No quote came back."},
        },
    }


def test_confirm_markdown_counts_every_outcome() -> None:
    text = render.render_confirm_markdown(confirm_payload())
    assert "**Confirmed:** 1" in text
    assert "**Invalidated:** 1" in text
    assert "**No quote available:** 1" in text
    assert "gapped past the entry." in text
    assert "$45.50" in text


def test_confirm_markdown_orders_symbols_deterministically() -> None:
    text = render.render_confirm_markdown(confirm_payload())
    assert text.index("**ABC**") < text.index("**QQQ**") < text.index("**XYZ**")


def test_confirm_title_and_sms_are_one_line_each() -> None:
    title = render.render_confirm_title(confirm_payload())
    assert title == "SWING confirm 2026-08-19: 1 confirmed, 1 invalidated"
    sms = render.render_confirm_sms(confirm_payload())
    assert "\n" not in sms
    assert "ABC confirmed" in sms
    assert "XYZ invalidated" in sms
    assert len(sms) <= render.SMS_MAX_CHARS


def test_confirm_handles_nothing_to_confirm() -> None:
    payload = {"asof": "2026-08-19", "results": {}}
    assert "no drafted picks left" in render.render_confirm_markdown(payload)
    assert "nothing to confirm" in render.render_confirm_sms(payload)


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(45.1, "$45.10"), (0, "$0.00"), (1234.5, "$1,234.50"), (None, "—"), ("nope", "—")],
)
def test_money_formatting(value, expected) -> None:
    assert render.money(value) == expected


def test_pct_and_num_tolerate_missing_values() -> None:
    assert render.pct(6.6) == "6.6%"
    assert render.pct(None) == "—"
    assert render.num(1.23456, 3) == "1.235"
    assert render.num(float("nan")) == "—"


def test_rendering_is_deterministic() -> None:
    payload = report()
    assert render.render_markdown(payload) == render.render_markdown(payload)
    assert render.render_html(payload) == render.render_html(payload)


# ---------------------------------------------------------------------------
# audit regressions
# ---------------------------------------------------------------------------


def test_the_view_model_holds_only_what_a_template_reads() -> None:
    """Audit DEBT-017: 7 of 18 keys had no reader; `notional_pct` had never had one."""
    row = render._row(record(), equity=100.0)
    assert set(row) == {
        "symbol",
        "entry",
        "stop",
        "shares",
        "risk_per_share",
        "risk_amount",
        "risk_pct",
        "notional",
        "earnings_label",
        "earnings_unknown",
        "thesis",
    }


def test_autoescaping_is_decided_by_the_suffix_not_a_substring() -> None:
    """Audit DEBT-017: `".html" in name` also matched a Markdown template."""
    assert render._autoescape("picks.html.j2") is True
    assert render._autoescape("picks.html") is True
    assert render._autoescape("picks.md.j2") is False
    assert render._autoescape("how-to-read-the-html.md.j2") is False
    assert render._autoescape(None) is False


def test_templates_are_found_through_the_package_not_a_filesystem_path() -> None:
    """Audit DEBT-017: FileSystemLoader(str(Traversable)) breaks a zipped install."""
    from jinja2 import PackageLoader

    environment = render._environment()
    assert isinstance(environment.loader, PackageLoader)
    assert "picks.md.j2" in set(environment.list_templates())
    assert render.template_dir().is_dir()  # still true for a normal checkout


def test_confirm_markdown_shows_the_picks_it_left_alone() -> None:
    """Audit BUG-018/BUG-019: skipped picks used to be invisible."""
    payload = confirm_payload() | {
        "skipped": {"OLD": "The journal already records this pick as ordered."}
    }
    text = render.render_confirm_markdown(payload)
    assert "## Left alone (1)" in text
    assert "**OLD**" in text
    assert "already records this pick as ordered" in text


def test_confirm_rendering_ignores_a_missing_skipped_key() -> None:
    text = render.render_confirm_markdown(confirm_payload())
    assert "Left alone" not in text
