"""Performance metrics.

Conventions worth stating, because half of all backtest disagreements are
really convention disagreements:

* Returns are computed from the **daily equity curve**, not from trade P&L, so
  that idle cash correctly drags on the numbers. A strategy that is 20% exposed
  does not get to report the Sharpe of a fully invested one.
* Annualisation uses 252 trading days.
* Sharpe uses a configurable risk-free rate, defaulting to zero. With a
  positive cash rate a low-exposure strategy looks better on a rate-adjusted
  basis; leaving it at zero is the conservative choice.
* Max drawdown is on close-to-close equity, expressed as a positive fraction.
* Profit factor is gross wins / gross losses. With no losing trades it is
  reported as infinity rather than being quietly clipped, because that is a
  sample-size warning, not a triumph.
* The benchmark comparison is **buy-and-hold on the regime benchmark over the
  exact window the equity curve covers**, scaled to the same starting capital.
  A strategy that trails buy-and-hold on its own benchmark has not earned the
  complexity it costs, and the report says so on the headline rather than
  leaving the reader to do the arithmetic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass
class Metrics:
    start: str
    end: str
    years: float
    initial_equity: float
    final_equity: float
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_drawdown_days: int
    calmar: float
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_r: float
    avg_win: float
    avg_loss: float
    avg_win_pct: float
    avg_loss_pct: float
    largest_win: float
    largest_loss: float
    avg_hold_days: float
    avg_exposure: float
    time_in_market: float
    trades_per_year: float

    # Buy-and-hold on the benchmark over the same window. Optional, because a
    # report may be built without one (no benchmark bars, ablation runs); the
    # summary and the JSON both have to survive that cleanly.
    benchmark_return: float | None = None
    benchmark_cagr: float | None = None
    benchmark_max_drawdown: float | None = None
    excess_cagr: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def summary_lines(self) -> list[str]:
        lines = [
            f"period            {self.start} -> {self.end}  ({self.years:.2f}y)",
            f"equity            {self.initial_equity:,.0f} -> {self.final_equity:,.0f}"
            f"  ({self.total_return:+.1%})",
            f"CAGR              {self.cagr:.2%}",
            f"volatility        {self.volatility:.2%}",
            f"Sharpe / Sortino  {self.sharpe:.2f} / {self.sortino:.2f}",
            f"max drawdown      {self.max_drawdown:.2%}  ({self.max_drawdown_days} days)",
            f"Calmar            {self.calmar:.2f}",
            f"trades            {self.n_trades}  ({self.trades_per_year:.1f}/yr)",
            f"win rate          {self.win_rate:.1%}",
            f"profit factor     {self.profit_factor:.2f}",
            f"expectancy        {self.expectancy_r:+.3f} R per trade",
            f"avg win / loss    {self.avg_win_pct:+.2%} / {self.avg_loss_pct:+.2%}",
            f"avg hold          {self.avg_hold_days:.1f} trading days",
            f"exposure          {self.avg_exposure:.1%} of capital, "
            f"{self.time_in_market:.1%} of days with a position",
        ]
        if (
            self.benchmark_return is not None
            and self.benchmark_cagr is not None
            and self.benchmark_max_drawdown is not None
        ):
            lines.append(
                f"benchmark B&H     {self.benchmark_return:+.1%} total, "
                f"{self.benchmark_cagr:.2%} CAGR, "
                f"{self.benchmark_max_drawdown:.2%} max drawdown"
            )
        if self.excess_cagr is not None:
            lines.append(
                f"excess CAGR       {self.excess_cagr:+.2%} vs buy-and-hold"
            )
        return lines


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    exposure: pd.Series | None = None,
    open_positions: pd.Series | None = None,
    risk_free_rate: float = 0.0,
) -> Metrics:
    equity = equity.dropna()
    if len(equity) < 2:
        return _empty_metrics(equity)

    returns = equity.pct_change().dropna()
    n_days = len(equity)
    years = n_days / TRADING_DAYS
    initial, final = float(equity.iloc[0]), float(equity.iloc[-1])
    total_return = final / initial - 1.0 if initial > 0 else 0.0

    cagr = annualised_return(initial, final, n_days)
    vol = float(returns.std(ddof=1)) * np.sqrt(TRADING_DAYS) if len(returns) > 1 else 0.0

    daily_rf = risk_free_rate / TRADING_DAYS
    excess = returns - daily_rf
    sharpe = (
        float(excess.mean() / excess.std(ddof=1)) * np.sqrt(TRADING_DAYS)
        if len(excess) > 1 and excess.std(ddof=1) > 0
        else 0.0
    )
    downside = excess[excess < 0]
    sortino = (
        float(excess.mean() / downside.std(ddof=1)) * np.sqrt(TRADING_DAYS)
        if len(downside) > 1 and downside.std(ddof=1) > 0
        else 0.0
    )

    max_dd, dd_days = drawdown_stats(equity)
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    stats = trade_stats(trades)

    avg_exposure = float(exposure.mean()) if exposure is not None and len(exposure) else 0.0
    time_in_market = (
        float((open_positions > 0).mean())
        if open_positions is not None and len(open_positions)
        else 0.0
    )

    return Metrics(
        start=str(equity.index[0].date()),
        end=str(equity.index[-1].date()),
        years=years,
        initial_equity=initial,
        final_equity=final,
        total_return=total_return,
        cagr=cagr,
        volatility=vol,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        max_drawdown_days=dd_days,
        calmar=calmar,
        trades_per_year=(stats["n_trades"] / years) if years > 0 else 0.0,
        avg_exposure=avg_exposure,
        time_in_market=time_in_market,
        **{k: v for k, v in stats.items() if k != "n_trades"},
        n_trades=stats["n_trades"],
    )


def annualised_return(initial: float, final: float, n_days: int) -> float:
    """CAGR under the house convention: ``n_days`` equity points, 252 per year.

    Factored out so the bootstrap and the benchmark comparison annualise
    exactly the way :func:`compute_metrics` does, rather than approximately.
    """
    years = n_days / TRADING_DAYS
    if initial <= 0 or years <= 0:
        return 0.0
    if final <= 0:                          # wiped out: -100%, not "no return"
        return -1.0
    return (final / initial) ** (1 / years) - 1.0


def benchmark_equity(
    bench_bars: pd.DataFrame, index: pd.DatetimeIndex, initial: float
) -> pd.Series:
    """Buy-and-hold on the benchmark's close, aligned to a strategy equity index.

    The benchmark is reindexed onto ``index`` and forward-filled, so a holiday
    the benchmark did not trade through does not punch a hole in the
    comparison, and then scaled so it starts at ``initial``. Both curves
    therefore start from the same capital on the same day, which is the only
    way "did this beat buy-and-hold?" has an answer.

    Returns an **empty series** when the benchmark has no usable bars at or
    before the window (the caller is expected to say so in the report rather
    than silently omit the comparison).
    """
    empty = pd.Series(dtype="float64", name="benchmark")
    if bench_bars is None or not len(bench_bars) or index is None or not len(index):
        return empty
    if "close" not in getattr(bench_bars, "columns", []):
        return empty

    close = pd.Series(bench_bars["close"]).astype(float).dropna()
    close = close[~close.index.duplicated(keep="last")].sort_index()
    if not len(close):
        return empty

    aligned = close.reindex(close.index.union(index)).ffill().reindex(index)
    aligned = aligned.dropna()          # bars before the benchmark's first day
    if not len(aligned):
        return empty

    first = float(aligned.iloc[0])
    if first <= 0:
        return empty
    return (aligned * (initial / first)).rename("benchmark")


def apply_benchmark(metrics: Metrics, bench_equity: pd.Series | None) -> Metrics:
    """Populate the benchmark fields on ``metrics`` in place, and return it.

    A benchmark curve with fewer than two points leaves every field ``None``,
    which is how the report knows to omit the comparison instead of printing a
    zero that reads like "buy-and-hold went nowhere".
    """
    if bench_equity is None or len(bench_equity) < 2:
        return metrics
    initial = float(bench_equity.iloc[0])
    final = float(bench_equity.iloc[-1])
    if initial <= 0:
        return metrics

    metrics.benchmark_return = final / initial - 1.0
    metrics.benchmark_cagr = annualised_return(initial, final, len(bench_equity))
    metrics.benchmark_max_drawdown = drawdown_stats(bench_equity)[0]
    metrics.excess_cagr = metrics.cagr - metrics.benchmark_cagr
    return metrics


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Fractional drawdown from the running peak (0 at a new high)."""
    peak = equity.cummax()
    return equity / peak - 1.0


