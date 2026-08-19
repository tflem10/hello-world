"""AC8 — the engine produces the EXACT expected trade list on engineered fixtures.

Everything here is synthetic and offline. The fixtures are deliberately boring:
a smooth exponential ramp with a flat volume profile, which satisfies the WP-D
trend template from bar ~252 onward and produces **no** entry signal at all
until a single volume spike is injected on a chosen bar. That gives complete
control over when a trade starts, so the trade that comes out can be checked
price by price.

Bar numbering used throughout::

    bar 300 = the signal bar   (volume spike, close-of-day decision)
    bar 301 = the fill bar     (entry at that bar's OPEN, plus costs)

Every expected number below is either arithmetic worked out in a comment or an
independent recomputation from the WP-D Series (never a copy of what the engine
happened to print).
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd
import pytest

from conftest import build_config
from swing.backtest.costs import CostModel
from swing.backtest.engine import (
    EQUITY_COLUMNS,
    EXIT_END_OF_DATA,
    EXIT_REASONS,
    TRADE_COLUMNS,
    EngineResult,
    SignalCache,
    run_engine,
)
from swing.indicators import atr as atr_fn
from swing.strategy import regime, rules, scoring
from swing.strategy.sizing import size_position

SIGNAL_BAR = 300
FILL_BAR = 301
WARMUP_BAR = 260

#: Every float an EngineResult exposes to a caller. Audit BUG-005 corrupted
#: each of these in turn, so they are checked as a set, not one at a time.
TRADE_FLOAT_COLUMNS = ("entry_price", "exit_price", "pnl", "pnl_pct", "entry_cost", "exit_cost")
EQUITY_FLOAT_COLUMNS = ("equity", "cash", "drawdown")
POSITION_FLOAT_FIELDS = (
    "entry_price",
    "entry_cost",
    "initial_stop",
    "stop",
    "last_close",
    "unrealized_pnl",
)


def assert_artifacts_are_finite(result: EngineResult) -> None:
    """No NaN and no infinity anywhere a number can hide in an EngineResult.

    The global guard for audit BUG-005: a single non-finite bar used to reach
    the trade list, the equity curve and the drawdown column with no exception,
    no warning and no trace in any artefact.
    """
    for column in TRADE_FLOAT_COLUMNS:
        values = result.trades[column].to_numpy(dtype="float64")
        assert np.isfinite(values).all(), f"non-finite {column} in trades:\n{result.trades}"
    for column in EQUITY_FLOAT_COLUMNS:
        values = result.equity[column].to_numpy(dtype="float64")
        bad = result.equity.index[~np.isfinite(values)]
        assert np.isfinite(values).all(), f"non-finite {column} in equity on {list(bad)}"
    for position in result.positions:
        for field in POSITION_FLOAT_FIELDS:
            value = float(getattr(position, field))
            assert math.isfinite(value), f"non-finite {field} on the {position.symbol} snapshot"


# ---------------------------------------------------------------------------
# fixture builders (imported by the other backtest test modules)
# ---------------------------------------------------------------------------


def ramp_bars(
    n: int = 400,
    start: str = "2020-01-02",
    base: float = 100.0,
    growth: float = 0.002,
    volume: float = 1_000_000.0,
) -> pd.DataFrame:
    """A smooth exponential uptrend in the Contract 3 format.

    Chosen because it makes every trend-template clause true by construction
    (stacked and rising SMAs, well above the 52-week low, at the 52-week high,
    ADX pinned high by unbroken directional movement) while the flat volume
    profile keeps ``entry_signal`` false: with constant volume the 50-day
    average equals today's volume, so the ``volume >= 1.3 x average`` clause
    never fires. Signals are then injected one bar at a time with
    :func:`spike_volume`.
    """
    index = pd.bdate_range(start=start, periods=n)
    close = [base * (1.0 + growth) ** i for i in range(n)]
    open_ = [base, *close[:-1]]
    high = [c * 1.005 for c in close]
    low = [min(o, c) * 0.995 for o, c in zip(open_, close, strict=True)]
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": [volume] * n,
        },
        index=index,
    ).astype(float)


def spike_volume(bars: pd.DataFrame, bar: int, multiple: float = 2.0) -> pd.DataFrame:
    """Double the volume on one bar, which is enough to fire the breakout entry.

    With a 50-day flat history the rolling average becomes ``1.02 x V`` and the
    spike is ``2.0 x V``, comfortably over the ``1.3 x`` threshold — and the
    following 49 bars are just as comfortably under it, so exactly one signal
    is produced.
    """
    frame = bars.copy()
    frame.iloc[bar, frame.columns.get_loc("volume")] = bars["volume"].iloc[bar] * multiple
    return frame


def set_bar(bars: pd.DataFrame, bar: int, **values: float) -> pd.DataFrame:
    """Return a copy of ``bars`` with named OHLC fields overridden on one bar."""
    frame = bars.copy()
    for column, value in values.items():
        frame.iloc[bar, frame.columns.get_loc(column)] = float(value)
    return frame


def engine_cfg(tmp_path, **overrides):
    """A config sized so trades are actually affordable (the $100 default is not)."""
    sections = {"account": {"equity": 100_000.0}}
    for name, values in overrides.items():
        sections.setdefault(name, {}).update(values)
    return build_config(tmp_path, **sections)


def expected_stops(bars: pd.DataFrame, cfg, signal_bar: int = SIGNAL_BAR) -> dict[int, float]:
    """Recompute the per-position ratchet independently of the engine.

    ``effective_stop(t) = max(effective_stop(t-1), chandelier_stop(t), initial)``
    — the Contract 11 amendment, applied straight to the WP-D Series.
    """
    initial = float(rules.initial_stop(bars, cfg).iloc[signal_bar])
    chandelier = rules.chandelier_stop(bars, cfg)
    stops: dict[int, float] = {}
    running = initial
    for i in range(signal_bar + 1, len(bars)):
        running = max(running, float(chandelier.iloc[i]), initial)
        stops[i] = running
    return stops


def single_symbol_run(bars: pd.DataFrame, cfg, spy: pd.DataFrame | None = None, end_bar=330):
    """Run the engine over one symbol from the warm-up bar to ``end_bar``."""
    spy = ramp_bars() if spy is None else spy
    return run_engine(
        {"AAA": bars},
        spy,
        cfg,
        start=bars.index[WARMUP_BAR].date(),
        end=bars.index[end_bar].date(),
    )


# ---------------------------------------------------------------------------
# the fixture itself behaves as advertised
# ---------------------------------------------------------------------------


def test_ramp_fixture_passes_the_trend_template_but_gives_no_signal(tmp_path):
    cfg = engine_cfg(tmp_path)
    bars = ramp_bars()
    assert bool(rules.trend_template(bars, cfg, is_etf=False).iloc[SIGNAL_BAR])
    assert bool(rules.liquidity_ok(bars, cfg, is_etf=False).iloc[SIGNAL_BAR])
    # Flat volume => no breakout confirmation anywhere in the series.
    assert int(rules.entry_signal(bars, cfg).sum()) == 0


def test_a_single_volume_spike_produces_exactly_one_signal(tmp_path):
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    signals = rules.entry_signal(bars, cfg)
    assert int(signals.sum()) == 1
    assert bool(signals.iloc[SIGNAL_BAR])


# ---------------------------------------------------------------------------
# AC8 — exact trades
# ---------------------------------------------------------------------------


def test_result_frames_have_the_contract_shape(tmp_path):
    cfg = engine_cfg(tmp_path)
    result = single_symbol_run(spike_volume(ramp_bars(), SIGNAL_BAR), cfg)
    assert list(result.trades.columns) == list(TRADE_COLUMNS)
    assert list(result.equity.columns) == list(EQUITY_COLUMNS)
    assert result.equity.index.name == "date"
    assert set(result.trades["exit_reason"]) <= set(EXIT_REASONS)


def test_entry_fills_at_the_next_open_with_costs_and_sizing(tmp_path):
    """The single most important semantic: decide at close(t), fill at open(t+1)."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    result = single_symbol_run(bars, cfg)

    assert len(result.trades) == 1
    trade = result.trades.iloc[0]

    # --- the fill bar and the raw fill price -------------------------------
    assert trade["entry_date"] == bars.index[FILL_BAR]
    raw_open = float(bars["open"].iloc[FILL_BAR])
    assert trade["entry_price"] == pytest.approx(raw_open)

    # --- the cost, from the ATR of the SIGNAL bar (never the fill bar) ------
    atr_at_signal = float(atr_fn(bars, cfg.strategy.atr_window).iloc[SIGNAL_BAR])
    per_share = CostModel.from_config(cfg).per_share(raw_open, atr_at_signal)
    # 182.102731 * 5/10_000 = 0.091051  slippage
    # 0.05 * 2.127484        = 0.106374  half-spread
    #                        = 0.197426  per share
    assert per_share == pytest.approx(0.197426, abs=1e-6)
    assert trade["entry_cost"] == pytest.approx(trade["shares"] * per_share)

    # --- the share count comes from WP-C sizing ----------------------------
    stop = float(rules.initial_stop(bars, cfg).iloc[SIGNAL_BAR])
    sized = size_position(cfg.account.equity, cfg.account.equity, raw_open + per_share, stop, cfg)
    assert int(trade["shares"]) == sized.shares
    # equity 100_000, risk 2.5% = $2_500; risk/share = 182.300157 - 177.847763
    # = 4.452394 -> 561 shares on risk alone, but the 25% notional cap allows
    # only 25_000 / 182.300157 = 137.14 -> 137 shares.
    assert int(trade["shares"]) == 137
    assert sized.capped_by == "position_cap"


