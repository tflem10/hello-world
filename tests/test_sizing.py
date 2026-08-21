"""Whole-share position sizing, caps, and the unaffordable path.

The unaffordable cases are the ones that actually matter at $100 of equity, so
they get the most coverage. Rounding up to "1 share" would silently multiply
the risk budget, and every test here exists to make sure that never happens.
"""

from __future__ import annotations

import math

import pytest

from swing.strategy.sizing import SizingLimit, size_from_atr, size_position


# ---------------------------------------------------------------------------
# the normal case
# ---------------------------------------------------------------------------
def test_risk_determines_share_count():
    # $10,000 equity, 2% risk = $200. Entry 50, stop 45 -> $5/share -> 40 shares.
    out = size_position(entry=50.0, stop=45.0, equity=10_000.0, risk_pct=0.02)
    assert out.shares == 40
    assert out.limit is SizingLimit.RISK
    assert out.risk_dollars == pytest.approx(200.0)
    assert out.risk_pct(10_000.0) == pytest.approx(0.02)


def test_shares_are_floored_never_rounded_up():
    # $200 budget / $6 per share = 33.33 -> 33, not 34.
    out = size_position(entry=50.0, stop=44.0, equity=10_000.0, risk_pct=0.02)
    assert out.shares == 33
    assert out.risk_dollars <= 10_000.0 * 0.02


def test_a_tighter_stop_buys_more_shares_for_the_same_risk():
    wide = size_position(entry=50.0, stop=40.0, equity=10_000.0, risk_pct=0.02)
    tight = size_position(entry=50.0, stop=48.0, equity=10_000.0, risk_pct=0.02)
    assert tight.shares > wide.shares
    assert tight.risk_dollars == pytest.approx(wide.risk_dollars, abs=10.0)


# ---------------------------------------------------------------------------
# caps
# ---------------------------------------------------------------------------
def test_position_cap_binds_before_risk_on_a_tight_stop():
    # Risk allows 200 shares ($200 / $1); 25% of $10k / $50 = 50 shares.
    out = size_position(
        entry=50.0, stop=49.0, equity=10_000.0, risk_pct=0.02, max_position_pct=0.25
    )
    assert out.shares == 50
    assert out.limit is SizingLimit.POSITION_CAP
    assert out.notional == pytest.approx(2_500.0)
    assert "position cap" in out.notes[0]


def test_buying_power_cap_binds_when_cash_is_short():
    out = size_position(
        entry=50.0, stop=45.0, equity=10_000.0, risk_pct=0.02,
        max_position_pct=1.0, available_cash=500.0,
    )
    assert out.shares == 10
    assert out.limit is SizingLimit.BUYING_POWER


def test_caps_never_increase_the_share_count():
    unconstrained = size_position(50.0, 45.0, 10_000.0, 0.02, max_position_pct=1.0)
    constrained = size_position(50.0, 45.0, 10_000.0, 0.02, max_position_pct=0.10)
    assert constrained.shares <= unconstrained.shares


# ---------------------------------------------------------------------------
# the small-account reality
# ---------------------------------------------------------------------------
def test_hundred_dollar_account_cannot_take_a_sixty_dollar_stock():
    out = size_position(
        entry=60.0, stop=56.0, equity=100.0, risk_pct=0.02, max_position_pct=0.25
    )
    assert out.shares == 0
    assert not out.affordable
    assert out.limit is SizingLimit.POSITION_CAP
    assert "$25.00" in out.notes[0]           # tells the user what the cap was


def test_zero_shares_when_one_share_would_blow_the_risk_budget():
    # $100 equity, 2% risk = $2. Entry $10, stop $9 -> $1/share risk buys 2
    # shares... but the 25% cap allows only 2 shares of $10 = $20 > $25? No:
    # cap allows 2 shares. So relax the cap and make the risk the binder.
    out = size_position(
        entry=10.0, stop=5.0, equity=100.0, risk_pct=0.02, max_position_pct=1.0
    )
    assert out.shares == 0
    assert out.limit is SizingLimit.UNAFFORDABLE
    assert "would risk" in out.notes[0]


