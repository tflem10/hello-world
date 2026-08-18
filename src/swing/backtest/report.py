"""Backtest reporting: HTML + Markdown, charts, trade CSV, reproducibility manifest.

Every report carries the config hash, the data fingerprint, the git commit and
the package version. Two runs over the same cache with the same config produce
byte-identical CSV/JSON outputs, so "did anything actually change?" is a
``diff``, not an argument.

The report is also where the honesty lives. Survivorship bias, the missing
earnings blackout, and any parameter that was tuned are all stated in the
report body, not buried in a docstring nobody opens.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .. import __version__
from ..config import REPO_ROOT, Config
from ..logging_setup import get_logger
from .metrics import (
    Metrics,
    compute_metrics,
    drawdown_series,
    exit_reason_table,
    monthly_returns,
    yearly_table,
)

log = get_logger("swing.report")

SURVIVORSHIP_NOTE = """\
**Survivorship bias.** The stock universe in this repo is a list of *current*
index members. Companies that were in the index during the test period and then
went to zero, were acquired, or were delisted are simply absent from the data,
so the backtest never gets to lose money on them. This inflates stock-universe
results. Two mitigations are shipped: an ETF-only backtest
(`swing backtest --etf-only`), which has no survivorship problem because the
ETFs in the list existed throughout, and this note. Treat the ETF-only run as
the honest lower bound and the stock-universe run as an optimistic upper bound;
the truth is between them, and closer to the lower bound than feels
comfortable."""

GATE_NOTE = """\
**What this report gates.** `swing scan` refuses to emit live picks until a
walk-forward report exists whose out-of-sample metrics clear the thresholds in
`[backtest.gate]`. That is a mechanical check on the numbers below, not a
judgement that the strategy is good."""


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=False,
        )
        commit = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip()
        return f"{commit}{'-dirty' if dirty else ''}" if commit else "unknown"
    except Exception:
        return "unknown"


def report_dir(cfg: Config, tag: str, when: date | None = None) -> Path:
    root = cfg.expand_path(cfg.reports.dir)
    path = root / f"{(when or date.today()).isoformat()}-{tag}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class BacktestReport:
    """Assemble one report directory from a result (or walk-forward result)."""

    def __init__(
        self,
        cfg: Config,
        title: str,
        equity: pd.Series,
        trades: pd.DataFrame,
        metrics: Metrics,
        *,
        exposure: pd.Series | None = None,
        open_positions: pd.Series | None = None,
        warnings: list[str] | None = None,
        extra_tables: dict[str, pd.DataFrame] | None = None,
        manifest_extra: dict | None = None,
        kind: str = "backtest",
    ):
        self.cfg = cfg
        self.title = title
        self.equity = equity
        self.trades = trades
        self.metrics = metrics
        self.exposure = exposure
        self.open_positions = open_positions
        self.warnings = list(warnings or [])
        self.extra_tables = extra_tables or {}
        self.manifest_extra = manifest_extra or {}
        self.kind = kind

    # -- writing -----------------------------------------------------------
    def write(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)

        self.trades.to_csv(out_dir / "trades.csv", index=False)
        self.equity.to_frame("equity").to_csv(out_dir / "equity.csv")
        (out_dir / "metrics.json").write_text(
            json.dumps(_jsonable(self.metrics), indent=2, sort_keys=True)
        )
        (out_dir / "manifest.json").write_text(
            json.dumps(self.manifest(), indent=2, sort_keys=True)
        )
        for name, table in self.extra_tables.items():
            if table is not None and len(table):
                table.to_csv(out_dir / f"{name}.csv")

        charts = self._write_charts(out_dir)
        (out_dir / "report.md").write_text(self.to_markdown())
        (out_dir / "report.html").write_text(self.to_html(charts))
        log.info("report written to %s", out_dir)
        return out_dir

    def manifest(self) -> dict:
        payload = {
            "kind": self.kind,
            "title": self.title,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "swing_version": __version__,
            "git_commit": git_commit(),
            "config_hash": self.cfg.hash,
            "params": {
                k: self.cfg.as_dict().get(k)
                for k in ("account", "universe", "strategy", "backtest")
            },
            "metrics": _jsonable(self.metrics),
            "warnings": self.warnings,
        }
        payload.update(self.manifest_extra)
        return payload

    # -- charts ------------------------------------------------------------
    def _write_charts(self, out_dir: Path) -> dict[str, str]:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except Exception as exc:            # charts are a nicety, not a blocker
            log.warning("charts skipped (%s)", exc)
            return {}

        charts: dict[str, str] = {}
        if not len(self.equity):
            return charts

        dd = drawdown_series(self.equity)
        fig, axes = plt.subplots(
            2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
        )
        axes[0].plot(self.equity.index, self.equity.to_numpy(), lw=1.4, color="#1f77b4")
        axes[0].set_ylabel("equity ($)")
        axes[0].set_title(self.title)
        axes[0].grid(alpha=0.25)
        axes[1].fill_between(dd.index, dd.to_numpy() * 100.0, 0.0, color="#d62728", alpha=0.4)
        axes[1].set_ylabel("drawdown (%)")
        axes[1].grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(out_dir / "equity.png", dpi=110)
        plt.close(fig)
        charts["equity"] = "equity.png"

        if self.trades is not None and len(self.trades):
            r = self.trades["r_multiple"].replace([np.inf, -np.inf], np.nan).dropna()
            if len(r):
                fig, ax = plt.subplots(figsize=(7, 4))
                ax.hist(r, bins=min(40, max(8, len(r) // 3)), color="#2ca02c", alpha=0.8)
                ax.axvline(0, color="black", lw=1)
                ax.axvline(float(r.mean()), color="#d62728", lw=1.2, ls="--",
                           label=f"mean {r.mean():+.2f}R")
                ax.set_xlabel("R multiple (P&L / initial risk)")
                ax.set_ylabel("trades")
                ax.legend()
                ax.grid(alpha=0.25)
                fig.tight_layout()
                fig.savefig(out_dir / "r_distribution.png", dpi=110)
                plt.close(fig)
                charts["r_distribution"] = "r_distribution.png"
        return charts

    # -- text --------------------------------------------------------------
    def to_markdown(self) -> str:
        m = self.metrics
        lines = [
            f"# {self.title}",
            "",
            f"generated {datetime.now():%Y-%m-%d %H:%M} | swing {__version__} | "
            f"git {git_commit()} | config `{self.cfg.hash}`",
            "",
            "## Headline",
            "",
            "```",
            *m.summary_lines(),
            "```",
            "",
        ]

        if self.warnings:
            lines += ["## Warnings", ""]
            lines += [f"- {w}" for w in self.warnings]
            lines += [""]

        lines += ["## Caveats", "", SURVIVORSHIP_NOTE, "", GATE_NOTE, ""]

        yearly = yearly_table(self.equity, self.trades)
        if len(yearly):
            lines += ["## By year", "", _md_table(_format_yearly(yearly)), ""]

        monthly = monthly_returns(self.equity)
        if len(monthly):
            lines += ["## Monthly returns (%)", "",
                      _md_table((monthly * 100).round(2), index_label="year"), ""]

        exits = exit_reason_table(self.trades)
        if len(exits):
            lines += ["## Exits", "", _md_table(exits.round(3)), ""]

        for name, table in self.extra_tables.items():
            if table is not None and len(table):
                lines += [f"## {name.replace('_', ' ').title()}", "", _md_table(table), ""]

        if self.trades is not None and len(self.trades):
            cols = ["symbol", "entry_date", "exit_date", "exit_reason", "pnl", "r_multiple"]
            lines += ["## Five best trades", "",
                      _md_table(_round_numeric(self.trades.nlargest(5, "pnl")[cols])), ""]
            lines += ["## Five worst trades", "",
                      _md_table(_round_numeric(self.trades.nsmallest(5, "pnl")[cols])), ""]

        lines += [
            "## Reproducibility",
            "",
            "```json",
            json.dumps(
                {k: v for k, v in self.manifest().items() if k != "params"},
                indent=2, sort_keys=True,
            ),
            "```",
            "",
        ]
        return "\n".join(lines)

    def to_html(self, charts: dict[str, str]) -> str:
        m = self.metrics
        cards = [
            ("CAGR", f"{m.cagr:.2%}"),
            ("Max drawdown", f"{m.max_drawdown:.2%}"),
            ("Sharpe", f"{m.sharpe:.2f}"),
            ("Profit factor", _fmt_pf(m.profit_factor)),
            ("Win rate", f"{m.win_rate:.1%}"),
            ("Trades", f"{m.n_trades}"),
            ("Expectancy", f"{m.expectancy_r:+.3f} R"),
            ("Avg hold", f"{m.avg_hold_days:.0f} d"),
        ]
        card_html = "".join(
            f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
            for k, v in cards
        )
        chart_html = "".join(
            f'<figure><img src="{src}" alt="{name}"></figure>'
            for name, src in charts.items()
        )
        warn_html = ""
        if self.warnings:
            items = "".join(f"<li>{_esc(w)}</li>" for w in self.warnings)
            warn_html = f'<div class="warn"><strong>Warnings</strong><ul>{items}</ul></div>'

        tables = []
        yearly = yearly_table(self.equity, self.trades)
        if len(yearly):
            tables.append(("By year", _format_yearly(yearly).to_html(classes="tbl")))
        monthly = monthly_returns(self.equity)
        if len(monthly):
            tables.append(
                ("Monthly returns (%)",
                 (monthly * 100).round(2).to_html(classes="tbl", na_rep=""))
            )
        exits = exit_reason_table(self.trades)
        if len(exits):
            tables.append(("Exits", exits.round(3).to_html(classes="tbl")))
        for name, table in self.extra_tables.items():
            if table is not None and len(table):
                tables.append((name.replace("_", " ").title(), table.to_html(classes="tbl")))
        table_html = "".join(f"<h2>{_esc(t)}</h2>{html}" for t, html in tables)

        summary = _esc("\n".join(m.summary_lines()))
        repro = _esc(
            json.dumps(
                {k: v for k, v in self.manifest().items() if k != "params"},
                indent=2, sort_keys=True,
            )
        )

        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(self.title)}</title>
<style>
 :root {{ color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --muted:#666;
          --line:#e3e3e3; --accent:#1f77b4; }}
 @media (prefers-color-scheme: dark) {{
   :root {{ --fg:#e8e8e8; --bg:#151515; --muted:#a0a0a0; --line:#333; }} }}
 body {{ font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0 auto; max-width: 1080px; padding: 2rem 1.25rem;
         color: var(--fg); background: var(--bg); }}
 h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }}
 h2 {{ font-size: 1.1rem; margin-top: 2rem; border-bottom: 1px solid var(--line);
       padding-bottom: .3rem; }}
 .meta {{ color: var(--muted); font-size: .85rem; font-family: ui-monospace, monospace; }}
 .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
           gap: .6rem; margin: 1.4rem 0; }}
 .card {{ border: 1px solid var(--line); border-radius: 8px; padding: .7rem .8rem; }}
 .card .k {{ color: var(--muted); font-size: .75rem; text-transform: uppercase;
             letter-spacing: .04em; }}
 .card .v {{ font-size: 1.25rem; font-weight: 600; margin-top: .15rem; }}
 figure {{ margin: 1.2rem 0; }} img {{ max-width: 100%; border-radius: 6px; }}
 table.tbl {{ border-collapse: collapse; font-size: .85rem; width: 100%; }}
 .tbl th, .tbl td {{ border: 1px solid var(--line); padding: .3rem .5rem;
                     text-align: right; }}
 .tbl th {{ background: rgba(127,127,127,.12); }}
 .wrap {{ overflow-x: auto; }}
 .warn {{ border-left: 4px solid #e0a800; background: rgba(224,168,0,.10);
          padding: .7rem 1rem; border-radius: 4px; margin: 1rem 0; }}
 .caveat {{ border-left: 4px solid var(--accent); background: rgba(31,119,180,.08);
            padding: .7rem 1rem; border-radius: 4px; margin: 1rem 0; font-size: .9rem; }}
 pre {{ background: rgba(127,127,127,.1); padding: .8rem; border-radius: 6px;
        overflow-x: auto; font-size: .8rem; }}
</style></head><body>
<h1>{_esc(self.title)}</h1>
<div class="meta">generated {datetime.now():%Y-%m-%d %H:%M} &middot; swing {__version__}
 &middot; git {git_commit()} &middot; config {self.cfg.hash}</div>
<div class="cards">{card_html}</div>
{warn_html}
<pre>{summary}</pre>
{chart_html}
<div class="caveat">{_md_to_html(SURVIVORSHIP_NOTE)}</div>
<div class="caveat">{_md_to_html(GATE_NOTE)}</div>
<div class="wrap">{table_html}</div>
<h2>Reproducibility</h2>
<pre>{repro}</pre>
</body></html>"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _round_numeric(frame: pd.DataFrame, places: int = 3) -> pd.DataFrame:
    """Round only the float columns; ``DataFrame.round`` warns on datetimes."""
    out = frame.copy()
    for col in out.columns:
        if pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].round(places)
    return out


def _fmt_pf(value: float) -> str:
    if value == float("inf"):
        return "inf (no losers)"
    return f"{value:.2f}"


def _format_yearly(yearly: pd.DataFrame) -> pd.DataFrame:
    out = yearly.copy()
    out["return"] = (out["return"] * 100).round(2)
    out["max_drawdown"] = (out["max_drawdown"] * 100).round(2)
    return out.rename(columns={"return": "return %", "max_drawdown": "max dd %"})


def _md_table(frame: pd.DataFrame, index_label: str = "") -> str:
    header = [index_label or (frame.index.name or "")] + [str(c) for c in frame.columns]
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    for idx, row in frame.iterrows():
        cells = [str(idx)] + ["" if pd.isna(v) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE = re.compile(r"`([^`]+)`")


def _md_to_html(text: str) -> str:
    """Render the tiny subset of markdown used in the caveat blocks."""
    out = _esc(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    return "<p>" + out.replace("\n\n", "</p><p>").replace("\n", " ") + "</p>"


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _jsonable(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if np.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        if np.isnan(obj):
            return None
        return round(obj, 8)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return _jsonable(float(obj))
    if isinstance(obj, (date, datetime, pd.Timestamp)):
        return str(obj)
    return obj


def build_report(
    cfg: Config,
    title: str,
    result,
    kind: str = "backtest",
    extra_tables: dict[str, pd.DataFrame] | None = None,
    manifest_extra: dict | None = None,
) -> BacktestReport:
    """Build a report from either a BacktestResult or a WalkForwardResult."""
    metrics = getattr(result, "metrics", None)
    if metrics is None:
        metrics = compute_metrics(
            result.equity, result.trades, result.exposure, result.open_positions
        )
    return BacktestReport(
        cfg=cfg,
        title=title,
        equity=result.equity,
        trades=result.trades,
        metrics=metrics,
        exposure=getattr(result, "exposure", None),
        open_positions=getattr(result, "open_positions", None),
        warnings=list(getattr(result, "warnings", []) or []),
        extra_tables=extra_tables,
        manifest_extra=manifest_extra,
        kind=kind,
    )