def max_drawdown(equity) -> float:
    """Deepest peak-to-trough fall, as a positive fraction. Array in, float out.

    The same number :func:`drawdown_stats` reports, without the
    underwater-streak loop and without building a ``pd.Series`` — the bootstrap
    calls this once per resample (1,000 curves of ~3,400 points) and never looks
    at the streak.

    NaNs are **skipped**, exactly as pandas' ``cummax``/``min`` skip them: a gap
    in a curve is a day without a mark, not a day the strategy fell to nothing.
    ``np.maximum.accumulate`` would instead poison every point after the gap and
    report a confident 0.0 — the one answer a drawdown must never be wrong
    about — so the running peak uses ``np.fmax`` and the minimum ignores NaN.
    """
    values = np.asarray(equity, dtype="float64")
    if len(values) < 2:
        return 0.0
    peak = np.fmax.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = values / peak - 1.0
    observed = dd[~np.isnan(dd)]
    if not len(observed):
        return 0.0
    return max(0.0, float(-observed.min()))


def drawdown_stats(equity: pd.Series) -> tuple[float, int]:
    """(max drawdown as a positive fraction, longest days spent below a peak)."""
    if len(equity) < 2:
        return 0.0, 0
    dd = drawdown_series(equity)
    max_dd = max(0.0, float(-dd.min()))

    underwater = dd < -1e-12
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return max_dd, int(longest)


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades is None or not len(trades):
        return {
            "n_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "expectancy_r": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "largest_win": 0.0, "largest_loss": 0.0, "avg_hold_days": 0.0,
        }
    pnl = trades["pnl"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    r = trades["r_multiple"].replace([np.inf, -np.inf], np.nan).dropna()
    return {
        "n_trades": int(len(trades)),
        "win_rate": float((pnl > 0).mean()),
        "profit_factor": profit_factor,
        "expectancy_r": float(r.mean()) if len(r) else 0.0,
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_win_pct": float(trades.loc[pnl > 0, "return_pct"].mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(trades.loc[pnl < 0, "return_pct"].mean()) if len(losses) else 0.0,
        "largest_win": float(pnl.max()),
        "largest_loss": float(pnl.min()),
        "avg_hold_days": float(trades["hold_days"].mean()),
    }


def yearly_table(equity: pd.Series, trades: pd.DataFrame) -> pd.DataFrame:
    """Per-calendar-year return, drawdown and trade count."""
    if not len(equity):
        return pd.DataFrame()
    rows = []
    for year, chunk in equity.groupby(equity.index.year):
        if len(chunk) < 2:
            continue
        start_value = float(chunk.iloc[0])
        ret = float(chunk.iloc[-1]) / start_value - 1.0 if start_value else 0.0
        max_dd, _ = drawdown_stats(chunk)
        n = 0
        if trades is not None and len(trades):
            n = int((pd.to_datetime(trades["entry_date"]).dt.year == year).sum())
        rows.append({"year": year, "return": ret, "max_drawdown": max_dd, "trades": n})
    return pd.DataFrame(rows).set_index("year")


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    """Year x month table of returns, for the heat map in the report."""
    if not len(equity):
        return pd.DataFrame()
    monthly = equity.resample("ME").last().pct_change().dropna()
    if not len(monthly):
        return pd.DataFrame()
    frame = pd.DataFrame(
        {"year": monthly.index.year, "month": monthly.index.month, "ret": monthly.to_numpy()}
    )
    return frame.pivot(index="year", columns="month", values="ret")


def exit_reason_table(trades: pd.DataFrame) -> pd.DataFrame:
    """How trades ended, and how each ending performed. Very diagnostic:
    a strategy whose trailing stops never fire is not actually trend-following."""
    if trades is None or not len(trades):
        return pd.DataFrame()
    grouped = trades.groupby("exit_reason").agg(
        trades=("pnl", "size"),
        total_pnl=("pnl", "sum"),
        avg_r=("r_multiple", "mean"),
        win_rate=("pnl", lambda x: float((x > 0).mean())),
        avg_hold=("hold_days", "mean"),
    )
    return grouped.sort_values("trades", ascending=False)


def _empty_metrics(equity: pd.Series) -> Metrics:
    value = float(equity.iloc[0]) if len(equity) else 0.0
    return Metrics(
        start=str(equity.index[0].date()) if len(equity) else "",
        end=str(equity.index[-1].date()) if len(equity) else "",
        years=0.0, initial_equity=value, final_equity=value, total_return=0.0,
        cagr=0.0, volatility=0.0, sharpe=0.0, sortino=0.0, max_drawdown=0.0,
        max_drawdown_days=0, calmar=0.0, n_trades=0, win_rate=0.0,
        profit_factor=0.0, expectancy_r=0.0, avg_win=0.0, avg_loss=0.0,
        avg_win_pct=0.0, avg_loss_pct=0.0, largest_win=0.0, largest_loss=0.0,
        avg_hold_days=0.0, avg_exposure=0.0, time_in_market=0.0, trades_per_year=0.0,
    )
