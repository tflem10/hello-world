#!/usr/bin/env python3
"""One-change-at-a-time ablation runner for the Trend-Momentum Core strategy.

Runs a walk-forward backtest for the shipping baseline plus a fixed set of single-parameter
variants, collects the out-of-sample metrics from each run's ``summary.json`` (SPEC Contract 11),
and writes a markdown comparison table to ``docs/ablation-results.md`` and stdout.

Every variant is produced with :func:`dataclasses.replace` on the loaded ``Config`` so that exactly
one knob differs from baseline. All runs use ``walkforward=True``; the tabulated metrics are
therefore the concatenated out-of-sample figures, not the in-sample-contaminated full-period ones.

The "Change" column is rendered from the *loaded* config at table-build time, so a table produced
against a customised ``config.toml`` states that config's real baseline values rather than the
shipping defaults it was written against (audit DEBT-009).

The sweep never touches the trading gate. Every run label starts with
:data:`swing.backtest.gate.ABLATION_PREFIX`, which the runner keys its "leave ``latest.json``
alone" branch on, and the prefix is *verified* on every variant before a single backtest starts —
so ``swing scan`` keeps gating on the last real ``swing backtest`` (audit DEBT-005).

Usage::

    uv run python scripts/ablations.py --universe etf
    uv run python scripts/ablations.py --universe stocks
    uv run python scripts/ablations.py --universe full --quick
    uv run python scripts/ablations.py --validate-only

``--validate-only`` applies every variant's transform to the loaded ``Config`` and exits without
running a single backtest. Because each config section validates in ``__post_init__``, this catches
a variant that has drifted out of the allowed range in about a second, instead of crashing partway
through a multi-minute sweep. The same check runs automatically before every real sweep.

Exit codes: ``0`` success, ``1`` at least one backtest failed, ``2`` variant validation failed
(nothing was run).

The variant set is fixed and small on purpose. See ``docs/indicator-research.md`` for the rationale
behind each variant and for why the best-scoring row must not be adopted as the shipping config.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_PATH = REPO_ROOT / "docs" / "ablation-results.md"

#: Number of calendar years covered by ``--quick``. Enough for indicator warm-up plus a 3-year
#: in-sample window and a couple of out-of-sample folds -- a smoke test, not a result.
QUICK_YEARS = 6

#: How the ``time_stop_off`` variant disables the time stop. ``0`` cannot be used for two
#: independent reasons: ``StrategyCfg`` validates ``time_stop_days >= 1``, and the engine's
#: ``hold_days >= time_stop_days`` test would read 0 as "exit on the entry bar" rather than
#: "never exit". A horizon far longer than any backtest window is the unambiguous encoding.
TIME_STOP_OFF_SENTINEL = 10_000

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


def ablation_prefix() -> str:
    """The label prefix that keeps a run out of ``reports/backtest/latest.json``.

    Read from :mod:`swing.backtest.gate` rather than restated here, so the guard this script
    relies on and the guard the runner enforces can never drift apart.
    """
    _ensure_src_on_path()
    from swing.backtest.gate import ABLATION_PREFIX

    return str(ABLATION_PREFIX)


def _fmt_setting(value: Any) -> str:
    """Format one config value for the "Change" column."""
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "True" if value else "False"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# --------------------------------------------------------------------------------------------
# variants
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One ablation: a name, a run label, and the config fields it overrides.

    The transform and its human description come from the same ``changes`` tuple, so the table
    can never claim a knob the sweep did not actually move (audit DEBT-009).
    """

    name: str
    label: str
    #: Config section the changes apply to (``"strategy"``, ``"regime"``); ``""`` for baseline.
    section: str = ""
    #: ``(field name, new value)`` pairs, applied together to :attr:`section`.
    changes: tuple[tuple[str, Any], ...] = ()
    #: Extra clause appended to the description — or the whole of it, for the baseline.
    note: str = ""

    def apply(self, cfg: Any) -> Any:
        """Return ``cfg`` with this variant's fields replaced (baseline returns it unchanged)."""
        if not self.changes:
            return cfg
        section = dataclasses.replace(getattr(cfg, self.section), **dict(self.changes))
        return dataclasses.replace(cfg, **{self.section: section})

    def describe(self, base_cfg: Any) -> str:
        """Render the "Change" cell against ``base_cfg``'s *actual* values.

        The old-value half is read with ``getattr`` at table-build time rather than hardcoded,
        so a sweep run against a customised ``config.toml`` documents that config's baseline
        instead of the shipping defaults this file happened to be written against (DEBT-009).
        """
        if not self.changes:
            return self.note or "none"
        section = getattr(base_cfg, self.section)
        keys = "/".join(f"`{self.section}.{name}`" for name, _ in self.changes)
        before = "/".join(_fmt_setting(getattr(section, name)) for name, _ in self.changes)
        after = "/".join(_fmt_setting(value) for _, value in self.changes)
        described = f"{keys} {before} -> {after}"
        return f"{described} ({self.note})" if self.note else described


