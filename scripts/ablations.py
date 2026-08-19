#!/usr/bin/env python3
"""One-change-at-a-time ablation runner for the Trend-Momentum Core strategy.

Runs a walk-forward backtest for the shipping baseline plus a fixed set of single-parameter
variants, collects the out-of-sample metrics from each run's ``summary.json`` (SPEC Contract 11),
and writes a markdown comparison table to ``docs/ablation-results.md`` and stdout.

Every variant is produced with :func:`dataclasses.replace` on the loaded ``Config`` so that exactly
one knob differs from baseline. All runs use ``walkforward=True``; the tabulated metrics are
therefore the concatenated out-of-sample figures, not the in-sample-contaminated full-period ones.

Usage::

    uv run python scripts/ablations.py --universe etf
    uv run python scripts/ablations.py --universe stocks
    uv run python scripts/ablations.py --universe full --quick

The variant set is fixed and small on purpose. See ``docs/indicator-research.md`` for the rationale
behind each variant and for why the best-scoring row must not be adopted as the shipping config.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = REPO_ROOT / "docs" / "ablation-results.md"

#: Number of calendar years covered by ``--quick``. Enough for indicator warm-up plus a 3-year
#: in-sample window and a couple of out-of-sample folds -- a smoke test, not a result.
QUICK_YEARS = 6

#: ``summary.json`` block that the table reports. Never ``full_period``.
METRIC_BLOCK = "oos"

#: (summary key, column header) pairs, in display order.
METRICS: tuple[tuple[str, str], ...] = (
    ("profit_factor", "PF"),
    ("cagr", "CAGR"),
    ("max_drawdown_pct", "MaxDD %"),
    ("sharpe", "Sharpe"),
    ("sortino", "Sortino"),
    ("win_rate", "Win %"),
    ("trades", "Trades"),
    ("avg_win", "Avg win"),
    ("avg_loss", "Avg loss"),
    ("avg_hold_days", "Hold d"),
    ("exposure_pct", "Expo %"),
)

#: Metrics shown in the "delta vs baseline" table.
DELTA_METRICS: tuple[tuple[str, str], ...] = (
    ("profit_factor", "PF"),
    ("cagr", "CAGR"),
    ("max_drawdown_pct", "MaxDD %"),
    ("trades", "Trades"),
)

BASELINE_NAME = "baseline"


# --------------------------------------------------------------------------------------------
# config plumbing
# --------------------------------------------------------------------------------------------


def _ensure_src_on_path() -> None:
    """Allow ``python3 scripts/ablations.py`` to work without an editable install."""
    src = REPO_ROOT / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def load_base_config() -> Any:
    """Load the resolved ``Config`` via SPEC Contract 1's ``load_config``."""
    _ensure_src_on_path()
    from swing.config import load_config

    return load_config()


def _replace_strategy(cfg: Any, **changes: Any) -> Any:
    """Return ``cfg`` with ``StrategyCfg`` fields replaced."""
    return dataclasses.replace(cfg, strategy=dataclasses.replace(cfg.strategy, **changes))


def _replace_regime(cfg: Any, **changes: Any) -> Any:
    """Return ``cfg`` with ``RegimeCfg`` fields replaced."""
    return dataclasses.replace(cfg, regime=dataclasses.replace(cfg.regime, **changes))


