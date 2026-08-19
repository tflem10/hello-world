# Strategy Specification — Trend-Momentum Core

The normative description of the strategy implemented by `swing`. Every rule below names its exact
configuration key from `Config` (SPEC Contract 1) and the function that implements it (SPEC
Contract 7). Where this document and the code disagree, that is a bug in one of them — file it.

Evidence for each component, with citations, lives in [`indicator-research.md`](indicator-research.md).
Backtest mechanics, cost model and validation live in [`backtest-methodology.md`](backtest-methodology.md).

> This document specifies a mechanical system. It is not investment advice and recommends no
> security. Nothing here predicts prices.

---

## 1. Design rationale

### Why long-only

1. **Broker and account reality.** The target account is a small cash Schwab account
   (`account.equity` starts at `100.0`, target `500.0`). Short selling requires margin, borrow
   availability, and carries unbounded loss — none of which is appropriate at this size, and some of
   which is simply unavailable.
2. **Asymmetric evidence.** The trend/momentum literature the system rests on is materially stronger
   on the long side for equities. Daniel & Moskowitz (2016) show momentum's crash risk is
   concentrated in the *short* leg during panic-state rebounds — the losers-basket rockets. Removing
   the short leg removes most of the documented tail risk.
3. **Structural drift.** Equity indices have a positive unconditional drift. A long-only trend system
   is betting *with* that drift and is flat when the bet is off; a short book is fighting it.
4. **Operational simplicity.** No locate, no hard-to-borrow fees, no dividend liability, no
   assignment risk. Fewer failure modes in an automated pipeline.

The cost is real and stated: the system holds cash — earning nothing in this model — through every
bear market. That is visible in the `exposure_pct` metric in every backtest report.

### Why 1–8 weeks

1. **It is where the evidence and the costs meet.** Below ~1 week, the cost model
   (5 bps + `spread_atr_frac` × ATR per side) consumes an increasing share of expected move, and the
   documented short-horizon edges have attenuated most (see `indicator-research.md` §5). Above ~8
   weeks, the position stops being a swing trade and becomes an unmanaged position competing for one
   of only four slots.
2. **Capital turnover on a four-slot book.** With `account.max_positions = 4`, holding period *is*
   the throughput constraint. At an 8-week cap, four slots turn over roughly 26 position-slots per
   year. Doubling the hold halves the number of independent bets and therefore widens the confidence
   interval on every conclusion the backtest reaches.
3. **The indicator windows are chosen to match it.** `donchian_window = 20` (≈4 weeks) sits in the
   middle of the range; `time_stop_days = 40` (≈8 weeks) *defines* the upper bound; the 63-day
   momentum leg (≈3 months) is the shortest lookback that is still longer than the hold, which is
   what keeps the ranking a *condition* measure rather than an echo of the trade itself.
4. **Human-in-the-loop cadence.** One scan at 17:30 ET and one confirmation at 09:00 ET per day is a
   sustainable operating rhythm. Intraday horizons are not.

---

## 2. Pipeline overview

Evaluated once per trading day, `asof` a specific date, in this order. Every stage is a filter; a
symbol that fails any stage is dropped and not evaluated further.

| Stage | What it does | Implementation |
|-------|--------------|----------------|
| 0 | Load and de-duplicate the universe | `swing.universe.load(cfg)` |
| 1 | **Regime gate** — global, entry-only | `swing.strategy.regime.entries_allowed` |
| 2 | **Liquidity filter** — price and dollar volume | `swing.strategy.rules.liquidity_ok` |
| 3 | **Trend template** — Minervini-style stage-2 gate | `swing.strategy.rules.trend_template` |
| 4 | **Entry signal** — Donchian breakout + volume confirm | `swing.strategy.rules.entry_signal` |
| 5 | **Earnings blackout** — block entries near known events | `swing.strategy.rules.earnings_blackout` |
| 6 | **Ranking** — risk-adjusted momentum, 52-week-high tiebreak | `swing.strategy.scoring.rank_candidates` |
| 7 | **Fundamentals soft filter** — stocks only | `swing.strategy.rules.fundamentals_ok` |
| 8 | **Slot allocation and sizing** | `swing.strategy.sizing.size_position` |
| 9 | Split into `picks` (affordable) and `watch` (not) | `swing.alerts.pipeline.run_scan` |

