"""CLI glue for ``swing backtest``.

Loads bars from the cache (never the network), then dispatches to whichever
analysis was asked for and writes a report directory. With no flags it runs the
walk-forward, because that is the number that gates live trading.
"""

from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

from ..config import Config
from ..data.cache import BarCache
from ..data.pipeline import load_bars
from ..data.universe import UNIVERSE_DIR, Symbol, build_universe, read_symbol_file
from ..logging_setup import get_logger
from ..strategy.rules import SymbolMeta, fundamentals_ok
from .engine import run_backtest
from .metrics import apply_benchmark, benchmark_equity, compute_metrics
from .report import bootstrap_extras, build_report, report_dir
from .walkforward import apply_overrides, parameter_sensitivity, run_walk_forward

log = get_logger("swing.backtest.runner")

SENSITIVITY_PARAMS = [
    "strategy.exit.initial_stop_atr",
    "strategy.exit.chandelier_atr",
    "strategy.exit.time_stop_days",
    "strategy.entry.donchian_len",
    "strategy.entry.volume_mult",
    "strategy.trend_template.adx_min",
    "strategy.rank.long_lookback",
]

# Each ablation turns exactly one component off, so the delta is attributable.
ABLATIONS: dict[str, dict] = {
    "baseline": {},
    "no_regime_filter": {"strategy.regime.enabled": False},
    "no_trend_template": {"strategy.trend_template.enabled": False},
    "no_adx_filter": {"strategy.trend_template.adx_min": 0.0},
    "no_volume_confirmation": {"strategy.entry.volume_mult": 0.0},
    "no_atr_normalised_rank": {"strategy.rank.normalize_by_atr": False},
    "rank_long_only": {"strategy.rank.long_weight": 1.0, "strategy.rank.short_weight": 0.0},
    "rank_short_only": {"strategy.rank.long_weight": 0.0, "strategy.rank.short_weight": 1.0},
    "no_momentum_skip": {"strategy.rank.skip_recent_days": 0},
    "no_trailing_stop": {"strategy.exit.chandelier_atr": 99.0},
    "no_time_stop": {"strategy.exit.time_stop_days": 0},
    "rsi2_entry": {"strategy.entry.mode": "rsi2_pullback"},
}


def load_universe_bars(cfg: Config, etf_only: bool = False):
    """Read bars + metadata for the configured universe straight from the cache."""
    if etf_only:
        symbols: list[Symbol] = read_symbol_file(UNIVERSE_DIR / "etfs.csv", source="etfs")
    else:
        symbols = build_universe(cfg)

    bars = load_bars(cfg, symbols, min_bars=260)
    if not bars:
        raise SystemExit(
            "the price cache is empty (or has fewer than 260 bars per symbol).\n"
            "Run `swing data --backfill` first — it needs network access."
        )

    cache = BarCache(cfg.expand_path(cfg.data.cache_dir))
    funds = cache.read_fundamentals()
    is_etf = {s.symbol: s.is_etf for s in symbols}

    meta = {
        sym: SymbolMeta(
            symbol=sym,
            is_etf=is_etf.get(sym, False),
            fundamentals_ok=fundamentals_ok(funds.get(sym), cfg.strategy),
        )
        for sym in bars
    }

    benchmark_symbol = str(cfg.strategy.regime.get("symbol", "SPY")).upper()
    benchmark = bars.get(benchmark_symbol)
    if benchmark is None:
        benchmark = load_bars(cfg, [benchmark_symbol]).get(benchmark_symbol)

    log.info(
        "loaded %d symbols from the cache (%s)",
        len(bars), "ETF universe" if etf_only else "full universe",
    )
    return bars, meta, benchmark