# --------------------------------------------------------------------------------------------
# variants
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One ablation: a name, a run label, a human description, and a config transform."""

    name: str
    label: str
    change: str
    apply: Callable[[Any], Any]


def build_variants() -> tuple[Variant, ...]:
    """The fixed ablation set. One change per variant, relative to shipping defaults."""
    return (
        Variant(
            name=BASELINE_NAME,
            label="ablate-baseline",
            change="none (shipping defaults)",
            apply=lambda cfg: cfg,
        ),
        Variant(
            name="regime_off",
            label="ablate-regime-off",
            change="`regime.enabled` True -> False",
            apply=lambda cfg: _replace_regime(cfg, enabled=False),
        ),
        Variant(
            name="adx_off",
            label="ablate-adx-off",
            change="`strategy.adx_min` 20.0 -> 0.0",
            apply=lambda cfg: _replace_strategy(cfg, adx_min=0.0),
        ),
        Variant(
            name="volume_off",
            label="ablate-volume-off",
            change="`strategy.volume_mult` 1.3 -> 1.0",
            apply=lambda cfg: _replace_strategy(cfg, volume_mult=1.0),
        ),
        Variant(
            name="skip_off",
            label="ablate-skip-off",
            change="`strategy.mom_skip_days` 5 -> 0",
            apply=lambda cfg: _replace_strategy(cfg, mom_skip_days=0),
        ),
        Variant(
            name="weights_equal",
            label="ablate-weights-equal",
            change="`strategy.mom_weight_126`/`mom_weight_63` 0.6/0.4 -> 0.5/0.5",
            apply=lambda cfg: _replace_strategy(cfg, mom_weight_126=0.5, mom_weight_63=0.5),
        ),
        Variant(
            name="chandelier_2",
            label="ablate-chandelier-2",
            change="`strategy.chandelier_mult` 3.0 -> 2.0",
            apply=lambda cfg: _replace_strategy(cfg, chandelier_mult=2.0),
        ),
        Variant(
            name="chandelier_4",
            label="ablate-chandelier-4",
            change="`strategy.chandelier_mult` 3.0 -> 4.0",
            apply=lambda cfg: _replace_strategy(cfg, chandelier_mult=4.0),
        ),
        Variant(
            name="time_stop_off",
            label="ablate-time-stop-off",
            change="`strategy.time_stop_days` 40 -> 0 (disabled)",
            apply=lambda cfg: _replace_strategy(cfg, time_stop_days=0),
        ),
        Variant(
            name="rsi2_on",
            label="ablate-rsi2-on",
            change="`strategy.rsi2_enabled` False -> True",
            apply=lambda cfg: _replace_strategy(cfg, rsi2_enabled=True),
        ),
    )


# --------------------------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------------------------


@dataclass
class RunOutcome:
    """Result of a single ablation run."""

    variant: Variant
    summary: dict[str, Any] | None = None
    error: str | None = None
    report_path: Path | None = None

    @property
    def metrics(self) -> dict[str, Any]:
        """The out-of-sample metric block, or an empty mapping when the run failed."""
        if self.summary is None:
            return {}
        block = self.summary.get(METRIC_BLOCK)
        return block if isinstance(block, dict) else {}


def _resolve_summary_path(returned: Path) -> Path:
    """``run_backtest`` returns the run directory; tolerate a direct summary.json too."""
    if returned.is_dir():
        return returned / "summary.json"
    return returned


def run_variant(
    base_cfg: Any,
    variant: Variant,
    *,
    universe: str,
    start: date,
    end: date,
) -> RunOutcome:
    """Apply one variant and execute a walk-forward backtest against SPEC Contract 2."""
    from swing.backtest.runner import run_backtest

    cfg = variant.apply(base_cfg)
    try:
        returned = Path(
            run_backtest(
                cfg,
                universe=universe,
                start=start,
                end=end,
                walkforward=True,
                label=variant.label,
            )
        )
        summary_path = _resolve_summary_path(returned)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        # One failing variant must not abort the sweep; record it and carry on.
        return RunOutcome(variant=variant, error=traceback.format_exc(limit=4).strip())
    return RunOutcome(variant=variant, summary=summary, report_path=returned)


def run_all(
    base_cfg: Any,
    variants: Sequence[Variant],
    *,
    universe: str,
    start: date,
    end: date,
) -> list[RunOutcome]:
    """Run every variant in order, reporting progress on stderr."""
    outcomes: list[RunOutcome] = []
    total = len(variants)
    for index, variant in enumerate(variants, start=1):
        print(f"[{index}/{total}] running {variant.name} ...", file=sys.stderr, flush=True)
        outcome = run_variant(base_cfg, variant, universe=universe, start=start, end=end)
        if outcome.error is not None:
            print(f"    FAILED: {outcome.error.splitlines()[-1]}", file=sys.stderr, flush=True)
        outcomes.append(outcome)
    return outcomes


# --------------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    """Format one metric cell."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _fmt_delta(value: Any, baseline: Any) -> str:
    """Format a delta cell relative to the baseline run."""
    if value is None or baseline is None:
        return "n/a"
    if isinstance(value, bool) or isinstance(baseline, bool):
        return "n/a"
    if not isinstance(value, (int, float)) or not isinstance(baseline, (int, float)):
        return "n/a"
    delta = value - baseline
    if isinstance(value, int) and isinstance(baseline, int):
        return f"{delta:+d}"
    return f"{delta:+.2f}"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Render a markdown table."""
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _identity_rows(outcomes: Sequence[RunOutcome]) -> list[str]:
    """Reproducibility triple from the baseline run (SPEC Contract 11)."""
    for outcome in outcomes:
        if outcome.variant.name == BASELINE_NAME and outcome.summary is not None:
            summary = outcome.summary
            return [
                f"- `config_hash` (baseline): `{summary.get('config_hash', 'n/a')}`",
                f"- `code_ref`: `{summary.get('code_ref', 'n/a')}`",
                f"- `data_hash`: `{summary.get('data_hash', 'n/a')}`",
            ]
    return ["- baseline run did not complete; no reproducibility triple recorded"]


@dataclass
class RunContext:
    """Everything about the sweep that is not a per-variant result."""

    universe: str
    start: date
    end: date
    quick: bool
    generated_on: date
    failures: list[str] = field(default_factory=list)


def render_markdown(outcomes: Sequence[RunOutcome], ctx: RunContext) -> str:
    """Render the full ``docs/ablation-results.md`` document."""
    baseline_metrics: dict[str, Any] = {}
    for outcome in outcomes:
        if outcome.variant.name == BASELINE_NAME:
            baseline_metrics = outcome.metrics
            break

    main_header = ["Variant", "Change"] + [label for _, label in METRICS]
    main_rows: list[list[str]] = []
    for outcome in outcomes:
        if outcome.error is not None:
            cells = ["FAILED"] * len(METRICS)
        else:
            cells = [_fmt(outcome.metrics.get(key)) for key, _ in METRICS]
        main_rows.append([f"`{outcome.variant.name}`", outcome.variant.change, *cells])

    delta_header = ["Variant"] + [f"d {label}" for _, label in DELTA_METRICS]
    delta_rows: list[list[str]] = []
    for outcome in outcomes:
        if outcome.variant.name == BASELINE_NAME or outcome.error is not None:
            continue
        cells = [
            _fmt_delta(outcome.metrics.get(key), baseline_metrics.get(key))
            for key, _ in DELTA_METRICS
        ]
        delta_rows.append([f"`{outcome.variant.name}`", *cells])

    window = f"{ctx.start.isoformat()} to {ctx.end.isoformat()}"
    mode = "quick (shortened window)" if ctx.quick else "full configured window"

    parts = [
        "# Ablation Results",
        "",
        "Generated by `scripts/ablations.py`. Every run is walk-forward, so the metrics below are",
        f'**out-of-sample** (`summary.json["{METRIC_BLOCK}"]`), not full-period.',
        "",
        "## Run parameters",
        "",
        f"- Universe: `{ctx.universe}`",
        f"- Window: {window}",
        f"- Mode: {mode}",
        f"- Generated: {ctx.generated_on.isoformat()}",
        "",
        *_identity_rows(outcomes),
        "",
        "## Out-of-sample metrics",
        "",
        _table(main_header, main_rows),
        "",
    ]

    if delta_rows:
        parts += [
            "## Delta vs baseline",
            "",
            "Positive is better for PF, CAGR and Trades; **negative is better for MaxDD**.",
            "",
            _table(delta_header, delta_rows),
            "",
        ]

    if ctx.failures:
        parts += ["## Failed runs", ""]
        for name in ctx.failures:
            parts.append(f"- `{name}` — see stderr output from the run for the traceback")
        parts.append("")

    parts += [
        "## How to read this",
        "",
        "A component earns its place when **removing it degrades** out-of-sample profit factor or",
        "materially worsens max drawdown. A component whose removal barely moves anything is a",
        "candidate for deletion on parsimony grounds — fewer parameters means a lower probability",
        "of backtest overfitting.",
        "",
        "**Do not adopt the best-scoring variant as the shipping configuration.** With the trade",
        "counts this system produces, most differences here sit inside the noise band; selecting",
        "the top row is precisely the data-snooping failure documented in",
        "[`indicator-research.md` §13]"
        "(indicator-research.md#13-cross-cutting-caveat-data-snooping-and-edge-decay).",
        "",
        "Stock-universe results additionally carry the survivorship haircut described in",
        "[`backtest-methodology.md` §8]"
        "(backtest-methodology.md#8-the-haircut-convention-and-the-deployment-decision-rule).",
        "ETF-universe results do not.",
        "",
        "Rationale for each variant:",
        "[`indicator-research.md` § Ablation plan](indicator-research.md#ablation-plan).",
        "",
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------------------------


def resolve_window(cfg: Any, *, quick: bool, today: date) -> tuple[date, date]:
    """Resolve the backtest window from config, shortened when ``--quick`` is set."""
    end: date = cfg.backtest.end or today
    start: date = cfg.backtest.start
    if quick:
        start = max(start, date(end.year - QUICK_YEARS, 1, 1))
    return start, end


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="ablations.py",
        description="Run one-change-at-a-time ablations and write docs/ablation-results.md.",
    )
    parser.add_argument(
        "--universe",
        choices=("etf", "stocks", "full"),
        default="etf",
        help="universe to backtest (default: etf, the near-survivorship-clean lower bound)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=f"shorten the window to roughly the last {QUICK_YEARS} years (smoke test)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)

    cfg = load_base_config()
    today = date.today()
    start, end = resolve_window(cfg, quick=args.quick, today=today)

    variants = build_variants()
    print(
        f"ablations: universe={args.universe} window={start} to {end} "
        f"variants={len(variants)} walkforward=True",
        file=sys.stderr,
        flush=True,
    )

    outcomes = run_all(cfg, variants, universe=args.universe, start=start, end=end)
    ctx = RunContext(
        universe=args.universe,
        start=start,
        end=end,
        quick=args.quick,
        generated_on=today,
        failures=[o.variant.name for o in outcomes if o.error is not None],
    )

    document = render_markdown(outcomes, ctx)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(document, encoding="utf-8")
    print(document)
    print(f"\nwrote {RESULTS_PATH}", file=sys.stderr)
    print(
        "WARNING: this sweep wrote reports/backtest/latest.json once per variant, so the "
        "gate now reflects the LAST variant, not the shipping baseline. Re-run the plain "
        "`swing backtest` before trusting `swing scan`.",
        file=sys.stderr,
    )

    return 1 if ctx.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