Ranking (6) precedes the fundamentals filter (7) because `fundamentals_ok` takes a
`rank_below_median` argument and therefore needs the score distribution to already exist.

**Vectorisation contract.** Stages 1–5 are implemented as functions returning `pd.Series` aligned to
the bar index, not scalars. The live scanner reads `.iloc[-1]`; the backtest engine reads the value
at any historical date. The same code path serves both — this is the single most important property
for keeping the backtest honest about what the scanner will actually do.

---

## 3. Stage 1 — Regime gate

```
swing.strategy.regime.entries_allowed(spy_bars, cfg) -> pd.Series[bool]
```

**Rule.** New entries are allowed on date *t* if and only if the regime symbol's close on *t* is
strictly greater than its `regime.sma_window`-day simple moving average on *t*.

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Regime gate enabled | `regime.enabled` | `True` |
| Regime reference symbol | `regime.symbol` | `"SPY"` |
| Regime SMA window (trading days) | `regime.sma_window` | `200` |

**Semantics.**

- When `regime.enabled` is `False`, the gate returns all-`True` and imposes no constraint.
- The gate is **entry-only**. Positions already open are never liquidated because the regime flipped;
  they are managed exclusively by their own stops (§8). This is deliberate — see
  `indicator-research.md` §4.
- During the SMA warm-up (first `regime.sma_window` bars) the value is `NaN` and entries are **not**
  allowed. Fail-closed, because an unknown regime is not a permissive one.
- A failed regime gate does **not** suppress the scan report. `run_scan` still writes the full report
  directory with `"regime_ok": false` and an empty `picks` list, so the operator can see the system
  is working and why it is quiet (SPEC Contract 9).

---

## 4. Stage 2 — Liquidity filter

```
swing.strategy.rules.liquidity_ok(bars, cfg, *, is_etf: bool) -> pd.Series[bool]
```

**Rule.** Both conditions must hold on date *t*:

1. `close(t) >= strategy.min_price`
2. Average dollar volume over the trailing `strategy.volume_avg_window` bars
   ≥ `strategy.min_dollar_volume`, where dollar volume on a bar is `close × volume`.

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Minimum close price (USD) | `strategy.min_price` | `5.0` |
| Minimum avg daily dollar volume (USD) | `strategy.min_dollar_volume` | `5_000_000.0` |
| Averaging window (trading days) | `strategy.volume_avg_window` | `50` |

**Semantics.** Applied identically to stocks and ETFs (`is_etf` does not relax it). Warm-up bars are
`False`. This runs before the trend template so that expensive rolling computations are never spent
on untradeable names.

---

## 5. Stage 3 — Trend template

```
swing.strategy.rules.trend_template(bars, cfg, *, is_etf: bool) -> pd.Series[bool]
```

All seven conditions must hold simultaneously on date *t*. Let `C = close(t)`,
`F = SMA(close, strategy.sma_fast)`, `M = SMA(close, strategy.sma_mid)`,
`S = SMA(close, strategy.sma_slow)`, all evaluated at *t*.

| # | Condition | Config keys |
|---|-----------|-------------|
| T1 | `C > M` **and** `C > S` | `strategy.sma_mid`, `strategy.sma_slow` |
| T2 | `M > S` | `strategy.sma_mid`, `strategy.sma_slow` |
| T3 | `S(t) > S(t − strategy.sma_slow_rising_days)` — the slow SMA is rising | `strategy.sma_slow_rising_days` |
| T4 | `F > M` **and** `F > S` | `strategy.sma_fast` |
| T5 | `C > F` | `strategy.sma_fast` |
| T6 | `C >= strategy.min_above_low_mult × low52w(t)` | `strategy.min_above_low_mult` |
| T7 | `C >= high52w(t) × (1 − strategy.max_below_high_pct / 100)` | `strategy.max_below_high_pct` |
| T8 | `ADX(strategy.atr_window)(t) >= strategy.adx_min` | `strategy.adx_min`, `strategy.atr_window` |

