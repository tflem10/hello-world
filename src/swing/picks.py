"""Pick sheets: the artefact the nightly scan produces.

A pick sheet is a dated, self-contained record of what the system decided and
why: the picks, the exact share counts and stops, the sizing constraint that
bound, the earnings situation, the market regime, the gate status, and the
config hash that produced it all. It serialises to JSON (machine-readable, and
what ``swing confirm`` and ``swing execute`` read the next morning) and renders
to Markdown/HTML/plain text for the alert channels.

The deliberate design decision here is that **unaffordable picks are not
hidden**. At $100 of equity almost every candidate lands in the watch list, and
the sheet says why in dollars. Silently dropping them would make the system
look like it found nothing, when in fact it found something you cannot yet buy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Config

STATUS_TRADABLE = "tradable"
STATUS_UNAFFORDABLE = "watch_unaffordable"
STATUS_INVALIDATED = "invalidated"
STATUS_CONFIRMED = "confirmed"
STATUS_ADJUSTED = "adjusted"


@dataclass
class Pick:
    """One candidate, fully sized, with its drafted order attached."""

    symbol: str
    rank: int
    status: str
    close: float                 # signal-bar close: the reference price
    atr: float
    stop: float
    trail_offset: float          # dollars below the high-water mark for the trail
    shares: int
    notional: float
    risk_dollars: float
    risk_pct: float
    equity_pct: float
    sizing_limit: str
    sizing_note: str = ""
    rank_score: float = float("nan")
    adx: float = float("nan")
    dollar_volume: float = float("nan")
    is_etf: bool = False
    name: str = ""
    earnings_date: str | None = None
    earnings_note: str = ""
    thesis: str = ""
    stop_limit_price: float = 0.0
    orders: dict[str, Any] = field(default_factory=dict)
    # populated by `swing confirm`
    confirm_price: float | None = None
    confirm_note: str = ""

    @property
    def tradable(self) -> bool:
        return self.status in (STATUS_TRADABLE, STATUS_CONFIRMED, STATUS_ADJUSTED)

    def as_dict(self) -> dict:
        return asdict(self)

    def block(self, equity: float) -> str:
        """The human-readable block that goes in the email and the pick sheet."""
        lines = [f"{self.rank}. {self.symbol}" + (f"  {self.name}" if self.name else "")]
        if self.status == STATUS_TRADABLE or self.tradable:
            lines += [
                f"   buy      {self.shares} sh  ref {self.close:.2f}"
                f"   (${self.notional:,.2f}, {self.equity_pct:.1%} of account)",
                f"   stop     {self.stop:.2f}   "
                f"(-{self.close - self.stop:.2f}, {(self.close - self.stop) / self.close:.1%})",
                f"   trail    {self.trail_offset:.2f} below the high-water close "
                f"(Chandelier)",
                f"   risk     ${self.risk_dollars:,.2f}  ({self.risk_pct:.2%} of account)",
            ]
            if self.stop_limit_price:
                lines.append(f"   stop-lmt {self.stop_limit_price:.2f} (stop-limit variant)")
        else:
            lines.append(f"   WATCH — {self.sizing_note or self.sizing_limit}")
            lines.append(
                f"   would be stop {self.stop:.2f} "
                f"(-{(self.close - self.stop) / self.close:.1%}) from {self.close:.2f}"
            )
        if self.earnings_note:
            lines.append(f"   earnings {self.earnings_note}")
        if self.thesis:
            lines.append(f"   why      {self.thesis}")
        if self.confirm_note:
            lines.append(f"   confirm  {self.confirm_note}")
        return "\n".join(lines)


@dataclass
class HoldingAction:
    """Something to do about a position already on the books."""

    symbol: str
    action: str
    detail: str


@dataclass
class PickSheet:
    as_of: str
    generated_at: str
    equity: float
    available_cash: float
    regime_ok: bool
    regime_note: str
    gate_passed: bool
    gate_note: str
    config_hash: str
    universe_size: int
    candidates_considered: int
    picks: list[Pick] = field(default_factory=list)
    watch: list[Pick] = field(default_factory=list)
    holdings: list[HoldingAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_as_of: str = ""
    swing_version: str = ""

    # -- serialisation -----------------------------------------------------
    def to_json(self) -> str:
        payload = {
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "equity": self.equity,
            "available_cash": self.available_cash,
            "regime_ok": self.regime_ok,
            "regime_note": self.regime_note,
            "gate_passed": self.gate_passed,
            "gate_note": self.gate_note,
            "config_hash": self.config_hash,
            "universe_size": self.universe_size,
            "candidates_considered": self.candidates_considered,
            "data_as_of": self.data_as_of,
            "swing_version": self.swing_version,
            "picks": [p.as_dict() for p in self.picks],
            "watch": [p.as_dict() for p in self.watch],
            "holdings": [asdict(h) for h in self.holdings],
            "warnings": self.warnings,
        }
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    @classmethod
    def from_json(cls, text: str) -> PickSheet:
        payload = json.loads(text)
        sheet = cls(
            as_of=payload["as_of"],
            generated_at=payload["generated_at"],
            equity=payload["equity"],
            available_cash=payload.get("available_cash", payload["equity"]),
            regime_ok=payload["regime_ok"],
            regime_note=payload.get("regime_note", ""),
            gate_passed=payload.get("gate_passed", False),
            gate_note=payload.get("gate_note", ""),
            config_hash=payload.get("config_hash", ""),
            universe_size=payload.get("universe_size", 0),
            candidates_considered=payload.get("candidates_considered", 0),
            data_as_of=payload.get("data_as_of", ""),
            swing_version=payload.get("swing_version", ""),
            warnings=payload.get("warnings", []),
        )
        sheet.picks = [Pick(**p) for p in payload.get("picks", [])]
        sheet.watch = [Pick(**p) for p in payload.get("watch", [])]
        sheet.holdings = [HoldingAction(**h) for h in payload.get("holdings", [])]
        return sheet

    # -- rendering ---------------------------------------------------------
    def headline(self) -> str:
        if not self.gate_passed:
            return f"swing {self.as_of}: BLOCKED by the backtest gate"
        if not self.regime_ok:
            return f"swing {self.as_of}: no new entries (market regime is off)"
        if self.picks:
            symbols = ", ".join(p.symbol for p in self.picks)
            return f"swing {self.as_of}: {len(self.picks)} pick(s) — {symbols}"
        if self.watch:
            return f"swing {self.as_of}: 0 tradable, {len(self.watch)} on watch"
        return f"swing {self.as_of}: no candidates"

    def to_text(self) -> str:
        """Plain text — used for push notifications and the email fallback."""
        lines = [
            f"swing pick sheet — {self.as_of}",
            f"generated {self.generated_at}   config {self.config_hash}",
            "",
            f"account   ${self.equity:,.2f} equity, ${self.available_cash:,.2f} cash",
            f"regime    {'RISK-ON' if self.regime_ok else 'RISK-OFF'} — {self.regime_note}",
            f"gate      {'PASS' if self.gate_passed else 'BLOCKED'} — {self.gate_note}",
            f"universe  {self.universe_size} symbols, "
            f"{self.candidates_considered} passed every filter",
            "",
        ]

        if self.picks:
            lines.append(f"PICKS ({len(self.picks)})")
            lines.append("-" * 60)
            for pick in self.picks:
                lines.append(pick.block(self.equity))
                lines.append("")
        else:
            lines += ["PICKS: none", ""]

        if self.watch:
            lines.append(
                f"WATCH — passed every filter, not entered ({len(self.watch)})"
            )
            lines.append("-" * 60)
            for pick in self.watch:
                lines.append(pick.block(self.equity))
                lines.append("")

        if self.holdings:
            lines.append("OPEN POSITIONS")
            lines.append("-" * 60)
            for h in self.holdings:
                lines.append(f"  {h.symbol:<6} {h.action:<16} {h.detail}")
            lines.append("")

        if self.warnings:
            lines.append("WARNINGS")
            lines.append("-" * 60)
            for w in self.warnings:
                lines.append(f"  - {w}")
            lines.append("")

        lines += [
            "This is a drafted plan, not advice, and not an executed order.",
            "Review every line before placing anything.",
        ]
        return "\n".join(lines)

    def to_markdown(self) -> str:
        lines = [f"# swing pick sheet — {self.as_of}", ""]
        lines += [
            f"- generated: {self.generated_at}",
            f"- config hash: `{self.config_hash}`",
            f"- equity: ${self.equity:,.2f} (cash ${self.available_cash:,.2f})",
            f"- regime: **{'risk-on' if self.regime_ok else 'risk-off'}** — {self.regime_note}",
            f"- backtest gate: **{'pass' if self.gate_passed else 'BLOCKED'}** — {self.gate_note}",
            f"- universe: {self.universe_size} symbols, "
            f"{self.candidates_considered} passed every filter",
            "",
        ]
        if self.warnings:
            lines += ["> **Warnings**", ""]
            lines += [f"> - {w}" for w in self.warnings]
            lines += [""]

        if self.picks:
            lines += ["## Picks", "",
                      "| # | symbol | shares | ref | stop | trail | risk $ | % acct | why |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for p in self.picks:
                lines.append(
                    f"| {p.rank} | **{p.symbol}** | {p.shares} | {p.close:.2f} | "
                    f"{p.stop:.2f} | {p.trail_offset:.2f} | {p.risk_dollars:,.2f} | "
                    f"{p.equity_pct:.1%} | {p.thesis} |"
                )
            lines.append("")
        else:
            lines += ["## Picks", "", "_none_", ""]

        if self.watch:
            lines += ["## Watch — passed every filter, not entered", "",
                      "| # | symbol | ref | stop | why not |", "|---|---|---|---|---|"]
            for p in self.watch:
                lines.append(
                    f"| {p.rank} | {p.symbol} | {p.close:.2f} | {p.stop:.2f} | "
                    f"{p.sizing_note} |"
                )
            lines.append("")

        if self.holdings:
            lines += ["## Open positions", "", "| symbol | action | detail |",
                      "|---|---|---|"]
            for h in self.holdings:
                lines.append(f"| {h.symbol} | {h.action} | {h.detail} |")
            lines.append("")

        lines += ["---", "",
                  "_Drafted plan, not advice and not an executed order._"]
        return "\n".join(lines)

    def to_html(self) -> str:
        rows = ""
        for p in self.picks:
            rows += (
                f"<tr><td>{p.rank}</td><td><b>{p.symbol}</b></td><td>{p.shares}</td>"
                f"<td>{p.close:.2f}</td><td>{p.stop:.2f}</td><td>{p.trail_offset:.2f}</td>"
                f"<td>${p.risk_dollars:,.2f}</td><td>{p.equity_pct:.1%}</td>"
                f"<td>{_esc(p.thesis)}</td></tr>"
            )
        picks_html = (
            "<table><tr><th>#</th><th>symbol</th><th>shares</th><th>ref</th><th>stop</th>"
            "<th>trail</th><th>risk</th><th>% acct</th><th>why</th></tr>"
            f"{rows}</table>"
            if self.picks
            else "<p><i>no tradable picks</i></p>"
        )

        watch_html = ""
        if self.watch:
            wrows = "".join(
                f"<tr><td>{p.symbol}</td><td>{p.close:.2f}</td><td>{p.stop:.2f}</td>"
                f"<td>{_esc(p.sizing_note)}</td></tr>"
                for p in self.watch
            )
            watch_html = (
                "<h2>Watch — passed every filter, not entered</h2>"
                "<table><tr><th>symbol</th><th>ref</th><th>stop</th><th>why not</th></tr>"
                f"{wrows}</table>"
            )

        holdings_html = ""
        if self.holdings:
            hrows = "".join(
                f"<tr><td>{h.symbol}</td><td>{_esc(h.action)}</td><td>{_esc(h.detail)}</td></tr>"
                for h in self.holdings
            )
            holdings_html = (
                "<h2>Open positions</h2><table>"
                f"<tr><th>symbol</th><th>action</th><th>detail</th></tr>{hrows}</table>"
            )

        warn_html = ""
        if self.warnings:
            warn_html = (
                '<div class="warn"><b>Warnings</b><ul>'
                + "".join(f"<li>{_esc(w)}</li>" for w in self.warnings)
                + "</ul></div>"
            )

        gate_class = "ok" if self.gate_passed else "bad"
        regime_class = "ok" if self.regime_ok else "bad"

        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>swing picks {self.as_of}</title>
<style>
 body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        max-width: 860px; margin: 0 auto; padding: 1.5rem; color: #1a1a1a; }}
 h1 {{ font-size: 1.35rem; margin-bottom: .2rem; }}
 h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
 .meta {{ color: #666; font-size: .85rem; font-family: ui-monospace, monospace; }}
 table {{ border-collapse: collapse; width: 100%; font-size: .87rem; margin-top: .4rem; }}
 th, td {{ border: 1px solid #ddd; padding: .35rem .5rem; text-align: right; }}
 th {{ background: #f2f2f2; }} td:nth-child(2), td:last-child {{ text-align: left; }}
 .ok {{ color: #1a7f37; font-weight: 600; }} .bad {{ color: #b3261e; font-weight: 600; }}
 .warn {{ border-left: 4px solid #e0a800; background: #fff8e1; padding: .6rem .9rem;
          margin: 1rem 0; border-radius: 4px; }}
 .foot {{ color: #666; font-size: .8rem; margin-top: 2rem; border-top: 1px solid #ddd;
          padding-top: .6rem; }}
</style></head><body>
<h1>swing pick sheet — {self.as_of}</h1>
<div class="meta">generated {self.generated_at} &middot; config {self.config_hash}
 &middot; data through {self.data_as_of}</div>
<p>Account <b>${self.equity:,.2f}</b> (cash ${self.available_cash:,.2f}) &middot;
 regime <span class="{regime_class}">{'risk-on' if self.regime_ok else 'risk-off'}</span>
 &middot; gate <span class="{gate_class}">{'pass' if self.gate_passed else 'BLOCKED'}</span></p>
<p class="meta">{_esc(self.regime_note)}<br>{_esc(self.gate_note)}</p>
{warn_html}
<h2>Picks</h2>
{picks_html}
{watch_html}
{holdings_html}
<p class="foot">Drafted plan, not advice and not an executed order.
Review every line before placing anything. Orders are attached as JSON.</p>
</body></html>"""