def build_variants() -> tuple[Variant, ...]:
    """The fixed ablation set. One change per variant, relative to the loaded config."""
    return (
        Variant(
            name=BASELINE_NAME,
            label="ablate-baseline",
            note="none (the loaded config, unmodified)",
        ),
        Variant(
            name="regime_off",
            label="ablate-regime-off",
            section="regime",
            changes=(("enabled", False),),
        ),
        Variant(
            name="adx_off",
            label="ablate-adx-off",
            section="strategy",
            changes=(("adx_min", 0.0),),
            note="0 disables the filter",
        ),
        Variant(
            name="volume_off",
            label="ablate-volume-off",
            section="strategy",
            changes=(("volume_mult", 1.0),),
        ),
        Variant(
            name="skip_off",
            label="ablate-skip-off",
            section="strategy",
            changes=(("mom_skip_days", 0),),
        ),
        Variant(
            name="weights_equal",
            label="ablate-weights-equal",
            section="strategy",
            changes=(("mom_weight_126", 0.5), ("mom_weight_63", 0.5)),
        ),
        Variant(
            name="chandelier_2",
            label="ablate-chandelier-2",
            section="strategy",
            changes=(("chandelier_mult", 2.0),),
        ),
        Variant(
            name="chandelier_4",
            label="ablate-chandelier-4",
            section="strategy",
            changes=(("chandelier_mult", 4.0),),
        ),
        Variant(
            name="time_stop_off",
            label="ablate-time-stop-off",
            section="strategy",
            changes=(("time_stop_days", TIME_STOP_OFF_SENTINEL),),
            note="time stop off; sentinel horizon, never reached",
        ),
        Variant(
            name="rsi2_on",
            label="ablate-rsi2-on",
            section="strategy",
            changes=(("rsi2_enabled", True),),
        ),
    )


# --------------------------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------------------------


def validate_variants(base_cfg: Any, variants: Sequence[Variant]) -> list[tuple[str, str]]:
    """Check every variant up front, without running any backtest.

    Two things are checked:

    * **The transform.** ``Config`` and its sections validate in ``__post_init__``, so
      ``dataclasses.replace`` raises immediately on an out-of-range value. Exercising all
      transforms before the sweep turns a validation drift into a one-second failure instead of
      a crash ten minutes into compute.
    * **The label.** Every run label must start with the runner's ablation prefix, because that
      prefix is the only thing keeping a deliberately crippled variant out of
      ``reports/backtest/latest.json`` and therefore out of the trading gate. A variant added
      without it would silently poison the gate, so it fails the sweep before anything runs
      (audit DEBT-005). ``run_backtest`` separately validates the label as a directory name.

    Returns ``(variant_name, message)`` for every variant that failed; empty means all are valid.
    Every variant is attempted, so a single run reports *all* problems rather than just the first.
    """
    prefix = ablation_prefix()
    failures: list[tuple[str, str]] = []
    for variant in variants:
        if not variant.label.startswith(prefix):
            failures.append(
                (
                    variant.name,
                    f"label {variant.label!r} does not start with {prefix!r}, so this run would "
                    f"overwrite reports/backtest/latest.json and hand the trading gate an "
                    f"ablation result",
                )
            )
        try:
            variant.apply(base_cfg)
        except Exception as exc:
            failures.append((variant.name, f"{type(exc).__name__}: {exc}"))
    return failures


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
    #: The loaded ``Config`` every variant was derived from. The "Change" column reads its
    #: before-values from this rather than from hardcoded literals (audit DEBT-009).
    base_cfg: Any
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
        change = outcome.variant.describe(ctx.base_cfg)
        main_rows.append([f"`{outcome.variant.name}`", change, *cells])

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
        "- The **Change** column shows each knob's value in the config this sweep loaded, "
        "not an assumed default.",
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
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="check that every variant produces a valid Config, then exit without backtesting",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)

    cfg = load_base_config()
    today = date.today()
    start, end = resolve_window(cfg, quick=args.quick, today=today)

    variants = build_variants()

    # Pre-flight: never burn a full sweep to discover a variant was invalid all along.
    invalid = validate_variants(cfg, variants)
    if invalid:
        print(
            f"ablations: variant validation FAILED ({len(invalid)} of {len(variants)}); "
            "no backtests were run",
            file=sys.stderr,
        )
        for name, message in invalid:
            print(f"  - {name}: {message}", file=sys.stderr)
        return 2
    print(f"ablations: validated {len(variants)} variants", file=sys.stderr, flush=True)

    if args.validate_only:
        return 0

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
        base_cfg=cfg,
        failures=[o.variant.name for o in outcomes if o.error is not None],
    )

    document = render_markdown(outcomes, ctx)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(document, encoding="utf-8")
    print(document)
    print(f"\nwrote {RESULTS_PATH}", file=sys.stderr)
    print(
        f"ablations: every run label started with {ablation_prefix()!r}, so "
        "reports/backtest/latest.json was left untouched and `swing scan` still gates on your "
        "last real `swing backtest`.",
        file=sys.stderr,
    )

    return 1 if ctx.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
