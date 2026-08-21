# Backtest Methodology

How `swing` measures the Trend-Momentum Core strategy, what the resulting numbers mean, and — more
importantly — what they do not mean.

The strategy itself is specified in [`strategy-spec.md`](strategy-spec.md); the evidence behind each
component is in [`indicator-research.md`](indicator-research.md).

**Central claim of this document:** the headline number produced by this system is a
*walk-forward, out-of-sample, cost-inclusive, survivorship-haircut-adjusted* estimate, and it is
still an optimistic one. Everything below exists to make the size of that optimism explicit rather
than to eliminate it, because it cannot be eliminated with the data available.

---

## Table of contents

1. [Simulation loop and timing](#1-simulation-loop-and-timing)
   — incl. [1.1 What the engine assumes about its bars](#11-what-the-engine-is-allowed-to-assume-about-its-bars)
2. [Fill model and gap-through handling](#2-fill-model-and-gap-through-handling)
3. [Cost model](#3-cost-model)
4. [Cash, shares and portfolio accounting](#4-cash-shares-and-portfolio-accounting)
   — incl. [4.1 Reference capital](#41-reference-capital)
5. [Walk-forward design](#5-walk-forward-design)
   — incl. [the candidate values (`[backtest.tuning_grid]`)](#the-candidate-values-backtesttuning_grid)
6. [Parameter sensitivity (±25%)](#6-parameter-sensitivity-25)
7. [Survivorship bias](#7-survivorship-bias)
8. [The haircut convention and the deployment decision rule](#8-the-haircut-convention-and-the-deployment-decision-rule)
9. [Reproducibility](#9-reproducibility)
10. [The gate](#10-the-gate)
11. [Known limitations](#11-known-limitations)

---

## 1. Simulation loop and timing

The engine is a **daily, bar-by-bar, event-ordered** simulator. It is not vectorised over time,
because path-dependent state (the chandelier ratchet, cash, slot occupancy) cannot be expressed
correctly as a pure vector operation.

For each trading day *t* in the test window, in this exact order:

1. **Open of day *t*.**
   a. Process **exits** queued at the close of *t−1* (stop hits, time stops), applying §2 fill rules.
   b. Process **entries** queued at the close of *t−1*, in rank order, subject to available cash and
      free slots.
2. **Close of day *t*.**
   a. Mark open positions to the close; ratchet each position's stop against that bar's chandelier
      level, so it can rise but never fall (`strategy-spec.md` §11.2).
   b. Evaluate exit conditions for every open position and **queue** those that fire for the open of
      *t+1*.
   c. Evaluate the full entry pipeline (`strategy-spec.md` §2) using data through the close of *t*
      only, rank candidates, and **queue** the top candidates for the open of *t+1*.
   d. Append the mark-to-market equity value to `equity.csv`.

**Signals at close, fills at next open.** This is the single most important anti-look-ahead property
of the design. A signal computed from bar *t*'s close cannot be filled at bar *t*'s close, because at
the moment the close prints, the close is not tradeable. Every entry and every exit therefore crosses
one overnight boundary, and the system pays the resulting gap — favourable and unfavourable alike.

**Look-ahead audit points.** Three places in the strategy could silently leak future information, and
each is closed explicitly:

| Risk | Mitigation |
|------|-----------|
| Donchian channel including the current bar's high | `donchian_high`/`donchian_low` are shifted by one bar (SPEC Contract 5) |
| Rolling statistics computed over the whole series then sliced | All indicators are causal rolling/`ewm` operations; no centred windows, no `bfill` |
| Universe membership known in advance | **Not closed.** This is the survivorship problem — see §7 |

**No wall-clock in logic.** `asof` is passed down from the entry point; `datetime.now()` is never
called inside strategy or engine code. This is what makes a rerun of a historical date reproduce that
date's decisions exactly.

### 1.1 What the engine is allowed to assume about its bars

Bad bars are a correctness problem, not a cosmetic one: a single `NaN` close that reaches the
arithmetic can mark a position at nothing, size against a phantom stop, or freeze a slot for the rest
of the run. Three layers deal with it, and they are stated here because the second and third exist
only because the first can be bypassed by a hand-built fixture.

**1. The data layer drops unusable rows (Contract 3, amendment A3).** `normalize_bars` removes any
bar where **any** of `open`/`high`/`low`/`close` is `NaN` or infinite — the whole row, because a
partial bar cannot be repaired without inventing prices. Volume is treated differently: a non-finite
volume becomes **`0.0`** rather than dropping the bar, since a missing volume print does not make the
prices wrong, and "no volume" is the honest reading for the liquidity and confirmation tests. What
reaches the engine is therefore a frame of finite float64 OHLCV on a unique, ascending, tz-naive
midnight index. An interior vendor gap arrives as a **missing date**, not as a `NaN` row.

**2. Indicators propagate `NaN` across gaps rather than smearing values (audit BUG-031).** The Wilder
recursion (`atr`, `adx`, `rsi`, `ema`) is seeded on the first *complete* window of consecutive
observations and its output is then re-masked wherever the input was missing, because a plain
`ewm` carries the previous mean straight across a hole and reports a stale average as if it were
measured. The one deliberate exception is `obv`, which treats a missing bar as zero flow and stays
flat across the gap; it is not traded by any rule.

**3. The engine guards anyway.** Any bar whose OHLC is not finite is mapped out of the symbol's
calendar and treated exactly as a day the symbol did not trade, with one warning per symbol naming
the count and the first and last offending date. The last known close is never overwritten with a
non-finite value, non-finite marks are excluded from the equity sum, and a fill is refused unless the
price is finite and positive.

**Symbols that stop printing bars** are closed rather than held forever. When a symbol's data ends
while a position is open, the engine books an `end_of_data` exit at that symbol's **last valid bar's
close** and warns. The alternative — freezing a slot for the remainder of the run — silently reduces
the strategy's capacity and flatters nothing in particular; it just makes the result wrong (audit
BUG-016). One seam is documented and unavoidable: the trade is dated to the symbol's last bar, but
the freed slot and the returned cash only appear on the next trading day, because the engine cannot
know a bar was the last one until the following bar fails to arrive.

---

## 2. Fill model and gap-through handling

Let `O(t)` be the open of the fill day, and `S` the effective stop price for a position.

### Entries

| Case | Fill price |
|------|-----------|
| Normal | `O(t)` plus entry costs (§3) |
| Gap up beyond a sanity band | Filled at `O(t)` anyway; no rejection |

Entries are **not** rejected on adverse gaps. The live system drafts a BUY LIMIT at the signal close
(SPEC Contract 10), so in practice a large gap up would *not* fill — but modelling that as "no trade"
in the backtest while the live system might still fill on an intraday pullback introduces a
discrepancy in the wrong direction. Filling at the open is the pessimistic, honest choice: the
backtest pays the full gap.

### Exits — the gap-through rule

This is the rule that determines whether a backtest is trustworthy.

```
if O(t) <= S:                # the market opened at or below the stop
    fill_price = O(t)        # NOT S — the stop is a market order once triggered
else:
    fill_price = S           # intraday touch, filled at the stop
```

**A stop order does not guarantee the stop price.** When a stock gaps down through the stop
overnight, the resting stop becomes a market order at the open and fills at the open — which may be
far below `S`. Modelling every stop exit as filling exactly at `S` is the most common way a backtest
manufactures a fictitious edge, and it is the specific mechanism by which earnings gaps
(`strategy-spec.md` §7) get hidden.

Consequence, stated so it is not a surprise later: **a single trade can lose considerably more than
`account.risk_pct` of equity.** The 2.5% risk budget bounds the *intended* loss, not the realised
one. This shows up as genuine left-tail weight in the equity curve, in `max_drawdown_pct`, and in
`avg_loss`, and it is the honest reason the drawdown gate is set as loose as 35%.

Intraday-touch detection uses `low(t) <= S` on the fill day. Within a single bar the engine does not
attempt to reconstruct the intrabar path; if both the stop and a large favourable move occur on the
same bar, the **stop is assumed to trigger first** (pessimistic tie-break).

### Time-stop exits

Filled at `O(t)` with no stop-price logic — a time stop is a market exit by definition.

---

## 3. Cost model

Costs are charged **per side** (once on entry, once on exit) and have two additive components.

```
cost_per_share = price × (backtest.slippage_bps / 10_000)     # slippage / commission proxy
               + backtest.spread_atr_frac × ATR               # half-spread + impact proxy
```

| Parameter | Config key | Default | Meaning |
|-----------|-----------|---------|---------|
| Slippage | `backtest.slippage_bps` | `5.0` | 5 basis points = 0.05% of notional, per side |
| Spread proxy | `backtest.spread_atr_frac` | `0.05` | 5% of one ATR, per side, in dollars |

`ATR` is `ATR(strategy.atr_window)` on the signal bar — the same value used for the stop, so the cost
scales with the same volatility measure that scales the position.

**Commissions are zero** in the base model. Schwab charges $0 commission on online US equity trades;
the 5 bps slippage term absorbs the residual (SEC/FINRA fees on sells, price improvement variance,
odd-lot handling).

### What this costs in practice

Cost per side as a percentage of price:

```
cost_pct_per_side = backtest.slippage_bps/100 + backtest.spread_atr_frac × ATRpct
                  = 0.05% + 0.05 × ATRpct
```

| Price | ATR | ATR% | Cost/side | Round trip | Round trip as % of the 2×ATR risk budget |
|-------|-----|------|-----------|------------|------------------------------------------|
| $20 | $0.40 | 2.0% | 0.15% | 0.30% | 7.5% |
| $20 | $0.60 | 3.0% | 0.20% | 0.40% | 6.7% |
| $50 | $2.00 | 4.0% | 0.25% | 0.50% | 6.3% |
| $8 | $0.40 | 5.0% | 0.30% | 0.60% | 6.0% |

Round-trip costs consume roughly **6–8% of the risk budget on every trade**, before the trade has
any chance to be right. Over ~30 OOS trades that is on the order of 2 risk-units of pure friction.
This is the main reason the RSI(2) overlay ships off (`indicator-research.md` §5) and the main reason
the time stop exists — trades that neither work nor fail still cost money.

### Is 5 bps + 0.05 ATR realistic?

For the universe the liquidity filter admits (`strategy.min_dollar_volume = 5_000_000`), on positions
of $25–$125 notional, it is **plausible but not conservative**. Our order size is a rounding error
against $5M/day so market impact is genuinely nil, but marketable limit orders on a $6/share name can
cross a spread wider than 0.05 ATR. The automatic sensitivity table (§6) does **not** cover the cost
parameters, so testing this assumption means raising both knobs by 25% in `config.toml` and re-running
the walk-forward.

---

## 4. Cash, shares and portfolio accounting

- **Whole shares only.** No fractional positions anywhere.
- **Cash accounting is explicit.** The engine tracks a cash balance; an entry debits
  `shares × fill_price + costs`, an exit credits `shares × fill_price − costs`. Buying power is
  never assumed.
- **No leverage, no margin, no shorting.** With `account.max_positions = 4` and
  `account.max_position_pct = 25.0`, a fully invested book is exactly 100% of equity.
- **No dividends, no interest on cash.** Both are omitted. Bar data is auto-adjusted (SPEC Contract
  3), so *price* returns already incorporate dividend adjustments for held positions; what is missing
  is interest earned on idle cash during regime-off periods. **This makes the backtest pessimistic**
  in high-rate environments and is the one systematic bias running in our favour.
- **`exposure_pct`** in the summary reports the average fraction of equity deployed, which is the
  metric that makes the omitted cash interest legible.
- **Slot contention** is resolved by `rank` (`strategy-spec.md` §10.1); ties are broken by symbol
  ascending so the resolution is deterministic.
- Position count is capped at `account.max_positions` at all times.

### 4.1 Reference capital

Every backtest starts from a **fixed reference capital**, not from the live account balance.

| Parameter | Config key | Default |
|-----------|-----------|---------|
| Backtest starting equity (USD) | `backtest.initial_equity` | `10_000.0` |

The engine seeds its simulated equity and cash from `backtest.initial_equity` and compounds from
there. **`account.equity` is not read by the backtest at all.**

**Why fixed reference capital.**

1. **Comparability.** Ablation variants, walk-forward folds and ±25% sensitivity cells are only
   comparable if they start from the same capital. If the starting equity tracked whatever happened
   to be in the live account on the day the run was launched, the same code and the same window
   would produce different metrics on different days — and `config_hash` would *not* change to
   explain why, since it deliberately excludes account size (§9). That silently breaks the
   reproducibility contract.
2. **A $100 account degenerates.** At `account.equity = 100.0` the notional cap is $25, so only
   names priced between `strategy.min_price` ($5) and $25 can be filled at all
   (`strategy-spec.md` §10.3), and most qualifying candidates size to **zero** shares. A backtest run
   at that capital would measure the affordability constraint, not the strategy: trade count would
   collapse far below `gates.min_trades = 30`, the surviving trades would be a low-priced,
   high-ATR%, unrepresentative slice of the universe, and profit factor, drawdown and win rate would
   all be artefacts of rounding. The gate would be unreachable for reasons that have nothing to do
   with whether the rules work.

**What $10,000 implies — friction still real, but not dominant.** At reference capital the notional
cap is `25% × $10,000 = $2,500` per position and the risk budget is `2.5% × $10,000 = $250`.

- Whole-share rounding is **still modelled and still costs something**: at $500/share the cap admits
  5 shares, so position size is quantised in 20% steps; at $2,000/share it admits 1 share, and the
  quantum is the whole position.
- Names priced **above $2,500/share drop out entirely** — they can never be filled. That exclusion is
  real and is left in deliberately rather than papered over.
- The notional cap still binds more often than the risk formula for most candidates, exactly as it
  does live.
- What changes is that friction stops being the *dominant* term. The great majority of the S&P 1500
  and the ETF list is tradeable at a $2,500 cap, so the measured result is driven by entries, exits
  and costs rather than by rounding.

$10,000 is chosen as a round number large enough to make the universe representative and small enough
that whole-share effects remain visible. It is not a claim about how much capital the strategy
requires.

**Separation of concerns — stated explicitly, because it is easy to misread.**

| Question | Answered by | Capital used |
|----------|-------------|--------------|
| Do the strategy's rules work out-of-sample, after costs? | the walk-forward backtest and the gate (§10) | `backtest.initial_equity` |
| Can *this* account take *this* pick tomorrow? | `swing scan` sizing (`strategy-spec.md` §10) | `account.equity` |

`swing scan` sizes every candidate at the real `account.equity`. At $100–$500 that means **most
qualifying candidates become watch-list entries with `shares = 0`** (`strategy-spec.md` §10.4). That
is the designed behaviour, not a malfunction, and it is why the watch list exists.

The consequence, stated plainly: **passing the gate does not mean the live account will fill many
picks.** The gate certifies the rules; the account decides what it can afford. A run of empty pick
lists and populated watch lists on a $100 account is a fully working system reporting a capital
constraint. Live results will therefore diverge from backtest results primarily through
affordability — a much larger effect at this account size than any modelling difference in §11.

---

## 5. Walk-forward design

### Structure

| Parameter | Config key | Default |
|-----------|-----------|---------|
| In-sample window (years) | `backtest.is_years` | `3` |
| Out-of-sample window (years) | `backtest.oos_years` | `1` |
| Data start | `backtest.start` | `2010-01-01` |
| Data end | `backtest.end` | `None` (= latest available bar) |

Windows are **anchored to calendar years and stepped annually** (a rolling, not expanding, IS
window):

| Fold | In-sample (tuning) | Out-of-sample (scored) |
|------|--------------------|------------------------|
| 1 | 2011-01-01 → 2013-12-31 | 2014 |
| 2 | 2012-01-01 → 2014-12-31 | 2015 |
| 3 | 2013-01-01 → 2015-12-31 | 2016 |
| … | … | … |
| N | *(latest complete 3 years)* | *(following year)* |

The first fold's IS window begins **after** the indicator warm-up. Bars are always loaded from
`data.start_date` (`2010-01-01`) regardless of the simulation window, and indicators are computed
over each symbol's whole series; only dates inside the window are simulated. Warm-up bars are
therefore read but never traded, and every indicator is converged on the window's first tradeable
day provided the window starts at least
`max(252, strategy.sma_slow) + strategy.sma_slow_rising_days` bars after the data does — which is
what the 2010 data start buys. With it, the first tradeable IS year is 2011 and the first OOS year
is 2014.

### Rules — these are the point of the exercise

1. **Parameters are tuned on the IS window only.** The OOS window is never touched during tuning, in
   any fold, for any purpose — not for early stopping, not for sanity checking, not for choosing which
   folds to report.
2. **Each fold's OOS is scored with the parameters that fold's IS selected.** Not with a globally
   best set.
3. **The headline result is the concatenation of every fold's OOS equity curve**, stitched
   end-to-end into a single continuous series. `summary.json["oos"]` is computed from that
   concatenated series, and it — not the full-period figure — is what the gate reads.
4. **`summary.json["full_period"]` is reported for context and is in-sample-contaminated by
   construction.** It will look better than the OOS block. Reading it as the result defeats the
   entire design. It is reported because omitting it would invite someone to compute it themselves,
   and because a large IS/OOS gap is itself diagnostic of overfitting.
5. **`summary.json["by_year"]`** reports per-year OOS metrics so a single carrying year is visible.
   A strategy whose entire OOS profit comes from 2020 has not been validated; it has been lucky once.
6. **A non-walk-forward run can never pass the gate** (SPEC Contract 11), regardless of how good it
   looks. `swing backtest` without walk-forward is a development tool.

### What is actually tuned

Deliberately, **almost nothing**. Most parameters are inherited conventions (Wilder's 14, Minervini's
50/150/200, Turtle's 20, LeBeau's 3×) rather than fitted values — see `indicator-research.md` §13 on
why this matters. Bailey et al.'s probability-of-backtest-overfitting rises with the number of trials;
holding the trial count low is a stronger defence than any statistical correction applied afterwards.

The IS tuning stage is restricted to a small, pre-declared grid over:
`strategy.atr_stop_mult`, `strategy.chandelier_mult`, `strategy.donchian_window`,
`strategy.volume_mult`. That is four parameters. Adding a fifth should require an explicit argument
in this document.

Note the consequence for those four settings: **setting them under `[strategy]` does not affect a
walk-forward run.** The tuner overwrites them in every fold. They still drive `swing scan` and any
`--no-walkforward` run, but a walk-forward result is a function of the grid, not of those four
config values.

#### The candidate values (`[backtest.tuning_grid]`)

The *parameter list* above is frozen. The *candidate values* are not, because freezing them makes a
whole class of research question unanswerable: "does this strategy whipsaw because a 2-ATR stop is
too tight for a 16-day median hold?" cannot be tested by a tuner that is only ever offered 1.5, 2.0
and 2.5.

| | |
|---|---|
| Config key | `[backtest.tuning_grid]`, one list per parameter |
| Default | absent — which means exactly the grid below, so every report predating the knob reproduces unchanged |
| Tunable keys | `atr_stop_mult`, `chandelier_mult`, `donchian_window`, `volume_mult`, and nothing else |
| Ceiling | **512 combinations** (`swing.config.MAX_TUNING_COMBINATIONS`); the default grid spends 81 |
| Recorded in | `summary.json["tuning_grid"]`, always, for walk-forward runs |

```toml
[backtest.tuning_grid]
atr_stop_mult   = [1.5, 2.0, 2.5, 3.0, 3.5]   # the wider-stops hypothesis
chandelier_mult = [2.5, 3.0, 3.5]
donchian_window = [15, 20, 25]
volume_mult     = [1.0, 1.3, 1.6]
```

Rules, all enforced at config load with a plain-English refusal:

- Only the four tunable parameters may appear. Naming any other setting is refused and the message
  lists the four.
- Each list must be non-empty and free of repeats (a repeat is a wasted simulation and inflates the
  candidate count the report shows).
- Every candidate must satisfy the same limits `[strategy]` puts on that field — positive
  multipliers; `donchian_window` a whole number ≥ 2 and ≤ `MAX_LOOKBACK_BARS` (380). The tuner writes
  its pick straight into `StrategyCfg`, so an illegal candidate is the same mistake as an illegal
  config value, just discovered several hundred simulations later.
- The lists must multiply out to at most 512 combinations. The cost is
  **combinations × folds × symbols**, so the ceiling is a wall-clock guard as much as a
  methodological one.
- Naming only some of the four is allowed. The rest are then *not tuned at all* and keep their
  `[strategy]` value in every fold — which is how you isolate one parameter's effect.
- Key order in the file does not matter: the grid is normalised to a canonical order, because the
  selection objective breaks ties on grid order and two files listing the same candidates must not
  be able to select different parameters from identical data.

**The honesty caveat, which runs opposite to the intuition.** A wider grid does not produce a
better-validated strategy; it produces a *worse-validated* one at the same headline number. Each
fold makes its selection from more candidates, so the number of trials rises, and with it the
probability that the winning cell won on in-sample noise — Bailey et al.'s probability of backtest
overfitting is increasing in the trial count, and the walk-forward split does not neutralise this.
It bounds the damage (the OOS year is still untouched) without removing it, because the OOS years
are then scored on parameters that were selected more aggressively. Going from 81 to 405
combinations means **a wider grid's out-of-sample result deserves more scepticism than a narrower
one's, not less** — and if a widened grid is what turns a failing gate into a passing one, the
honest reading is that the gate was passed by searching harder, not by finding an edge.

Two mechanisms keep this visible rather than deniable:

- `summary.json["tuning_grid"]` records the grid actually searched on every walk-forward run, so a
  report can never be read as having used the default when it did not. A `--no-walkforward` run
  records `{}`, because it tunes nothing.
- `config_hash` covers the grid whenever it is not the default, so a widened-grid report is never
  confused with a standard one. An absent grid and one that spells the default out hash identically,
  since they are the same experiment — which is what keeps every pre-existing report comparable.

### The selection objective, verbatim

Every walk-forward report carries the objective in `summary.json["objective"]`, so the rule that
picked each fold's parameters is readable next to its results. The string is
`swing.backtest.walkforward.OBJECTIVE_DESCRIPTION`:

> In-sample selection maximises profit factor among parameter sets with at least 8 in-sample trades
> (sets below that floor rank last whatever their ratio), breaking ties by more trades, then by
> shallower maximum drawdown, then by the order the tuning grid lists them in. A parameter set with no losing trades
> at all reports the 9999.0 profit-factor sentinel rather than a measurement, so it is ranked as 0.0
> and wins only on trade count and drawdown. Profit factor is used because it is the quantity the
> deployment gate tests.

Two details in there are load-bearing:

- **A "perfect" parameter set is not selected for being perfect.** Profit factor is undefined with
  zero losing trades, and JSON cannot hold infinity, so `metrics.PROFIT_FACTOR_CAP = 9999.0` is
  written along with `profit_factor_capped: true`. Ranking a sentinel as the best score would hand
  every fold to whichever cell happened to take three lucky trades; it is therefore ranked as
  **0.0** and can only win on the tiebreakers (audit BUG-041). Reports render such a value as
  "no losing trades" rather than as a number.
- **The eight-trade floor is a floor, not a filter.** Sets below it are ranked last but still
  eligible, so a fold in which nothing clears the floor still selects *something* rather than
  failing.

The default grid is 81 combinations (3 × 3 × 3 × 3) evaluated per fold, and ties resolve to the
first grid point, so selection is deterministic. A run that configures its own grid resolves ties
the same way, against that grid's canonical order.

### Sample-size honesty

With `account.max_positions = 4`, a 1–8 week hold and a regime gate that is off perhaps 20–25% of the
time, a single OOS year yields on the order of **15–35 trades**. Twelve OOS years yield a few hundred.
This is a small sample for estimating a profit factor. Confidence intervals on every metric in the
summary are wide, and differences between ablation variants (`indicator-research.md` §"Ablation plan")
of less than roughly 0.2 in profit factor should be treated as noise.

---

## 6. Parameter sensitivity (±25%)

A strategy that only works at one parameter setting is a curve fit. Every report therefore includes a
sensitivity table: each parameter is varied to **−25%, baseline, +25%** on its own, with all others
held at baseline, and the run is repeated.

`SENSITIVITY_PARAMS` is five knobs — the four tuning-grid members plus `adx_min`:

| Parameter | −25% | Baseline | +25% |
|-----------|------|----------|------|
| `strategy.atr_stop_mult` | 1.5 | **2.0** | 2.5 |
| `strategy.chandelier_mult` | 2.25 | **3.0** | 3.75 |
| `strategy.donchian_window` | 15 | **20** | 25 |
| `strategy.volume_mult` | 0.975 | **1.3** | 1.625 |
| `strategy.adx_min` | 15.0 | **20.0** | 25.0 |

(The −25%/+25% columns show the values at shipping defaults; the code perturbs whatever the loaded
config holds.) Integer knobs are rounded to the nearest integer, and a cell is **skipped** — with no
row in the table — when the rounded value comes back equal to the baseline, or when the perturbed
value is one the config's own validation refuses. Skips are logged, not silently dropped.

**Read the table for shape, and read it as in-sample.** Two properties of how it is produced bound
what it can tell you:

- It is computed over the **full period with the configured parameters**, not fold by fold. It is not
  an out-of-sample measurement and is not comparable to the `oos` block; it answers "is this result
  balanced on a knife edge?", not "how would this have done".
- Four of the five parameters are also **tuning-grid members** (§5), so on a walk-forward run the
  headline result does not use the config values this table perturbs — the tuner re-selects them per
  fold. The same caveat that makes an ablation of a grid parameter a no-op applies here
  (`indicator-research.md`, "Ablation plan").

The desirable pattern is a *plateau*: metrics that degrade gracefully and monotonically as you move
away from baseline. The alarming patterns are (a) a sharp peak exactly at baseline, which means the
value was fitted, and (b) sign flips — a parameter whose ±25% variants straddle profitable and
unprofitable, which means the result is not robust to a parameter we do not actually know the true
value of.

Cost parameters are **not** in the table. Sensitivity to the cost model has to be checked by editing
`backtest.slippage_bps` / `backtest.spread_atr_frac` and re-running, and §3 already admits that model
is plausible rather than conservative — so that re-run is worth doing before any conclusion is drawn.

Sensitivity tables are **diagnostic, not selection**. Choosing the best-performing cell is exactly
the data-snooping failure Sullivan, Timmermann & White (1999) quantify.

---

## 7. Survivorship bias

This is the largest single source of error in the stock-universe results, it is not fixable with the
data available, and it is therefore documented precisely rather than glossed.

### The exact mechanism

The universe comes from **committed CSV snapshots** of current S&P 500/400/600 membership
(SPEC Contract 4: `src/swing/assets/universe/sp500.csv` etc.). Those snapshots were taken *today*.
Running a backtest over 2014–2025 against a 2026 membership list introduces two distinct biases:

**Bias A — delisting / failure survivorship.** Companies that went bankrupt, were delisted, or were
acquired at a discount between 2014 and 2026 are simply absent from the file. They can never be
picked, and can never produce the −40% gap-through-stop loss they would have produced. Shumway (1997,
*Journal of Finance* 52(1):327–340) showed that CRSP's omitted delisting returns are large and that
negative-reason delistings are typically surprises — precisely the events a trend system cannot dodge.
Published estimates of the aggregate effect on index-level returns run **1.5–2.0 percentage points
per year** (a commonly cited CRSP comparison over 1926–2001 gives 7.4% survivorship-free versus 9.0%
survivorship-biased annualised).

**Bias B — index-membership look-ahead, and it is worse for us.** Index inclusion is *itself* an
outcome of past performance. A company enters the S&P 500 after growing; it exits after shrinking.
Backtesting a **momentum** strategy on today's membership therefore selects, at every historical
date, from a pool of companies that we know went on to grow. This is not a generic return bias — it
is correlated with the exact signal the strategy trades on, which makes it strictly worse than Bias A
for this system than it would be for, say, a mean-reversion strategy. We are unaware of a clean
published estimate for this configuration; we treat it as **at least as large as Bias A**.

Note that Bias A and Bias B do not cancel. Both inflate results in the same direction.

### Why free data cannot fix it

1. **yfinance serves currently-listed tickers.** A request for a delisted symbol returns empty or
   errors; there is no historical-symbol archive behind the free endpoint. Even a perfect
   point-in-time membership list would leave us unable to fetch bars for the names on it.
2. **Point-in-time index membership is a paid product.** Historical constituent lists with
   add/remove dates come from CRSP, Compustat, S&P's own feeds, or vendors like Norgate. There is no
   free, complete, licence-clean source.
3. **Ticker reuse poisons naive reconstruction.** Symbols are recycled after delisting, so scraping
   historical membership from archived web pages and then fetching bars by symbol can silently
   attach one company's history to another company's ticker — producing errors that are worse than
   the bias, because they are undetectable in aggregate statistics.

Rebuilding the universe layer around a paid point-in-time source is the correct fix and is out of
scope for v1. Until then the bias is **quantified by convention (§8), not removed.**

### The ETF-only run as a lower bound

`swing backtest --universe etf` (`run_backtest(cfg, universe="etf", ...)`) runs the identical
strategy over the ~40 curated ETFs. That run is **materially less survivorship-biased**:

- The core holdings (SPY, QQQ, IWM, the nine sector SPDRs, GLD, TLT, EFA, EEM) have existed
  continuously across the whole test window. There is no "companies that failed" population to omit.
- ETF closures do happen, and the curated list was assembled today, so the run is not perfectly
  clean — a fund that closed in 2018 is not in the file. But closures concentrate in niche,
  low-AUM products that the `min_dollar_volume` filter would have excluded anyway.

**Interpretation.** The ETF run is treated as a **lower bound on the strategy's genuine, tradeable
edge**, for two reasons pulling in the same direction: it is nearly survivorship-clean, *and* the ETF
universe is intrinsically less favourable to a cross-sectional momentum strategy (~40 correlated,
diversified baskets rather than 1,500 idiosyncratic names — much less cross-sectional dispersion for
the ranking to exploit). A strategy that clears the gate on ETFs has cleared a real bar.

The stock run is treated as an **upper bound**. The truth is between them, and closer to the ETF end
than the arithmetic mean would suggest.

Both runs are executed as part of integration (SPEC AC18) and both reports are retained.

---

## 8. The haircut convention and the deployment decision rule

Because §7 cannot be fixed, it is **priced**. The following convention is fixed here, in advance, so
it cannot be adjusted after seeing results.

### The haircut

Applied to **stock-universe** results only (`universe = "stocks"` or `"full"`). ETF-only results take
no survivorship haircut.

| Metric | Adjustment | Justification |
|--------|-----------|---------------|
| **CAGR** | subtract **4.0 percentage points** | ~2.0 pp for delisting survivorship (Bias A, Shumway 1997 / CRSP comparisons) + ~2.0 pp for index-membership look-ahead (Bias B, judgment — treated as at least as large as Bias A) |
| **Profit factor** | `PF_adj = 1 + (PF_oos − 1) × 0.75` | a 25% haircut on profit *in excess of breakeven*; anchored to the low end of McLean & Pontiff (2016)'s 26% out-of-sample decay |
| **Max drawdown** | `DD_adj = DD_oos × 1.25` | omitted failures are concentrated in the left tail; drawdowns are the metric most distorted by their absence |
| **Trade count, win rate, Sharpe** | unadjusted, but flagged | no principled scaling; treat as optimistic |

These numbers are **conventions, not measurements.** They are stated so that the same haircut is
applied to every run, including runs that would otherwise look good enough to deploy.

### Relationship to the mechanical gate

The mechanical gate (`swing.backtest.gate.check`, §10) operates on **raw OOS metrics** — it is frozen
behaviour in SPEC Contract 11 and does not know about haircuts. The thresholds in `GatesCfg` were
chosen *with the haircut already in mind*:

```
gates.min_profit_factor = 1.3   ⇒   PF_adj = 1 + (1.3 − 1) × 0.75 = 1.225
gates.max_drawdown_pct  = 35.0  ⇒   DD_adj = 35.0 × 1.25 = 43.75%
```

A **bare** pass of the mechanical gate therefore corresponds to a haircut-adjusted profit factor of
about 1.22 and a haircut-adjusted drawdown of about 44%. That is a marginal system, not a good one.

### Deployment decision rule (operator-level, layered above the gate)

Capital is deployed only when **all four** hold:

1. `gate.check(cfg)` passes on the walk-forward **stock-universe** run — raw OOS PF ≥ 1.3, max DD
   ≤ 35%, ≥ 30 trades.
2. The **haircut-adjusted** stock-universe figures still clear a *reduced* bar: `PF_adj ≥ 1.15` and
   `DD_adj ≤ 45%`.
3. The **ETF-only** walk-forward run (the lower bound, §7) has `PF_oos ≥ 1.0` — it need not clear the
   full gate, but a strategy that loses money on a nearly survivorship-clean universe has not
   demonstrated an edge.
4. OOS profit is not concentrated in a single year: no single year in `summary.json["by_year"]`
   contributes more than 60% of total OOS profit.

Conditions 2–4 are **not** enforced in code. They are recorded here so that the decision is made
against a rule written before the numbers were seen, rather than reasoned toward afterwards.

### An additional, unpriced consideration

McLean & Pontiff (2016) find published predictors return 26% less out-of-sample and **58% less
post-publication**. Every effect this strategy uses was published between 1963 and 2013. A
conservative operator should mentally apply a further haircut on top of the survivorship one. We do
not encode it, because compounding two judgment-based haircuts produces a number with no defensible
interpretation — but it should not be forgotten when the ablation table looks encouraging.

---

## 9. Reproducibility

Two runs with the same inputs must produce **byte-identical** `trades.csv` and `equity.csv`
(SPEC AC9). This is a hard requirement, tested, not an aspiration.

### Identity triple in `summary.json`

Every run records three hashes (SPEC Contract 11):

| Key | Content |
|-----|---------|
| `config_hash` | SHA-256 over a canonical serialisation (keys sorted, `Path`s stringified, dates ISO-formatted) of the settings that decide what the strategy would have done: the whole `[strategy]`, `[backtest]` and `[gates]` sections, plus `account.max_positions` / `risk_pct` / `max_position_pct` and `regime.enabled` / `symbol` / `sma_window`. **Deliberately not the whole config** — account size, alert channels, paths and broker credentials are excluded, so the same rules hash identically on two machines and a deposit does not invalidate a report. |
| `code_ref` | `git rev-parse HEAD`, or the literal `"unknown"` outside a checkout or when git cannot answer within five seconds. It records the commit, **not** whether the tree was clean — a run made on top of uncommitted edits reports the parent commit and is not reproducible by anyone else, so do not base a deployment decision on a run you have not committed. |
| `data_hash` | SHA-256 over `"{SYMBOL}:{last bar date}:{row count}"` for every symbol in the run, symbols sorted ascending, joined with a pipe character; a symbol that returned nothing contributes `"{SYMBOL}:empty:0"`. Deliberately cheap — it does not scan the price columns. |

`data_hash` matters more than it looks. yfinance's adjusted history is **not stable**: a split or a
dividend restatement rewrites the entire past series. Without `data_hash`, a run that fails to
reproduce is indistinguishable from a code bug. With it, the two cases separate immediately. Note
what it does and does not catch: a symbol appearing, disappearing or gaining bars changes the hash,
while a restatement that rewrites past closes **without** changing the last bar date or the row count
does not. The cache's own overlap check is the layer that catches that one — it re-fetches a symbol's
whole history when the freshly downloaded overlap disagrees with what is stored.

### Determinism requirements on the engine

- **Stable, total sort orders everywhere.** Candidate ranking is `score` desc → `high_prox` desc →
  `symbol` asc. Never rely on dict or set iteration order for anything that reaches an output file.
- **Fixed seeds** for any stochastic component. (The v1 strategy has none; the requirement stands so
  that adding one later cannot silently break AC9.)
- **No wall-clock values in `trades.csv`, `equity.csv` or `summary.json`.** A `generated_at`
  timestamp appears **only** in `report.html`, which is not part of the byte-identity check.
- **Fixed float formatting** on write — an explicit decimal precision, not `repr`, so platform float
  repr differences cannot alter bytes.
- **No parallelism in the simulation loop**, or parallelism only where results are re-sorted into a
  deterministic order before writing.

### Output layout (SPEC Contract 11)

```
<reports_dir>/backtest/<label-or-timestamp>/
    summary.json     # metrics + config_hash + code_ref + data_hash
    report.html      # human-readable; the only file containing a timestamp
    report.md
    trades.csv       # one row per closed trade
    equity.csv       # one row per bar
<reports_dir>/backtest/latest.json     # copy of the most recent run's summary.json
```

`latest.json` is what `gate.check` reads. It is a copy rather than a symlink so a moved or pruned run
directory cannot silently invalidate the gate. A `--label` names **one directory** under
`<reports_dir>/backtest` and nothing else: it must match `^[A-Za-z0-9._-]+$` and is validated before
any data is loaded, so a label cannot relocate the run directory and leave a stale `latest.json`
behind (audit BUG-042). `latest.json` is always located from the gate's own path, never derived from
wherever the run happened to land.

A run whose label starts with `ablate` **does not update `latest.json` at all**, and the gate
independently refuses an `ablate`-labelled report if one is copied over it by hand. That is why
`scripts/ablations.py` can sweep ten crippled variants without touching your trading permission.

Backtest run directories are **never pruned automatically** — only `reports/scan-YYYY-MM-DD/`
directories are, at 90 days (see the README). A backtest report is evidence, and evidence that
deletes itself is not much use; delete old runs by hand when you want the disk back.

### What is in `summary.json`

Beyond the identity triple, the keys a reader is most likely to need:

| Key | Meaning |
|-----|---------|
| `walkforward` | JSON `true` only for a walk-forward run — the gate tests this identically |
| `label`, `universe`, `n_symbols` | which run this was; `n_symbols` counts symbols that returned data |
| `start`, `end` | the simulation window; `end` is clipped to the last available bar |
| `oos_start`, `oos_end` | first and last day actually **measured** out-of-sample. Present only for a walk-forward run with at least one fold (audit BUG-043) |
| `initial_equity` | the reference capital the run traded (§4.1) |
| `earnings_blackout_simulated` | whether the run could apply a historical earnings blackout (§11, limitation 7) |
| `objective` | the selection rule quoted in §5; `""` for a non-walk-forward run |
| `tuning_grid` | the candidate values the tuner was actually offered (§5); `{}` for a non-walk-forward run, which tunes nothing. A grid other than the default also changes `config_hash` |
| `oos` | the headline metric block — the concatenated OOS curve. **For a non-walk-forward run this is a copy of `full_period`**, so the presence of an `oos` block says nothing about whether the run was walk-forward |
| `full_period` | in-sample-contaminated context (§5, rule 4) |
| `by_year`, `windows`, `sensitivity`, `costs` | per-year metrics, per-fold detail, the ±25% table (§6), and the cost settings |

Each metric block carries `profit_factor_capped` alongside the twelve metrics, which is how a
`9999.0` profit factor is distinguishable from a measurement.

### Two dates on every report: "Measured period" and "Data span"

`report.md`, `report.html` and `swing report` all print both, because conflating them is the easiest
way to overstate a result:

| Line | What it is |
|------|-----------|
| **Measured period** | the stretch the headline numbers actually cover — `oos_start` to `oos_end` for a walk-forward run, i.e. the concatenated out-of-sample years only |
| **Data span** | the simulation window (`start` to `end`) the run was asked for |

For a walk-forward run over 2010–2026 the measured period typically starts in 2014: everything before
the first fold's OOS year was in-sample tuning or warm-up and is not part of the headline. For a
non-walk-forward run the two lines are identical, and the report says in a banner that the numbers
are in-sample and cannot open the gate.

---

## 10. The gate

```
swing.backtest.gate.check(cfg) -> GateResult(passed: bool, reasons: list[str], report_path: Path | None)
```

`gate.check` passes **iff** `<reports_dir>/backtest/latest.json` exists, the run was walk-forward,
and all three conditions hold on the `oos` block:

| Condition | Config key | Default |
|-----------|-----------|---------|
| `oos.profit_factor` ≥ threshold | `gates.min_profit_factor` | `1.3` |
| `oos.max_drawdown_pct` ≤ threshold | `gates.max_drawdown_pct` | `35.0` |
| `oos.trades` ≥ threshold | `gates.min_trades` | `30` |

**A non-walk-forward run never passes**, whatever its metrics (SPEC Contract 11). `summary.json`
carries `"walkforward": bool` for exactly this check.

### `latest.json` is untrusted input (audit BUG-017)

`latest.json` is an ordinary file that a human, a script, or a half-finished write can produce, so
the gate parses it defensively rather than believing it. Every one of these is a refusal, and each
prints a plain-English sentence to whoever was about to be told they may not trade:

| Refusal | Trigger |
|---------|---------|
| no report | the file does not exist |
| unreadable report | invalid JSON, unreadable file, or a top-level value that is not a JSON object |
| non-finite report | the JSON literals `Infinity`, `-Infinity` or `NaN` anywhere in the file — rejected **at parse time**, since they are a JSON extension rather than JSON, and every number is re-checked for finiteness after parsing so an overflowing `1e400` cannot arrive as `inf` either |
| not walk-forward | `walkforward` is not the JSON literal `true`. The test is identity against `True`, so the *string* `"false"` — which Python truthiness reads as true — fails as it should |
| ablation report | the `label` starts with `ablate`; a deliberately crippled variant can never be the reference |
| no OOS block | `oos` is missing or is not an object |
| capped profit factor | `oos.profit_factor_capped` is truthy — zero losing trades means the `9999.0` figure is a sentinel standing in for infinity, not a measurement, and "too good to trust" closes the gate rather than opening it. Plain truthiness here (unlike `walkforward`'s strict `is True`) because every ambiguous value lands on the safe side; a missing key means "not capped", so older reports stay valid |
| threshold failures | profit factor, drawdown or trade count against the table above |

Unusable numbers fall back to the **worst** possible reading — profit factor `0.0`, drawdown `100%`,
trades `0` — so a damaged report fails closed rather than passing on a default.

### Why these three, and why these values

- **Profit factor 1.3** — gross profit ÷ gross loss, after all costs. It is the metric least
  distorted by a small sample and by a couple of outlier trades (unlike Sharpe, which needs many
  observations, or CAGR, which a single 2020-style year can carry). 1.3 is chosen so that a bare pass
  still clears ~1.22 after the §8 haircut, i.e. still positive-expectancy under our own stated
  pessimism.
- **Max drawdown 35%** — deliberately loose. A concentrated 4-position long-only equity book with
  ATR stops that gap through *will* have large drawdowns; setting 20% here would reject the strategy
  for behaving as designed. It is a check against catastrophic misbehaviour, not a comfort target.
  After the haircut this corresponds to ~44%, which should be read as the real expectation.
- **30 trades minimum** — a floor on statistical meaningfulness. Below 30 OOS trades the profit
  factor is essentially unestimated. This is a low bar, and passing it is not the same as having a
  reliable estimate (§5, "Sample-size honesty").

### Effect on `swing scan`

Per SPEC Contract 9 and AC11: **`swing scan` refuses to emit picks when the gate fails**, unless
`--force` is passed. On a failed gate it still writes the complete report directory, with
`"gate": {"passed": false, "reasons": [...]}` and empty `picks` and `watch` arrays, and the rendered
sheet states which condition failed.

This is the central safety property of the system. The strategy cannot recommend a trade until it has
demonstrated, out-of-sample, on the operator's own data, with the operator's own costs, that it meets
a bar set in advance. `--force` exists for development and is logged loudly when used.

---

## 11. Known limitations

Collected in one place, ordered by how much they should worry a reader.

1. **Survivorship bias in the stock universe (§7).** The largest and least fixable error. Priced by
   convention in §8; not removed.
2. **Point-in-time universe membership is unavailable at all**, so Bias B (index-inclusion
   look-ahead) is correlated with the strategy's own signal — worse than a generic return bias.
3. **Every effect used is long-published** (§8, McLean & Pontiff 2016). Post-publication decay is
   acknowledged and not priced.
4. **Small trade counts** (§5). Confidence intervals on every reported metric are wide; ablation
   differences below ~0.2 profit factor are noise.
5. **Cost model is plausible, not conservative** (§3), and it is **not** one of the five knobs the
   automatic sensitivity table varies (§6) — checking it takes a deliberate re-run at higher costs.
6. **Intrabar path is not modelled.** Stop-versus-target ordering within a bar is resolved
   pessimistically, but real intrabar sequences (a stop touched then reversed) are not reproducible
   from daily bars.
7. **Earnings-date coverage is incomplete, and the run says when it had none** (`strategy-spec.md`
   §7). The runner asks the provider for each symbol's **historical** announcement dates over the
   loaded window and feeds them to the same `earnings_blackout` rule the scanner uses, so a
   historical bar inside a blackout is blocked in the backtest the way it would have been live
   (amendment A12). Two honest caveats remain. First, coverage is still partial: an announcement the
   free provider does not know about blocks nothing, and that fraction is not random — it correlates
   with company size. Second, when no historical earnings source is available at all — the provider
   exposes only "the next date", or the lookup fails — the run sets
   `summary.json["earnings_blackout_simulated"] = false` and every report renders a warning saying
   the backtest took entries the live scanner would have blocked and is therefore slightly
   optimistic. The flag records that the *mechanism* ran, not that any particular symbol had dates.
8. **The backtest runs at reference capital, not at the live balance** (§4.1). It simulates from
   `backtest.initial_equity = 10_000.0`, so it takes trades a $100–$500 live account cannot afford.
   This is deliberate — the alternative measures rounding rather than the strategy — but it means
   backtest metrics are *not* a forecast of this account's results. The affordability gap is the
   largest single source of live-versus-backtest divergence at current equity.
9. **No dividends or cash interest** (§4). Runs pessimistic on the cash side, and cash exposure is
   large when the regime gate is off.
10. **yfinance adjusted history is mutable.** `data_hash` (§9) detects this but cannot prevent it;
    a rerun months later may legitimately produce different numbers for the same code and config.