def test_time_stop_signals_at_exactly_forty_trading_days_and_fills_next_open(tmp_path):
    """time_stop_days=40: armed at the close of the 40th day held, filled the next open."""
    cfg = engine_cfg(tmp_path)
    assert cfg.strategy.time_stop_days == 40
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    result = single_symbol_run(bars, cfg, end_bar=360)

    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["exit_reason"] == "time"

    entry_at = bars.index.get_loc(trade["entry_date"])
    exit_at = bars.index.get_loc(trade["exit_date"])
    # Armed at bar entry+40 (exactly forty trading days held), filled at the
    # open of bar entry+41.
    assert exit_at - entry_at == 41
    assert int(trade["hold_days"]) == 41
    assert trade["exit_price"] == pytest.approx(float(bars["open"].iloc[exit_at]))


def test_gap_through_the_stop_fills_at_the_open_not_at_the_stop(tmp_path):
    """The cost retail backtests fake away: an overnight gap gets the open."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    stops = expected_stops(bars, cfg)

    # On the bar after the fill the stop is still the initial stop (the
    # chandelier has not ratcheted above it yet), so gapping under it exits.
    gap_bar = FILL_BAR + 1
    assert stops[FILL_BAR] == pytest.approx(float(rules.initial_stop(bars, cfg).iloc[SIGNAL_BAR]))
    gap_open = 175.0
    assert gap_open < stops[FILL_BAR]
    bars = set_bar(bars, gap_bar, open=gap_open, low=174.0)

    result = single_symbol_run(bars, cfg)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]

    assert trade["exit_date"] == bars.index[gap_bar]
    assert trade["exit_price"] == pytest.approx(gap_open)  # the OPEN, not 177.85
    assert trade["exit_price"] < stops[FILL_BAR]
    # The initial stop was the binding one, so the reason is "stop".
    assert trade["exit_reason"] == "stop"
    assert int(trade["hold_days"]) == 1

    # P&L by hand: 137 * (175.0 - 182.102731) = -973.074
    #              - 27.047304 entry cost - 26.589914 exit cost = -1026.711
    assert trade["pnl"] == pytest.approx(
        137 * (gap_open - trade["entry_price"]) - trade["entry_cost"] - trade["exit_cost"]
    )
    assert trade["pnl"] == pytest.approx(-1026.711388, abs=1e-5)


def test_intraday_breach_fills_at_the_stop_price(tmp_path):
    """A resting stop order that is touched intraday fills AT the stop."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    stops = expected_stops(bars, cfg)

    breach_bar = 320
    stop_level = stops[breach_bar - 1]
    # Dip the low under the stop while leaving the open well above it: this is
    # the "opened fine, sold off during the day" case.
    bars = set_bar(bars, breach_bar, low=stop_level - 0.50)
    assert float(bars["open"].iloc[breach_bar]) > stop_level

    result = single_symbol_run(bars, cfg)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]

    assert trade["exit_date"] == bars.index[breach_bar]
    assert trade["exit_price"] == pytest.approx(stop_level)
    # By bar 319 the chandelier has ratcheted above the initial stop, so the
    # binding stop — and the reported reason — is the chandelier.
    assert trade["exit_reason"] == "chandelier"
    assert stop_level > float(rules.initial_stop(bars, cfg).iloc[SIGNAL_BAR])