| Parameter | Config key | Default | Meaning |
|-----------|-----------|---------|---------|
| Fast SMA | `strategy.sma_fast` | `50` | trading days |
| Mid SMA | `strategy.sma_mid` | `150` | trading days |
| Slow SMA | `strategy.sma_slow` | `200` | trading days |
| Slow-SMA rising lookback | `strategy.sma_slow_rising_days` | `21` | ≈ 1 calendar month |
| Min multiple of 52-week low | `strategy.min_above_low_mult` | `1.25` | 25% above the low |
| Max % below 52-week high | `strategy.max_below_high_pct` | `25.0` | within 25% of the high |
| Min ADX | `strategy.adx_min` | `20.0` | Wilder's "trending" boundary |
| ADX / ATR smoothing period | `strategy.atr_window` | `14` | shared with ATR |

**Definitions.** `low52w(t)` and `high52w(t)` are the rolling minimum of `low` and rolling maximum of
`high` over the trailing 252 trading days ending at *t* inclusive. 252 is a fixed constant, not a
config key — "52-week" is definitional, not tunable.

**ETF relaxed path.** The template applies to ETFs in full, including T8. What `is_etf=True` relaxes
is **only** the fundamentals soft filter of §9, which is skipped entirely. ETFs get no relief on
trend, liquidity, entry, earnings, ranking, sizing, or exits.

**Warm-up.** The template requires at least `max(strategy.sma_slow, 252) + strategy.sma_slow_rising_days`
bars of history. Symbols with less are `False` throughout and are excluded from `rank_candidates`
("only includes symbols with enough history", Contract 7).

**Deviation from Minervini.** His eighth criterion (IBD Relative Strength rank ≥ 70) is not
implemented; the momentum score of §8 does that job at the ranking stage. See
`indicator-research.md` §3.

---

## 6. Stage 4 — Entry signal

```
swing.strategy.rules.entry_signal(bars, cfg) -> pd.Series[bool]
```

**Rule.** Both conditions must hold on date *t*:

1. **Breakout or proximity.**
   `close(t) >= donchian_high(bars, strategy.donchian_window)(t) × (1 − strategy.breakout_proximity_pct / 100)`

   `donchian_high` is the rolling maximum of `high` over the prior `strategy.donchian_window` bars,
   **shifted by one** so it never includes bar *t* itself (Contract 5). Comparing today's close
   against a channel that already contains today's high would be look-ahead within the bar.

2. **Volume confirmation.**
   `volume(t) >= strategy.volume_mult × SMA(volume, strategy.volume_avg_window)(t)`

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Donchian channel lookback | `strategy.donchian_window` | `20` |
| Breakout proximity tolerance (%) | `strategy.breakout_proximity_pct` | `2.0` |
| Volume confirmation multiple | `strategy.volume_mult` | `1.3` |
| Volume averaging window | `strategy.volume_avg_window` | `50` |

**Why proximity rather than a strict break.** The scan is a nightly batch and fills happen at the
*next open* (`backtest-methodology.md` §2). A tick-perfect close-above-the-channel requirement makes
selection a coin-flip on the closing print while we still pay the overnight gap. The 2% tolerance
admits candidates that are credibly at the top of their range. Setting
`strategy.breakout_proximity_pct = 0.0` restores the strict Turtle rule.

---

## 7. Stage 5 — Earnings blackout

```
swing.strategy.rules.earnings_blackout(index, earnings, cfg) -> pd.Series[bool]   # True = BLOCKED
```

**Rule.** For a symbol with a known next earnings date `E`, bar *t* is **blocked** when

```
0 <= (E − t) <= strategy.earnings_blackout_days      # calendar days
```

That is: entries are blocked from `strategy.earnings_blackout_days` calendar days before the
announcement up to and including the announcement date. Bars after `E` are not blocked — the gap risk
being avoided has already occurred.

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Blackout window before earnings (calendar days) | `strategy.earnings_blackout_days` | `10` |

