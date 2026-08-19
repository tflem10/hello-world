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

__all__ = ["CAPPED_BY_VALUES", "SizeResult", "size_position"]

#: Every value ``SizeResult.capped_by`` is allowed to take (Contract 6).
#:
#: ``"risk"`` is part of the frozen contract's enum but is never emitted: when
#: plain risk sizing is what binds, ``capped_by`` is ``None``. It is listed here
#: so a consumer that switches on the enum stays exhaustive.
CAPPED_BY_VALUES: frozenset[str | None] = frozenset(
    {None, "risk", "position_cap", "cash", "unaffordable"}
)

#: Relative tolerance used when rounding a share count down.
#:
#: ``10 - 9.8`` is ``0.19999999999999996`` in binary floating point, so a risk
#: budget that divides *exactly* into 10 shares on paper can come out as
#: ``9.999999999999998`` and lose a share to a rounding artefact. Anything within
#: this tolerance of a whole number is treated as that whole number before the
#: floor is taken. It is far too small to change a genuine fractional result.
_FLOOR_TOLERANCE = 1e-9


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
            ceiling bit, ``"cash"`` when available cash bit, and
            ``"unaffordable"`` whenever the answer is zero shares, whichever
            stage got it there.
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
        ``affordable`` is always ``shares >= 1``.

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