def test_chandelier_stop_only_ever_ratchets_up(tmp_path):
    """A volatility spike collapses the raw chandelier; the position's stop must not move.

    Contract 11 amendment:
    ``effective_stop(t) = max(effective_stop(t-1), chandelier_stop(t), initial)``.
    """
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)

    # Blow the range out on two bars: ATR jumps, so the RAW chandelier level
    # (highest close - 3 x ATR) falls hard.
    for bar in (316, 317):
        close = float(bars["close"].iloc[bar])
        bars = set_bar(bars, bar, high=close * 1.10, low=close * 0.90)

    chandelier = rules.chandelier_stop(bars, cfg)
    stops = expected_stops(bars, cfg)
    ratcheted = stops[317]
    raw = float(chandelier.iloc[317])
    # The raw series really did collapse — otherwise this test proves nothing.
    assert raw < ratcheted - 10.0
    assert ratcheted == pytest.approx(stops[315])  # frozen at the pre-spike peak

    breach_bar = 318
    bars = set_bar(bars, breach_bar, low=ratcheted - 0.25)
    assert float(bars["open"].iloc[breach_bar]) > ratcheted

    result = single_symbol_run(bars, cfg)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    assert trade["exit_price"] == pytest.approx(ratcheted)
    assert trade["exit_price"] != pytest.approx(raw)
    assert trade["exit_reason"] == "chandelier"


def test_stop_sequence_is_monotonic_over_the_whole_hold(tmp_path):
    """Directly assert the ratchet property on the recomputed stop path."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    stops = expected_stops(bars, cfg)
    path = [stops[i] for i in sorted(stops)]
    assert len(path) > 50
    assert all(later >= earlier for earlier, later in zip(path, path[1:], strict=False))


# ---------------------------------------------------------------------------
# regime
# ---------------------------------------------------------------------------


def falling_spy(n: int = 400, turn: int = 320) -> pd.DataFrame:
    """SPY that ramps up then falls hard, so the 200-day regime filter switches off."""
    index = pd.bdate_range(start="2020-01-02", periods=n)
    close = [
        100.0 * (1.002**i) if i <= turn else 100.0 * (1.002**turn) * (0.97 ** (i - turn))
        for i in range(n)
    ]
    open_ = [100.0, *close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": [c * 1.005 for c in close],
            "low": [min(o, c) * 0.995 for o, c in zip(open_, close, strict=True)],
            "close": close,
            "volume": [1_000_000.0] * n,
        },
        index=index,
    ).astype(float)


def test_regime_off_blocks_new_entries_but_open_positions_keep_trailing(tmp_path):
    """Contract 7/11: the regime filter gates ENTRIES ONLY."""
    cfg = engine_cfg(tmp_path, strategy={"time_stop_days": 300})
    spy = falling_spy()
    allowed = regime.entries_allowed(spy, cfg)
    assert bool(allowed.iloc[SIGNAL_BAR]) is True  # on when AAA signals
    assert bool(allowed.iloc[345]) is False  # off when BBB signals

    held = spike_volume(ramp_bars(), SIGNAL_BAR)
    blocked = spike_volume(ramp_bars(base=50.0), 345)

    result = run_engine(
        {"AAA": held, "BBB": blocked},
        spy,
        cfg,
        start=held.index[WARMUP_BAR].date(),
        end=held.index[380].date(),
    )

    # BBB signalled while the regime was off: no trade at all.
    assert set(result.trades["symbol"]) == {"AAA"}
    # AAA was untouched by the regime and kept trailing right through it: its
    # stop is far above where it started.
    assert len(result.positions) == 1
    position = result.positions[0]
    assert position.symbol == "AAA"
    assert position.stop > position.initial_stop
    assert result.trades.iloc[0]["exit_reason"] == "end_of_data"


# ---------------------------------------------------------------------------
# slot contention and cash
# ---------------------------------------------------------------------------


def contested_universe(spike_bar: int = SIGNAL_BAR) -> dict[str, pd.DataFrame]:
    """Five symbols that all signal on the same bar, with separated momentum scores."""
    growths = {"AAA": 0.0015, "BBB": 0.0018, "CCC": 0.0021, "DDD": 0.0024, "EEE": 0.0027}
    return {
        symbol: spike_volume(ramp_bars(growth=growth), spike_bar)
        for symbol, growth in growths.items()
    }


def test_engine_ranking_matches_rank_candidates(tmp_path):
    """The engine's precomputed ranking must agree with the scanner's ranking function."""
    cfg = engine_cfg(tmp_path)
    bars = contested_universe()
    asof = bars["AAA"].index[SIGNAL_BAR]
    ranking = scoring.rank_candidates(bars, asof, cfg)
    assert list(ranking.index) == ["EEE", "DDD", "CCC", "BBB", "AAA"]

    result = run_engine(
        bars,
        ramp_bars(),
        engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0}),
        start=bars["AAA"].index[WARMUP_BAR].date(),
        end=bars["AAA"].index[310].date(),
    )
    # The four that entered are the four the ranking put on top.
    assert set(result.trades["symbol"]) == set(ranking.index[:4])


def test_slot_contention_admits_the_top_four_by_score(tmp_path):
    """Five simultaneous signals, four slots: the best four get in, in rank order."""
    cfg = engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0})
    assert cfg.account.max_positions == 4
    bars = contested_universe()

    result = run_engine(
        bars,
        ramp_bars(),
        cfg,
        start=bars["AAA"].index[WARMUP_BAR].date(),
        end=bars["AAA"].index[310].date(),
    )
    assert sorted(result.trades["symbol"]) == ["BBB", "CCC", "DDD", "EEE"]
    assert "AAA" not in set(result.trades["symbol"])  # rank 5, no slot
    # All four filled on the same bar, at that bar's open.
    assert set(result.trades["entry_date"]) == {bars["AAA"].index[FILL_BAR]}
    assert int(result.equity["n_positions"].max()) == 4


def test_cash_exhaustion_skips_a_pick_without_burning_a_slot(tmp_path):
    """A pick that sizes to zero shares is skipped; it does not consume a slot.

    Expectation updated for audit BUG-053 (bench depth): the engine now carries
    ``max_positions * 3`` orders overnight, so the candidates ranked past the
    slot count are still available when the ones above them size to zero. AAA
    (rank 5, and the cheapest of the five) therefore takes the leftover cash
    that used to be stranded.
    """
    cfg = engine_cfg(tmp_path, account={"equity": 30_000.0, "max_position_pct": 100.0})
    bars = contested_universe()

    result = run_engine(
        bars,
        ramp_bars(),
        cfg,
        start=bars["AAA"].index[WARMUP_BAR].date(),
        end=bars["AAA"].index[310].date(),
    )
    # EEE (rank 1) takes almost all the cash; DDD gets the 3 shares that are
    # left; CCC and BBB size to zero; AAA, off the old four-deep bench entirely,
    # affords the single share the remaining ~$160 buys.
    assert sorted(result.trades["symbol"]) == ["AAA", "DDD", "EEE"]
    shares = dict(zip(result.trades["symbol"], result.trades["shares"], strict=True))
    assert shares["EEE"] == 130
    assert shares["DDD"] == 3
    assert shares["AAA"] == 1
    # Cash never goes negative — no margin, ever.
    assert float(result.equity["cash"].min()) >= 0.0


def test_one_position_per_symbol_at_a_time(tmp_path):
    """A second signal in a symbol already held is ignored, not doubled up."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(spike_volume(ramp_bars(), SIGNAL_BAR), SIGNAL_BAR + 5)
    result = single_symbol_run(bars, cfg, end_bar=320)
    # Two signals, but the position from the first is still open at the second.
    assert len(result.trades) == 1


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


