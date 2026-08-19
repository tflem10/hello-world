"""Rendering the scan and confirmation reports for humans.

Everything here is a pure function of the Contract 9 payload: hand it the same
``picks.json`` dict and you get back the same Markdown and the same HTML, every
time. Nothing reads the clock, the filesystem or the network, which is what
makes the report reproducible and the tests boring.

The house style has one opinion worth stating: **the watch list is not an
afterthought.** On a $100 account almost every idea that passes the rules sizes
to zero shares, so a report that buries them under "picks" would be a report
that is empty most nights and therefore ignored. Watch entries get their own
prominent table, immediately after the picks, with the reason they are there.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jinja2 import Environment

__all__ = [
    "EARNINGS_UNKNOWN_LABEL",
    "clip",
    "render_confirm_markdown",
    "render_confirm_sms",
    "render_confirm_title",
    "render_html",
    "render_markdown",
    "render_sms",
    "summary_text",
    "summary_title",
    "template_dir",
]

#: What an unknown earnings date is called everywhere in the output.
EARNINGS_UNKNOWN_LABEL = "UNKNOWN"

#: Carrier SMS gateways truncate hard; stay well inside one multipart message.
SMS_MAX_CHARS = 450


# --------------------------------------------------------------------------
# jinja2 plumbing
# --------------------------------------------------------------------------


def template_dir() -> Path:
    """Directory holding the committed report templates."""
    return Path(str(resources.files("swing.alerts"))) / "templates"


def _autoescape(name: str | None) -> bool:
    """Escape HTML templates, leave Markdown alone."""
    return bool(name) and ".html" in name


@lru_cache(maxsize=1)
def _environment() -> Environment:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    env = Environment(
        loader=FileSystemLoader(str(template_dir())),
        autoescape=_autoescape,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["money"] = money
    env.filters["pct"] = pct
    env.filters["num"] = num
    return env


def _render(template: str, **context: Any) -> str:
    return _environment().get_template(template).render(**context)


# --------------------------------------------------------------------------
# formatting helpers (also exposed as jinja2 filters)
# --------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def money(value: Any, dash: str = "—") -> str:
    """Format a dollar amount, or ``dash`` when it is missing or not a number."""
    number = _finite(value)
    return dash if number is None else f"${number:,.2f}"


def pct(value: Any, digits: int = 1, dash: str = "—") -> str:
    """Format a percentage that is already expressed in percent units."""
    number = _finite(value)
    return dash if number is None else f"{number:.{digits}f}%"


def num(value: Any, digits: int = 2, dash: str = "—") -> str:
    """Format a plain number to a fixed number of decimals."""
    number = _finite(value)
    return dash if number is None else f"{number:,.{digits}f}"


# --------------------------------------------------------------------------
# view model
# --------------------------------------------------------------------------


def _earnings_label(record: Mapping[str, Any]) -> str:
    """How a pick's earnings date is shown, including the loud unknown case."""
    if not record.get("earnings_known"):
        return EARNINGS_UNKNOWN_LABEL
    return str(record.get("earnings_date") or EARNINGS_UNKNOWN_LABEL)


def _row(record: Mapping[str, Any], equity: float) -> dict[str, Any]:
    """Turn one PickRecord dict into everything the templates want to show."""
    entry = _finite(record.get("entry")) or 0.0
    stop = _finite(record.get("stop")) or 0.0
    shares = int(record.get("shares") or 0)
    risk_amount = _finite(record.get("risk_amount")) or 0.0
    notional = round(entry * shares, 2)
    risk_pct = round(100.0 * risk_amount / equity, 2) if equity > 0 else None
    notional_pct = round(100.0 * notional / equity, 2) if equity > 0 else None
    label = _earnings_label(record)
    return {
        "symbol": str(record.get("symbol", "")),
        "kind": str(record.get("kind", "pick")),
        "status": str(record.get("status", "")),
        "entry": entry,
        "stop": stop,
        "shares": shares,
        "risk_per_share": round(entry - stop, 2),
        "risk_amount": risk_amount,
        "risk_pct": risk_pct,
        "notional": notional,
        "notional_pct": notional_pct,
        "score": _finite(record.get("score")),
        "atr": _finite(record.get("atr")),
        "earnings_date": record.get("earnings_date"),
        "earnings_known": bool(record.get("earnings_known")),
        "earnings_label": label,
        "earnings_unknown": label == EARNINGS_UNKNOWN_LABEL,
        "thesis": str(record.get("thesis", "")),
    }