def load_earnings(
    cfg: Config, bars: dict[str, pd.DataFrame] | None = None
) -> tuple[dict | None, dict, list[str]]:
    """Load the optional historical earnings calendar named by ``[data]``.

    Returns ``(calendar, manifest_extra, warnings)``. With no
    ``data.earnings_calendar`` key — the shipped default — this returns
    ``(None, {}, [])`` and every backtest behaves exactly as it did before,
    warning included: the engine says loudly that the blackout was not applied.

    ``bars`` is the universe the calendar will be applied to, and it is passed
    so coverage can be checked. It matters more than it looks: the engine's
    "no historical earnings calendar" warning is all-or-nothing (it fires only
    when the calendar is empty), so a calendar covering three symbols out of a
    thousand silences the warning for the *whole* run and the report then reads
    as though the blackout was applied universe-wide. Partial coverage is
    partial protection and gets its own warning here.

    The key lives under ``[data]`` rather than ``[backtest]`` on purpose.
    ``Config.hash`` covers ``[account][universe][strategy][backtest]``, and a
    new key there would re-lock every user's gate.
    """
    raw = str(cfg.data.get("earnings_calendar", "") or "").strip()
    if not raw:
        return None, {}, []

    path = cfg.expand_path(raw)
    # Imported lazily so the whole backtest package does not depend on a module
    # that only matters when the user actually supplies a calendar.
    from ..data.earnings_calendar import load_earnings_calendar

    try:
        calendar = load_earnings_calendar(path)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"earnings calendar not found: {path}\n"
            "Set [data] earnings_calendar to an existing file, or remove the key "
            "to run without an earnings blackout (the report will say so)."
        ) from exc

    n_symbols = len(calendar)
    n_dates = sum(len(v) for v in calendar.values())
    universe = set(bars or {})
    covered = len(universe & set(calendar))
    log.info(
        "earnings calendar: %d symbols, %d dated events, covering %d of %d "
        "universe symbols (%s)",
        n_symbols, n_dates, covered, len(universe), path,
    )

    warnings: list[str] = []
    if universe and covered < len(universe):
        missing = len(universe) - covered
        warnings.append(
            f"earnings calendar covers {covered} of {len(universe)} universe "
            f"symbols; the other {missing} get NO earnings blackout in this "
            "backtest — partial protection that the missing-calendar warning no "
            "longer flags, because a calendar *was* supplied. Entries in the "
            "uncovered names are taken through earnings here and would be "
            "blocked live, so expect live to take fewer trades than this implies."
        )

    return calendar, {
        "earnings_calendar": str(path),
        "earnings_calendar_symbols": n_symbols,
        "earnings_calendar_dates": n_dates,
        "earnings_calendar_covered": covered,
        "earnings_calendar_universe": len(universe),
    }, warnings


def _benchmark_series(
    cfg: Config, benchmark, equity: pd.Series
) -> tuple[pd.Series | None, list[str]]:
    """Buy-and-hold benchmark equity over exactly the reported window.

    The comparison has to cover the same dates as the curve it is compared
    against — for the walk-forward that is the concatenated out-of-sample
    window, not the full history.
    """
    symbol = str(cfg.strategy.regime.get("symbol", "SPY")).upper()
    if benchmark is None or not len(benchmark) or equity is None or not len(equity):
        return None, [
            f"benchmark {symbol} has no bars in the reported window, so no "
            "buy-and-hold comparison is shown."
        ]
    series = benchmark_equity(benchmark, equity.index, float(equity.iloc[0]))
    if not len(series):
        return None, [
            f"benchmark {symbol} has no bars in the reported window, so no "
            "buy-and-hold comparison is shown."
        ]
    return series, []


def run_backtest_command(args: argparse.Namespace, cfg: Config) -> int:
    start = date.fromisoformat(args.start) if args.start else date.fromisoformat(
        str(cfg.backtest.start)
    )
    end = date.fromisoformat(args.end) if args.end else (
        date.fromisoformat(str(cfg.backtest.end)) if str(cfg.backtest.get("end", "")) else None
    )

    ran_anything = False
    if args.full:
        _run_full(cfg, args, start, end, etf_only=args.etf_only)
        ran_anything = True
    if args.ablations:
        _run_ablations(cfg, args, start, end, etf_only=args.etf_only)
        ran_anything = True
    if args.sensitivity:
        _run_sensitivity(cfg, args, start, end, etf_only=args.etf_only)
        ran_anything = True
    if args.walk_forward or not ran_anything:
        _run_walk_forward(cfg, args, start, end, etf_only=args.etf_only)
    return 0