def test_equity_curve_reconciles_with_the_trade_list_exactly(tmp_path):
    """final equity == initial + sum(pnl) + the exit costs of the end-of-data marks.

    The equity curve marks open positions at the raw close, while an
    ``end_of_data`` trade charges a (hypothetical) exit cost to liquidate them.
    The difference between the two views is exactly those costs — and nothing
    else, or money is leaking somewhere.
    """
    cfg = engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0})
    bars = contested_universe()
    result = run_engine(
        bars,
        ramp_bars(),
        cfg,
        start=bars["AAA"].index[WARMUP_BAR].date(),
        end=bars["AAA"].index[330].date(),
    )
    assert len(result.trades) > 0

    trades = result.trades
    eod_exit_costs = float(trades.loc[trades["exit_reason"] == "end_of_data", "exit_cost"].sum())
    final_equity = float(result.equity["equity"].iloc[-1])

    assert final_equity == pytest.approx(
        result.initial_equity + float(trades["pnl"].sum()) + eod_exit_costs, abs=1e-6
    )

    # And every individual trade's P&L is the documented formula.
    for _, trade in trades.iterrows():
        assert trade["pnl"] == pytest.approx(
            trade["shares"] * (trade["exit_price"] - trade["entry_price"])
            - trade["entry_cost"]
            - trade["exit_cost"]
        )