def _rows(records: Any, equity: float) -> list[dict[str, Any]]:
    if not isinstance(records, Sequence):
        return []
    return [_row(r, equity) for r in records if isinstance(r, Mapping)]


def _context(report: Mapping[str, Any], notes: Sequence[str] = ()) -> dict[str, Any]:
    """Build the template context shared by every scan renderer."""
    equity = _finite(report.get("equity")) or 0.0
    gate = report.get("gate") if isinstance(report.get("gate"), Mapping) else {}
    gate_reasons = [str(r) for r in (gate.get("reasons") or [])]
    picks = _rows(report.get("picks"), equity)
    watch = _rows(report.get("watch"), equity)
    return {
        "asof": str(report.get("asof", "")),
        "generated_at": str(report.get("generated_at", "")),
        "equity": equity,
        "regime_ok": bool(report.get("regime_ok")),
        "gate_passed": bool(gate.get("passed")),
        "gate_reasons": gate_reasons,
        "picks": picks,
        "watch": watch,
        "notes": [str(n) for n in notes],
        "total_risk": round(sum(r["risk_amount"] for r in picks), 2),
        "total_notional": round(sum(r["notional"] for r in picks), 2),
        "any_earnings_unknown": any(r["earnings_unknown"] for r in (*picks, *watch)),
    }


# --------------------------------------------------------------------------
# scan renderers
# --------------------------------------------------------------------------


def render_markdown(report: Mapping[str, Any], *, notes: Sequence[str] = ()) -> str:
    """Render the pick sheet as Markdown."""
    return _render("picks.md.j2", **_context(report, notes))


def render_html(
    report: Mapping[str, Any],
    *,
    notes: Sequence[str] = (),
    orders: Mapping[str, Any] | None = None,
) -> str:
    """Render the pick sheet as one self-contained, dark-friendly HTML page.

    Args:
        report: the Contract 9 payload.
        notes: plain-English lines explaining anything unusual about this run.
        orders: optional ``{symbol: draft}`` mapping; when given, each pick gets
            a collapsible block holding its drafted Schwab JSON, so the page is
            everything the user needs in one file.
    """
    import json

    context = _context(report, notes)
    context["order_json"] = {
        str(symbol): json.dumps(draft, indent=2) for symbol, draft in sorted((orders or {}).items())
    }
    return _render("picks.html.j2", **context)


def summary_title(report: Mapping[str, Any]) -> str:
    """One-line notification title, e.g. ``SWING 2026-08-18: 2 picks, 3 watch``."""
    context = _context(report)
    if not context["gate_passed"] and not context["picks"]:
        return f"SWING {context['asof']}: gate not passed, no picks"
    if not context["regime_ok"] and not context["picks"]:
        return f"SWING {context['asof']}: regime off, no picks"
    return f"SWING {context['asof']}: {len(context['picks'])} picks, {len(context['watch'])} watch"


def summary_text(report: Mapping[str, Any], *, notes: Sequence[str] = ()) -> str:
    """Short Markdown body for push notifications — a handful of lines, no tables."""
    context = _context(report, notes)
    lines: list[str] = []
    lines.append(f"**Gate:** {'passed' if context['gate_passed'] else 'NOT passed'}")
    lines.append(f"**Regime:** {'entries allowed' if context['regime_ok'] else 'entries blocked'}")
    if context["picks"]:
        lines.append("")
        lines.append(f"**Picks ({len(context['picks'])})**")
        for row in context["picks"]:
            lines.append(
                f"- {row['symbol']} {row['shares']}sh @ {money(row['entry'])} "
                f"stop {money(row['stop'])} risk {money(row['risk_amount'])}"
            )
    if context["watch"]:
        lines.append("")
        lines.append(f"**Watch — 0 shares affordable ({len(context['watch'])})**")
        for row in context["watch"]:
            lines.append(f"- {row['symbol']} @ {money(row['entry'])} stop {money(row['stop'])}")
    if not context["picks"] and not context["watch"]:
        lines.append("")
        lines.append("No picks tonight.")
    for note in context["notes"]:
        lines.append("")
        lines.append(f"_{note}_")
    return "\n".join(lines)


