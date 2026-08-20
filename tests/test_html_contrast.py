"""WCAG AA contrast, and the dark-mode structure that stops the report vanishing.

BUG-051. ``swing/backtest/report.py`` used to set ``color: #222`` on ``body``
and declare no background and no ``color-scheme``. A browser in dark mode
painted its own near-black canvas behind that near-black text and the entire
report disappeared — headings, tables, provenance, everything except the two
banners, which happened to declare backgrounds of their own. The screenshot of
the bug is a green callout floating in a void.

This module is the guarantee that it cannot come back, for both HTML artefacts
the system generates: the backtest report and the nightly pick sheet.

WHY IT PARSES THE STYLESHEETS INSTEAD OF LISTING COLOURS
--------------------------------------------------------
Every hex value here is read out of the real stylesheet at import time —
``swing.backtest.report.STYLESHEET`` and the ``<style>`` block of
``picks.html.j2``. Nothing is copied. Darken a token in either file and these
tests re-measure it; add a rule that paints a background and forgets a
foreground and ``test_no_rule_paints_a_background_without_a_foreground`` names
it. A test that restated the palette would only ever prove the palette equals
itself.

WHAT IS DECLARED, AND WHY IT HAS TO BE
---------------------------------------
Two things cannot be read out of CSS without implementing the cascade:

* which painted surface a colour-only rule actually sits on (``.thesis`` is
  ``--muted`` text, but on ``--card`` or on ``--watch-bg``?), and
* which foreground a background-only rule inherits.

Those live in the small ``*_SURFACES`` / ``*_INHERITED_INK`` tables below. They
are declarations of *structure*, never of colour, and
``test_declaration_maps_still_match_the_stylesheets`` fails if a selector in
them is renamed away.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

import swing.alerts
from swing.backtest.report import CHART_COLORS, STYLESHEET, render_html

# ---------------------------------------------------------------------------
# 1. WCAG 2.1 relative luminance and contrast ratio, from the spec
# ---------------------------------------------------------------------------

#: WCAG 2.1 SC 1.4.3 — normal-size text.
AA_NORMAL = 4.5
#: WCAG 2.1 SC 1.4.3 — large text: >= 24px, or >= 18.66px when bold.
AA_LARGE = 3.0
#: WCAG 2.1 SC 1.4.11 — graphics and meaningful boundaries, e.g. chart ink.
AA_NON_TEXT = 3.0

LARGE_TEXT_PX = 24.0
LARGE_BOLD_PX = 18.66
BOLD_WEIGHT = 700

_HEX_DIGITS = re.compile(r"\A[0-9a-fA-F]{6}\Z")


def _channels(color: str) -> tuple[int, int, int]:
    """The three 8-bit channels of a ``#rgb`` or ``#rrggbb`` colour."""
    digits = color.strip().lstrip("#")
    if len(digits) == 3:
        digits = "".join(digit * 2 for digit in digits)
    if not _HEX_DIGITS.match(digits):
        raise ValueError(f"{color!r} is not a #rgb or #rrggbb colour")
    return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _linearise(channel: float) -> float:
    """Undo the sRGB transfer function for one 0..1 channel (WCAG 2.1)."""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG 2.1 relative luminance, 0.0 (black) to 1.0 (white)."""
    red, green, blue = (_linearise(value / 255.0) for value in _channels(color))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    first, second = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def wcag_threshold(font_px: float | None, *, bold: bool = False) -> float:
    """The AA ratio a run of text at this size and weight has to clear."""
    if font_px is None:  # size not declared on the rule — assume the strict bar
        return AA_NORMAL
    if font_px >= LARGE_TEXT_PX or (bold and font_px >= LARGE_BOLD_PX):
        return AA_LARGE
    return AA_NORMAL


# ---------------------------------------------------------------------------
# 2. just enough CSS parsing to read a palette and a rule table
# ---------------------------------------------------------------------------

_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
_VAR = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,([^()]*))?\)")
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_DARK_AT_RULE = re.compile(r"@media[^{]*prefers-color-scheme\s*:\s*dark[^{]*\{", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL | re.IGNORECASE)
_PX = re.compile(r"(\d+(?:\.\d+)?)px")


def _split_dark_block(css: str) -> tuple[str, str]:
    """Split CSS into (everything outside the dark block, the dark block's body)."""
    match = _DARK_AT_RULE.search(css)
    if match is None:
        return css, ""
    opening = match.end() - 1
    depth = 0
    for index in range(opening, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[: match.start()] + css[index + 1 :], css[opening + 1 : index]
    raise ValueError("the prefers-color-scheme: dark block is never closed")


def _declarations(body: str) -> dict[str, str]:
    """``"a: 1; b: 2"`` -> ``{"a": "1", "b": "2"}``. Later wins, as in CSS."""
    out: dict[str, str] = {}
    for chunk in body.split(";"):
        if ":" not in chunk:
            continue
        name, _, value = chunk.partition(":")
        out[name.strip().lower()] = value.strip()
    return out


@dataclass(frozen=True)
class Rule:
    selector: str
    decls: dict[str, str]

    @property
    def background(self) -> str | None:
        return self.decls.get("background") or self.decls.get("background-color")

    @property
    def foreground(self) -> str | None:
        return self.decls.get("color")

    @property
    def font_px(self) -> float | None:
        raw = self.decls.get("font-size") or self.decls.get("font")
        match = _PX.search(raw) if raw else None
        return float(match.group(1)) if match else None

    @property
    def bold(self) -> bool:
        weight = self.decls.get("font-weight", "").strip()
        digits = re.search(r"\d+", weight)
        if digits:
            return int(digits.group()) >= BOLD_WEIGHT
        return weight in {"bold", "bolder"}


def _rules(css: str) -> list[Rule]:
    return [
        Rule(selector=match.group(1).strip(), decls=_declarations(match.group(2)))
        for match in _RULE.finditer(css)
        if match.group(1).strip()
    ]


@dataclass(frozen=True)
class Stylesheet:
    """One parsed stylesheet: its two palettes and its non-``:root`` rules."""

    name: str
    rules: tuple[Rule, ...]
    light: dict[str, str]
    dark: dict[str, str]
    dark_overrides: frozenset[str]
    declares_color_scheme: bool
    has_dark_block: bool

    @classmethod
    def parse(cls, css: str, *, name: str) -> Stylesheet:
        outside, dark_body = _split_dark_block(_COMMENT.sub("", css))
        light_rules = _rules(outside)

        def tokens(rules: list[Rule]) -> dict[str, str]:
            out: dict[str, str] = {}
            for rule in rules:
                if rule.selector == ":root":
                    out.update({k: v for k, v in rule.decls.items() if k.startswith("--")})
            return out

        light = tokens(light_rules)
        overrides = tokens(_rules(dark_body))
        return cls(
            name=name,
            rules=tuple(rule for rule in light_rules if rule.selector not in {":root", "*"}),
            light=light,
            dark={**light, **overrides},
            dark_overrides=frozenset(overrides),
            declares_color_scheme=any(
                rule.selector == ":root" and "color-scheme" in rule.decls for rule in light_rules
            ),
            has_dark_block=bool(dark_body.strip()),
        )

    def palette(self, theme: str) -> dict[str, str]:
        return self.light if theme == "light" else self.dark

    def rule(self, selector: str) -> Rule | None:
        for candidate in self.rules:
            if candidate.selector == selector:
                return candidate
        return None

    def selectors(self) -> set[str]:
        return {rule.selector for rule in self.rules}


def resolve(expression: str, palette: dict[str, str], *, _depth: int = 0) -> str:
    """Substitute ``var(--x)`` (and ``var(--x, fallback)``) until no vars remain."""
    if _depth > 8:
        raise ValueError(f"{expression!r} resolves in a cycle")

    def substitute(match: re.Match[str]) -> str:
        token, fallback = match.group(1), (match.group(2) or "").strip()
        if token in palette:
            return palette[token]
        if fallback:
            return fallback
        raise KeyError(f"{token} is used but never defined")

    resolved = _VAR.sub(substitute, expression)
    if not _VAR.search(resolved):
        return resolved
    # Still holding a var() — keep going. A self-referential token substitutes
    # to itself forever and is caught by the depth cap above.
    return resolve(resolved, palette, _depth=_depth + 1)


def colour_of(expression: str, palette: dict[str, str]) -> str | None:
    """The first literal colour in a resolved declaration, or None if it has none."""
    match = _HEX.search(resolve(expression, palette))
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# 3. the two documents under test
# ---------------------------------------------------------------------------

PICKS_PATH = Path(swing.alerts.__file__).resolve().parent / "templates" / "picks.html.j2"
PICKS_SOURCE = PICKS_PATH.read_text(encoding="utf-8")
_PICKS_STYLE_MATCH = _STYLE_BLOCK.search(PICKS_SOURCE)
assert _PICKS_STYLE_MATCH is not None, f"{PICKS_PATH} has no <style> block"

REPORT = Stylesheet.parse(STYLESHEET, name="report.py")
PICKS = Stylesheet.parse(_PICKS_STYLE_MATCH.group(1), name="picks.html.j2")

#: A report rendered with no equity history: cheap (no matplotlib), and enough
#: to check the skeleton the stylesheet is delivered inside.
REPORT_HTML = render_html({"label": "contrast"}, pd.DataFrame(), pd.DataFrame())

# --- structural declarations (never colours) -------------------------------

#: Rules that paint a surface and let the ink cascade in from ``body``. Legal,
#: but only because the resulting pair is measured below in both themes.
REPORT_INHERITED_INK: dict[str, str] = {}
PICKS_INHERITED_INK: dict[str, str] = {
    "section": "var(--ink)",
    "section.watch": "var(--ink)",
    "pre": "var(--ink)",
}

#: Colour-only rules whose element does NOT sit on the body background. Each
#: entry lists every painted surface the element can appear over.
REPORT_SURFACES: dict[str, tuple[str, ...]] = {}
PICKS_SURFACES: dict[str, tuple[str, ...]] = {
    ".lede": ("var(--card)", "var(--watch-bg)"),
    "th": ("var(--card)", "var(--watch-bg)"),
    ".thesis": ("var(--card)", "var(--watch-bg)"),
    ".empty": ("var(--card)", "var(--watch-bg)"),
}

#: Tokens a dark block is allowed NOT to restate. ``--plate`` has to equal the
#: colour baked into the chart PNGs, and a PNG cannot answer the viewer's theme.
THEME_INVARIANT: dict[str, frozenset[str]] = {
    "report.py": frozenset({"--plate", "--plate-ink"}),
    "picks.html.j2": frozenset(),
}

#: Inks that are not bound to one surface, and every surface they may land on.
#: Checked as a cross-product so a token cannot go stale by being unused today
#: and picked up by a new rule tomorrow — which is how ``--accent``, defined by
#: the pick sheet and used by nothing, was found sitting at 4.32:1 on ``--bg``.
#:
#: ``--plate`` is deliberately absent: it is theme-invariant and permanently
#: paired with ``--plate-ink``, so a theme-flipping ink never lands on it.
FREE_INKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "report.py": (("--ink", "--muted"), ("--bg", "--card")),
    "picks.html.j2": (("--ink", "--muted", "--accent"), ("--bg", "--card", "--watch-bg")),
}

DOCUMENTS = (
    (REPORT, REPORT_INHERITED_INK, REPORT_SURFACES),
    (PICKS, PICKS_INHERITED_INK, PICKS_SURFACES),
)
THEMES = ("light", "dark")


# ---------------------------------------------------------------------------
# 4. every text pair the two stylesheets actually produce
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """One foreground/background pair a rule puts on screen."""

    document: str
    selector: str
    origin: str
    fg: str
    bg: str
    threshold: float

    @property
    def slug(self) -> str:
        surface = self.bg.replace("var(", "").replace(")", "")
        return f"{self.document}|{self.selector}|on:{surface}".replace(" ", "")


def _pairs_for(
    sheet: Stylesheet,
    inherited_ink: dict[str, str],
    surfaces: dict[str, tuple[str, ...]],
) -> list[Pair]:
    body = sheet.rule("body")
    assert body is not None and body.background, f"{sheet.name}: body paints no background"
    default_surface = (body.background,)

    pairs: list[Pair] = []
    for rule in sheet.rules:
        background, foreground = rule.background, rule.foreground
        threshold = wcag_threshold(rule.font_px, bold=rule.bold)
        if background and foreground:
            pairs.append(
                Pair(sheet.name, rule.selector, "declared", foreground, background, threshold)
            )
        elif background and rule.selector in inherited_ink:
            pairs.append(
                Pair(
                    sheet.name,
                    rule.selector,
                    "inherited ink",
                    inherited_ink[rule.selector],
                    background,
                    threshold,
                )
            )
        elif foreground and not background:
            for surface in surfaces.get(rule.selector, default_surface):
                pairs.append(
                    Pair(
                        sheet.name,
                        rule.selector,
                        "inherited surface",
                        foreground,
                        surface,
                        threshold,
                    )
                )
    return pairs


PAIRS: list[Pair] = [
    pair for sheet, ink, surfaces in DOCUMENTS for pair in _pairs_for(sheet, ink, surfaces)
]
CASES = [(theme, pair) for pair in PAIRS for theme in THEMES]
CASE_IDS = [f"{theme}-{pair.slug}" for pair in PAIRS for theme in THEMES]

SHEETS = {sheet.name: sheet for sheet, _ink, _surfaces in DOCUMENTS}


def measure(pair: Pair, theme: str) -> tuple[str, str, float]:
    palette = SHEETS[pair.document].palette(theme)
    foreground, background = colour_of(pair.fg, palette), colour_of(pair.bg, palette)
    assert foreground and background, f"{pair.slug} resolves to a non-colour in {theme}"
    return foreground, background, contrast_ratio(foreground, background)


@pytest.mark.parametrize(("theme", "pair"), CASES, ids=CASE_IDS)
def test_every_text_pair_meets_wcag_aa(theme: str, pair: Pair) -> None:
    """Every foreground the stylesheets put on a background, in both themes."""
    foreground, background, ratio = measure(pair, theme)
    assert ratio >= pair.threshold, (
        f"{pair.document} `{pair.selector}` ({pair.origin}) in {theme} mode: "
        f"{foreground} on {background} is {ratio:.2f}:1, "
        f"WCAG AA needs {pair.threshold}:1"
    )


def test_there_is_something_to_measure_in_both_documents() -> None:
    """A parser that silently matched nothing would make every test above pass."""
    for sheet in SHEETS.values():
        found = [pair for pair in PAIRS if pair.document == sheet.name]
        assert len(found) >= 6, f"{sheet.name}: only {len(found)} pairs parsed — parser broken?"


# ---------------------------------------------------------------------------
# 5. structure: the shape of the bug, not just its colours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sheet", list(SHEETS.values()), ids=list(SHEETS))
def test_both_documents_declare_a_color_scheme(sheet: Stylesheet) -> None:
    """Without this the browser never tells the page which canvas it painted."""
    assert sheet.declares_color_scheme, (
        f"{sheet.name}: :root declares no color-scheme, so form controls, "
        f"scrollbars and the default canvas stay light in a dark browser"
    )


@pytest.mark.parametrize("sheet", list(SHEETS.values()), ids=list(SHEETS))
def test_both_documents_paint_the_body_explicitly(sheet: Stylesheet) -> None:
    """BUG-051 in one assertion: body must own both halves of the pair."""
    body = sheet.rule("body")
    assert body is not None, f"{sheet.name}: no body rule at all"
    assert body.background, (
        f"{sheet.name}: body sets no background, so a dark-mode browser paints "
        f"its own canvas behind the text (this is BUG-051)"
    )
    assert body.foreground, f"{sheet.name}: body sets no color"


@pytest.mark.parametrize("sheet", list(SHEETS.values()), ids=list(SHEETS))
def test_both_documents_have_a_dark_palette(sheet: Stylesheet) -> None:
    assert sheet.has_dark_block, f"{sheet.name}: no prefers-color-scheme: dark block"


@pytest.mark.parametrize("sheet", list(SHEETS.values()), ids=list(SHEETS))
def test_dark_mode_restates_every_theme_dependent_token(sheet: Stylesheet) -> None:
    """A light token with no dark counterpart is the next BUG-051 waiting."""
    invariant = THEME_INVARIANT[sheet.name]
    missing = sorted(set(sheet.light) - sheet.dark_overrides - invariant)
    assert not missing, (
        f"{sheet.name}: {', '.join(missing)} defined for light mode only. Either "
        f"restate it under prefers-color-scheme: dark or declare it invariant."
    )


@pytest.mark.parametrize(
    ("sheet", "inherited_ink"),
    [(sheet, ink) for sheet, ink, _surfaces in DOCUMENTS],
    ids=list(SHEETS),
)
def test_no_rule_paints_a_background_without_a_foreground(
    sheet: Stylesheet, inherited_ink: dict[str, str]
) -> None:
    """The bug class, generalised: a painted surface with unowned ink on it."""
    orphans = [
        rule.selector
        for rule in sheet.rules
        if rule.background and not rule.foreground and rule.selector not in inherited_ink
    ]
    assert not orphans, (
        f"{sheet.name}: {', '.join(orphans)} paint a background but set no color. "
        f"Set one in the same rule, or name the inherited ink in "
        f"{sheet.name.split('.')[0].upper()}_INHERITED_INK so it gets measured."
    )


@pytest.mark.parametrize(
    ("sheet", "inherited_ink", "surfaces"),
    list(DOCUMENTS),
    ids=list(SHEETS),
)
def test_declaration_maps_still_match_the_stylesheets(
    sheet: Stylesheet, inherited_ink: dict[str, str], surfaces: dict[str, tuple[str, ...]]
) -> None:
    """Rename a selector and the map that describes it must fail, not go quiet."""
    known = sheet.selectors()
    for label, mapping in (("inherited ink", inherited_ink), ("surfaces", surfaces)):
        stale = sorted(set(mapping) - known)
        assert not stale, f"{sheet.name} {label} map names selectors that no longer exist: {stale}"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("name", list(SHEETS), ids=list(SHEETS))
def test_free_ink_tokens_clear_aa_on_every_surface(name: str, theme: str) -> None:
    """Unbound inks are checked against every surface, used there yet or not."""
    sheet, palette = SHEETS[name], SHEETS[name].palette(theme)
    inks, surfaces = FREE_INKS[name]
    for ink in inks:
        for surface in surfaces:
            assert ink in palette and surface in palette, f"{sheet.name}: {ink}/{surface} undefined"
            ratio = contrast_ratio(palette[ink], palette[surface])
            assert ratio >= AA_NORMAL, (
                f"{sheet.name} {theme}: {ink} ({palette[ink]}) on {surface} "
                f"({palette[surface]}) is {ratio:.2f}:1, needs {AA_NORMAL}:1"
            )


def test_the_report_skeleton_is_declared_and_responsive() -> None:
    assert REPORT_HTML.startswith("<!doctype html>")
    assert '<html lang="en">' in REPORT_HTML
    assert '<meta charset="utf-8">' in REPORT_HTML
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in REPORT_HTML
    # The stylesheet under test is the stylesheet actually shipped.
    assert STYLESHEET in REPORT_HTML


def test_the_pick_sheet_skeleton_is_declared_and_responsive() -> None:
    assert PICKS_SOURCE.startswith("<!doctype html>")
    assert '<html lang="en">' in PICKS_SOURCE
    assert '<meta charset="utf-8">' in PICKS_SOURCE
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in PICKS_SOURCE


# ---------------------------------------------------------------------------
# 6. the charts, which are PNGs and cannot answer prefers-color-scheme
# ---------------------------------------------------------------------------

CHART_INK = [
    ("equity line", "equity", AA_NON_TEXT),
    ("drawdown line", "drawdown", AA_NON_TEXT),
    ("chart title", "ink", AA_NORMAL),
    ("axis labels, ticks and spines", "muted", AA_NORMAL),
]


@pytest.mark.parametrize(("what", "key", "threshold"), CHART_INK, ids=[c[0] for c in CHART_INK])
def test_chart_ink_reads_against_the_plate_it_is_drawn_on(
    what: str, key: str, threshold: float
) -> None:
    ratio = contrast_ratio(CHART_COLORS[key], CHART_COLORS["plate"])
    assert ratio >= threshold, (
        f"chart {what} {CHART_COLORS[key]} on plate {CHART_COLORS['plate']} is "
        f"{ratio:.2f}:1, needs {threshold}:1"
    )


@pytest.mark.parametrize("theme", THEMES)
def test_the_plate_in_the_css_is_the_plate_in_the_png(theme: str) -> None:
    """If these drift the chart becomes a hole punched in the page."""
    palette = REPORT.palette(theme)
    assert palette["--plate"] == CHART_COLORS["plate"], (
        f"{theme}: --plate is {palette['--plate']} but the PNG is painted "
        f"{CHART_COLORS['plate']} — the image will not sit flush on its container"
    )
    assert palette["--plate-ink"] == CHART_COLORS["ink"]


def test_the_rendered_png_really_is_painted_the_plate_colour() -> None:
    """Not "the constants agree" — "the pixels agree".

    ``savefig`` honours ``savefig.facecolor`` rather than the figure's own when
    it is left to default, and ``bbox_inches="tight"`` re-renders through a new
    bounding box. Both are quiet ways for the figure to come back white while
    every constant in this module still lines up. So decode the PNG and look.
    """
    import base64
    import io

    import matplotlib
    from matplotlib import image as mpimg

    matplotlib.use("Agg")
    from swing.backtest.report import _drawdown_chart, _equity_chart

    equity = pd.DataFrame(
        {"equity": [10_000.0, 10_400.0, 9_900.0], "drawdown": [0.0, 0.0, -0.048]},
        index=pd.bdate_range("2024-01-01", periods=3),
    )
    expected = _channels(CHART_COLORS["plate"])
    for what, uri in (("equity", _equity_chart(equity)), ("drawdown", _drawdown_chart(equity))):
        payload = uri.removeprefix("data:image/png;base64,")
        pixels = mpimg.imread(io.BytesIO(base64.b64decode(payload)))
        corner = tuple(round(float(channel) * 255) for channel in pixels[0, 0][:3])
        assert corner == expected, (
            f"the {what} PNG's own background is rgb{corner}, but the page paints "
            f"rgb{expected} behind it — the chart will read as a hole in the page"
        )


def test_the_dark_plate_gets_a_rim_that_reads_against_the_page() -> None:
    """In dark mode the plate is a light panel: it needs an edge, not a glow."""
    dark = REPORT.dark
    ratio = contrast_ratio(dark["--plate-line"], dark["--bg"])
    assert ratio >= AA_NON_TEXT, (
        f"dark --plate-line {dark['--plate-line']} on page {dark['--bg']} is "
        f"{ratio:.2f}:1, needs {AA_NON_TEXT}:1 to read as a deliberate frame"
    )
    plate_rule = REPORT.rule(".plate")
    assert plate_rule is not None
    assert "border" in plate_rule.decls and "border-radius" in plate_rule.decls


def test_equity_and_drawdown_stay_visually_distinct() -> None:
    """Both clear 3:1 on the plate, which alone would permit one colour twice."""
    equity, drawdown = _channels(CHART_COLORS["equity"]), _channels(CHART_COLORS["drawdown"])
    assert max(abs(a - b) for a, b in zip(equity, drawdown, strict=True)) >= 60
    assert equity[2] > equity[0], "the equity line should be blue-dominant"
    assert drawdown[0] > drawdown[2], "the drawdown line should be red-dominant"
    # The fill is decoration, but it still has to be visible, and the line that
    # carries the shape has to be visible on top of it.
    assert contrast_ratio(CHART_COLORS["drawdown_fill"], CHART_COLORS["plate"]) >= 1.2
    assert contrast_ratio(CHART_COLORS["drawdown"], CHART_COLORS["drawdown_fill"]) >= AA_NON_TEXT


def test_no_chart_colour_is_left_to_matplotlib() -> None:
    """Every colour the charts draw with is named here, not defaulted."""
    expected = {"plate", "ink", "muted", "grid", "equity", "drawdown", "drawdown_fill"}
    assert set(CHART_COLORS) == expected
    for key, value in CHART_COLORS.items():
        assert _channels(value), key


# ---------------------------------------------------------------------------
# 7. BUG-051 regression — fails against report.py as it stood before this fix
# ---------------------------------------------------------------------------


def test_the_report_no_longer_inherits_the_browser_canvas_bug051() -> None:
    """The exact regression: pre-fix, body was ``color: #222`` and nothing else.

    Confirmed to fail against the pre-fix stylesheet, which had no ``:root``, no
    ``color-scheme``, no dark block, and a ``body`` rule that set a foreground
    and left the canvas to the browser. Restore that file and this test reports
    "body must own its canvas — this is the bug" before any other assertion runs.
    """
    body = REPORT.rule("body")
    assert body is not None
    assert body.background, "body must own its canvas — this is the bug"
    assert body.foreground, "body must own its ink"

    assert "prefers-color-scheme: dark" in STYLESHEET
    assert "color-scheme: light dark" in STYLESHEET

    light_bg = colour_of(body.background, REPORT.light)
    dark_bg = colour_of(body.background, REPORT.dark)
    assert light_bg != dark_bg, "the two themes must not paint the same canvas"
    assert relative_luminance(light_bg) > 0.5 > relative_luminance(dark_bg)

    # And the elements that stayed readable through the bug (they declared
    # backgrounds) are no longer the only ones that do.
    for selector in ("table", "th", "code", "table.heat td.pos", "table.heat td.neg"):
        rule = REPORT.rule(selector)
        assert rule is not None, f"{selector} disappeared from the stylesheet"
        assert rule.background and rule.foreground, f"{selector} paints only half a pair"


# ---------------------------------------------------------------------------
# 8. the measuring instrument itself
# ---------------------------------------------------------------------------


def test_the_contrast_formula_matches_the_wcag_reference_values() -> None:
    assert relative_luminance("#000000") == pytest.approx(0.0)
    assert relative_luminance("#ffffff") == pytest.approx(1.0)
    assert relative_luminance("#fff") == relative_luminance("#ffffff")
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0)  # symmetric
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    # The canonical AA boundary pair: #777 fails on white, #767676 passes.
    assert contrast_ratio("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)


@pytest.mark.parametrize(
    ("font_px", "bold", "expected"),
    [
        (15.0, False, AA_NORMAL),
        (23.9, False, AA_NORMAL),
        (24.0, False, AA_LARGE),
        (18.66, True, AA_LARGE),
        (18.65, True, AA_NORMAL),
        (18.66, False, AA_NORMAL),
        (None, False, AA_NORMAL),
        (None, True, AA_NORMAL),
    ],
)
def test_the_large_text_boundary_is_the_wcag_one(
    font_px: float | None, bold: bool, expected: float
) -> None:
    assert wcag_threshold(font_px, bold=bold) == expected


def test_a_malformed_colour_is_refused_rather_than_scored() -> None:
    for bad in ("", "#12", "rebeccapurple", "#gggggg", "rgb(1,2,3)"):
        with pytest.raises(ValueError):
            relative_luminance(bad)


def test_var_resolution_follows_chains_and_refuses_cycles() -> None:
    palette = {"--a": "var(--b)", "--b": "#123456", "--loop": "var(--loop)"}
    assert resolve("var(--a)", palette) == "#123456"
    assert resolve("var(--missing, #abcdef)", palette) == "#abcdef"
    assert colour_of("1px solid var(--b)", palette) == "#123456"
    assert colour_of("none", palette) is None
    with pytest.raises(ValueError):
        resolve("var(--loop)", palette)
    with pytest.raises(KeyError):
        resolve("var(--nope)", palette)
