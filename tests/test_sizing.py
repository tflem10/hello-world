"""AC6 — position sizing: whole shares, risk first, both caps honoured.

The headline case this system has to get right is the small account. At $100 of
equity and 2.5% risk there are only $2.50 to lose on a trade, so a $230 stock
with an $8 stop cannot be bought at all — the honest answer is zero shares and a
*watch* tag, not a rounded-up single share that risks three times the budget.
Those cases are pinned first, then the caps, then the arithmetic invariants that
must hold for every input.
"""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from swing.config import Config
from swing.strategy.sizing import CAPPED_BY_VALUES, SizeResult, size_position

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(cfg_factory: Any) -> Config:
    """Default account: $100 equity, 2.5% risk, 25% max per position."""
    return cfg_factory()


def account_cfg(cfg_factory: Any, **account: float) -> Config:
    """A config with the [account] section overridden."""
    return cfg_factory(account=account)


# ---------------------------------------------------------------------------
# the $100 account: the case this system actually runs in
# ---------------------------------------------------------------------------


def test_expensive_stock_is_unaffordable_on_a_hundred_dollar_account(cfg: Config) -> None:
    """AAPL-like: $2.50 of risk budget against an $8 stop buys nothing."""
    result = size_position(equity=100.0, cash=100.0, entry=230.0, stop=222.0, cfg=cfg)
    assert result == SizeResult(
        shares=0, risk_amount=0.0, notional=0.0, affordable=False, capped_by="unaffordable"
    )


def test_cheap_etf_is_affordable_on_a_hundred_dollar_account(cfg: Config) -> None:
    """$2.50 of budget against a $1.50 stop is 1.67 shares, floored to 1."""
    result = size_position(equity=100.0, cash=100.0, entry=20.0, stop=18.5, cfg=cfg)
    assert result.shares == 1
    assert result.affordable is True
    assert result.capped_by is None
    assert result.risk_amount == pytest.approx(1.5)
    assert result.notional == pytest.approx(20.0)


def test_a_hundred_dollar_account_rejects_most_of_the_sp500(cfg: Config) -> None:
    """Anything whose stop is wider than $2.50 is out of reach, whatever it costs."""
    for entry, stop in [(230.0, 222.0), (500.0, 480.0), (95.0, 90.0), (30.0, 26.0)]:
        result = size_position(equity=100.0, cash=100.0, entry=entry, stop=stop, cfg=cfg)
        assert result.affordable is False, f"{entry}/{stop} should be unaffordable"
        assert result.capped_by == "unaffordable"


def test_five_hundred_dollar_account_scales_up(cfg_factory: Any) -> None:
    """5x the equity is 5x the risk budget — but the notional cap arrives first."""
    cfg = account_cfg(cfg_factory, equity=500.0, risk_pct=2.5, max_position_pct=25.0)
    result = size_position(equity=500.0, cash=500.0, entry=20.0, stop=18.5, cfg=cfg)
    # risk: 12.50 / 1.50 = 8.33 -> 8 shares; notional cap: 25% of 500 = $125 -> 6 shares
    assert result.shares == 6
    assert result.capped_by == "position_cap"
    assert result.notional == pytest.approx(120.0)
    assert result.risk_amount == pytest.approx(9.0)


def test_equity_argument_wins_over_the_configured_equity(cfg: Config) -> None:
    """cfg.account.equity is a default for the CLI; the live number is the argument."""
    assert cfg.account.equity == 100.0
    result = size_position(equity=50_000.0, cash=50_000.0, entry=20.0, stop=18.5, cfg=cfg)
    assert result.shares > 1


# ---------------------------------------------------------------------------
# the caps
# ---------------------------------------------------------------------------