def render_sms(report: Mapping[str, Any]) -> str:
    """Render the scan as one short plain-text line for a carrier SMS gateway."""
    context = _context(report)
    head = f"SWING {context['asof']}"
    if not context["gate_passed"]:
        return clip(f"{head}: backtest gate NOT passed, no picks.")
    if not context["regime_ok"]:
        return clip(f"{head}: market regime off, no new entries.")
    if not context["picks"] and not context["watch"]:
        return clip(f"{head}: no candidates passed the rules.")

    parts: list[str] = []
    for row in context["picks"]:
        parts.append(f"{row['symbol']} {row['shares']}sh@{row['entry']:.2f} stop {row['stop']:.2f}")
    body = f"{head}: {len(context['picks'])} picks"
    if parts:
        body = f"{body}: {'; '.join(parts)}"
    if context["watch"]:
        watch = ", ".join(row["symbol"] for row in context["watch"])
        body = f"{body}. Watch ({len(context['watch'])}): {watch}"
    return clip(f"{body}.")


def clip(text: str, limit: int = SMS_MAX_CHARS) -> str:
    """Trim to ``limit`` characters on a word boundary, marking the cut."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return f"{cut}…"


# --------------------------------------------------------------------------
# confirmation renderers
# --------------------------------------------------------------------------

#: How each confirm outcome is described in prose.
_CONFIRM_LABELS: dict[str, str] = {
    "confirmed": "confirmed",
    "invalidated": "invalidated",
    "unknown": "no quote",
}


def _confirm_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    results = payload.get("results")
    rows: list[dict[str, Any]] = []
    if isinstance(results, Mapping):
        for symbol in sorted(results):
            entry = results[symbol]
            if not isinstance(entry, Mapping):
                continue
            status = str(entry.get("status", "unknown"))
            rows.append(
                {
                    "symbol": str(symbol),
                    "quote": _finite(entry.get("quote")),
                    "status": status,
                    "label": _CONFIRM_LABELS.get(status, status),
                    "reason": str(entry.get("reason", "")),
                }
            )
    counts = {
        name: sum(1 for row in rows if row["status"] == name)
        for name in ("confirmed", "invalidated", "unknown")
    }
    return {
        "asof": str(payload.get("asof", "")),
        "rows": rows,
        "counts": counts,
    }


def render_confirm_title(payload: Mapping[str, Any]) -> str:
    """One-line notification title for a confirmation run."""
    context = _confirm_context(payload)
    counts = context["counts"]
    return (
        f"SWING confirm {context['asof']}: {counts['confirmed']} confirmed, "
        f"{counts['invalidated']} invalidated"
    )


def render_confirm_markdown(payload: Mapping[str, Any]) -> str:
    """Render the confirmation results as Markdown."""
    return _render("confirm.md.j2", **_confirm_context(payload))


def render_confirm_sms(payload: Mapping[str, Any]) -> str:
    """Render the confirmation results as one short plain-text line."""
    context = _confirm_context(payload)
    counts = context["counts"]
    if not context["rows"]:
        return clip(f"SWING confirm {context['asof']}: nothing to confirm.")
    parts = [f"{row['symbol']} {row['label']}" for row in context["rows"]]
    return clip(
        f"SWING confirm {context['asof']}: {counts['confirmed']} ok, "
        f"{counts['invalidated']} invalidated. {'; '.join(parts)}."
    )