def test_a_fully_closed_run_reconciles_to_the_penny(tmp_path):
    """With no position open at the end, final equity is exactly initial + P&L."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    result = single_symbol_run(bars, cfg, end_bar=360)
    assert set(result.trades["exit_reason"]) == {"time"}
    assert result.positions == []
    assert float(result.equity["equity"].iloc[-1]) == pytest.approx(
        result.initial_equity + float(result.trades["pnl"].sum()), abs=1e-9
    )


def test_drawdown_column_is_a_non_positive_fraction(tmp_path):
    cfg = engine_cfg(tmp_path)
    result = single_symbol_run(spike_volume(ramp_bars(), SIGNAL_BAR), cfg)
    assert float(result.equity["drawdown"].max()) <= 0.0
    assert float(result.equity["drawdown"].min()) > -1.0


# ---------------------------------------------------------------------------
# determinism and edge cases
# ---------------------------------------------------------------------------


def test_two_identical_runs_produce_identical_frames(tmp_path):
    """The in-memory half of AC9."""
    cfg = engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0})
    bars = contested_universe()
    kwargs = {
        "start": bars["AAA"].index[WARMUP_BAR].date(),
        "end": bars["AAA"].index[330].date(),
    }
    first = run_engine(bars, ramp_bars(), cfg, **kwargs)
    second = run_engine(bars, ramp_bars(), cfg, **kwargs)
    pd.testing.assert_frame_equal(first.trades, second.trades)
    pd.testing.assert_frame_equal(first.equity, second.equity)


def test_symbol_insertion_order_does_not_change_the_result(tmp_path):
    """Determinism must not depend on dict ordering."""
    cfg = engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0})
    bars = contested_universe()
    reversed_bars = dict(reversed(list(bars.items())))
    kwargs = {
        "start": bars["AAA"].index[WARMUP_BAR].date(),
        "end": bars["AAA"].index[330].date(),
    }
    first = run_engine(bars, ramp_bars(), cfg, **kwargs)
    second = run_engine(reversed_bars, ramp_bars(), cfg, **kwargs)
    pd.testing.assert_frame_equal(first.trades, second.trades)


def test_signal_cache_does_not_change_results(tmp_path):
    """The cache is a speed knob; sharing it across runs must be invisible."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    spy = ramp_bars()
    kwargs = {"start": bars.index[WARMUP_BAR].date(), "end": bars.index[330].date()}

    cache = SignalCache()
    cached_first = run_engine({"AAA": bars}, spy, cfg, cache=cache, **kwargs)
    cached_second = run_engine({"AAA": bars}, spy, cfg, cache=cache, **kwargs)
    uncached = run_engine({"AAA": bars}, spy, cfg, **kwargs)

    pd.testing.assert_frame_equal(cached_first.trades, uncached.trades)
    pd.testing.assert_frame_equal(cached_second.trades, uncached.trades)
    assert cache.hits > 0  # the second run really did reuse the first run's work


def test_empty_universe_returns_well_formed_empty_frames(tmp_path):
    cfg = engine_cfg(tmp_path)
    result = run_engine({}, ramp_bars(), cfg)
    assert result.trades.empty
    assert result.equity.empty
    assert list(result.trades.columns) == list(TRADE_COLUMNS)
    assert list(result.equity.columns) == list(EQUITY_COLUMNS)
    assert result.positions == []


def test_start_and_end_bound_the_simulation_not_the_indicator_warmup(tmp_path):
    """Indicators warm up on bars before ``start`` — otherwise walk-forward is a lie."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    # Start only two bars before the signal: far too little history to warm up
    # a 200-day SMA, yet the signal still fires because the engine computed the
    # rules on the whole frame.
    result = run_engine(
        {"AAA": bars},
        ramp_bars(),
        cfg,
        start=bars.index[SIGNAL_BAR - 2].date(),
        end=bars.index[320].date(),
    )
    assert len(result.trades) == 1
    assert result.equity.index[0] == bars.index[SIGNAL_BAR - 2]
    assert result.equity.index[-1] == bars.index[320]


def test_a_signal_on_the_final_bar_never_fills(tmp_path):
    """There is no next open to fill against, so the order lapses."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    result = run_engine(
        {"AAA": bars},
        ramp_bars(),
        cfg,
        start=bars.index[WARMUP_BAR].date(),
        end=bars.index[SIGNAL_BAR].date(),
    )
    assert result.trades.empty
    assert result.positions == []


def test_regime_with_no_spy_data_blocks_every_entry(tmp_path):
    """Refusing to invent a regime we cannot observe is the conservative direction."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    empty_spy = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    result = run_engine(
        {"AAA": bars},
        empty_spy,
        cfg,
        start=bars.index[WARMUP_BAR].date(),
        end=bars.index[330].date(),
    )
    assert result.trades.empty


def test_regime_disabled_with_no_spy_data_still_trades(tmp_path):
    cfg = engine_cfg(tmp_path, regime={"enabled": False})
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    empty_spy = pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
    result = run_engine(
        {"AAA": bars},
        empty_spy,
        cfg,
        start=bars.index[WARMUP_BAR].date(),
        end=bars.index[330].date(),
    )
    assert len(result.trades) == 1


# ---------------------------------------------------------------------------
# BUG-005 — a bar with non-finite OHLC is not a bar
#
# All four scenarios are the ones the audit reproduced. Each of them used to
# put a NaN into an artefact; none of them may now.
# ---------------------------------------------------------------------------


def test_clean_fixtures_produce_finite_artifacts(tmp_path):
    """The guard itself has to pass on a healthy run, or it proves nothing."""
    cfg = engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0})
    bars = contested_universe()
    result = run_engine(
        bars,
        ramp_bars(),
        cfg,
        start=bars["AAA"].index[WARMUP_BAR].date(),
        end=bars["AAA"].index[330].date(),
    )
    assert len(result.trades) > 0
    assert_artifacts_are_finite(result)


def test_a_nan_open_on_the_time_stop_bar_does_not_poison_the_trade(tmp_path):
    """BUG-005 scenario 1: NaN open on a time-stop bar gave exit_price=NaN, pnl=NaN.

    The poisoned trade then vanished from the profit factor (NaN fails both
    ``> 0`` and ``< 0``) while still counting in the trade total and the
    win-rate denominator, and 59 of 141 equity rows went NaN.
    """
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    time_stop_bar = FILL_BAR + cfg.strategy.time_stop_days  # armed at close, fills here
    poisoned = set_bar(bars, time_stop_bar, open=float("nan"))

    result = single_symbol_run(poisoned, cfg, end_bar=360)

    assert_artifacts_are_finite(result)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    # The time stop stays armed and fills at the next bar that actually printed.
    assert trade["exit_reason"] == "time"
    assert trade["exit_date"] == bars.index[time_stop_bar + 1]
    assert trade["exit_price"] == pytest.approx(float(bars["open"].iloc[time_stop_bar + 1]))


def test_a_nan_close_on_the_final_bar_still_books_the_position(tmp_path):
    """BUG-005 scenario 2: the position was filtered out and its money vanished.

    The audit measured $26,436 of a $100k account disappearing, booked nowhere:
    not in the trade list, not in the equity curve, not in ``positions``.
    """
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    poisoned = set_bar(bars, 330, close=float("nan"))

    result = single_symbol_run(poisoned, cfg, end_bar=330)

    assert_artifacts_are_finite(result)
    assert len(result.trades) == 1
    trade = result.trades.iloc[0]
    # Marked out at the last bar that had a real close, not silently dropped.
    assert trade["exit_reason"] == EXIT_END_OF_DATA
    assert trade["exit_date"] == bars.index[329]
    assert trade["exit_price"] == pytest.approx(float(bars["close"].iloc[329]))
    # Every dollar is accounted for.
    assert float(result.equity["equity"].iloc[-1]) == pytest.approx(
        result.initial_equity + float(result.trades["pnl"].sum()), abs=1e-9
    )


def test_a_nan_close_mid_hold_does_not_fabricate_a_drawdown(tmp_path):
    """BUG-005 scenario 3: one NaN close made max drawdown read 25.67% vs 0.029%.

    Two such bars flip the 35% gate on otherwise identical data.
    """
    from swing.backtest.metrics import max_drawdown

    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    clean = single_symbol_run(bars, cfg, end_bar=330)
    poisoned = single_symbol_run(set_bar(bars, 315, close=float("nan")), cfg, end_bar=330)

    assert_artifacts_are_finite(poisoned)
    clean_dd, _ = max_drawdown(clean.equity["equity"])
    poisoned_dd, _ = max_drawdown(poisoned.equity["equity"])
    assert poisoned_dd == pytest.approx(clean_dd, abs=0.05)
    assert poisoned_dd < 1.0
    # The mark simply carried at the last good close for one day, so the book
    # is worth exactly what it was worth yesterday — not zero, and not NaN.
    curve = poisoned.equity["equity"]
    assert float(curve.loc[bars.index[315]]) == pytest.approx(float(curve.loc[bars.index[314]]))


def test_a_nan_open_with_a_low_under_the_stop_does_not_fill_at_the_stop(tmp_path):
    """BUG-005 scenario 4: the gap-through branch was skipped and the fill landed
    AT the stop — a silent optimistic bias exactly when the data is least trustworthy."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    stops = expected_stops(bars, cfg)
    breach_bar = 320
    stop_level = stops[breach_bar - 1]
    poisoned = set_bar(bars, breach_bar, open=float("nan"), low=stop_level - 5.0)

    result = single_symbol_run(poisoned, cfg, end_bar=330)

    assert_artifacts_are_finite(result)
    # No exit is priced off a bar the engine cannot trust.
    if len(result.trades):
        assert result.trades.iloc[0]["exit_date"] != bars.index[breach_bar]