def test_notional_cap_binds_when_the_stop_is_tight(cfg_factory: Any) -> None:
    """A half-percent stop would justify 500 shares; 25% of equity allows 25."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=2.5, max_position_pct=25.0)
    result = size_position(equity=10_000.0, cash=10_000.0, entry=100.0, stop=99.5, cfg=cfg)
    # risk: 250 / 0.50 = 500 shares; notional cap: 25% of 10,000 = $2,500 -> 25 shares
    assert result.shares == 25
    assert result.capped_by == "position_cap"
    assert result.notional == pytest.approx(2_500.0)
    assert result.risk_amount == pytest.approx(12.5)


def test_cash_cap_binds_when_the_account_is_mostly_invested(cfg_factory: Any) -> None:
    """Three positions already open: the notional cap allows 25 shares, cash allows 8."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=2.5, max_position_pct=25.0)
    result = size_position(equity=10_000.0, cash=800.0, entry=100.0, stop=99.5, cfg=cfg)
    assert result.shares == 8
    assert result.capped_by == "cash"
    assert result.notional == pytest.approx(800.0)


def test_when_both_caps_land_on_the_same_number_the_notional_cap_is_reported(
    cfg_factory: Any,
) -> None:
    """Tie-break: the notional cap is applied first and survives a cash deposit."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=2.5, max_position_pct=25.0)
    result = size_position(equity=10_000.0, cash=2_500.0, entry=100.0, stop=99.5, cfg=cfg)
    assert result.shares == 25
    assert result.capped_by == "position_cap"


def test_caps_that_reduce_the_size_to_zero_report_unaffordable(cfg_factory: Any) -> None:
    """Risk sizing said 500 shares; $50 of cash says none. Zero is always unaffordable."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=2.5, max_position_pct=25.0)
    result = size_position(equity=10_000.0, cash=50.0, entry=100.0, stop=99.5, cfg=cfg)
    assert result == SizeResult(
        shares=0, risk_amount=0.0, notional=0.0, affordable=False, capped_by="unaffordable"
    )


def test_a_tiny_notional_cap_can_zero_the_position(cfg_factory: Any) -> None:
    cfg = account_cfg(cfg_factory, equity=1_000.0, risk_pct=5.0, max_position_pct=1.0)
    # notional cap is 1% of $1,000 = $10, which does not buy one $100 share
    result = size_position(equity=1_000.0, cash=1_000.0, entry=100.0, stop=99.0, cfg=cfg)
    assert result.shares == 0
    assert result.capped_by == "unaffordable"


def test_no_cap_reported_when_risk_sizing_alone_decides(cfg_factory: Any) -> None:
    """capped_by is None — not "risk" — when the risk budget is what binds."""
    cfg = account_cfg(cfg_factory, equity=100_000.0, risk_pct=1.0, max_position_pct=100.0)
    result = size_position(equity=100_000.0, cash=100_000.0, entry=50.0, stop=45.0, cfg=cfg)
    # risk: 1,000 / 5 = 200 shares; caps allow 2,000 -> risk wins
    assert result.shares == 200
    assert result.capped_by is None


