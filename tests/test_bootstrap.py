"""Block-bootstrap confidence intervals.

The point of these tests is that the intervals are *reproducible* and
*ordered*. Reports in this repo are byte-identical between runs, and a
confidence interval computed from an unseeded RNG would quietly break that —
the gate reads the manifest, and a manifest that changes on every run is a
manifest nobody can diff.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing.backtest.bootstrap import MIN_RETURNS, bootstrap_equity


def _equity(values, start="2015-01-01") -> pd.Series:
    return pd.Series(
        np.asarray(values, dtype="float64"),
        index=pd.bdate_range(start=start, periods=len(values)),
        name="equity",
    )


def _noisy_curve(n: int = 500, drift: float = 0.0005, vol: float = 0.01, seed: int = 4):
    rng = np.random.default_rng(seed)
    return _equity(100.0 * np.exp(np.cumsum(rng.normal(drift, vol, n))))


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_same_seed_gives_identical_numbers():
    eq = _noisy_curve()
    a = bootstrap_equity(eq, n_resamples=200, block_days=20, seed=7)
    b = bootstrap_equity(eq, n_resamples=200, block_days=20, seed=7)
    assert a is not None and b is not None
    assert a.as_dict() == b.as_dict()


def test_the_interval_is_pinned_to_the_values_it_has_always_reported():
    """Pinned before the max-drawdown loop was vectorised, and unchanged by it.

    The manifest these numbers land in is meant to be diffable between runs and
    between commits, so a shift in the fourth decimal is a broken report rather
    than a rounding detail.
    """
    result = bootstrap_equity(_noisy_curve(), n_resamples=200, block_days=20, seed=7)
    assert result.cagr_p5 == pytest.approx(-0.099831921318, abs=1e-12)
    assert result.cagr_p50 == pytest.approx(0.135790687579, abs=1e-12)
    assert result.cagr_p95 == pytest.approx(0.444944145781, abs=1e-12)
    assert result.maxdd_p5 == pytest.approx(0.093988889561, abs=1e-12)
    assert result.maxdd_p50 == pytest.approx(0.181981069425, abs=1e-12)
    assert result.maxdd_p95 == pytest.approx(0.336095140202, abs=1e-12)
    assert result.prob_cagr_le_zero == pytest.approx(0.21)


def test_a_different_seed_gives_different_numbers():
    """If the seed did not matter, the RNG would not be the only randomness."""
    eq = _noisy_curve()
    a = bootstrap_equity(eq, n_resamples=200, block_days=20, seed=7)
    b = bootstrap_equity(eq, n_resamples=200, block_days=20, seed=8)
    assert a.cagr_p50 != b.cagr_p50


# ---------------------------------------------------------------------------
# shape of the result
# ---------------------------------------------------------------------------
def test_percentiles_are_ordered():
    result = bootstrap_equity(_noisy_curve(), n_resamples=200, block_days=20, seed=7)
    assert result.cagr_p5 <= result.cagr_p50 <= result.cagr_p95
    assert result.maxdd_p5 <= result.maxdd_p50 <= result.maxdd_p95


def test_reported_settings_match_the_request():
    eq = _noisy_curve(n=400)
    result = bootstrap_equity(eq, n_resamples=64, block_days=15, seed=3)
    assert result.n_resamples == 64
    assert result.block_days == 15
    # One point per daily return, plus the starting equity.
    assert result.n_days == len(eq)


def test_a_monotonic_rise_can_never_lose_money():
    """Every block of a strictly rising curve is a positive return, so no
    reshuffling of those blocks can produce a losing path."""
    eq = _equity(100.0 * np.exp(np.cumsum(np.full(300, 0.001))))
    result = bootstrap_equity(eq, n_resamples=200, block_days=20, seed=7)
    assert result.prob_cagr_le_zero == 0.0
    assert result.cagr_p5 > 0.0
    assert result.maxdd_p95 == pytest.approx(0.0)


def test_a_monotonic_fall_always_loses_money():
    eq = _equity(100.0 * np.exp(np.cumsum(np.full(300, -0.001))))
    result = bootstrap_equity(eq, n_resamples=200, block_days=20, seed=7)
    assert result.prob_cagr_le_zero == 1.0
    assert result.cagr_p95 < 0.0


def test_block_days_of_one_is_the_iid_degenerate_case():
    """block_days=1 is a plain i.i.d. bootstrap — it must still run and still
    produce ordered percentiles, because a user can configure it."""
    result = bootstrap_equity(_noisy_curve(), n_resamples=200, block_days=1, seed=7)
    assert result is not None
    assert result.block_days == 1
    assert result.cagr_p5 <= result.cagr_p50 <= result.cagr_p95


def test_block_longer_than_the_sample_is_clamped():
    eq = _noisy_curve(n=120)
    result = bootstrap_equity(eq, n_resamples=20, block_days=5_000, seed=7)
    assert result.block_days == len(eq) - 1


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_a_short_sample_returns_none_instead_of_a_fake_interval():
    eq = _noisy_curve(n=MIN_RETURNS)          # MIN_RETURNS - 1 daily returns
    assert bootstrap_equity(eq, n_resamples=50, block_days=10, seed=7) is None


def test_the_shortest_admissible_sample_still_bootstraps():
    eq = _noisy_curve(n=MIN_RETURNS + 1)      # exactly MIN_RETURNS returns
    assert bootstrap_equity(eq, n_resamples=50, block_days=10, seed=7) is not None


@pytest.mark.parametrize("equity", [None, pd.Series(dtype="float64"), pd.Series([1.0])])
def test_degenerate_inputs_return_none(equity):
    assert bootstrap_equity(equity, n_resamples=10, block_days=5, seed=7) is None


# ---------------------------------------------------------------------------
# presentation
# ---------------------------------------------------------------------------
def test_label_and_frame_are_report_ready():
    result = bootstrap_equity(_noisy_curve(), n_resamples=100, block_days=20, seed=7)
    assert result.label() == "bootstrap (n=100, block=20d)"
    frame = result.to_frame()
    assert list(frame.index) == ["cagr", "max_drawdown"]
    assert list(frame.columns) == ["p5", "p50", "p95"]
    assert all(isinstance(line, str) for line in result.summary_lines())