def test_a_non_finite_bar_is_reported_once_per_symbol(tmp_path, caplog):
    """One warning naming the symbol and the dates — not one per bar, not silence."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    poisoned = set_bar(set_bar(bars, 310, close=float("nan")), 315, high=float("nan"))

    with caplog.at_level(logging.WARNING, logger="swing.backtest.engine"):
        single_symbol_run(poisoned, cfg, end_bar=330)

    messages = [r.getMessage() for r in caplog.records if "not a finite number" in r.getMessage()]
    assert len(messages) == 1
    assert "AAA" in messages[0]
    assert "2021-03-11" in messages[0]  # first bad bar
    assert "2021-03-18" in messages[0]  # last bad bar


def test_a_non_finite_bar_cannot_produce_a_signal(tmp_path):
    """A bar the engine will not price is also a bar it will not act on."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    poisoned = set_bar(bars, SIGNAL_BAR, low=float("nan"))
    result = single_symbol_run(poisoned, cfg, end_bar=330)
    assert result.trades.empty
    assert_artifacts_are_finite(result)


# ---------------------------------------------------------------------------
# BUG-016 — a symbol whose bars end mid-run must not freeze its slot
# ---------------------------------------------------------------------------


def dead_symbol_universe(dies_at: int = 305, revive_signal: int = 320):
    """AAA signals, then stops printing; BBB signals later and needs the only slot."""
    aaa = spike_volume(ramp_bars(growth=0.0024), SIGNAL_BAR).iloc[: dies_at + 1]
    bbb = spike_volume(ramp_bars(growth=0.0018, base=80.0), revive_signal)
    return aaa, bbb


def test_a_dead_symbol_releases_its_slot_so_a_later_signal_can_trade(tmp_path, caplog):
    """BUG-016 repro: a position whose symbol stopped printing held the book's only
    slot for 4.5 months, and the later signal never traded at all."""
    cfg = engine_cfg(tmp_path, account={"max_positions": 1, "max_position_pct": 100.0})
    aaa, bbb = dead_symbol_universe()

    with caplog.at_level(logging.WARNING, logger="swing.backtest.engine"):
        result = run_engine(
            {"AAA": aaa, "BBB": bbb},
            ramp_bars(),
            cfg,
            start=bbb.index[WARMUP_BAR].date(),
            end=bbb.index[360].date(),
        )

    assert_artifacts_are_finite(result)
    # Both traded. Before the fix this was {"AAA"} and BBB never got in.
    assert set(result.trades["symbol"]) == {"AAA", "BBB"}

    dead = result.trades[result.trades["symbol"] == "AAA"].iloc[0]
    assert dead["exit_reason"] == EXIT_END_OF_DATA
    assert dead["exit_date"] == aaa.index[-1]  # its own last bar, not months later
    assert dead["exit_price"] == pytest.approx(float(aaa["close"].iloc[-1]))

    # The slot really is free in between: the equity curve and the trade list
    # now tell the same story about where the capital was.
    gap = result.equity.loc[bbb.index[306] : bbb.index[320]]
    assert len(gap) > 5
    assert int(gap["n_positions"].max()) == 0
    assert int(result.equity["n_positions"].max()) == 1

    warnings = [r.getMessage() for r in caplog.records if "stopped printing bars" in r.getMessage()]
    assert len(warnings) == 1
    assert "AAA" in warnings[0]


