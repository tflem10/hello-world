"""Backtest engine behaviour, on synthetic series with known answers.

The headline test is :func:`test_single_trade_matches_hand_computed_arithmetic`,
which reproduces one trade end to end from arithmetic written out in the
docstring. Everything else pins a specific rule: no look-ahead, gap-through
stops, ratcheting trails, position caps, cash accounting, determinism.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from swing.backtest.engine import EXIT_GAP, EXIT_TIME, EXIT_TRAIL, run_backtest

from .conftest import breakout_series, engine_config, make_bars, trending_bars


# ---------------------------------------------------------------------------
# the exact trade
# ---------------------------------------------------------------------------
def test_single_trade_matches_hand_computed_arithmetic():
    """One breakout, one stop-out, every number derived by hand.

    Series: 200 flat bars at 100 (high 101, low 99 -> TR 2, so ATR(14) = 2.00
    exactly), then a bar closing at 110, then 110, then a collapse to 90.

    breakout bar (index 200)
        open = prior close = 100, close = 110, high = 110*1.01 = 111.1,
        low = 100*0.99 = 99
        TR   = max(111.1-99, |111.1-100|, |99-100|) = 12.1
        ATR  = (2.00*13 + 12.1) / 14 = 2.7214285714

    fill (index 201, at the open, which equals the prior close of 110)
        entry = 110*(1 + 5bps) + 0.02*ATR = 110.055 + 0.0544286 = 110.1094286
        stop  = 110 - 2*ATR                                     = 104.5571429
        risk/share = 5.5522857 -> floor($200 / 5.5522857) = 36 shares

    exit (index 202: opens at 110, low 89.1 pierces the stop intraday)
        fill  = 104.5571429*(1 - 5bps) - 0.02*ATR = 104.4504357
        P&L   = 36 * (104.4504357 - 110.1094286)  = -203.7237
        R     = -203.7237 / (36 * 5.5522857)      = -1.019
    """
    bars = breakout_series(after=[110.0, 90.0, 90.0, 90.0])
    cfg = engine_config()

    result = run_backtest(cfg, {"AAA": bars})

    assert len(result.trades) == 1
    t = result.trades.iloc[0]
    assert t["entry_date"] == bars.index[201]
    assert t["entry_price"] == pytest.approx(110.1094286, abs=1e-6)
    assert t["initial_stop"] == pytest.approx(104.5571429, abs=1e-6)
    assert t["shares"] == 36
    assert t["exit_date"] == bars.index[202]
    assert t["exit_price"] == pytest.approx(104.4504357, abs=1e-6)
    assert t["exit_reason"] == "stop"
    assert t["pnl"] == pytest.approx(-203.7237, abs=1e-3)
    assert t["r_multiple"] == pytest.approx(-1.019, abs=1e-3)


def test_realised_loss_is_close_to_one_r():
    """A stop-out should lose ~1R. More than ~1.05R means costs are wrong."""
    bars = breakout_series(after=[110.0, 90.0, 90.0])
    result = run_backtest(engine_config(), {"AAA": bars})
    r = float(result.trades.iloc[0]["r_multiple"])
    assert -1.10 < r < -1.00


def test_final_equity_reconciles_with_trade_pnl():
    bars = breakout_series(after=[110.0, 90.0, 90.0, 90.0])
    cfg = engine_config(initial_equity=10_000.0)
    result = run_backtest(cfg, {"AAA": bars})
    expected = 10_000.0 + float(result.trades["pnl"].sum())
    assert float(result.equity.iloc[-1]) == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# timing / look-ahead
# ---------------------------------------------------------------------------
def test_fill_happens_at_the_next_open_not_the_signal_close():
    bars = breakout_series(after=[130.0, 90.0, 90.0])
    result = run_backtest(engine_config(), {"AAA": bars})
    t = result.trades.iloc[0]
    next_open = float(bars["open"].iloc[201])       # 110
    next_close = float(bars["close"].iloc[201])     # 130
    # We must have bought at the open. Filling anywhere near that bar's close
    # would mean the engine peeked at the day it was trading into.
    assert t["entry_price"] == pytest.approx(next_open, rel=0.01)
    assert t["entry_price"] < next_close - 15.0


def test_truncating_the_future_does_not_change_past_trades():
    """The classic look-ahead check: cut the data short, re-run, compare."""
    bars = trending_bars(n=600, daily_drift=0.0015, noise=0.012, seed=5, volume=3_000_000)
    cfg = engine_config()

    full = run_backtest(cfg, {"AAA": bars})
    short = run_backtest(cfg, {"AAA": bars.iloc[:500]})

    cutoff = bars.index[499]
    # The truncated run force-closes whatever was still open on its final bar;
    # that liquidation is an artefact of the cut, not a real trade.
    def real_trades(result):
        t = result.trades
        return t[(t["exit_date"] <= cutoff) & (t["exit_reason"] != "end_of_backtest")]

    closed_before = real_trades(full)
    matching = real_trades(short)
    assert len(closed_before) == len(matching) >= 5
    pd.testing.assert_frame_equal(
        closed_before.reset_index(drop=True),
        matching.reset_index(drop=True),
        check_dtype=False,
    )


def test_no_trade_is_entered_on_the_signal_bar_itself():
    bars = breakout_series(after=[110.0, 90.0])
    result = run_backtest(engine_config(), {"AAA": bars})
    assert result.trades.iloc[0]["entry_date"] > bars.index[200]


# ---------------------------------------------------------------------------
# exits
# ---------------------------------------------------------------------------
def test_gap_through_the_stop_fills_at_the_open_not_the_stop_price():
    """The single most common way a backtest flatters itself.

    A stop order does not fill at the stop price when the stock opens below it
    — it becomes a market order and fills at the open. Modelling this as a
    clean stop-price fill turns every crash into a 1R loss on paper.
    """
    closes = [100.0] * 200 + [110.0, 110.0, 70.0, 70.0]
    bars = make_bars(closes, volume=1_000_000.0)
    # Force bar 202 to *open* at 70 (a gap down), not at the prior close.
    bars.iloc[202, bars.columns.get_loc("open")] = 70.0
    bars.iloc[202, bars.columns.get_loc("high")] = 71.0
    bars.iloc[202, bars.columns.get_loc("low")] = 69.0

    result = run_backtest(engine_config(), {"AAA": bars})
    t = result.trades.iloc[0]
    assert t["exit_reason"] == EXIT_GAP
    assert t["exit_price"] < t["initial_stop"]         # worse than the stop, as in life
    assert t["exit_price"] == pytest.approx(70.0, rel=0.01)
    assert t["r_multiple"] < -1.0                       # a gap costs more than 1R


def test_time_stop_fires_after_exactly_n_bars():
    n_bars = 8
    # A flat aftermath: no stop is hit, so only the time stop can end it.
    bars = breakout_series(after=[110.0] * 40)
    cfg = engine_config(strategy__exit__time_stop_days=n_bars)
    result = run_backtest(cfg, {"AAA": bars})
    t = result.trades.iloc[0]
    assert t["exit_reason"] == EXIT_TIME
    entry_pos = bars.index.get_loc(t["entry_date"])
    exit_pos = bars.index.get_loc(t["exit_date"])
    # Entry bar counts as held bar 1; exit fills at the open the morning after
    # the Nth bar closes.
    assert exit_pos - entry_pos == n_bars


def test_time_stop_of_zero_disables_it():
    bars = breakout_series(after=[110.0] * 40)
    cfg = engine_config(strategy__exit__time_stop_days=0)
    result = run_backtest(cfg, {"AAA": bars})
    assert result.trades.iloc[0]["exit_reason"] != EXIT_TIME


def test_trailing_stop_ratchets_up_and_never_down():
    # Rise steadily, then fall back: the trail must capture some of the gain.
    after = [110.0 + 2.0 * i for i in range(30)] + [120.0, 110.0, 100.0, 90.0]
    bars = breakout_series(after=after)
    cfg = engine_config(strategy__exit__time_stop_days=0)
    result = run_backtest(cfg, {"AAA": bars})
    t = result.trades.iloc[0]
    assert t["exit_reason"] == EXIT_TRAIL
    assert t["final_stop"] > t["initial_stop"]
    assert t["pnl"] > 0                       # the trail locked in a profit


def test_open_position_is_closed_at_the_end_of_the_backtest():
    bars = breakout_series(after=[110.0] * 5)
    cfg = engine_config(strategy__exit__time_stop_days=0)
    result = run_backtest(cfg, {"AAA": bars})
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["exit_reason"] == "end_of_backtest"
    assert result.trades.iloc[0]["exit_date"] == bars.index[-1]


# ---------------------------------------------------------------------------
# portfolio constraints
# ---------------------------------------------------------------------------
def _multi_symbol_universe(n_symbols: int = 8, seed: int = 2):
    rng = np.random.default_rng(seed)
    bars = {}
    for i in range(n_symbols):
        closes = 40.0 * np.exp(
            np.cumsum(np.full(700, 0.0015) + rng.normal(0, 0.018, 700))
        )
        bars[f"S{i:02d}"] = make_bars(
            closes, start="2019-01-01", volume=rng.uniform(2e6, 8e6, 700)
        )
    return bars


def test_concurrent_positions_never_exceed_the_cap():
    bars = _multi_symbol_universe()
    cfg = engine_config(account__max_concurrent_positions=3)
    result = run_backtest(cfg, bars)
    assert result.open_positions.max() <= 3
    assert result.n_trades > 0


def test_a_lower_cap_produces_fewer_trades():
    bars = _multi_symbol_universe()
    tight = run_backtest(engine_config(account__max_concurrent_positions=1), bars)
    loose = run_backtest(engine_config(account__max_concurrent_positions=6), bars)
    assert tight.n_trades < loose.n_trades


def test_the_highest_ranked_candidate_is_taken_when_slots_are_scarce():
    """Two simultaneous breakouts, one slot: the better rank score must win."""
    strong = breakout_series(
        flat_bars=200, flat_price=100.0, breakout_price=110.0, after=[110.0] * 20
    )
    # Give the weak name a lower momentum score by making it drift down first.
    weak_closes = list(np.linspace(140.0, 100.0, 200)) + [110.0] + [110.0] * 20
    weak = make_bars(weak_closes, volume=1_000_000.0)

    cfg = engine_config(
        account__max_concurrent_positions=1, strategy__exit__time_stop_days=0
    )
    result = run_backtest(cfg, {"STRONG": strong, "WEAK": weak})
    assert len(result.trades) >= 1
    assert result.trades.iloc[0]["symbol"] == "STRONG"


def test_risk_per_trade_never_exceeds_the_budget():
    bars = _multi_symbol_universe()
    cfg = engine_config(initial_equity=50_000.0, account__risk_pct=0.02)
    result = run_backtest(cfg, bars)
    assert len(result.trades) > 5
    # Equity moves during the run, so allow the budget to be measured against
    # the largest equity the curve reached.
    ceiling = float(result.equity.max()) * 0.02 + 1e-6
    assert (result.trades["risk_dollars"] <= ceiling).all()


def test_position_cap_limits_notional():
    bars = _multi_symbol_universe()
    cfg = engine_config(initial_equity=50_000.0, account__max_position_pct=0.10)
    result = run_backtest(cfg, bars)
    notional = result.trades["entry_price"] * result.trades["shares"]
    assert (notional <= float(result.equity.max()) * 0.10 + 1e-6).all()


def test_shares_are_always_whole():
    bars = _multi_symbol_universe()
    result = run_backtest(engine_config(), bars)
    assert (result.trades["shares"] == result.trades["shares"].astype(int)).all()
    assert (result.trades["shares"] >= 1).all()


def test_a_tiny_account_takes_no_trades_it_cannot_afford():
    bars = _multi_symbol_universe()
    cfg = engine_config(initial_equity=100.0, account__max_position_pct=0.25)
    result = run_backtest(cfg, bars)
    # $25 max position against $40+ share prices: nothing is affordable.
    assert result.n_trades == 0
    assert float(result.equity.iloc[-1]) == pytest.approx(100.0)


def test_cash_never_goes_negative():
    bars = _multi_symbol_universe()
    result = run_backtest(engine_config(initial_equity=5_000.0), bars)
    assert (result.cash >= -1e-6).all()


def test_equity_equals_cash_plus_holdings():
    bars = _multi_symbol_universe(n_symbols=3)
    result = run_backtest(engine_config(), bars)
    # When flat, equity and cash must agree exactly.
    flat_days = result.open_positions == 0
    assert flat_days.any()
    pd.testing.assert_series_equal(
        result.equity[flat_days], result.cash[flat_days],
        check_names=False, rtol=1e-9,
    )


# ---------------------------------------------------------------------------
# regime filter
# ---------------------------------------------------------------------------
def test_regime_filter_blocks_entries_in_a_downtrend():
    bars = breakout_series(after=[110.0] * 30)
    # A benchmark in a persistent decline is below its 200-SMA throughout.
    bench = make_bars(list(np.linspace(400.0, 200.0, 260)), volume=1e8)

    blocked = run_backtest(
        engine_config(strategy__regime__enabled=True), {"AAA": bars}, benchmark=bench
    )
    allowed = run_backtest(engine_config(), {"AAA": bars})
    assert blocked.n_trades == 0
    assert allowed.n_trades >= 1


def test_regime_filter_allows_entries_in_an_uptrend():
    bars = breakout_series(after=[110.0] * 30)
    bench = make_bars(list(np.linspace(200.0, 400.0, 260)), volume=1e8)
    result = run_backtest(
        engine_config(strategy__regime__enabled=True), {"AAA": bars}, benchmark=bench
    )
    assert result.n_trades >= 1


def test_missing_benchmark_degrades_to_no_filter_with_a_warning():
    bars = breakout_series(after=[110.0] * 30)
    result = run_backtest(engine_config(strategy__regime__enabled=True), {"AAA": bars})
    assert result.n_trades >= 1
    assert any("regime" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------
def test_higher_slippage_produces_worse_results():
    bars = _multi_symbol_universe()
    cheap = run_backtest(engine_config(backtest__slippage_bps=0.0,
                                       backtest__spread_atr_frac=0.0), bars)
    dear = run_backtest(engine_config(backtest__slippage_bps=50.0,
                                      backtest__spread_atr_frac=0.10), bars)
    assert float(dear.equity.iloc[-1]) < float(cheap.equity.iloc[-1])


def test_commission_is_charged_on_both_sides():
    bars = breakout_series(after=[110.0, 90.0, 90.0])
    free = run_backtest(engine_config(backtest__commission_per_trade=0.0), {"AAA": bars})
    paid = run_backtest(engine_config(backtest__commission_per_trade=5.0), {"AAA": bars})
    difference = float(free.equity.iloc[-1]) - float(paid.equity.iloc[-1])
    assert difference == pytest.approx(10.0, abs=1e-6)


# ---------------------------------------------------------------------------
# determinism and edge cases
# ---------------------------------------------------------------------------
def test_two_runs_are_byte_identical():
    bars = _multi_symbol_universe()
    cfg = engine_config()
    a = run_backtest(cfg, bars)
    b = run_backtest(cfg, bars)
    assert a.trades.to_csv(index=False) == b.trades.to_csv(index=False)
    assert a.equity.to_csv() == b.equity.to_csv()
    assert a.data_hash == b.data_hash
    assert a.config_hash == b.config_hash


def test_no_signals_means_no_trades_and_flat_equity():
    bars = make_bars([100.0] * 400, volume=1_000_000.0)
    result = run_backtest(engine_config(), {"AAA": bars})
    assert result.n_trades == 0
    assert result.equity.nunique() == 1


def test_empty_universe_raises_a_clear_error():
    with pytest.raises(ValueError, match="no symbols"):
        run_backtest(engine_config(), {})


def test_window_outside_the_data_raises():
    bars = trending_bars(n=300)
    with pytest.raises(ValueError, match="no bars"):
        run_backtest(engine_config(), {"AAA": bars},
                     start=pd.Timestamp("1990-01-01").date(),
                     end=pd.Timestamp("1990-12-31").date())


def test_symbols_with_too_little_history_are_simply_ignored():
    bars = {
        "GOOD": breakout_series(after=[110.0, 90.0, 90.0]),
        "SHORT": make_bars([50.0] * 12, volume=1_000_000.0),
    }
    result = run_backtest(engine_config(), bars)
    assert set(result.trades["symbol"]) == {"GOOD"}


def test_feature_cache_does_not_change_results():
    bars = _multi_symbol_universe(n_symbols=4)
    cfg = engine_config()
    shared: dict = {}
    a = run_backtest(cfg, bars, feature_cache=shared)
    b = run_backtest(cfg, bars, feature_cache=shared)
    assert shared                                  # the cache was populated
    assert a.trades.to_csv(index=False) == b.trades.to_csv(index=False)


def test_earnings_blackout_blocks_entries_around_the_date():
    bars = breakout_series(after=[110.0] * 20)
    entry_bar = bars.index[201].date()
    cfg = engine_config(strategy__earnings__blackout_days_before=10)
    clean = run_backtest(cfg, {"AAA": bars})
    blocked = run_backtest(cfg, {"AAA": bars}, earnings={"AAA": [entry_bar]})
    assert clean.n_trades == 1
    assert blocked.n_trades == 0


def test_missing_earnings_calendar_is_warned_about_not_silently_skipped():
    bars = breakout_series(after=[110.0] * 20)
    result = run_backtest(engine_config(), {"AAA": bars})
    assert any("earnings" in w for w in result.warnings)
