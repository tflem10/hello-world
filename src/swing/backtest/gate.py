"""FROZEN CONTRACT 11 (gate) — the mechanical permission to trade.

The gate is the whole point of this project. A strategy does not get to emit
picks because it looks clever; it gets to emit picks because a walk-forward
backtest, whose parameters never saw the data they were tested on, cleared three
numbers the user wrote down *in advance*:

* ``gates.min_profit_factor`` — gross wins per dollar of gross losses,
* ``gates.max_drawdown_pct``  — the worst peak-to-trough loss tolerated,
* ``gates.min_trades``        — how much evidence counts as evidence.

Three rules make the gate hard to fool:

1. **No report, no picks.** A missing ``latest.json`` fails.
2. **Non-walk-forward runs never pass.** A full-period backtest with tuned
   parameters is an in-sample fit; letting it open the gate would defeat the
   design.
3. **Ablation runs never become the reference.** Runs labelled ``ablate*`` do
   not update ``latest.json`` at all (see :mod:`swing.backtest.runner`), so a
   deliberately-crippled variant cannot be mistaken for the baseline.

Every failure is a plain-English sentence, because it is printed to a human who
is about to be told they may not trade today and deserves to know why.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = ["GateResult", "backtest_dir", "check", "latest_path", "load_latest"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateResult:
    """The verdict.

    Attributes:
        passed: whether picks may be emitted.
        reasons: every reason it failed, in a fixed order. Empty when passed.
        report_path: the run directory the verdict was read from, or the
            expected ``latest.json`` location when there is nothing to read.
    """

    passed: bool
    reasons: list[str]
    report_path: Path | None


def backtest_dir(cfg: Config) -> Path:
    """``<reports_dir>/backtest`` — where every backtest run is written."""
    return Path(cfg.paths.reports_dir) / "backtest"


def latest_path(cfg: Config) -> Path:
    """``<reports_dir>/backtest/latest.json`` — the gate's reference report."""
    return backtest_dir(cfg) / "latest.json"


def load_latest(cfg: Config) -> dict[str, Any] | None:
    """Read ``latest.json``, or return ``None`` when it is missing or unreadable."""
    path = latest_path(cfg)
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _resolve_report_path(cfg: Config, summary: dict[str, Any]) -> Path:
    """Point at the run directory when it still exists, else at latest.json."""
    label = str(summary.get("label") or "").strip()
    if label:
        candidate = backtest_dir(cfg) / label
        if candidate.is_dir():
            return candidate
    return latest_path(cfg)


def check(cfg: Config) -> GateResult:
    """Decide whether the strategy has earned the right to emit picks today.

    Reads ``<reports_dir>/backtest/latest.json`` and tests its ``oos`` block —
    the concatenated out-of-sample record — against ``cfg.gates``.

    Returns:
        A :class:`GateResult` whose ``reasons`` are complete sentences, each
        naming the measured value, the required value, and what to do about it.
    """
    gates = cfg.gates
    summary = load_latest(cfg)

    if summary is None:
        return GateResult(
            passed=False,
            reasons=[
                f"No backtest report was found at {latest_path(cfg)}, so there is no evidence "
                f"this strategy works. Run `swing backtest` and try again."
            ],
            report_path=latest_path(cfg),
        )

    report_path = _resolve_report_path(cfg, summary)
    reasons: list[str] = []

    if not bool(summary.get("walkforward", False)):
        reasons.append(
            "The most recent backtest was not a walk-forward run, and a backtest whose "
            "parameters were fitted to the same data they were measured on cannot open this "
            "gate. Run `swing backtest` without --no-walkforward."
        )

    oos = summary.get("oos")
    if not isinstance(oos, dict):
        reasons.append(
            "The most recent backtest report has no out-of-sample results section, so it "
            "cannot be judged. Re-run `swing backtest` to regenerate it."
        )
        return GateResult(passed=False, reasons=reasons, report_path=report_path)

    # Defaults fail *closed*: a missing or corrupt number is treated as the
    # worst possible reading, never as a pass.
    profit_factor = _as_float(oos.get("profit_factor"), default=0.0)
    max_drawdown = _as_float(oos.get("max_drawdown_pct"), default=100.0)
    trades = _as_int(oos.get("trades"), default=0)

    if profit_factor < gates.min_profit_factor:
        reasons.append(
            f"Out-of-sample profit factor is {profit_factor:.2f}, below the required "
            f"{gates.min_profit_factor:.2f} — the strategy did not make enough per dollar "
            f"lost to be worth trading."
        )
    if max_drawdown > gates.max_drawdown_pct:
        reasons.append(
            f"Out-of-sample maximum drawdown is {max_drawdown:.1f}%, worse than the "
            f"{gates.max_drawdown_pct:.1f}% you said you would tolerate."
        )
    if trades < gates.min_trades:
        reasons.append(
            f"Only {trades} out-of-sample trades were taken, fewer than the {gates.min_trades} "
            f"needed before the result means anything. Lengthen the backtest or widen the "
            f"universe."
        )

    return GateResult(passed=not reasons, reasons=reasons, report_path=report_path)


def _as_float(value: Any, *, default: float) -> float:
    """Coerce a JSON value to float, falling back to ``default`` on anything unusable."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # NaN-safe


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