def test_a_dead_symbol_frees_its_capital_not_just_its_slot(tmp_path):
    """The other half of the same bug: the cash was locked up too.

    ``QUIET`` never signals; it is here only so the master calendar keeps
    running after AAA's last bar, which is the whole point of the scenario.
    """
    cfg = engine_cfg(tmp_path, account={"max_positions": 1, "max_position_pct": 100.0})
    aaa, _bbb = dead_symbol_universe()
    quiet = ramp_bars(base=40.0)
    result = run_engine(
        {"AAA": aaa, "QUIET": quiet},
        ramp_bars(),
        cfg,
        start=aaa.index[WARMUP_BAR].date(),
        end=quiet.index[360].date(),
    )
    assert list(result.trades["symbol"]) == ["AAA"]
    # From the bar after its last print onward the book is all cash.
    after = result.equity.loc[quiet.index[306] :]
    assert len(after) > 50
    assert (after["cash"] == after["equity"]).all()
    assert int(after["n_positions"].max()) == 0


def test_a_one_day_halt_is_not_mistaken_for_the_end_of_data(tmp_path):
    """A gap in the middle must still carry the position — the old, correct behaviour."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    halted = bars.drop(index=bars.index[310])
    result = run_engine(
        {"AAA": halted, "BBB": ramp_bars(base=60.0)},
        ramp_bars(),
        cfg,
        start=bars.index[WARMUP_BAR].date(),
        end=bars.index[330].date(),
    )
    assert len(result.trades) == 1
    assert result.trades.iloc[0]["exit_reason"] == EXIT_END_OF_DATA
    assert result.trades.iloc[0]["exit_date"] == bars.index[330]


# ---------------------------------------------------------------------------
# BUG-053 — a zero-share pick must be replaced, not wasted
# ---------------------------------------------------------------------------


def test_a_zero_share_pick_is_replaced_by_the_next_affordable_candidate(tmp_path):
    """BUG-053: ``ranked[:max_positions]`` could not replace a pick that sized to zero.

    Five candidates, two slots, and only the CHEAPEST (rank 5 by momentum) is
    affordable. With a bench exactly as deep as the slot count the engine only
    ever saw EEE and DDD, both unaffordable, and traded nothing at all — while
    ``engine.py``'s own contract promised "the next-ranked candidate gets it".
    """
    cfg = engine_cfg(
        tmp_path, account={"equity": 170.0, "max_positions": 2, "max_position_pct": 100.0}
    )
    bars = contested_universe()
    kwargs = {
        "start": bars["AAA"].index[WARMUP_BAR].date(),
        "end": bars["AAA"].index[310].date(),
    }

    result = run_engine(bars, ramp_bars(), cfg, **kwargs)

    assert list(result.trades["symbol"]) == ["AAA"]
    assert int(result.trades.iloc[0]["shares"]) == 1
    assert float(result.equity["cash"].min()) >= 0.0


def test_the_bench_never_exceeds_the_slot_count_in_open_positions(tmp_path):
    """A deeper bench must not become a deeper book."""
    cfg = engine_cfg(
        tmp_path, account={"equity": 400_000.0, "max_positions": 2, "max_position_pct": 50.0}
    )
    bars = contested_universe()
    result = run_engine(
        bars,
        ramp_bars(),
        cfg,
        start=bars["AAA"].index[WARMUP_BAR].date(),
        end=bars["AAA"].index[310].date(),
    )
    assert int(result.equity["n_positions"].max()) == 2
    assert len(result.trades) == 2


# ---------------------------------------------------------------------------
# BUG-003 residual — the engine must drop what the scanner drops
# ---------------------------------------------------------------------------


def test_a_non_finite_momentum_score_never_opens_a_position(tmp_path, monkeypatch):
    """BUG-003 residual: ``rank_candidates`` DROPS a non-finite score; the engine
    only sorted it last, so a free slot could still fill it.

    That is precisely the scanner/engine divergence the shared-rules design
    exists to prevent — and the deeper BUG-053 bench made it reachable.
    """
    real_score = scoring.momentum_score

    def scoreless_for_bad(bars, cfg, **kwargs):
        if float(bars["close"].iloc[0]) == pytest.approx(77.0):
            return pd.Series(float("nan"), index=bars.index, name="momentum_score")
        return real_score(bars, cfg, **kwargs)

    monkeypatch.setattr("swing.strategy.scoring.momentum_score", scoreless_for_bad)

    cfg = engine_cfg(tmp_path)  # four slots, only two candidates: no contention
    good = spike_volume(ramp_bars(base=100.0, growth=0.0020), SIGNAL_BAR)
    bad = spike_volume(ramp_bars(base=77.0, growth=0.0024), SIGNAL_BAR)

    result = run_engine(
        {"GOOD": good, "BAD": bad},
        ramp_bars(),
        cfg,
        start=good.index[WARMUP_BAR].date(),
        end=good.index[330].date(),
    )

    assert set(result.trades["symbol"]) == {"GOOD"}
    assert "BAD" not in set(result.trades["symbol"])
    assert_artifacts_are_finite(result)


# ---------------------------------------------------------------------------
# A12 — the earnings value may be a sequence of announcement dates
# ---------------------------------------------------------------------------


def test_an_earnings_history_sequence_blocks_every_date_it_names(tmp_path):
    """Contract amendment A12: the engine takes past AND future announcement dates.

    Also the hashability pin — a list value used to blow the SignalCache key up
    with ``TypeError: unhashable type: 'list'`` before a single bar was
    simulated.
    """
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    spy = ramp_bars()
    kwargs = {"start": bars.index[WARMUP_BAR].date(), "end": bars.index[330].date()}
    far_apart = (bars.index[100].date(), bars.index[200].date())
    straddling = (bars.index[100].date(), bars.index[SIGNAL_BAR + 5].date())

    clear = run_engine({"AAA": bars}, spy, cfg, earnings={"AAA": far_apart}, **kwargs)
    assert len(clear.trades) == 1

    blocked = run_engine({"AAA": bars}, spy, cfg, earnings={"AAA": straddling}, **kwargs)
    assert blocked.trades.empty

    # A plain list is the shape a provider actually returns; it must key too.
    as_list = run_engine({"AAA": bars}, spy, cfg, earnings={"AAA": list(straddling)}, **kwargs)
    pd.testing.assert_frame_equal(as_list.trades, blocked.trades)


def test_two_different_earnings_sequences_do_not_share_a_cache_entry(tmp_path):
    """Normalising to a tuple must not collapse distinct histories onto one key."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    spy = ramp_bars()
    kwargs = {"start": bars.index[WARMUP_BAR].date(), "end": bars.index[330].date()}
    cache = SignalCache()

    clear = run_engine(
        {"AAA": bars}, spy, cfg, earnings={"AAA": [bars.index[100].date()]}, cache=cache, **kwargs
    )
    blocked = run_engine(
        {"AAA": bars},
        spy,
        cfg,
        earnings={"AAA": [bars.index[100].date(), bars.index[SIGNAL_BAR + 5].date()]},
        cache=cache,
        **kwargs,
    )
    assert len(clear.trades) == 1
    assert blocked.trades.empty