**Unknown-date path — normative.** When the provider returns `None` for a symbol
(`DataProvider.earnings_dates` may return `date | None`, Contract 3), `earnings_blackout` returns an
**all-`False`** Series — nothing is blocked — and the condition is propagated, not swallowed:

1. The resulting `PickRecord` carries `earnings_known = False` (Contract 8).
2. `run_scan` renders a visible warning on the pick in `picks.md` / `picks.html` and includes it in
   the notification body. The operator sees "earnings date unknown" next to the symbol.
3. The pick is **not** suppressed.

This is **fail-open with a loud warning**, chosen deliberately over fail-closed. Free earnings-date
coverage is incomplete in a way that correlates with company size, so failing closed would silently
delete a large, non-random slice of the universe — and would do so invisibly inside the backtest,
where there is no operator to notice. See `indicator-research.md` §12.

**Known limitation.** A 1–8 week hold against a quarterly announcement cycle means many positions
will carry through an earnings release regardless. The blackout stops us *opening* into a known
event; it does not make the system earnings-neutral. Contract 11 permits an optional
earnings-tighten exit; it is not enabled by default and has no config key in v1.

---

## 8. Stage 6 — Ranking

```
swing.strategy.scoring.momentum_score(bars, cfg) -> pd.Series[float]
swing.strategy.scoring.rank_candidates(bars_by_symbol, asof, cfg) -> pd.DataFrame
```

### 8.1 Score formula

Let `ATRpct(t) = 100 × ATR(bars, strategy.atr_window)(t) / close(t)`.

Let `ROC(n)(t) = 100 × (close(t) / close(t − n) − 1)` (percent, Contract 5).

Let `ROC_skip(n, k)(t) = 100 × (close(t − k) / close(t − k − n) − 1)` — the same rate of change
measured over the `n` bars ending `k` bars ago.

```
score(t) =  strategy.mom_weight_126 × ROC_skip(126, strategy.mom_skip_days)(t) / ATRpct(t)
          + strategy.mom_weight_63  × ROC(63)(t)                              / ATRpct(t)
```

With defaults:

```
score(t) = 0.6 × ROC(126, skip 5)(t) / ATRpct(t)  +  0.4 × ROC(63)(t) / ATRpct(t)
```

| Parameter | Config key | Default | Meaning |
|-----------|-----------|---------|---------|
| Weight on the ~6-month leg | `strategy.mom_weight_126` | `0.6` | 126 trading days ≈ 6 months |
| Weight on the ~3-month leg | `strategy.mom_weight_63` | `0.4` | 63 trading days ≈ 3 months |
| Skip on the ~6-month leg | `strategy.mom_skip_days` | `5` | ≈ 1 week, avoids short-term reversal |
| ATR window for `ATRpct` | `strategy.atr_window` | `14` | shared with stops and ADX |

Notes:

- The lookback lengths **126** and **63** are constants of the formula, not config keys. Only the
  weights and the skip are tunable.
- The skip applies to the 126-day leg only. The 63-day leg is unskipped.
- Weights are not normalised by the code. They sum to 1.0 at defaults; if a user sets them otherwise
  the score is scaled accordingly, which is harmless because the score is used only for **ordering**.
- Division by `ATRpct` is the risk adjustment. Without it the ranking systematically selects the
  highest-volatility names in the universe, which on a whole-share small account is the worst
  possible bias (§10).
- Requires ≥ `126 + strategy.mom_skip_days + strategy.atr_window` bars of history; shorter series
  yield `NaN` and are excluded from `rank_candidates`.

### 8.2 `rank_candidates` output

A `DataFrame` indexed by symbol with columns:

| Column | Type | Meaning |
|--------|------|---------|
| `score` | float | the value above, evaluated at `asof` |
| `atr` | float | `ATR(strategy.atr_window)` at `asof`, in dollars |
| `close` | float | close at `asof` |
| `high_prox` | float | 0..1, `1.0` = at the 52-week high |
| `rank` | int | 1 = best |

