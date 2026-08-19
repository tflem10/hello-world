"""FROZEN CONTRACT 6 — how many shares to buy.

Position sizing is the only place in this system where a mistake is guaranteed
to cost money, so the arithmetic is deliberately boring and every branch is
named. The rule is Van Tharp's classic risk-first sizing, with two hard ceilings
bolted on top:

1. **Risk sizing.** Decide how many dollars you are willing to lose if the stop
   is hit — ``equity x risk_pct/100`` — and divide by the per-share loss
   ``entry - stop``. Round *down* to whole shares; you cannot buy a fraction of
   one, and rounding up would quietly exceed the risk budget.
2. **Notional cap.** No single position may be worth more than
   ``max_position_pct/100`` of equity, however tight its stop is. A stop half a
   percent away would otherwise justify a position several times the account.
3. **Cash cap.** You cannot spend money you do not have. This system is
   cash-only by design; no margin is ever assumed.

Ahead of all three sits a floor on the *divisor*. Risk-first sizing divides by
``entry - stop``, and on a pegged instrument — a halted stock, a merger target
sitting on the deal price — that distance shrinks toward zero while the arithmetic
happily returns thousands of shares against a few dollars of "risk". A stop a
fraction of a cent below the entry is not a real stop, it is a rounding artefact,
so anything under ``max(MIN_RISK_PER_SHARE_ABS, MIN_RISK_PER_SHARE_FRAC * entry)``
returns zero shares with ``capped_by="risk_floor"`` (audit BUG-054).

The caps are applied in that order and the smallest survivor wins. On a small
account most candidates come back with zero shares — that is the honest answer,
not a bug, and the scanner is expected to show those names as *watch* ideas
rather than picks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only needed for type checking
    from swing.config import Config

__all__ = [
    "CAPPED_BY_VALUES",
    "MIN_RISK_PER_SHARE_ABS",
    "MIN_RISK_PER_SHARE_FRAC",
    "SizeResult",
    "size_position",
]

#: Every value ``SizeResult.capped_by`` is allowed to take (Contract 6).
#:
#: ``"risk"`` is part of the frozen contract's enum but is never emitted: when
#: plain risk sizing is what binds, ``capped_by`` is ``None``. It is listed here
#: so a consumer that switches on the enum stays exhaustive.
#:
#: ``"risk_floor"`` was added by audit BUG-054 (spec amendment A6): the stop is
#: too close to the entry for the risk arithmetic to mean anything.
CAPPED_BY_VALUES: frozenset[str | None] = frozenset(
    {None, "risk", "position_cap", "cash", "unaffordable", "risk_floor"}
)

#: Smallest absolute per-share risk that counts as a real stop, in dollars.
#:
#: One cent is the tick size of every instrument this system trades, so a stop
#: closer than that to the entry cannot be filled where it claims to be.
MIN_RISK_PER_SHARE_ABS = 0.01

#: Smallest per-share risk as a fraction of the entry price — 0.1%.
#:
#: The absolute floor alone is too permissive on an expensive name: a 1-cent
#: stop on a $600 ETF still divides a real risk budget by almost nothing. The
#: two floors are combined with ``max``, so the binding one is whichever is
#: larger at that price (the fraction, above $10).
MIN_RISK_PER_SHARE_FRAC = 0.001

#: Relative tolerance used when rounding a share count down.
#:
#: ``10 - 9.8`` is ``0.19999999999999996`` in binary floating point, so a risk
#: budget that divides *exactly* into 10 shares on paper can come out as
#: ``9.999999999999998`` and lose a share to a rounding artefact. Anything within
#: this tolerance of a whole number is treated as that whole number before the
#: floor is taken. It is far too small to change a genuine fractional result.
_FLOOR_TOLERANCE = 1e-9

#: Relative tolerance used when comparing the risk per share against its floor.
#:
#: Same class of problem as ``_FLOOR_TOLERANCE``, same remedy. ``10.00 - 9.99``
#: is ``0.009999999999999787`` in binary floating point — two parts in 10^16
#: below a one-cent floor — so a stop exactly one tick away from a sub-$10 entry
#: would be refused by a strict ``<`` even though A6's intent is that a stop *at*
#: the floor is fine and only a tighter one is refused. Anything within this
#: relative distance of the floor counts as being at it, which is far too small
#: a band to admit a genuinely tighter stop (audit BUG-054 / A6, QA follow-up).
_RISK_FLOOR_TOLERANCE = 1e-9


@dataclass(frozen=True)
class SizeResult:
    """The outcome of sizing one candidate trade.

    Attributes:
        shares: whole shares to buy; ``0`` when the trade cannot be taken.
        risk_amount: dollars at risk if the stop fills, ``shares * (entry - stop)``.
        notional: dollars committed at entry, ``shares * entry``.
        affordable: ``True`` exactly when ``shares >= 1``.
        capped_by: which rule decided the final size — ``None`` when the risk
            budget alone did, ``"position_cap"`` when the per-position notional
            ceiling bit, ``"cash"`` when available cash bit, ``"risk_floor"``
            when the stop was too close to the entry to size against at all,
            and ``"unaffordable"`` whenever the answer is zero shares for any
            other reason, whichever stage got it there.
    """

    shares: int
    risk_amount: float
    notional: float
    affordable: bool
    capped_by: str | None


#: The single result returned for every trade that cannot be taken.
_UNAFFORDABLE = SizeResult(
    shares=0, risk_amount=0.0, notional=0.0, affordable=False, capped_by="unaffordable"
)

#: The result returned when ``entry - stop`` is below the risk-per-share floor.
_RISK_FLOOR = SizeResult(
    shares=0, risk_amount=0.0, notional=0.0, affordable=False, capped_by="risk_floor"
)


def _check_price(value: float, name: str) -> float:
    """Coerce a price to float, rejecting NaN/inf with a plain-English sentence."""
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"The {name} must be a number of dollars, but it is {value!r}.") from exc
    if not math.isfinite(price):
        raise ValueError(
            f"The {name} must be a real number of dollars, but it is {price}. "
            f"A missing price usually means the bar data has a gap on this day."
        )
    return price


def _floor_shares(value: float) -> int:
    """Round a share count down to a whole number, forgiving binary-float dust.

    Returns 0 for anything that is not a positive finite number.
    """
    if not math.isfinite(value) or value <= 0.0:
        return 0
    nearest = round(value)
    if abs(value - nearest) <= _FLOOR_TOLERANCE * max(1.0, abs(value)):
        return int(nearest)
    return int(math.floor(value))


def size_position(
    equity: float,
    cash: float,
    entry: float,
    stop: float,
    cfg: Config,
) -> SizeResult:
    """Decide how many whole shares to buy, risk first and capped twice.

    The calculation, in order:

    0. Risk floor: ``entry - stop`` must be at least
       ``max(MIN_RISK_PER_SHARE_ABS, MIN_RISK_PER_SHARE_FRAC * entry)``,
       otherwise there is no meaningful distance to size against and the answer
       is zero shares with ``capped_by="risk_floor"``. A distance exactly on the
       floor passes; the comparison forgives binary dust the same way the share
       floor does, so a one-tick stop is never lost to it.
    1. ``risk_shares = floor(equity * risk_pct/100 / (entry - stop))``.
       If that is less than one share the trade is unaffordable and everything
       returns zero — no cap is even consulted.
    2. Notional cap: ``shares * entry <= max_position_pct/100 * equity``.
    3. Cash cap: ``shares * entry <= cash``.

    The final share count is the smallest of the three. ``capped_by`` names the
    binding constraint: ``None`` if the risk budget alone decided it, otherwise
    the cap that cut it — and when both caps land on the same number, the
    notional cap is reported, since it is applied first and is the constraint
    that survives a cash top-up.

    Negative ``equity`` or ``cash`` are treated as zero rather than rejected: a
    margin-debit balance is a real thing a broker will report, and the correct
    answer to "how much can I buy" is then "nothing", not a crash.

    Args:
        equity: total account equity in dollars — the base for both the risk
            budget and the notional cap.
        cash: settled cash actually available to spend, in dollars.
        entry: intended entry price per share; must be above 0 and above ``stop``.
        stop: initial stop price per share; must be at or above 0 and below ``entry``.
        cfg: the full :class:`~swing.config.Config`; ``cfg.account.risk_pct`` and
            ``cfg.account.max_position_pct`` are read.

    Returns:
        A :class:`SizeResult`. ``shares`` is always a whole number and
        ``affordable`` is always ``shares >= 1``. A stop closer to the entry
        than the risk floor comes back as zero shares with
        ``capped_by="risk_floor"`` rather than a five-figure position sized
        against fictional risk (audit BUG-054).

    Raises:
        ValueError: if ``entry`` is not above 0, if ``stop`` is negative, if
            ``entry`` is not above ``stop`` (there would be no risk to divide
            by, or the "stop" would be a profit target), or if any argument is
            NaN or infinite. Every message is a complete sentence naming the
            offending number.
    """
    equity = _check_price(equity, "account equity")
    cash = _check_price(cash, "available cash")
    entry = _check_price(entry, "entry price")
    stop = _check_price(stop, "stop price")

    if entry <= 0.0:
        raise ValueError(
            f"The entry price must be greater than 0, but it is {entry}. "
            f"A share cannot be bought for nothing."
        )
    if stop < 0.0:
        raise ValueError(
            f"The stop price cannot be negative, but it is {stop}. "
            f"The worst a long position can do is go to zero."
        )
    if entry <= stop:
        raise ValueError(
            f"The entry price ({entry}) must be above the stop price ({stop}). "
            f"A stop at or above the entry is not a stop — there would be no "
            f"downside to size against."
        )

    equity = max(equity, 0.0)
    cash = max(cash, 0.0)

    risk_per_share = entry - stop
    risk_floor = max(MIN_RISK_PER_SHARE_ABS, MIN_RISK_PER_SHARE_FRAC * entry)
    if risk_per_share < risk_floor * (1.0 - _RISK_FLOOR_TOLERANCE):
        # Sub-tick "risk" is a rounding artefact, not a stop: dividing the risk
        # budget by it sizes a position that the stop cannot actually protect
        # (audit BUG-054). The tolerance keeps a stop that is exactly one tick
        # away — binary dust and all — on the allowed side of the comparison.
        return _RISK_FLOOR

    risk_budget = equity * (cfg.account.risk_pct / 100.0)
    risk_shares = _floor_shares(risk_budget / risk_per_share)

    if risk_shares < 1:
        return _UNAFFORDABLE

    position_cap_shares = _floor_shares(equity * (cfg.account.max_position_pct / 100.0) / entry)
    cash_shares = _floor_shares(cash / entry)

    shares = min(risk_shares, position_cap_shares, cash_shares)
    if shares < 1:
        return _UNAFFORDABLE

    capped_by: str | None = None
    if shares < risk_shares:
        # Order matters on a tie: the notional cap is applied first and is the
        # constraint that would still bite after a cash deposit.
        capped_by = "position_cap" if position_cap_shares <= cash_shares else "cash"

    return SizeResult(
        shares=shares,
        risk_amount=shares * risk_per_share,
        notional=shares * entry,
        affordable=True,
        capped_by=capped_by,
    )