# ---------------------------------------------------------------------------
# PERF-001 / PERF-004 / PERF-005 — caching is a speed knob and nothing else
# ---------------------------------------------------------------------------


def test_a_shared_cache_across_parameter_sets_changes_no_frame(tmp_path):
    """The determinism guard on the whole caching layer.

    Runs three parameter sets twice — once each with ``cache=None`` (every array,
    the calendar and the regime filter rebuilt from scratch) and once with all
    three sharing one cache, the way walk-forward drives it — and requires the
    trades, the equity curve and the position snapshots to match exactly.
    """
    bars = contested_universe()
    spy = ramp_bars()
    kwargs = {
        "start": bars["AAA"].index[WARMUP_BAR].date(),
        "end": bars["AAA"].index[330].date(),
    }
    cache = SignalCache()
    for overrides in ({"atr_stop_mult": 1.5}, {"atr_stop_mult": 2.5}, {"chandelier_mult": 3.5}):
        cfg = engine_cfg(
            tmp_path,
            account={"equity": 200_000.0, "max_position_pct": 20.0},
            strategy=overrides,
        )
        uncached = run_engine(bars, spy, cfg, **kwargs)
        cached = run_engine(bars, spy, cfg, cache=cache, **kwargs)
        pd.testing.assert_frame_equal(cached.trades, uncached.trades)
        pd.testing.assert_frame_equal(cached.equity, uncached.equity)
        assert cached.positions == uncached.positions
        assert cached.initial_equity == uncached.initial_equity
    assert cache.hits > 0


def test_the_cache_reports_its_size(tmp_path):
    """PERF-005: an unbounded cache with no size metric was ~520 MB per fold, unseen."""
    cfg = engine_cfg(tmp_path)
    bars = spike_volume(ramp_bars(), SIGNAL_BAR)
    cache = SignalCache()
    run_engine(
        {"AAA": bars},
        ramp_bars(),
        cfg,
        cache=cache,
        start=bars.index[WARMUP_BAR].date(),
        end=bars.index[330].date(),
    )
    stats = cache.stats()
    assert stats["bytes"] > 0
    assert stats["entries"] > 0
    assert stats["misses"] > 0
    assert stats["evictions"] == 0
    assert stats["max_bytes"] == 1 << 30


def test_a_byte_budget_is_enforced_and_changes_nothing(tmp_path):
    """PERF-005: eviction can only cost time — a re-miss rebuilds the same array."""
    cfg = engine_cfg(tmp_path, account={"equity": 200_000.0, "max_position_pct": 20.0})
    bars = contested_universe()
    kwargs = {
        "start": bars["AAA"].index[WARMUP_BAR].date(),
        "end": bars["AAA"].index[330].date(),
    }
    generous = run_engine(bars, ramp_bars(), cfg, cache=SignalCache(), **kwargs)

    tiny = SignalCache(max_bytes=50_000)
    squeezed = run_engine(bars, ramp_bars(), cfg, cache=tiny, **kwargs)

    pd.testing.assert_frame_equal(squeezed.trades, generous.trades)
    pd.testing.assert_frame_equal(squeezed.equity, generous.equity)
    assert tiny.evictions > 0
    assert tiny.stats()["bytes"] <= 50_000


def test_eviction_sheds_tuned_entries_before_grid_invariant_ones():
    """Throwing away a tuned entry costs one rebuild; a grid-invariant one costs 81."""
    payload = np.zeros(125, dtype="float64")  # exactly 1000 bytes each
    cache = SignalCache(max_bytes=3_000)
    for kind in ("trend", "entry", "atr"):
        cache.get((kind, "AAA"), payload.copy)
    assert cache.stats()["bytes"] == 3_000
    assert cache.evictions == 0

    cache.get(("chandelier", "AAA"), payload.copy)  # one over budget
    assert cache.evictions == 1
    # The two grid-invariant entries survived; the tuned one did not.
    before = cache.hits
    cache.get(("trend", "AAA"), payload.copy)
    cache.get(("atr", "AAA"), payload.copy)
    assert cache.hits == before + 2


def test_clearing_the_cache_resets_its_accounting():
    cache = SignalCache(max_bytes=1_000_000)
    cache.get(("trend", "AAA"), lambda: np.zeros(100))
    assert cache.stats()["bytes"] > 0
    cache.clear()
    assert cache.stats() == {
        "entries": 0,
        "hits": 0,
        "misses": 0,
        "evictions": 0,
        "bytes": 0,
        "max_bytes": 1_000_000,
    }