def test_a_cap_landing_exactly_on_the_risk_size_is_not_reported(cfg_factory: Any) -> None:
    """capped_by is only set when a cap actually *reduced* the size."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=5.0, max_position_pct=50.0)
    # risk: 500 / 5 = 100 shares; notional cap: $5,000 / $50 = 100 shares — a dead heat
    result = size_position(equity=10_000.0, cash=10_000.0, entry=50.0, stop=45.0, cfg=cfg)
    assert result.shares == 100
    assert result.capped_by is None


# ---------------------------------------------------------------------------
# rounding: whole shares, floored, without losing one to binary dust
# ---------------------------------------------------------------------------


def test_risk_of_exactly_one_share_is_affordable(cfg: Config) -> None:
    """$2.50 of budget against a $2.50 stop is exactly 1.0 shares — take it."""
    result = size_position(equity=100.0, cash=100.0, entry=20.0, stop=17.5, cfg=cfg)
    assert result.shares == 1
    assert result.affordable is True
    assert result.risk_amount == pytest.approx(2.5)


def test_a_hair_more_risk_than_one_share_is_unaffordable(cfg: Config) -> None:
    """One cent wider and 0.996 shares floors to nothing. No rounding up, ever."""
    result = size_position(equity=100.0, cash=100.0, entry=20.0, stop=17.49, cfg=cfg)
    assert result.shares == 0
    assert result.affordable is False


@pytest.mark.parametrize(
    "stop,expected",
    [
        (17.50, 1),  # exactly 1.0 shares
        (17.49, 0),  # 0.996 shares
        (18.75, 2),  # exactly 2.0 shares
        (18.74, 1),  # 1.98 shares
        (19.00, 2),  # 2.5 shares
        (19.50, 5),  # exactly 5.0 shares
    ],
)
def test_floor_behaviour_at_the_boundaries(cfg_factory: Any, stop: float, expected: int) -> None:
    """Isolates the floor: the caps are opened wide so only risk sizing decides."""
    cfg = account_cfg(cfg_factory, equity=100.0, risk_pct=2.5, max_position_pct=100.0)
    result = size_position(equity=100.0, cash=1_000.0, entry=20.0, stop=stop, cfg=cfg)
    assert result.shares == expected


def test_binary_float_dust_does_not_cost_a_share(cfg_factory: Any) -> None:
    """``100 * 3% / (10 - 9.7)`` is 9.999999999999977 in binary; that is 10 shares.

    A plain ``math.floor`` would hand back 9 here and quietly under-risk the
    trade because 0.3 is not representable — the kind of bug that never shows up
    in a backtest summary but is wrong on every single fill.
    """
    cfg = account_cfg(cfg_factory, equity=100.0, risk_pct=3.0, max_position_pct=100.0)
    raw = 100.0 * 0.03 / (10.0 - 9.7)
    assert math.floor(raw) == 9, "fixture no longer exercises the dust case"

    result = size_position(equity=100.0, cash=1_000.0, entry=10.0, stop=9.7, cfg=cfg)
    assert result.shares == 10


@pytest.mark.parametrize(
    "equity,risk_pct,entry,stop,expected",
    [
        (100.0, 1.5, 12.0, 11.7, 5),
        (100.0, 2.0, 5.0, 4.8, 10),
        (100.0, 1.0, 4.0, 3.9, 10),
        (100.0, 2.5, 4.0, 3.9, 25),
        (100.0, 2.0, 4.0, 3.9, 20),
    ],
)
def test_dust_tolerance_across_several_known_cases(
    cfg_factory: Any, equity: float, risk_pct: float, entry: float, stop: float, expected: int
) -> None:
    """Each case divides evenly on paper but lands just under a whole share in binary.

    The notional cap is deliberately non-binding in every case (``expected *
    entry <= equity``), so a failure here can only mean the floor is wrong.
    """
    cfg = account_cfg(cfg_factory, equity=equity, risk_pct=risk_pct, max_position_pct=100.0)
    raw = equity * (risk_pct / 100.0) / (entry - stop)
    assert math.floor(raw) == expected - 1, "case no longer exercises binary dust"
    assert expected * entry <= equity, "notional cap must not bind in this case"

    result = size_position(equity=equity, cash=1e9, entry=entry, stop=stop, cfg=cfg)
    assert result.shares == expected


def test_shares_are_always_a_python_int(cfg: Config) -> None:
    result = size_position(equity=100.0, cash=100.0, entry=20.0, stop=18.5, cfg=cfg)
    assert isinstance(result.shares, int)
    assert not isinstance(result.shares, bool)


# ---------------------------------------------------------------------------
# degenerate but legal inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cash", [0.0, -0.01, -5_000.0])
def test_no_cash_means_no_position(cfg_factory: Any, cash: float) -> None:
    """A margin debit is a real balance; the answer is 'nothing', not a crash."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=2.5)
    result = size_position(equity=10_000.0, cash=cash, entry=100.0, stop=99.5, cfg=cfg)
    assert result.shares == 0
    assert result.capped_by == "unaffordable"


@pytest.mark.parametrize("equity", [0.0, -1_000.0])
def test_no_equity_means_no_risk_budget(cfg: Config, equity: float) -> None:
    result = size_position(equity=equity, cash=10_000.0, entry=20.0, stop=18.5, cfg=cfg)
    assert result.shares == 0
    assert result.capped_by == "unaffordable"