`high_prox(t) = close(t) / high52w(t)`, clipped to `[0, 1]`.

**Sort order — normative.** `score` descending, **tiebreak `high_prox` descending**. Any residual tie
is broken by symbol ascending, so the ordering is total and deterministic (required for the
byte-identical-rerun property, AC9). The 52-week-high tiebreak is the George & Hwang effect applied
where it has real discriminating power; it is deliberately *not* a primary sort key
(`indicator-research.md` §2).

---

## 9. Stage 7 — Fundamentals soft filter

```
swing.strategy.rules.fundamentals_ok(f, rank_below_median, cfg) -> bool
```

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Filter enabled | `strategy.fundamentals_filter` | `True` |

**Rule.** Returns `True` (pass) in every case except one. A candidate is rejected **only** when
*all* of the following hold:

1. `strategy.fundamentals_filter` is `True`, **and**
2. the instrument is a stock (ETFs skip this stage entirely — `is_etf` relaxed path), **and**
3. `f` is not `None` and both `f.eps_growth` and `f.revenue_growth` are not `None`, **and**
4. both `f.eps_growth < 0` and `f.revenue_growth < 0`, **and**
5. `rank_below_median` is `True` — the candidate scored below the median of the current candidate set.

**Semantics, stated plainly.**

- **Missing data always passes.** `f is None`, or either growth field `None`, is a pass. Free
  fundamentals coverage is patchy and stale; treating absence as failure would encode a data-vendor
  artefact as a trading rule.
- It is a **disqualifier for the clearly deteriorating**, not a selector for the best. A strong chart
  on a merely mediocre business is not excluded.
- Condition 5 means a top-half candidate is never rejected on fundamentals. The filter can only
  break ties against the weaker half of an already-qualified set.
- This is the weakest gate in the system and is expected to have little measurable effect. It exists
  as a sanity guard, not as an edge. See `indicator-research.md` §11.

---

## 10. Stage 8 — Slot allocation and position sizing

```
swing.strategy.sizing.size_position(equity, cash, entry, stop, cfg) -> SizeResult
```

### 10.1 Slot allocation

Candidates surviving stages 1–7 are taken in `rank` order. The number of new positions opened on a
day is limited to `account.max_positions − (currently open positions)`. When more candidates qualify
than there are free slots, the higher-ranked ones win — this is the only place the momentum score
does any work.

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Maximum concurrent positions | `account.max_positions` | `4` |

