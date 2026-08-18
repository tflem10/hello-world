"""The backtest gate.

``swing scan`` will not emit live picks until a walk-forward report exists whose
**out-of-sample** metrics clear the thresholds in ``[backtest.gate]`` *and* whose
config hash matches the config you are about to trade. The hash check is the
part that matters in practice: it means editing a stop multiple silently
re-locks the gate until you re-validate, rather than letting a freshly
hand-tuned parameter set go straight to your phone.

The gate is a floor, not an endorsement. Clearing it means the numbers met the
minimums you wrote down before looking; it says nothing about whether the
strategy will work next year.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..logging_setup import get_logger

log = get_logger("swing.gate")

WALK_FORWARD_KIND = "walk_forward"


@dataclass
class GateStatus:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    report_path: Path | None = None
    metrics: dict | None = None
    checked: list[tuple[str, float, float, bool]] = field(default_factory=list)

    def describe(self) -> str:
        head = "PASS" if self.passed else "BLOCKED"
        lines = [f"backtest gate: {head}"]
        if self.report_path:
            lines.append(f"  report: {self.report_path}")
        for name, actual, threshold, ok in self.checked:
            mark = "ok " if ok else "FAIL"
            lines.append(f"  [{mark}] {name:<16} {actual:>10.3f}  (limit {threshold:g})")
        for reason in self.reasons:
            lines.append(f"  - {reason}")
        return "\n".join(lines)


def find_reports(cfg: Config, kind: str | None = None) -> list[tuple[Path, dict]]:
    """All report manifests under the reports directory, newest first."""
    root = cfg.expand_path(cfg.reports.dir)
    found: list[tuple[Path, dict]] = []
    if not root.exists():
        return found
    for manifest_path in root.glob("*/manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if kind and payload.get("kind") != kind:
            continue
        found.append((manifest_path.parent, payload))
    found.sort(key=lambda item: item[1].get("generated_at", ""), reverse=True)
    return found


def check_gate(cfg: Config) -> GateStatus:
    gate = cfg.backtest.gate
    if not bool(gate.get("enabled", True)):
        return GateStatus(
            passed=True,
            reasons=["gate disabled in config ([backtest.gate] enabled = false)"],
        )

    reports = find_reports(cfg, kind=WALK_FORWARD_KIND)
    if not reports:
        return GateStatus(
            passed=False,
            reasons=[
                "no walk-forward report found. Run `swing backtest --walk-forward` "
                "before trading anything.",
            ],
        )

    matching = [(p, m) for p, m in reports if m.get("config_hash") == cfg.hash]
    if not matching:
        newest = reports[0]
        return GateStatus(
            passed=False,
            report_path=newest[0],
            reasons=[
                f"the newest walk-forward report was produced with config hash "
                f"{newest[1].get('config_hash')}, but the current config hashes to "
                f"{cfg.hash}. Strategy parameters changed since the last validation; "
                "re-run `swing backtest --walk-forward`.",
            ],
        )

    path, manifest = matching[0]
    metrics = manifest.get("metrics") or {}
    checks: list[tuple[str, float, float, bool]] = []
    reasons: list[str] = []

    pf = _as_float(metrics.get("profit_factor"))
    min_pf = float(gate.get("min_profit_factor", 0.0))
    ok = pf >= min_pf
    checks.append(("profit_factor", pf, min_pf, ok))
    if not ok:
        reasons.append(f"out-of-sample profit factor {pf:.2f} is below {min_pf:g}")

    dd = _as_float(metrics.get("max_drawdown"))
    max_dd = float(gate.get("max_drawdown_pct", 1.0))
    ok = dd <= max_dd
    checks.append(("max_drawdown", dd, max_dd, ok))
    if not ok:
        reasons.append(f"out-of-sample max drawdown {dd:.1%} exceeds {max_dd:.1%}")

    n = _as_float(metrics.get("n_trades"))
    min_n = float(gate.get("min_trades", 0))
    ok = n >= min_n
    checks.append(("n_trades", n, min_n, ok))
    if not ok:
        reasons.append(
            f"only {int(n)} out-of-sample trades; {int(min_n)} required for the "
            "statistics to mean anything"
        )

    sharpe = _as_float(metrics.get("sharpe"))
    min_sharpe = float(gate.get("min_sharpe", -99.0))
    ok = sharpe >= min_sharpe
    checks.append(("sharpe", sharpe, min_sharpe, ok))
    if not ok:
        reasons.append(f"out-of-sample Sharpe {sharpe:.2f} is below {min_sharpe:g}")

    passed = all(item[3] for item in checks)
    return GateStatus(
        passed=passed, reasons=reasons, report_path=path, metrics=metrics, checked=checks
    )


def _as_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        # metrics.json serialises an infinite profit factor as the string "inf".
        # Treat it as zero: no losing trades means too small a sample, not a win.
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
