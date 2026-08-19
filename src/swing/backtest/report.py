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
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pandas as pd

from swing.backtest.metrics import PROFIT_FACTOR_CAP

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "METRIC_LABELS",
    "format_metric",
    "print_latest",
    "render_html",
    "render_markdown",
]

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
# markdown
# ---------------------------------------------------------------------------


def _metric_table(metrics: dict[str, Any]) -> list[str]:
    lines = ["| Metric | Value |", "| --- | --- |"]
    for key, (label, _unit) in METRIC_LABELS.items():
        if key in metrics:
            lines.append(f"| {label} | {format_metric(key, metrics[key])} |")
    return lines


def render_markdown(summary: dict[str, Any]) -> str:
    """Render ``summary.json`` as a Markdown report."""
    walkforward = bool(summary.get("walkforward"))
    out: list[str] = [
        f"# Backtest — {summary.get('label', 'unlabelled')}",
        "",
        f"- **Universe**: {summary.get('universe', 'unknown')} "
        f"({summary.get('n_symbols', '?')} symbols)",
        f"- **Period**: {summary.get('start', '?')} to {summary.get('end', '?')}",
        f"- **Walk-forward**: {'yes' if walkforward else 'NO — cannot open the trading gate'}",
        f"- **Config hash**: `{summary.get('config_hash', '')[:16]}`",
        f"- **Code ref**: `{summary.get('code_ref', 'unknown')}`",
        f"- **Data hash**: `{summary.get('data_hash', '')[:16]}`",
        "",
    ]

    if walkforward:
        out += [
            "## Out-of-sample (headline)",
            "",
            "These are the numbers the deployment gate reads. Every parameter used to",
            "produce them was chosen on data that ended before the trade did.",
            "",
        ]
    else:
        out += [
            "## Full period (in-sample — NOT gate-eligible)",
            "",
            "This run tuned nothing, but it also proved nothing out of sample.",
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
    """Serialise a matplotlib figure to a base64 ``data:`` URI and close it."""
    import matplotlib.pyplot as plt

    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=110, bbox_inches="tight")
    plt.close(figure)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _equity_chart(equity: pd.DataFrame) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(10, 4))
    axes.plot(equity.index, equity["equity"], linewidth=1.2, color="#1f4e79")
    axes.set_title("Out-of-sample equity")
    axes.set_ylabel("Account equity ($)")
    axes.grid(alpha=0.3)
    return _png_data_uri(figure)