The backtest engine applies the identical rule (Contract 11: "entries ranked by score when slots are
contested").

### 10.2 Sizing formula

Given account `equity`, available `cash`, the intended `entry` price and the initial `stop`:

```
risk_budget    = equity × account.risk_pct / 100
risk_per_share = entry − stop                        # = strategy.atr_stop_mult × ATR
shares_risk    = floor(risk_budget / risk_per_share)

notional_cap   = equity × account.max_position_pct / 100
shares_cap     = floor(notional_cap / entry)
shares_cash    = floor(cash / entry)

shares         = min(shares_risk, shares_cap, shares_cash)
```

If `shares < 1`: `SizeResult(shares=0, affordable=False, capped_by="unaffordable")`.

Otherwise `capped_by` records which constraint bound, in this precedence:
`"risk"` if `shares == shares_risk`, else `"position_cap"` if `shares == shares_cap`, else `"cash"`;
`None` when no constraint bound below the risk figure.

`notional = shares × entry`; `risk_amount = shares × risk_per_share`.

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Account equity (USD) | `account.equity` | `100.0` |
| Risk per trade (% of equity) | `account.risk_pct` | `2.5` |
| Max notional per position (% of equity) | `account.max_position_pct` | `25.0` |
| Max concurrent positions | `account.max_positions` | `4` |

**Whole shares only.** No fractional shares. `floor` everywhere. Four positions at 25% each means a
fully invested book is 100% of equity — no leverage is possible by construction.

**Which equity.** `size_position` takes `equity` as an argument, so the same formula serves both
callers with different capital: `swing scan` passes the live `account.equity`, while the backtest
engine passes its simulated running equity seeded from `backtest.initial_equity` (`10_000.0`) and
never reads `account.equity` — see [`backtest-methodology.md` §4.1](backtest-methodology.md#41-reference-capital)
for why strategy validation deliberately uses fixed reference capital. The percentages
(`account.risk_pct`, `account.max_position_pct`) and `account.max_positions` apply identically in
both cases.

### 10.3 What this actually means at $100 equity (the live scan)

This subsection describes the **live** path only — `swing scan` with `account.equity = 100.0`.
Backtests do not run at this capital (§10.2, "Which equity").

At `equity = 100.0`: `risk_budget = $2.50`, `notional_cap = $25.00`.

For one share to fit, `entry ≤ $25`. Combined with `strategy.min_price = 5.0`, **the affordable band
at $100 equity is roughly $5–$25 per share** — a small and unrepresentative slice of the S&P
1500 + ETF universe. The notional cap binds far more often than the risk formula.

Worked examples (all with `strategy.atr_stop_mult = 2.0`, so `risk_per_share = 2 × ATR`):

| Equity | Entry | ATR | Stop | Risk/sh | `shares_risk` | `shares_cap` | **shares** | `capped_by` |
|--------|-------|-----|------|---------|---------------|--------------|------------|-------------|
| $100 | $8.00 | $0.25 | $7.50 | $0.50 | 5 | 3 | **3** | `position_cap` |
| $100 | $18.00 | $0.60 | $16.80 | $1.20 | 2 | 1 | **1** | `position_cap` |
| $100 | $42.00 | $1.50 | $39.00 | $3.00 | 0 | 0 | **0** | `unaffordable` |
| $500 | $18.00 | $0.60 | $16.80 | $1.20 | 10 | 6 | **6** | `position_cap` |
| $500 | $42.00 | $1.50 | $39.00 | $3.00 | 4 | 2 | **2** | `position_cap` |
| $500 | $180.00 | $4.00 | $172.00 | $8.00 | 1 | 0 | **0** | `unaffordable` |

### 10.4 Unaffordable → watch list

A candidate that passes every gate but sizes to zero shares is **not discarded**. It is emitted as a
`PickRecord` with `kind = "watch"` and `shares = 0`, written to the `watch` array of `picks.json`
(Contract 9) and shown in a separate section of the pick sheet.

Rationale: at $100 equity most qualifying candidates are unaffordable, and a scan that silently
produced nothing would be indistinguishable from a broken scan. The watch list makes the system's
constraint visible, gives the operator something to act on if they add capital, and preserves a
record of what the strategy *would* have taken — which matters when reconciling live results against
the backtest, since the backtest does not apply the $100 affordability constraint retroactively.

`kind = "watch"` records are excluded from order drafting (Contract 10) and from all execution
guardrail accounting (Contract 12).

---

## 11. Exits

Three exits are evaluated for every open position. Whichever triggers first wins. All are evaluated
on close and executed at the next open, with gap-through handling per `backtest-methodology.md` §2.

### 11.1 Initial stop

```
swing.strategy.rules.initial_stop(bars, cfg) -> pd.Series[float]
initial_stop(t) = close(t) − strategy.atr_stop_mult × ATR(bars, strategy.atr_window)(t)
```

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Initial stop ATR multiple | `strategy.atr_stop_mult` | `2.0` |
| ATR window | `strategy.atr_window` | `14` |

Fixed at the signal bar and stored on the `PickRecord` as `stop`. It is the denominator of the sizing
formula, so it must be known before the position exists. It never moves down; it is superseded by
the chandelier once the chandelier rises above it.

### 11.2 Chandelier trailing stop (ratcheted)

```
swing.strategy.rules.chandelier_stop(bars, cfg) -> pd.Series[float]
chandelier_stop(t) = rolling_max(close)(t) − strategy.chandelier_mult × ATR(bars, strategy.atr_window)(t)
```

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Chandelier ATR multiple | `strategy.chandelier_mult` | `3.0` |

**The ratchet lives in the consumer, not the rule** (Contract 7 is explicit: "NO ratchet; consumers
ratchet per-position"). `chandelier_stop` returns an unratcheted rolling series so the same function
is usable at any date. The backtest engine and the live position manager each maintain, per position:

```
running_max(t)  = max(close(entry_bar) … close(t))
raw_chand(t)    = running_max(t) − strategy.chandelier_mult × ATR(t)
effective(t)    = max(effective(t−1), raw_chand(t), initial_stop_at_entry)
```

`effective` is **monotone non-decreasing** for the life of the position. A falling ATR can raise it;
nothing can lower it. The running maximum is over closes since entry, not over the whole series.

The chandelier is wider than the initial stop by design: the initial stop asks "was I wrong
immediately?", the trailing stop asks "is the trend over?" and needs room to not be shaken out by
normal retracement.

### 11.3 Time stop

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Maximum holding period (trading days) | `strategy.time_stop_days` | `40` |

A position open for `strategy.time_stop_days` **trading days** since its fill bar is closed at the
next open regardless of P&L. 40 trading days ≈ 8 weeks — the stated upper bound of the holding
horizon, so this parameter is definitional rather than optimised.

**There is no "off" value.** `strategy.time_stop_days` is validated as `>= 1`, and `0` would in any
case mean "exit on the entry bar" under the engine's `hold_days >= time_stop_days` test, not
"never exit". To disable the time stop, set a horizon longer than any run — the `time_stop_off`
ablation variant uses `10_000` trading days (`scripts/ablations.py`).

### 11.4 Exit precedence

On any bar where more than one exit condition is met, precedence is:

1. **Gap through a stop at the open** — filled at the open price, worse than the stop
   (`backtest-methodology.md` §2). This dominates because it happens before any other evaluation.
2. **Stop hit** — `max(initial, effective chandelier)`.
3. **Time stop.**

A position is never opened and closed on the same bar.

---

## 12. Complete parameter index

Every field of `StrategyCfg`, `RegimeCfg` and the risk-relevant `AccountCfg` fields, with the section
that specifies it.

| Config key | Default | Section |
|------------|---------|---------|
| `account.equity` | `100.0` | [§10 Sizing](#10-stage-8--slot-allocation-and-position-sizing) |
| `account.risk_pct` | `2.5` | [§10 Sizing](#10-stage-8--slot-allocation-and-position-sizing) |
| `account.max_positions` | `4` | [§10 Sizing](#10-stage-8--slot-allocation-and-position-sizing) |
| `account.max_position_pct` | `25.0` | [§10 Sizing](#10-stage-8--slot-allocation-and-position-sizing) |
| `strategy.min_price` | `5.0` | [§4 Liquidity](#4-stage-2--liquidity-filter) |
| `strategy.min_dollar_volume` | `5_000_000.0` | [§4 Liquidity](#4-stage-2--liquidity-filter) |
| `strategy.sma_fast` | `50` | [§5 Trend template](#5-stage-3--trend-template) |
| `strategy.sma_mid` | `150` | [§5 Trend template](#5-stage-3--trend-template) |
| `strategy.sma_slow` | `200` | [§5 Trend template](#5-stage-3--trend-template) |
| `strategy.sma_slow_rising_days` | `21` | [§5 Trend template](#5-stage-3--trend-template) T3 |
| `strategy.min_above_low_mult` | `1.25` | [§5 Trend template](#5-stage-3--trend-template) T6 |
| `strategy.max_below_high_pct` | `25.0` | [§5 Trend template](#5-stage-3--trend-template) T7 |
| `strategy.adx_min` | `20.0` | [§5 Trend template](#5-stage-3--trend-template) T8 |
| `strategy.donchian_window` | `20` | [§6 Entry](#6-stage-4--entry-signal) |
| `strategy.breakout_proximity_pct` | `2.0` | [§6 Entry](#6-stage-4--entry-signal) |
| `strategy.volume_mult` | `1.3` | [§6 Entry](#6-stage-4--entry-signal) |
| `strategy.volume_avg_window` | `50` | [§4 Liquidity](#4-stage-2--liquidity-filter), [§6 Entry](#6-stage-4--entry-signal) |
| `strategy.atr_window` | `14` | [§5](#5-stage-3--trend-template), [§8](#8-stage-6--ranking), [§11](#11-exits) |
| `strategy.atr_stop_mult` | `2.0` | [§11.1 Initial stop](#111-initial-stop) |
| `strategy.chandelier_mult` | `3.0` | [§11.2 Chandelier](#112-chandelier-trailing-stop-ratcheted) |
| `strategy.time_stop_days` | `40` | [§11.3 Time stop](#113-time-stop) |
| `strategy.earnings_blackout_days` | `10` | [§7 Earnings blackout](#7-stage-5--earnings-blackout) |
| `strategy.mom_weight_126` | `0.6` | [§8.1 Score formula](#81-score-formula) |
| `strategy.mom_weight_63` | `0.4` | [§8.1 Score formula](#81-score-formula) |
| `strategy.mom_skip_days` | `5` | [§8.1 Score formula](#81-score-formula) |
| `strategy.rsi2_enabled` | `False` | [§13 Optional overlay](#13-optional-overlay--rsi2) |
| `strategy.fundamentals_filter` | `True` | [§9 Fundamentals](#9-stage-7--fundamentals-soft-filter) |
| `regime.enabled` | `True` | [§3 Regime gate](#3-stage-1--regime-gate) |
| `regime.symbol` | `"SPY"` | [§3 Regime gate](#3-stage-1--regime-gate) |
| `regime.sma_window` | `200` | [§3 Regime gate](#3-stage-1--regime-gate) |

---

## 13. Optional overlay — RSI(2)

| Parameter | Config key | Default |
|-----------|-----------|---------|
| RSI(2) pullback overlay | `strategy.rsi2_enabled` | `False` |

**Ships disabled.** When enabled, it adds an alternative entry path: a candidate that has passed the
regime gate, liquidity filter and trend template may also be entered on a short-horizon pullback
(RSI over a 2-period Wilder lookback below its oversold threshold) without satisfying the Donchian
breakout condition of §6. All other stages — earnings blackout, ranking, fundamentals, sizing, and
all three exits — apply unchanged.

It is off by default for three stated reasons (`indicator-research.md` §5): horizon mismatch with a
1–8 week hold, documented post-2010 attenuation of daily-frequency mean-reversion edges, and
disproportionate sensitivity to the cost model on a small whole-share account.

It is retained and configurable rather than deleted so the hypothesis can be measured on our own
data with our own costs, out-of-sample, via the `rsi2_on` ablation (`scripts/ablations.py`).

---

## 14. Emitted artifacts

Per SPEC Contracts 8 and 9, a scan produces `PickRecord`s in
`<reports_dir>/scan-YYYY-MM-DD/picks.json`:

| Field | Source in this spec |
|-------|---------------------|
| `symbol` | universe |
| `date` | `asof` |
| `kind` | `"pick"` if `SizeResult.affordable`, else `"watch"` (§10.4) |
| `entry` | close at `asof` (the limit price for the next-open order) |
| `stop` | `initial_stop` at `asof` (§11.1) |
| `shares` | `SizeResult.shares` (§10.2) |
| `risk_amount` | `SizeResult.risk_amount` (§10.2) |
| `score` | `rank_candidates.score` (§8.1) |
| `atr` | `ATR(strategy.atr_window)` at `asof` |
| `earnings_date` | provider, ISO string or `None` (§7) |
| `earnings_known` | `False` triggers the visible warning (§7) |
| `thesis` | human-readable summary of which gates passed |
| `status` | `"drafted"` at scan time |

**The scan refuses to emit picks when `swing.backtest.gate.check(cfg)` fails**, unless `--force` is
passed. It still writes the full report directory with the gate status and reasons and an empty
`picks` array. The gate thresholds and their rationale are in `backtest-methodology.md` §7.
