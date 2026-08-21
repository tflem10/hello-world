"""Position sizing: risk-first, whole shares, hard caps.

The rule is the ordinary one — risk a fixed fraction of equity per trade, where
"risk" is the distance from entry to the initial stop:

    shares = floor(equity * risk_pct / (entry - stop))

then clip that by a per-position notional cap and by available cash.

Three things make this less trivial than the formula suggests:

1. **Whole shares only.** The Schwab retail Trader API does not accept
   fractional quantities, so ``floor`` is not a rounding preference, it is the
   API contract. On a small account the floor frequently lands on zero.
2. **Zero shares is a legitimate answer.** At $100 of equity with a 2% risk
   budget you are risking $2 per trade; a $60 stock with a $4 stop needs
   0.5 shares. The honest output is "cannot take this trade yet", surfaced as a
   watch-list entry — not a silently rounded-up 1 share that risks 4% instead
   of 2%.
3. **Which constraint bound matters.** The pick sheet reports it, because
   "capped by the 25% position limit" and "cannot afford one share" call for
   completely different responses from the user.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


class SizingLimit(StrEnum):
    """Which constraint determined the final share count."""

    RISK = "risk"                      # the intended, normal case
    POSITION_CAP = "position_cap"      # max_position_pct bound
    BUYING_POWER = "buying_power"      # not enough cash
    UNAFFORDABLE = "unaffordable"      # cannot buy even one share
    INVALID = "invalid"                # bad inputs (stop above entry, etc.)


@dataclass(frozen=True)
class PositionSize:
    shares: int
    entry: float
    stop: float
    limit: SizingLimit
    risk_budget: float                 # dollars we were willing to lose
    notes: list[str] = field(default_factory=list)

    @property
    def affordable(self) -> bool:
        return self.shares >= 1

    @property
    def risk_per_share(self) -> float:
        return max(self.entry - self.stop, 0.0)

    @property
    def risk_dollars(self) -> float:
        """Actual dollars at risk if the stop fills exactly at the stop price."""
        return self.shares * self.risk_per_share

    @property
    def notional(self) -> float:
        return self.shares * self.entry

    def equity_pct(self, equity: float) -> float:
        return self.notional / equity if equity > 0 else 0.0

    def risk_pct(self, equity: float) -> float:
        return self.risk_dollars / equity if equity > 0 else 0.0

    def describe(self, equity: float) -> str:
        if not self.affordable:
            return f"0 shares - {self.notes[0] if self.notes else self.limit.value}"
        return (
            f"{self.shares} sh @ {self.entry:.2f} = ${self.notional:,.2f} "
            f"({self.equity_pct(equity):.1%} of equity), "
            f"risk ${self.risk_dollars:,.2f} ({self.risk_pct(equity):.2%})"
            + (f" [capped by {self.limit.value}]" if self.limit != SizingLimit.RISK else "")
        )


def size_position(
    entry: float,
    stop: float,
    equity: float,
    risk_pct: float,
    max_position_pct: float = 1.0,
    available_cash: float | None = None,
) -> PositionSize:
    """Compute a whole-share position size.

    ``available_cash`` defaults to ``equity`` (fully investable). Pass the real
    cash balance when sizing alongside existing open positions.
    """
    notes: list[str] = []
    cash = equity if available_cash is None else available_cash

    if entry <= 0:
        return PositionSize(0, entry, stop, SizingLimit.INVALID, 0.0,
                            [f"entry price must be positive (got {entry})"])
    if stop >= entry:
        return PositionSize(0, entry, stop, SizingLimit.INVALID, 0.0,
                            [f"stop {stop:.2f} is not below entry {entry:.2f}"])
    if equity <= 0 or risk_pct <= 0:
        return PositionSize(0, entry, stop, SizingLimit.INVALID, 0.0,
                            ["equity and risk_pct must both be positive"])

    risk_budget = equity * risk_pct
    risk_per_share = entry - stop

    by_risk = math.floor(risk_budget / risk_per_share)
    by_cap = math.floor((equity * max_position_pct) / entry)
    by_cash = math.floor(max(cash, 0.0) / entry)

    shares = min(by_risk, by_cap, by_cash)
    if shares < 0:
        shares = 0

    if shares < 1:
        # Explain *why* zero, in the order the user can act on.
        if by_cash < 1:
            reason = (
                f"one share costs ${entry:,.2f}; available cash is ${cash:,.2f}"
            )
            limit = SizingLimit.BUYING_POWER
        elif by_cap < 1:
            reason = (
                f"one share (${entry:,.2f}) exceeds the "
                f"{max_position_pct:.0%} per-position cap "
                f"(${equity * max_position_pct:,.2f})"
            )
            limit = SizingLimit.POSITION_CAP
        else:
            reason = (
                f"risk budget ${risk_budget:,.2f} buys "
                f"{risk_budget / risk_per_share:.2f} shares at "
                f"${risk_per_share:.2f} of risk per share; "
                "taking 1 whole share would risk "
                f"${risk_per_share:,.2f} "
                f"({risk_per_share / equity:.1%} of equity)"
            )
            limit = SizingLimit.UNAFFORDABLE
        notes.append(reason)
        return PositionSize(0, entry, stop, limit, risk_budget, notes)

    if shares == by_risk and by_risk <= min(by_cap, by_cash):
        limit = SizingLimit.RISK
    elif shares == by_cap and by_cap < by_cash:
        limit = SizingLimit.POSITION_CAP
        notes.append(
            f"risk budget allowed {by_risk} shares; the "
            f"{max_position_pct:.0%} position cap allows {by_cap}"
        )
    else:
        limit = SizingLimit.BUYING_POWER
        notes.append(f"risk budget allowed {by_risk} shares; cash allows {by_cash}")

    return PositionSize(shares, entry, stop, limit, risk_budget, notes)


def size_from_atr(
    entry: float,
    atr_value: float,
    stop_atr_mult: float,
    equity: float,
    risk_pct: float,
    max_position_pct: float = 1.0,
    available_cash: float | None = None,
) -> tuple[PositionSize, float]:
    """Convenience wrapper: derive the stop from ATR, then size. Returns (size, stop)."""
    if atr_value is None or atr_value <= 0 or not math.isfinite(atr_value):
        return (
            PositionSize(0, entry, 0.0, SizingLimit.INVALID, 0.0,
                         ["ATR is unavailable or non-positive; cannot place a stop"]),
            0.0,
        )
    stop = entry - stop_atr_mult * atr_value
    return (
        size_position(entry, stop, equity, risk_pct, max_position_pct, available_cash),
        stop,
    )
