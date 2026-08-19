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
   not update ``latest.json`` at all (see :mod:`swing.backtest.runner`) — and
   if one is copied over it anyway, this module refuses it on sight.

THE REPORT IS UNTRUSTED INPUT (audit BUG-017)
----------------------------------------------
``latest.json`` is an ordinary file a human, a script or a half-finished write
can produce, so it is parsed defensively rather than believed:

* ``walkforward`` must be the JSON literal ``true``. Python truthiness would
  read the *string* ``"false"`` as True and open the gate on an in-sample fit.
* ``Infinity`` / ``-Infinity`` / ``NaN`` are rejected at parse time (they are
  JSON extensions, not JSON), and every number is re-checked for finiteness
  after parsing so a plain ``1e400`` cannot slip through as ``inf`` either.
* An unusable number always falls back to the *worst* possible reading, and a
  profit factor flagged ``profit_factor_capped`` is a sentinel standing in for
  infinity rather than a measurement, so it refuses instead of sailing through.

Every failure is a plain-English sentence, because it is printed to a human who
is about to be told they may not trade today and deserves to know why.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from swing.config import Config

__all__ = [
    "ABLATION_PREFIX",
    "GateResult",
    "backtest_dir",
    "check",
    "latest_path",
    "load_latest",
    "read_latest",
]

log = logging.getLogger(__name__)

#: Runs whose label starts with this are deliberately crippled variants. They
#: never write ``latest.json`` (:mod:`swing.backtest.runner`) and never open the
#: gate if one is put there by hand.
ABLATION_PREFIX = "ablate"

#: What a caller is told when the report contains a number JSON cannot hold.
NON_FINITE_PROBLEM = (
    "the report contains a non-finite number (Infinity or NaN), which no real measurement "
    "produces, so it cannot be trusted"
)


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


class _NonFiniteJSON(ValueError):
    """Raised while parsing when the report carries ``Infinity`` / ``NaN``."""


def _reject_constant(name: str) -> float:
    raise _NonFiniteJSON(name)


def read_latest(cfg: Config) -> tuple[dict[str, Any] | None, str | None]:
    """Read ``latest.json`` and say what is wrong with it when it is unusable.

    Returns:
        ``(summary, problem)``. Exactly one is ever non-``None``: a parsed
        object, or a plain-English clause naming why there is none. A missing
        file is reported as ``(None, None)`` — that is not a corruption, it is
        simply the absence of a report, and callers phrase it their own way.
    """
    path = latest_path(cfg)
    if not path.is_file():
        return None, None
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle, parse_constant=_reject_constant)
    except _NonFiniteJSON as exc:
        log.warning("Refusing %s: it contains the JSON literal %s.", path, exc)
        return None, NON_FINITE_PROBLEM
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return None, "the report could not be read or is not valid JSON"
    if not isinstance(data, dict):
        return None, "the report is not a JSON object"
    return data, None


def load_latest(cfg: Config) -> dict[str, Any] | None:
    """Read ``latest.json``, or return ``None`` when it is missing or unreadable."""
    summary, _problem = read_latest(cfg)
    return summary


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
    summary, problem = read_latest(cfg)

    if summary is None:
        if problem is not None:
            return GateResult(
                passed=False,
                reasons=[
                    f"The backtest report at {latest_path(cfg)} cannot be used because "
                    f"{problem}. Re-run `swing backtest` to regenerate it."
                ],
                report_path=latest_path(cfg),
            )
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

    # `is True`, not truthiness: bool("false") is True, and a hand-edited or
    # script-generated report is exactly where that string comes from.
    if summary.get("walkforward") is not True:
        reasons.append(
            "The most recent backtest was not a walk-forward run, and a backtest whose "
            "parameters were fitted to the same data they were measured on cannot open this "
            "gate. Run `swing backtest` without --no-walkforward."
        )

    if str(summary.get("label") or "").startswith(ABLATION_PREFIX):
        reasons.append(
            f"The most recent backtest is labelled "
            f"{str(summary.get('label') or '')!r}, which marks it as an ablation — a "
            f"deliberately crippled variant run to measure one component's contribution. "
            f"An ablation can never be the reference. Re-run `swing backtest` with a normal "
            f"label."
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

    # Plain truthiness here, and `is True` for `walkforward` above — the
    # asymmetry is deliberate, because the two flags point opposite ways.
    # Truthiness on `walkforward` would OPEN the gate on the string "false";
    # truthiness on this flag only ever CLOSES it, so every ambiguous value
    # (True, "true", 1) lands on the safe side. A missing key means "not
    # capped", which keeps reports written before the flag existed valid.
    if oos.get("profit_factor_capped"):
        reasons.append(
            "The out-of-sample window recorded zero losing trades, so its profit factor is a "
            "sentinel rather than a measurement — which is too good to trust. Investigate the "
            "data or configuration before trading."
        )
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
    """Coerce a JSON value to a FINITE float, falling back to ``default`` otherwise.

    ``default`` is always the worst possible reading, so a report whose numbers
    are missing, quoted, NaN or infinite fails the gate rather than sailing
    through it (audit BUG-017). ``1e400`` parses as ``inf`` without ever
    touching ``parse_constant``, which is why finiteness is re-checked here.
    """
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _as_int(value: Any, *, default: int) -> int:
    """Coerce a JSON value to int; ``inf`` raises OverflowError, not a traceback."""
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result