def test_unaffordable_message_names_the_cash_shortfall():
    out = size_position(
        entry=500.0, stop=450.0, equity=100.0, risk_pct=0.03,
        max_position_pct=1.0, available_cash=100.0,
    )
    assert out.shares == 0
    assert out.limit is SizingLimit.BUYING_POWER
    assert "available cash" in out.notes[0]


def test_sizing_scales_with_the_account():
    """The same setup goes from untradable at $100 to tradable at $500."""
    setup = dict(entry=60.0, stop=54.0, risk_pct=0.03, max_position_pct=0.25)
    at_100 = size_position(equity=100.0, **setup)
    at_500 = size_position(equity=500.0, **setup)
    at_5000 = size_position(equity=5_000.0, **setup)
    assert at_100.shares == 0
    assert at_500.shares >= 1
    assert at_5000.shares > at_500.shares


def test_describe_is_human_readable_in_both_branches():
    ok = size_position(20.0, 18.0, 5_000.0, 0.02, 0.25)
    assert "sh @" in ok.describe(5_000.0) and "risk $" in ok.describe(5_000.0)
    bad = size_position(600.0, 560.0, 100.0, 0.02, 0.25)
    assert bad.describe(100.0).startswith("0 shares - ")


# ---------------------------------------------------------------------------
# invalid inputs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        dict(entry=0.0, stop=-1.0, equity=1000.0, risk_pct=0.02),
        dict(entry=-5.0, stop=-10.0, equity=1000.0, risk_pct=0.02),
        dict(entry=50.0, stop=50.0, equity=1000.0, risk_pct=0.02),   # zero risk
        dict(entry=50.0, stop=55.0, equity=1000.0, risk_pct=0.02),   # stop above entry
        dict(entry=50.0, stop=45.0, equity=0.0, risk_pct=0.02),
        dict(entry=50.0, stop=45.0, equity=1000.0, risk_pct=0.0),
    ],
)
def test_invalid_inputs_return_zero_shares_not_an_exception(kwargs):
    out = size_position(**kwargs)
    assert out.shares == 0
    assert out.limit is SizingLimit.INVALID
    assert out.notes


def test_negative_cash_is_treated_as_zero():
    out = size_position(50.0, 45.0, 10_000.0, 0.02, available_cash=-100.0)
    assert out.shares == 0


# ---------------------------------------------------------------------------
# ATR-derived stops
# ---------------------------------------------------------------------------
def test_size_from_atr_derives_the_stop():
    size, stop = size_from_atr(
        entry=100.0, atr_value=2.0, stop_atr_mult=2.0,
        equity=10_000.0, risk_pct=0.02, max_position_pct=1.0,
    )
    assert stop == pytest.approx(96.0)
    assert size.shares == 50                 # $200 / $4
    assert size.risk_dollars == pytest.approx(200.0)


@pytest.mark.parametrize("bad_atr", [0.0, -1.0, float("nan"), None])
def test_size_from_atr_refuses_without_a_usable_atr(bad_atr):
    size, stop = size_from_atr(100.0, bad_atr, 2.0, 10_000.0, 0.02)
    assert size.shares == 0
    assert size.limit is SizingLimit.INVALID
    assert stop == 0.0


def test_a_wider_atr_means_fewer_shares():
    calm, _ = size_from_atr(100.0, 1.0, 2.0, 10_000.0, 0.02, 1.0)
    wild, _ = size_from_atr(100.0, 5.0, 2.0, 10_000.0, 0.02, 1.0)
    assert calm.shares > wild.shares
    # ...but roughly the same dollars at risk. That is the entire point of
    # volatility-normalised sizing.
    assert calm.risk_dollars == pytest.approx(wild.risk_dollars, abs=20.0)


def test_risk_never_exceeds_the_budget_across_a_grid():
    equity, risk_pct = 25_000.0, 0.02
    for entry in (5.0, 17.5, 60.0, 250.0, 1_000.0):
        for atr_value in (0.05, 0.5, 3.0, 25.0):
            size, stop = size_from_atr(
                entry, atr_value, 2.0, equity, risk_pct, max_position_pct=0.25
            )
            if not size.affordable:
                continue
            assert size.risk_dollars <= equity * risk_pct + 1e-9
            assert size.notional <= equity * 0.25 + 1e-9
            assert stop < entry
            assert size.shares == math.floor(size.shares)