def _drawdown_chart(equity: pd.DataFrame) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(figsize=(10, 2.6))
    drawdown = equity["drawdown"].astype("float64") * 100.0
    axes.fill_between(equity.index, drawdown, 0.0, color="#a4262c", alpha=0.5)
    axes.set_title("Drawdown")
    axes.set_ylabel("%")
    axes.grid(alpha=0.3)
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
    return (
        '<table class="heat"><thead><tr><th>Year</th>'
        f"{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest — {label}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          margin: 2rem auto; max-width: 1000px; padding: 0 1rem; color: #222; }}
  h1 {{ margin-bottom: 0.2rem; }}
  .sub {{ color: #666; margin-top: 0; font-size: 0.9rem; }}
  .banner {{ padding: 0.75rem 1rem; border-radius: 6px; margin: 1rem 0; font-weight: 600; }}
  .banner.ok {{ background: #e6f4ea; color: #1e4620; }}
  .banner.warn {{ background: #fdecea; color: #611a15; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.92rem; }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: right; }}
  th {{ background: #f4f6f8; }}
  td:first-child, th:first-child {{ text-align: left; }}
  table.heat td {{ text-align: center; min-width: 3.2rem; }}
  table.heat td.pos {{ background: #e6f4ea; }}
  table.heat td.neg {{ background: #fdecea; }}
  table.heat td.empty {{ background: #fafafa; }}
  img {{ width: 100%; height: auto; margin: 0.5rem 0; }}
  code {{ background: #f4f6f8; padding: 0.1rem 0.3rem; border-radius: 3px; }}
  footer {{ color: #888; font-size: 0.8rem; margin-top: 2rem; }}
</style>
</head>
<body>
<h1>Backtest — {label}</h1>
<p class="sub">{universe} universe ({n_symbols} symbols) &middot; {start} to {end}</p>
<div class="banner {banner_class}">{banner}</div>

<h2>{headline_title}</h2>
{headline_table}

{full_period_section}

<h2>Equity</h2>
<img src="{equity_chart}" alt="Out-of-sample equity curve">
<img src="{drawdown_chart}" alt="Drawdown">

<h2>Monthly returns (%)</h2>
{monthly_table}

{by_year_section}
{folds_section}
{sensitivity_section}

<h2>Provenance</h2>
<table>
<tr><td>Config hash</td><td><code>{config_hash}</code></td></tr>
<tr><td>Code ref</td><td><code>{code_ref}</code></td></tr>
<tr><td>Data hash</td><td><code>{data_hash}</code></td></tr>
</table>

<footer>Generated at {generated_at}. Costs charged per side:
{slippage_bps} bps slippage plus {spread_atr_frac} x ATR spread.</footer>
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

    if walkforward:
        banner = (
            "Walk-forward run: the headline numbers below are out-of-sample and are what the "
            "trading gate reads."
        )
        banner_class = "ok"
        headline_title = "Out-of-sample (headline)"
    else:
        banner = (
            "NOT a walk-forward run. These numbers are in-sample and cannot open the trading gate."
        )
        banner_class = "warn"
        headline_title = "Full period (in-sample)"

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
            "<table><thead><tr><th>In-sample</th><th>Out-of-sample</th><th>Chosen parameters"
            "</th><th>IS PF</th><th>OOS PF</th><th>OOS trades</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>",
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
            "<table><thead><tr><th>Parameter</th><th>Variant</th><th>Value</th>"
            "<th>Profit factor</th><th>CAGR</th><th>Max DD</th><th>Trades</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>",
        )

    stamp = generated_at or datetime.now()
    costs = summary.get("costs") or {}
    return _HTML_TEMPLATE.format(
        label=summary.get("label", "unlabelled"),
        universe=summary.get("universe", "unknown"),
        n_symbols=summary.get("n_symbols", "?"),
        start=summary.get("start", "?"),
        end=summary.get("end", "?"),
        banner=banner,
        banner_class=banner_class,
        headline_title=headline_title,
        headline_table=_html_metric_table(headline),
        full_period_section=full_period_section,
        equity_chart=_equity_chart(equity) if not equity.empty else "",
        drawdown_chart=_drawdown_chart(equity) if not equity.empty else "",
        monthly_table=_monthly_table_html(equity),
        by_year_section=by_year_section,
        folds_section=folds_section,
        sensitivity_section=sensitivity_section,
        config_hash=summary.get("config_hash", ""),
        code_ref=summary.get("code_ref", "unknown"),
        data_hash=summary.get("data_hash", ""),
        generated_at=stamp.isoformat(timespec="seconds"),
        slippage_bps=costs.get("slippage_bps", "?"),
        spread_atr_frac=costs.get("spread_atr_frac", "?"),
    )


# ---------------------------------------------------------------------------
# terminal
# ---------------------------------------------------------------------------


def print_latest(cfg: Config) -> None:
    """FROZEN CONTRACT 2 — print the most recent backtest summary and the gate verdict.

    Prints (rather than returns) because it is the body of ``swing report``. If
    there is no report yet it says so and explains how to make one, instead of
    raising at a user who has simply not run a backtest.
    """
    from swing.backtest import gate

    summary = gate.load_latest(cfg)
    if summary is None:
        print(f"No backtest report found at {gate.latest_path(cfg)}.")
        print("Run `swing backtest` first — the scanner will not emit picks without one.")
        return

    walkforward = bool(summary.get("walkforward"))
    print(f"Backtest: {summary.get('label', 'unlabelled')}")
    print(
        f"  universe   {summary.get('universe', 'unknown')} "
        f"({summary.get('n_symbols', '?')} symbols)"
    )
    print(f"  period     {summary.get('start', '?')} to {summary.get('end', '?')}")
    print(f"  method     {'walk-forward' if walkforward else 'full period (in-sample only)'}")
    print(f"  code_ref   {summary.get('code_ref', 'unknown')}")
    print()

    headline = (summary.get("oos") if walkforward else summary.get("full_period")) or {}
    heading = "Out-of-sample results" if walkforward else "Full-period results (in-sample)"
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