# ---------------------------------------------------------------------------
def _tag(args: argparse.Namespace, base: str, etf_only: bool) -> str:
    parts = [base]
    if etf_only:
        parts.append("etf")
    if args.tag:
        parts.append(str(args.tag))
    return "-".join(parts)


def _run_walk_forward(cfg, args, start, end, etf_only: bool) -> None:
    bars, meta, benchmark = load_universe_bars(cfg, etf_only=etf_only)
    earnings, earnings_manifest, earnings_warnings = load_earnings(cfg, bars)
    result = run_walk_forward(
        cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
        start=start, end=end,
    )

    bench_equity, bench_warnings = _benchmark_series(cfg, benchmark, result.equity)
    apply_benchmark(result.metrics, bench_equity)
    boot_tables, boot_manifest, boot_warnings = bootstrap_extras(cfg, result.equity)

    warnings = list(result.warnings) + earnings_warnings + bench_warnings + boot_warnings
    if etf_only:
        warnings.append(
            "ETF-only universe: no survivorship bias, and no single-stock upside "
            "either. Read this as the conservative lower bound."
        )
    else:
        warnings.append(
            "Stock universe is a current-membership list, so these results are "
            "survivorship-biased upward. Compare against the ETF-only run."
        )

    report = build_report(
        cfg,
        title=(
            "Walk-forward (out-of-sample) — "
            + ("ETF universe" if etf_only else "full universe")
        ),
        result=result,
        kind="walk_forward",
        benchmark=bench_equity,
        extra_tables={"walk_forward_windows": result.window_table(), **boot_tables},
        manifest_extra={
            **earnings_manifest,
            **boot_manifest,
            "windows": len(result.windows),
            "in_sample_years": int(cfg.backtest.walk_forward.in_sample_years),
            "out_of_sample_years": int(cfg.backtest.walk_forward.out_of_sample_years),
            "etf_only": etf_only,
            "chosen_params": result.chosen_params,
            "universe_size": result.segments[0].universe_size if result.segments else 0,
            "data_hash": result.segments[0].data_hash if result.segments else "",
        },
    )
    report.warnings = warnings
    out = report.write(report_dir(cfg, _tag(args, "walkforward", etf_only)))

    print()
    print("\n".join(result.metrics.summary_lines()))
    print()
    print(result.window_table().to_string(index=False))
    print()
    print(f"report: {out / 'report.html'}")


def _run_full(cfg, args, start, end, etf_only: bool) -> None:
    bars, meta, benchmark = load_universe_bars(cfg, etf_only=etf_only)
    earnings, earnings_manifest, earnings_warnings = load_earnings(cfg, bars)
    result = run_backtest(
        cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
        start=start, end=end, label="full",
    )
    metrics = compute_metrics(
        result.equity, result.trades, result.exposure, result.open_positions
    )
    bench_equity, bench_warnings = _benchmark_series(cfg, benchmark, result.equity)
    apply_benchmark(metrics, bench_equity)
    report = build_report(
        cfg,
        title="Full-period backtest — " + ("ETF universe" if etf_only else "full universe"),
        result=result,
        kind="full_period",
        benchmark=bench_equity,
        manifest_extra={**earnings_manifest, "etf_only": etf_only,
                        "data_hash": result.data_hash,
                        "universe_size": result.universe_size},
    )
    report.metrics = metrics
    report.warnings = list(result.warnings) + earnings_warnings + bench_warnings + [
        "This is an in-sample, full-period run over parameters that were chosen "
        "with knowledge of this whole period. It is reported for context only and "
        "does NOT satisfy the trading gate — only the walk-forward report does."
    ]
    out = report.write(report_dir(cfg, _tag(args, "fullperiod", etf_only)))
    print()
    print("\n".join(metrics.summary_lines()))
    print(f"\nreport: {out / 'report.html'}")