def _esc(text: Any) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# on-disk layout
# ---------------------------------------------------------------------------
def sheet_dir(cfg: Config, when: date | str) -> Path:
    root = cfg.expand_path(cfg.reports.dir)
    return root / f"scan-{when}"


def write_sheet(cfg: Config, sheet: PickSheet, when: date | str | None = None) -> Path:
    out = sheet_dir(cfg, when or sheet.as_of)
    out.mkdir(parents=True, exist_ok=True)
    (out / "picks.json").write_text(sheet.to_json())
    (out / "picks.md").write_text(sheet.to_markdown())
    (out / "picks.html").write_text(sheet.to_html())
    (out / "picks.txt").write_text(sheet.to_text())

    orders_dir = out / "orders"
    orders_dir.mkdir(exist_ok=True)
    for pick in sheet.picks:
        for name, payload in (pick.orders or {}).items():
            (orders_dir / f"{pick.symbol}-{name}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True)
            )
    return out


def latest_sheet_path(cfg: Config) -> Path | None:
    root = cfg.expand_path(cfg.reports.dir)
    if not root.exists():
        return None
    candidates = sorted(root.glob("scan-*/picks.json"))
    return candidates[-1] if candidates else None


def load_sheet(path: Path) -> PickSheet:
    return PickSheet.from_json(Path(path).read_text())


def now_stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