def test_a_zero_stop_is_allowed(cfg_factory: Any) -> None:
    """Risking the whole share price is legal, if reckless: risk per share = entry."""
    cfg = account_cfg(cfg_factory, equity=10_000.0, risk_pct=2.5, max_position_pct=100.0)
    result = size_position(equity=10_000.0, cash=10_000.0, entry=10.0, stop=0.0, cfg=cfg)
    assert result.shares == 25  # 250 / 10
    assert result.risk_amount == pytest.approx(250.0)


def test_a_penny_stock_with_a_penny_stop(cfg_factory: Any) -> None:
    """A one-cent stop justifies thousands of shares; cash is what actually stops it."""
    cfg = account_cfg(cfg_factory, equity=1_000.0, risk_pct=2.0, max_position_pct=100.0)
    result = size_position(equity=1_000.0, cash=500.0, entry=1.0, stop=0.99, cfg=cfg)
    # risk: $20 / $0.01 = 2,000 shares; notional cap: 1,000 shares; cash: 500 shares
    assert result.shares == 500
    assert result.capped_by == "cash"


# ---------------------------------------------------------------------------
# errors — every message a plain-English sentence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry,stop", [(20.0, 20.0), (20.0, 20.01), (20.0, 100.0)])
def test_stop_at_or_above_entry_is_rejected(cfg: Config, entry: float, stop: float) -> None:
    with pytest.raises(ValueError, match="must be above the stop price"):
        size_position(equity=100.0, cash=100.0, entry=entry, stop=stop, cfg=cfg)


@pytest.mark.parametrize("entry", [0.0, -0.01, -20.0])
def test_non_positive_entry_is_rejected(cfg: Config, entry: float) -> None:
    with pytest.raises(ValueError, match="entry price must be greater than 0"):
        size_position(equity=100.0, cash=100.0, entry=entry, stop=-50.0, cfg=cfg)


def test_negative_stop_is_rejected(cfg: Config) -> None:
    with pytest.raises(ValueError, match="stop price cannot be negative"):
        size_position(equity=100.0, cash=100.0, entry=20.0, stop=-1.0, cfg=cfg)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["equity", "cash", "entry", "stop"])
def test_non_finite_inputs_are_rejected(cfg: Config, field: str, bad: float) -> None:
    """A NaN close would otherwise sail through and size a position at zero shares."""
    kwargs: dict[str, float] = {"equity": 100.0, "cash": 100.0, "entry": 20.0, "stop": 18.5}
    kwargs[field] = bad
    with pytest.raises(ValueError, match="real number of dollars"):
        size_position(cfg=cfg, **kwargs)


def test_error_messages_are_complete_sentences(cfg: Config) -> None:
    with pytest.raises(ValueError) as excinfo:
        size_position(equity=100.0, cash=100.0, entry=20.0, stop=25.0, cfg=cfg)
    message = str(excinfo.value)
    assert message[0].isupper()
    assert message.rstrip().endswith(".")
    assert "20.0" in message and "25.0" in message


# ---------------------------------------------------------------------------
# invariants that must hold for every input
# ---------------------------------------------------------------------------

GRID = [
    (equity, cash, entry, stop, risk_pct, cap_pct)
    for equity, cash in [(100.0, 100.0), (500.0, 500.0), (10_000.0, 2_000.0), (25_000.0, 25_000.0)]
    for entry, stop in [(20.0, 18.5), (230.0, 222.0), (100.0, 99.5), (7.25, 6.8), (55.0, 50.0)]
    for risk_pct in (0.5, 2.5, 10.0)
    for cap_pct in (5.0, 25.0, 100.0)
]