def _run_ablations(cfg, args, start, end, etf_only: bool) -> None:
    """Turn each component off in turn and measure what it was worth.

    This is what docs/indicator-research.md cites when it claims a component
    earns its place. A component that does not survive its own ablation should
    be removed from the defaults, not defended.
    """
    bars, meta, benchmark = load_universe_bars(cfg, etf_only=etf_only)
    earnings, earnings_manifest, earnings_warnings = load_earnings(cfg, bars)
    feature_cache: dict = {}
    rows = []
    baseline_metrics = None

    for name, overrides in ABLATIONS.items():
        trial = apply_overrides(cfg, overrides) if overrides else cfg
        log.info("ablation: %s", name)
        result = run_backtest(
            trial, bars, meta=meta, benchmark=benchmark, earnings=earnings,
            start=start, end=end, label=name, feature_cache=feature_cache,
        )
        m = compute_metrics(result.equity, result.trades, result.exposure,
                            result.open_positions)
        if name == "baseline":
            baseline_metrics = m
        rows.append(
            {
                "variant": name,
                "change": ", ".join(f"{k}={v}" for k, v in overrides.items()) or "-",
                "trades": m.n_trades,
                "cagr": round(m.cagr, 4),
                "sharpe": round(m.sharpe, 3),
                "profit_factor": round(m.profit_factor, 3),
                "max_dd": round(m.max_drawdown, 4),
                "expectancy_r": round(m.expectancy_r, 4),
            }
        )

    table = pd.DataFrame(rows).set_index("variant")
    if baseline_metrics is not None:
        table["cagr_delta"] = (table["cagr"] - round(baseline_metrics.cagr, 4)).round(4)
        table["sharpe_delta"] = (
            table["sharpe"] - round(baseline_metrics.sharpe, 3)
        ).round(3)

    # The report body needs *a* result; use the baseline run for the curves.
    baseline_result = run_backtest(
        cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
        start=start, end=end, label="baseline", feature_cache=feature_cache,
    )
    report = build_report(
        cfg,
        title="Component ablations — " + ("ETF universe" if etf_only else "full universe"),
        result=baseline_result,
        kind="ablation",
        extra_tables={"ablations": table},
        manifest_extra={**earnings_manifest, "etf_only": etf_only,
                        "ablations": table.reset_index().to_dict("records")},
    )
    report.warnings = list(baseline_result.warnings) + earnings_warnings + [
        "Ablations are run over the full period in-sample. They answer 'what did "
        "this component contribute here?', not 'will it contribute next year'. "
        "A component whose removal barely moves the numbers is a candidate for "
        "deletion regardless of what the literature says."
    ]
    out = report.write(report_dir(cfg, _tag(args, "ablations", etf_only)))
    print()
    print(table.to_string())
    print(f"\nreport: {out / 'report.html'}")


def _run_sensitivity(cfg, args, start, end, etf_only: bool) -> None:
    bars, meta, benchmark = load_universe_bars(cfg, etf_only=etf_only)
    earnings, earnings_manifest, earnings_warnings = load_earnings(cfg, bars)
    table = parameter_sensitivity(
        cfg, bars, SENSITIVITY_PARAMS, meta=meta, benchmark=benchmark,
        earnings=earnings, start=start, end=end,
    )
    baseline = run_backtest(
        cfg, bars, meta=meta, benchmark=benchmark, earnings=earnings,
        start=start, end=end, label="baseline",
    )
    report = build_report(
        cfg,
        title="Parameter sensitivity (+/-25%)",
        result=baseline,
        kind="sensitivity",
        extra_tables={"sensitivity": table.set_index("parameter")},
        manifest_extra=dict(earnings_manifest),
    )
    report.warnings = list(baseline.warnings) + earnings_warnings + [
        "Read this table for FLATNESS, not for the best cell. If a parameter's "
        "profit factor collapses when it moves 25%, the strategy is balanced on a "
        "knife edge and the backtest is measuring the edge of the knife."
    ]
    out = report.write(report_dir(cfg, _tag(args, "sensitivity", etf_only)))
    print()
    print(table.to_string(index=False))
    print(f"\nreport: {out / 'report.html'}")
