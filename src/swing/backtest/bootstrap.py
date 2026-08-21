"""Block-bootstrap confidence intervals for a walk-forward equity curve.

Why this exists
---------------
``docs/indicator-research.md`` §14 documents two findings that a point estimate
cannot express: the out-of-sample sample is small (a handful of one-year
blocks), and grid selection is fragile enough that a 1-in-10^13 change in an
indicator reshuffles the chosen parameters. A report that answers "CAGR 11.4%"
and stops invites the reader to believe the fourth significant figure. This
module answers "CAGR 11.4%, and a plausible range for it is −3% to +24%",
which is the same information honestly stated.

Method
------
A **circular block bootstrap** over the daily returns of the equity curve.
Blocks of ``block_days`` consecutive returns are drawn with replacement,
wrapping around the end of the series, until the original length is refilled;
the blocks preserve short-horizon autocorrelation and volatility clustering
that an i.i.d. bootstrap would destroy. Each resampled return path is
compounded back into an equity curve, and CAGR and max drawdown are computed
with exactly the conventions in :mod:`swing.backtest.metrics`. Percentiles are
taken across resamples.

The honest limitation
---------------------
**These intervals UNDERSTATE the true uncertainty, and they are a floor on the
error bars rather than an estimate of them.** Resampling one historical path
can only ever reshuffle the regimes that path happened to contain: the 2008
that did not occur inside the sample cannot be drawn, and neither can a decade
in which the strategy's edge simply is not there. On top of that, the returns
being resampled come from a parameter set that was *chosen* — the selection
happened before this module ran and no resampling of the outcome can undo it.
Read a wide interval as conclusive (the strategy is indistinguishable from
noise) and a narrow one as inconclusive (it is at least not obviously noise,
within the one history observed).

Determinism: ``np.random.default_rng(seed)`` is the only source of randomness,
so two runs of the same report are byte-identical, as everything else here is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .metrics import annualised_return, max_drawdown

# Below this many daily returns the resampled distribution is a description of
# the noise in a handful of days, not of the strategy. Reporting an interval
# there would be worse than reporting none.
MIN_RETURNS = 60


@dataclass
class BootstrapResult:
    """Percentiles of CAGR and max drawdown across resampled equity paths."""

    cagr_p5: float
    cagr_p50: float
    cagr_p95: float
    maxdd_p5: float
    maxdd_p50: float
    maxdd_p95: float
    prob_cagr_le_zero: float
    n_resamples: int
    block_days: int
    n_days: int

    def as_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        return f"bootstrap (n={self.n_resamples}, block={self.block_days}d)"

    def summary_lines(self) -> list[str]:
        return [
            f"CAGR  p5/p50/p95   {self.cagr_p5:.2%} / {self.cagr_p50:.2%} / "
            f"{self.cagr_p95:.2%}",
            f"maxDD p5/p50/p95   {self.maxdd_p5:.2%} / {self.maxdd_p50:.2%} / "
            f"{self.maxdd_p95:.2%}",
            f"P(CAGR <= 0)       {self.prob_cagr_le_zero:.1%}",
        ]

    def to_frame(self) -> pd.DataFrame:
        """Two-row table for the report (percentiles across resamples)."""
        return pd.DataFrame(
            [
                {
                    "p5": round(self.cagr_p5, 4),
                    "p50": round(self.cagr_p50, 4),
                    "p95": round(self.cagr_p95, 4),
                },
                {
                    "p5": round(self.maxdd_p5, 4),
                    "p50": round(self.maxdd_p50, 4),
                    "p95": round(self.maxdd_p95, 4),
                },
            ],
            index=pd.Index(["cagr", "max_drawdown"], name="statistic"),
        )


def bootstrap_equity(
    equity: pd.Series,
    n_resamples: int = 1000,
    block_days: int = 20,
    seed: int = 7,
) -> BootstrapResult | None:
    """Circular block bootstrap over the daily returns of ``equity``.

    Returns ``None`` when there are fewer than :data:`MIN_RETURNS` daily
    returns — the caller is expected to say "sample too small to bootstrap"
    rather than print an interval computed from nothing.
    """
    if equity is None or not len(equity):
        return None
    equity = pd.Series(equity).dropna()
    if len(equity) < 2:
        return None

    returns = equity.pct_change().dropna().to_numpy(dtype="float64")
    n = int(len(returns))
    if n < MIN_RETURNS:
        return None

    n_resamples = max(1, int(n_resamples))
    block = int(block_days)
    block = max(1, min(block, n))

    rng = np.random.default_rng(int(seed))
    n_blocks = int(np.ceil(n / block))

    # Circular: a block starting near the end wraps to the front, so every
    # observation has an equal chance of appearing and no edge is truncated.
    starts = rng.integers(0, n, size=(n_resamples, n_blocks))
    offsets = np.arange(block)
    idx = (starts[:, :, None] + offsets[None, None, :]) % n
    idx = idx.reshape(n_resamples, n_blocks * block)[:, :n]

    initial = float(equity.iloc[0])
    paths = initial * np.cumprod(1.0 + returns[idx], axis=1)

    # Each rebuilt curve has the same number of points as the original: the
    # starting equity plus one point per resampled return.
    n_points = n + 1

    cagrs = np.empty(n_resamples, dtype="float64")
    maxdds = np.empty(n_resamples, dtype="float64")
    path = np.empty(n_points, dtype="float64")
    path[0] = initial
    for i in range(n_resamples):
        path[1:] = paths[i]
        cagrs[i] = annualised_return(initial, float(path[-1]), n_points)
        maxdds[i] = max_drawdown(path)

    c5, c50, c95 = (float(v) for v in np.percentile(cagrs, [5, 50, 95]))
    d5, d50, d95 = (float(v) for v in np.percentile(maxdds, [5, 50, 95]))

    return BootstrapResult(
        cagr_p5=c5,
        cagr_p50=c50,
        cagr_p95=c95,
        maxdd_p5=d5,
        maxdd_p50=d50,
        maxdd_p95=d95,
        prob_cagr_le_zero=float(np.mean(cagrs <= 0.0)),
        n_resamples=n_resamples,
        block_days=block,
        n_days=n_points,
    )

