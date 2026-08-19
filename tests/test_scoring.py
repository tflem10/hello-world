"""AC7 — momentum scoring and candidate ranking, against hand-computed answers.

Every score in this module is worked out on paper in the test that asserts it.
That is possible because of one fixture trick: if the high and low are held
constant and far enough from the close, then ``high - low`` dominates both gap
terms of the true range on every bar, so the true range is a constant and ATR
is exactly that constant. With ATR pinned, the score reduces to arithmetic over
four closes, and the expected value can be written as a literal.

With ``high = 200`` and ``low = 60`` the true range is 140 on every bar for any
close between 60 and 200, which is the band every fixture here stays inside.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing.indicators import atr
from swing.strategy.scoring import (
    MIN_HISTORY_ROWS,
    RANKING_COLUMNS,
    momentum_score,
    rank_candidates,
)

BAND_HIGH = 200.0
BAND_LOW = 60.0
BAND_ATR = BAND_HIGH - BAND_LOW  # 140.0, exactly, on every bar


def band_bars(close: object, start: str = "2018-01-02") -> pd.DataFrame:
    """Constant-band Contract 3 bars: ATR is exactly 140 once warmed up."""
    close_values = np.asarray(close, dtype=float)
    n = close_values.size
    return pd.DataFrame(
        {
            "open": close_values.copy(),
            "high": np.full(n, BAND_HIGH),
            "low": np.full(n, BAND_LOW),
            "close": close_values,
            "volume": np.full(n, 1_000_000.0),
        },
        index=pd.bdate_range(start=start, periods=n),
    )


def step_close(n: int, step_at: int, before: float = 100.0, after: float = 120.0) -> np.ndarray:
    """A single step in price: ``before`` up to ``step_at``, ``after`` from then on."""
    close = np.full(n, before)
    close[step_at:] = after
    return close


# ---------------------------------------------------------------------------
# momentum_score
# ---------------------------------------------------------------------------


def test_momentum_score_matches_a_hand_computed_value(cfg_factory) -> None:
    """Price steps 100 -> 120 on bar 101; read the score on the last bar, 199.

    The 126-day leg skips 5 days, so it compares bar 194 ($120) with bar 68
    ($100): +20%. The 63-day leg compares bar 199 ($120) with bar 136 ($120): 0%.
    ATR% is 140/120*100 = 116.6667.

        score = 0.6 * (20 / 116.6667) + 0.4 * (0 / 116.6667) = 0.10285714
    """
    cfg = cfg_factory()
    bars = band_bars(step_close(200, 101))

    score = momentum_score(bars, cfg)

    assert atr(bars, 14).iloc[-1] == pytest.approx(BAND_ATR)
    assert score.iloc[-1] == pytest.approx(0.10285714285714286)


def test_momentum_score_skips_the_most_recent_days_on_the_126_day_leg(cfg_factory) -> None:
    """The last five bars jump to $150. The 63-day leg sees it; the 126-day leg must not.

    126-day leg: bar 194 ($120) over bar 68 ($100) = +20%, unchanged by the jump.
    63-day leg:  bar 199 ($150) over bar 136 ($120) = +25%.
    ATR% = 140/150*100 = 93.3333.

        score = 0.6 * (20 / 93.3333) + 0.4 * (25 / 93.3333)
              = 0.12857143 + 0.10714286 = 0.23571429
    """
    cfg = cfg_factory()
    close = step_close(200, 101)
    close[195:] = 150.0
    bars = band_bars(close)

    assert momentum_score(bars, cfg).iloc[-1] == pytest.approx(0.2357142857142857)


def test_momentum_score_honours_the_skip_setting(cfg_factory) -> None:
    """With the skip switched off the 126-day leg sees the jump: bar 199 over bar 73.

    126-day leg: $150 over $100 = +50%; 63-day leg unchanged at +25%.

        score = 0.6 * (50 / 93.3333) + 0.4 * (25 / 93.3333) = 0.42857143
    """
    cfg = cfg_factory(strategy={"mom_skip_days": 0})
    close = step_close(200, 101)
    close[195:] = 150.0

    assert momentum_score(band_bars(close), cfg).iloc[-1] == pytest.approx(0.4285714285714286)


@pytest.mark.parametrize(
    ("weight_126", "weight_63", "expected"),
    [
        (1.0, 0.0, 0.21428571428571427),  # 20 / 93.3333
        (0.0, 1.0, 0.26785714285714285),  # 25 / 93.3333
        (0.5, 0.5, 0.24107142857142855),  # the average of the two legs
    ],
)
def test_momentum_score_honours_the_blend_weights(
    cfg_factory, weight_126: float, weight_63: float, expected: float
) -> None:
    cfg = cfg_factory(strategy={"mom_weight_126": weight_126, "mom_weight_63": weight_63})
    close = step_close(200, 101)
    close[195:] = 150.0

    assert momentum_score(band_bars(close), cfg).iloc[-1] == pytest.approx(expected)


def test_momentum_score_is_negative_for_a_decline(cfg_factory) -> None:
    """A falling name must score below a flat one, which must score zero."""
    cfg = cfg_factory()
    falling = momentum_score(band_bars(step_close(200, 101, before=150.0, after=100.0)), cfg)
    flat = momentum_score(band_bars(np.full(200, 120.0)), cfg)

    assert falling.iloc[-1] < 0.0
    assert flat.iloc[-1] == pytest.approx(0.0)


def test_momentum_score_warms_up_after_skip_plus_126_bars(cfg_factory) -> None:
    """mom_skip_days (5) + 126 bars of history are needed before a score exists."""
    cfg = cfg_factory()
    score = momentum_score(band_bars(step_close(200, 101)), cfg)

    assert score.iloc[:131].isna().all()
    assert score.iloc[131:].notna().all()


def test_momentum_score_is_volatility_adjusted(cfg_factory) -> None:
    """Identical price paths, different ATR: the noisier name must score lower.

    Both names run 100 -> 120 on the same bar. The only difference is the band
    width, i.e. the ATR — which is exactly what the score divides by.
    """
    cfg = cfg_factory()
    close = step_close(200, 101)
    calm = band_bars(close).assign(high=160.0, low=80.0)  # true range 80
    wild = band_bars(close)  # true range 140

    assert atr(calm, 14).iloc[-1] == pytest.approx(80.0)
    assert momentum_score(calm, cfg).iloc[-1] > momentum_score(wild, cfg).iloc[-1]


def test_momentum_score_is_aligned_and_typed(cfg_factory) -> None:
    bars = band_bars(step_close(200, 101))
    score = momentum_score(bars, cfg_factory())

    assert score.dtype == np.float64
    pd.testing.assert_index_equal(score.index, bars.index)


# ---------------------------------------------------------------------------
# rank_candidates
# ---------------------------------------------------------------------------

# Two 300-bar price paths, both stepping up on bar 200 and both ending at a new
# 52-week high. Scores read on bar 299:
#   MID  : bar 294 ($120) over bar 168 ($100) = +20%, ATR% = 116.6667 -> 0.10285714
#   HIGH : bar 294 ($150) over bar 168 ($100) = +50%, ATR% =  93.3333 -> 0.32142857
MID_SCORE = 0.10285714285714286
HIGH_SCORE = 0.32142857142857145


def mid_path() -> np.ndarray:
    return step_close(300, 200, after=120.0)


def high_path() -> np.ndarray:
    return step_close(300, 200, after=150.0)


def spiked(close: np.ndarray, at: int = 60, price: float = 195.0) -> np.ndarray:
    """Raise one early bar above every later close, denting high_prox only.

    Bar 60 sits inside the 252-bar high window read on bar 299 but is not one of
    the four bars the score reads (299, 294, 236, 168), and the constant band
    keeps ATR at 140 — so the score is untouched and only high_prox moves.
    """
    close = close.copy()
    close[at] = price
    return close


def test_rank_candidates_orders_by_score_then_high_prox_then_symbol(cfg_factory) -> None:
    """One table exercising the whole sort: score first, then high_prox, then symbol.

    BBB and CCC are identical (equal score, equal high_prox) so the symbol
    breaks the tie. ZZZ and AAA share a score but AAA carries an early $195
    spike, so its high_prox is 120/195 = 0.6154 and it ranks last — proving
    high_prox outranks the alphabet.
    """
    cfg = cfg_factory()
    bars_by_symbol = {
        "AAA": band_bars(spiked(mid_path())),
        "BBB": band_bars(high_path()),
        "CCC": band_bars(high_path()),
        "ZZZ": band_bars(mid_path()),
    }
    asof = bars_by_symbol["AAA"].index[299]

    table = rank_candidates(bars_by_symbol, asof, cfg)

    assert list(table.index) == ["BBB", "CCC", "ZZZ", "AAA"]
    assert table["rank"].tolist() == [1, 2, 3, 4]
    assert table.loc["BBB", "score"] == pytest.approx(HIGH_SCORE)
    assert table.loc["CCC", "score"] == pytest.approx(HIGH_SCORE)
    assert table.loc["ZZZ", "score"] == pytest.approx(MID_SCORE)
    assert table.loc["AAA", "score"] == pytest.approx(MID_SCORE)
    assert table.loc["ZZZ", "high_prox"] == pytest.approx(1.0)
    assert table.loc["AAA", "high_prox"] == pytest.approx(120.0 / 195.0)


def test_rank_candidates_reports_the_contract_columns(cfg_factory) -> None:
    cfg = cfg_factory()
    table = rank_candidates({"MID": band_bars(mid_path())}, band_bars(mid_path()).index[299], cfg)

    assert list(table.columns) == list(RANKING_COLUMNS)
    assert table.index.name == "symbol"
    assert table.loc["MID", "close"] == pytest.approx(120.0)
    assert table.loc["MID", "atr"] == pytest.approx(BAND_ATR)
    assert table.loc["MID", "high_prox"] == pytest.approx(1.0)
    assert table["rank"].dtype == np.int64
    for column in ("score", "atr", "close", "high_prox"):
        assert table[column].dtype == np.float64


def test_rank_candidates_requires_260_bars_of_history(cfg_factory) -> None:
    """259 bars is not enough; 260 is."""
    cfg = cfg_factory()
    long_enough = band_bars(step_close(MIN_HISTORY_ROWS, 160))
    too_short = band_bars(step_close(MIN_HISTORY_ROWS - 1, 160))

    table = rank_candidates({"OK": long_enough, "SHORT": too_short}, long_enough.index[-1], cfg)

    assert list(table.index) == ["OK"]
    assert table.loc["OK", "score"] == pytest.approx(MID_SCORE)


def test_rank_candidates_counts_history_up_to_asof_only(cfg_factory) -> None:
    """A 300-bar frame has 260 bars at bar 259 and 259 at bar 258 — the cut-off is exact."""
    cfg = cfg_factory()
    bars = band_bars(step_close(300, 160))

    assert list(rank_candidates({"SYM": bars}, bars.index[259], cfg).index) == ["SYM"]
    assert rank_candidates({"SYM": bars}, bars.index[258], cfg).empty


def test_rank_candidates_ignores_bars_after_asof(cfg_factory) -> None:
    """Truncating the frame at asof and letting the function truncate must agree.

    The tail here collapses to $60, which would wreck the score if it leaked in.
    """
    cfg = cfg_factory()
    close = np.concatenate([mid_path(), np.full(100, 60.0)])
    with_future = band_bars(close)
    without_future = band_bars(mid_path())
    asof = without_future.index[299]

    pd.testing.assert_frame_equal(
        rank_candidates({"SYM": with_future}, asof, cfg),
        rank_candidates({"SYM": without_future}, asof, cfg),
    )


def test_rank_candidates_uses_the_last_bar_at_or_before_asof(cfg_factory) -> None:
    """An asof that falls between two bars resolves back to the earlier one."""
    cfg = cfg_factory()
    bars = band_bars(mid_path())
    on_the_bar = rank_candidates({"SYM": bars}, bars.index[299], cfg)
    mid_gap = rank_candidates({"SYM": bars}, bars.index[299] + pd.Timedelta(hours=12), cfg)

    pd.testing.assert_frame_equal(on_the_bar, mid_gap)


def test_rank_candidates_drops_symbols_with_an_undefined_score(cfg_factory) -> None:
    """A missing last close makes the score NaN, and a NaN score is not rankable."""
    cfg = cfg_factory()
    broken = band_bars(mid_path())
    broken.loc[broken.index[299], "close"] = np.nan

    table = rank_candidates({"GOOD": band_bars(mid_path()), "BAD": broken}, broken.index[299], cfg)

    assert list(table.index) == ["GOOD"]


def test_rank_candidates_returns_an_empty_table_for_no_input(cfg_factory) -> None:
    table = rank_candidates({}, pd.Timestamp("2020-01-02"), cfg_factory())

    assert table.empty
    assert len(table) == 0
    assert list(table.columns) == list(RANKING_COLUMNS)
    assert table.index.name == "symbol"


def test_rank_candidates_returns_an_empty_table_when_nothing_qualifies(cfg_factory) -> None:
    cfg = cfg_factory()
    short = band_bars(np.full(100, 120.0))

    table = rank_candidates({"A": short, "B": short}, short.index[-1], cfg)

    assert table.empty
    assert list(table.columns) == list(RANKING_COLUMNS)


def test_rank_candidates_is_deterministic(cfg_factory) -> None:
    """Same inputs, same table — insertion order of the dict must not matter."""
    cfg = cfg_factory()
    forward = {"AAA": band_bars(spiked(mid_path())), "ZZZ": band_bars(mid_path())}
    reversed_order = {"ZZZ": band_bars(mid_path()), "AAA": band_bars(spiked(mid_path()))}
    asof = forward["AAA"].index[299]

    pd.testing.assert_frame_equal(
        rank_candidates(forward, asof, cfg), rank_candidates(reversed_order, asof, cfg)
    )
    pd.testing.assert_frame_equal(
        rank_candidates(forward, asof, cfg), rank_candidates(forward, asof, cfg)
    )
