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
from swing.strategy import scoring
from swing.strategy.scoring import (
    MIN_ATR_PCT,
    MIN_HISTORY_ROWS,
    RANKING_COLUMNS,
    SCORE_ATR_WINDOW,
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


# ---------------------------------------------------------------------------
# the ATR% floor and the finiteness filter (audit BUG-003)
# ---------------------------------------------------------------------------


def frozen_quote_bars(
    n: int = 320, pop_at: int = 200, base: float = 100.0, deal: float = 200.0
) -> pd.DataFrame:
    """An uptrend, a takeover pop, then a quote frozen at the deal price.

    This is the shape the audit reproduced: after ``pop_at`` the high, low and
    close are all the deal price, so every true range is exactly zero and
    Wilder's ATR decays by 13/14 a bar while the trailing 126-day return stays
    large. 119 frozen sessions take ATR from ~3.0 to 0.00094 — 0.00047% of
    price — and the old score climbed to **47,750** against 11.56 for the
    healthy trend below.
    """
    close = base * 1.002 ** np.arange(n, dtype=float)
    close[pop_at:] = deal
    high = close * 1.01
    low = close * 0.99
    high[pop_at:] = deal
    low[pop_at:] = deal
    return pd.DataFrame(
        {
            "open": close.copy(),
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=pd.bdate_range(start="2018-01-02", periods=n),
    )


def healthy_trend_bars(n: int = 320, base: float = 100.0) -> pd.DataFrame:
    """The same uptrend with the band left alive all the way through: ATR ~2% of price."""
    close = base * 1.002 ** np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "open": close.copy(),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=pd.bdate_range(start="2018-01-02", periods=n),
    )


def test_a_frozen_quote_scores_nan_instead_of_a_huge_number(cfg_factory) -> None:
    """The BUG-003 fixture: ATR% under the floor makes the score undefined."""
    cfg = cfg_factory()
    bars = frozen_quote_bars()

    atr_pct = atr(bars, SCORE_ATR_WINDOW).iloc[-1] / bars["close"].iloc[-1] * 100.0
    assert atr_pct < MIN_ATR_PCT, "fixture no longer reaches the floor"

    assert np.isnan(momentum_score(bars, cfg).iloc[-1])


def test_a_frozen_quote_is_excluded_from_the_ranking(cfg_factory) -> None:
    """It used to rank #1 with a score of 47,750 and consume a real position slot."""
    cfg = cfg_factory()
    bars_by_symbol = {"HEALTHY": healthy_trend_bars(), "PINNED": frozen_quote_bars()}
    asof = bars_by_symbol["PINNED"].index[-1]

    table = rank_candidates(bars_by_symbol, asof, cfg)

    assert list(table.index) == ["HEALTHY"]
    assert table.loc["HEALTHY", "rank"] == 1


def test_the_floor_only_removes_names_that_have_stopped_moving(cfg_factory) -> None:
    """A merely quiet name — ATR% above the floor — still scores and still ranks.

    Forty frozen sessions are not enough to reach the floor. The rule is a
    divisor floor, not a ban on low volatility.
    """
    cfg = cfg_factory()
    quiet = frozen_quote_bars(n=300, pop_at=260)
    asof = quiet.index[-1]

    atr_pct = atr(quiet, SCORE_ATR_WINDOW).iloc[-1] / quiet["close"].iloc[-1] * 100.0
    assert atr_pct > MIN_ATR_PCT, "fixture is meant to stay above the floor"

    table = rank_candidates({"QUIET": quiet}, asof, cfg)

    assert list(table.index) == ["QUIET"]
    assert np.isfinite(table.loc["QUIET", "score"])


def test_the_floor_is_inclusive_at_exactly_min_atr_pct(cfg_factory, monkeypatch) -> None:
    """``atr_pct >= MIN_ATR_PCT`` keeps the boundary value and drops the next float down.

    Moving the constant rather than the fixture is what makes this exact: the
    band fixture's ATR% is a float nobody can write as a literal, so the test
    compares against that float itself.
    """
    cfg = cfg_factory()
    bars = band_bars(mid_path())
    atr_pct = float(atr(bars, SCORE_ATR_WINDOW).iloc[-1] / bars["close"].iloc[-1] * 100.0)

    monkeypatch.setattr(scoring, "MIN_ATR_PCT", atr_pct)
    assert np.isfinite(momentum_score(bars, cfg).iloc[-1])

    monkeypatch.setattr(scoring, "MIN_ATR_PCT", np.nextafter(atr_pct, np.inf))
    assert np.isnan(momentum_score(bars, cfg).iloc[-1])


def test_min_atr_pct_is_the_documented_constant() -> None:
    """Five basis points of daily range, per the BUG-003 fix and the module docstring."""
    assert MIN_ATR_PCT == 0.05


def stale_zero_close_bars() -> pd.DataFrame:
    """A vendor zero 131 bars back — ``skip + 126`` — divides the long leg by nothing.

    The 126-day return is then ``+inf``, and so is the score: the exact value
    ``pd.isna`` says is present and ``np.isfinite`` says is not.
    """
    close = step_close(300, 200, after=120.0)
    close[299 - 131] = 0.0
    return band_bars(close)


def test_an_infinite_score_is_not_a_valid_score(cfg_factory) -> None:
    """``pd.isna(inf)`` is False, so the old filter let it through — and it sorted first."""
    cfg = cfg_factory()
    bars = stale_zero_close_bars()

    score = momentum_score(bars, cfg).iloc[-1]
    assert np.isposinf(score), "fixture no longer produces an infinite score"
    assert not pd.isna(score), "this is exactly what the old pd.isna filter missed"

    table = rank_candidates({"STALE": bars, "NORMAL": band_bars(mid_path())}, bars.index[299], cfg)

    assert list(table.index) == ["NORMAL"]


def test_scanner_exclusion_agrees_with_the_engine_ordering_on_non_finite_scores(
    cfg_factory,
) -> None:
    """The shared-rules contract: a non-finite score never wins, in either consumer.

    ``rank_candidates`` drops it; the backtest engine keeps the candidate but
    sorts it last (``swing.backtest.engine._rank_key``: "Non-finite scores sort
    last", encoded here as the leading ``0 if finite else 1`` key). Before the
    fix the two disagreed in the worst possible direction — the scanner ranked
    an infinite score **first** while the engine ranked it last (audit BUG-003).
    """
    cfg = cfg_factory()
    bars_by_symbol = {"STALE": stale_zero_close_bars(), "NORMAL": band_bars(mid_path())}
    asof = bars_by_symbol["NORMAL"].index[299]

    scores = {
        symbol: float(momentum_score(bars, cfg).iloc[-1]) for symbol, bars in bars_by_symbol.items()
    }
    engine_order = sorted(
        scores,
        key=lambda symbol: (
            0 if np.isfinite(scores[symbol]) else 1,
            -scores[symbol] if np.isfinite(scores[symbol]) else 0.0,
            symbol,
        ),
    )
    scanner_order = list(rank_candidates(bars_by_symbol, asof, cfg).index)

    assert engine_order[0] == scanner_order[0] == "NORMAL"
    assert "STALE" not in scanner_order  # the scanner drops what the engine sorts last


# ---------------------------------------------------------------------------
# ATR is computed once per symbol (audit PERF-009)
# ---------------------------------------------------------------------------


def test_rank_candidates_computes_atr_once_per_symbol(cfg_factory, monkeypatch) -> None:
    """The score divides by ATR and the table reports it; that is one call, not two."""
    cfg = cfg_factory()
    calls: list[int] = []
    real_atr = scoring.atr

    def counting_atr(bars: pd.DataFrame, n: int) -> pd.Series:
        calls.append(n)
        return real_atr(bars, n)

    monkeypatch.setattr(scoring, "atr", counting_atr)
    table = rank_candidates(
        {"AAA": band_bars(mid_path()), "BBB": band_bars(high_path())},
        band_bars(mid_path()).index[299],
        cfg,
    )

    assert len(table) == 2
    assert calls == [SCORE_ATR_WINDOW, SCORE_ATR_WINDOW]


def test_a_supplied_atr_series_gives_the_same_score(cfg_factory) -> None:
    """The optimisation hook must be an optimisation, not a second code path."""
    cfg = cfg_factory()
    bars = band_bars(mid_path())

    pd.testing.assert_series_equal(
        momentum_score(bars, cfg),
        momentum_score(bars, cfg, atr_series=atr(bars, SCORE_ATR_WINDOW)),
    )


# ---------------------------------------------------------------------------
# the score's ATR window is a constant, not the config knob (audit DEBT-010)
# ---------------------------------------------------------------------------


def test_the_score_window_constants_are_the_documented_values() -> None:
    """``docs/strategy-spec.md`` names both numbers; a doc/code drift is what DEBT-010 is."""
    assert SCORE_ATR_WINDOW == 14
    assert MIN_HISTORY_ROWS == 260


@pytest.mark.parametrize("atr_window", [2, 14, 30, 100])
def test_the_score_ignores_strategy_atr_window(cfg_factory, atr_window: int) -> None:
    """``strategy.atr_window`` sizes stops; the ranking uses SCORE_ATR_WINDOW alone.

    A maintainer trusting the spec's old wording would have wired the knob in
    here and silently rescaled every score in the system.
    """
    bars = band_bars(mid_path())
    baseline = momentum_score(bars, cfg_factory(strategy={"atr_window": 14}))

    pd.testing.assert_series_equal(
        momentum_score(bars, cfg_factory(strategy={"atr_window": atr_window})), baseline
    )


@pytest.mark.parametrize("atr_window", [2, 30, 100])
def test_the_ranking_ignores_strategy_atr_window(cfg_factory, atr_window: int) -> None:
    """Same table, whatever the stop-sizing window is set to — including the atr column."""
    bars_by_symbol = {"AAA": band_bars(spiked(mid_path())), "BBB": band_bars(high_path())}
    asof = bars_by_symbol["AAA"].index[299]

    pd.testing.assert_frame_equal(
        rank_candidates(bars_by_symbol, asof, cfg_factory(strategy={"atr_window": atr_window})),
        rank_candidates(bars_by_symbol, asof, cfg_factory(strategy={"atr_window": 14})),
    )