@pytest.mark.parametrize("equity,cash,entry,stop,risk_pct,cap_pct", GRID)
def test_invariants_hold_across_the_grid(
    cfg_factory: Any,
    equity: float,
    cash: float,
    entry: float,
    stop: float,
    risk_pct: float,
    cap_pct: float,
) -> None:
    """Whole shares, both caps respected, and the risk budget never exceeded.

    The tolerances are one part in a million: they exist only so the deliberate
    binary-dust forgiveness in the share floor cannot trip an invariant, and are
    far tighter than a single cent on any of these numbers.
    """
    cfg = account_cfg(cfg_factory, equity=equity, risk_pct=risk_pct, max_position_pct=cap_pct)
    result = size_position(equity=equity, cash=cash, entry=entry, stop=stop, cfg=cfg)

    assert isinstance(result, SizeResult)
    assert isinstance(result.shares, int)
    assert result.shares >= 0
    assert result.affordable == (result.shares >= 1)
    assert result.capped_by in CAPPED_BY_VALUES

    assert result.risk_amount == pytest.approx(result.shares * (entry - stop))
    assert result.notional == pytest.approx(result.shares * entry)

    if result.shares == 0:
        assert result.capped_by == "unaffordable"
        assert result.risk_amount == 0.0
        assert result.notional == 0.0
        return

    assert result.capped_by != "unaffordable"
    assert result.risk_amount <= equity * (risk_pct / 100.0) * (1 + 1e-6)
    assert result.notional <= equity * (cap_pct / 100.0) * (1 + 1e-6)
    assert result.notional <= cash * (1 + 1e-6)


@pytest.mark.parametrize("equity,cash,entry,stop,risk_pct,cap_pct", GRID)
def test_sizing_is_deterministic(
    cfg_factory: Any,
    equity: float,
    cash: float,
    entry: float,
    stop: float,
    risk_pct: float,
    cap_pct: float,
) -> None:
    cfg = account_cfg(cfg_factory, equity=equity, risk_pct=risk_pct, max_position_pct=cap_pct)
    args = {"equity": equity, "cash": cash, "entry": entry, "stop": stop, "cfg": cfg}
    assert size_position(**args) == size_position(**args)


@pytest.mark.parametrize("risk_pct", [0.5, 1.0, 2.5, 5.0, 10.0])
def test_more_risk_never_buys_fewer_shares(cfg_factory: Any, risk_pct: float) -> None:
    """Monotonicity in the risk budget, holding the caps wide open."""
    cfg_low = account_cfg(cfg_factory, equity=50_000.0, risk_pct=0.5, max_position_pct=100.0)
    cfg_high = account_cfg(cfg_factory, equity=50_000.0, risk_pct=risk_pct, max_position_pct=100.0)
    args = {"equity": 50_000.0, "cash": 1e9, "entry": 50.0, "stop": 45.0}
    assert size_position(cfg=cfg_high, **args).shares >= size_position(cfg=cfg_low, **args).shares


@pytest.mark.parametrize("cash", [0.0, 100.0, 1_000.0, 10_000.0, 1e9])
def test_more_cash_never_buys_fewer_shares(cfg_factory: Any, cash: float) -> None:
    cfg = account_cfg(cfg_factory, equity=50_000.0, risk_pct=2.5, max_position_pct=100.0)
    args = {"equity": 50_000.0, "entry": 50.0, "stop": 45.0, "cfg": cfg}
    baseline = size_position(cash=0.0, **args).shares
    assert size_position(cash=cash, **args).shares >= baseline


def test_a_wider_stop_never_buys_more_shares(cfg_factory: Any) -> None:
    """More risk per share must mean fewer shares — the whole point of the method."""
    cfg = account_cfg(cfg_factory, equity=50_000.0, risk_pct=2.5, max_position_pct=100.0)
    previous = None
    for stop in (49.0, 48.0, 45.0, 40.0, 25.0, 1.0):
        shares = size_position(equity=50_000.0, cash=1e9, entry=50.0, stop=stop, cfg=cfg).shares
        if previous is not None:
            assert shares <= previous
        previous = shares


# ---------------------------------------------------------------------------
# the value object itself
# ---------------------------------------------------------------------------


def test_size_result_is_frozen(cfg: Config) -> None:
    result = size_position(equity=100.0, cash=100.0, entry=20.0, stop=18.5, cfg=cfg)
    with pytest.raises(FrozenInstanceError):
        result.shares = 99  # type: ignore[misc]


def test_capped_by_enum_matches_the_frozen_contract() -> None:
    """Contract 6 froze this enum; a consumer switching on it must stay exhaustive."""
    assert set(CAPPED_BY_VALUES) == {None, "risk", "position_cap", "cash", "unaffordable"}
