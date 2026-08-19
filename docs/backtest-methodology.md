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
2. [Fill model and gap-through handling](#2-fill-model-and-gap-through-handling)
3. [Cost model](#3-cost-model)
4. [Cash, shares and portfolio accounting](#4-cash-shares-and-portfolio-accounting)
5. [Walk-forward design](#5-walk-forward-design)
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
   a. Mark open positions to the close; update each position's running maximum and ratcheted
      chandelier stop (`strategy-spec.md` §11.2).
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
cross a spread wider than 0.05 ATR. Sensitivity to this assumption is reported in §6.

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

**Equity used for sizing during a backtest** is the simulated running equity, not
`account.equity`. A run starting at `account.equity = 100.0` in 2013 compounds; if the strategy works
the affordability constraint (`strategy-spec.md` §10.3) relaxes over the run, and if it does not, the
constraint tightens. Both are correct and both are what would have happened.

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

The first fold's IS window begins **after** the indicator warm-up. Bars are loaded from
`data.start_date` (`2010-01-01`), and each window reserves a warm-up buffer of
`max(252, strategy.sma_slow) + strategy.sma_slow_rising_days` bars before its start so that every
indicator is fully converged on the window's first tradeable day. Warm-up bars are read but never
traded. With a 2010 data start, the first tradeable IS year is 2011 and the first OOS year is 2014.

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

### Sample-size honesty

With `account.max_positions = 4`, a 1–8 week hold and a regime gate that is off perhaps 20–25% of the
time, a single OOS year yields on the order of **15–35 trades**. Twelve OOS years yield a few hundred.
This is a small sample for estimating a profit factor. Confidence intervals on every metric in the
summary are wide, and differences between ablation variants (`indicator-research.md` §"Ablation plan")
of less than roughly 0.2 in profit factor should be treated as noise.

---

## 6. Parameter sensitivity (±25%)

A strategy that only works at one parameter setting is a curve fit. Every walk-forward report
therefore includes sensitivity tables: each parameter is varied to **−25%, baseline, +25%** with all
others held at baseline, and the OOS metrics are recomputed.

| Parameter | −25% | Baseline | +25% |
|-----------|------|----------|------|
| `strategy.atr_stop_mult` | 1.5 | **2.0** | 2.5 |
| `strategy.chandelier_mult` | 2.25 | **3.0** | 3.75 |
| `strategy.donchian_window` | 15 | **20** | 25 |
| `strategy.time_stop_days` | 30 | **40** | 50 |
| `strategy.volume_mult` | 0.975 | **1.3** | 1.625 |
| `strategy.adx_min` | 15.0 | **20.0** | 25.0 |
| `strategy.mom_weight_126` | 0.45 | **0.6** | 0.75 |
| `strategy.mom_skip_days` | 4 | **5** | 6 |
| `strategy.max_below_high_pct` | 18.75 | **25.0** | 31.25 |
| `strategy.min_above_low_mult` | 1.1875 | **1.25** | 1.3125 |
| `regime.sma_window` | 150 | **200** | 250 |
| `backtest.slippage_bps` | 3.75 | **5.0** | 6.25 |
| `backtest.spread_atr_frac` | 0.0375 | **0.05** | 0.0625 |

Integer-valued parameters are rounded to the nearest integer. `strategy.mom_weight_63` is set to
`1 − strategy.mom_weight_126` when the 126 weight is varied, so the pair remains a partition.

**How to read them.** The desirable pattern is a *plateau*: metrics that degrade gracefully and
monotonically as you move away from baseline. The alarming patterns are (a) a sharp peak exactly at
baseline, which means the value was fitted, and (b) sign flips — a parameter whose ±25% variants
straddle profitable and unprofitable, which means the result is not robust to a parameter we do not
actually know the true value of.

Cost parameters (`slippage_bps`, `spread_atr_frac`) are included deliberately. If a +25% cost
assumption flips the strategy to unprofitable, the strategy is a cost-model artefact, and §3 already
admits the cost model is plausible rather than conservative.

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
| `config_hash` | SHA-256 over a canonical serialisation of the **fully resolved** `Config` — every default materialised, keys sorted, `Path` values stringified, dates ISO-formatted. Two configs that differ in any field produce different hashes; two that differ only in comments or key order produce the same hash. |
| `code_ref` | Git commit SHA of the working tree, with a `-dirty` suffix when uncommitted changes are present. A `-dirty` result is not reproducible by anyone else and should never be the basis of a deployment decision. |
| `data_hash` | SHA-256 over the ordered, per-symbol tuple `(symbol, first_date, last_date, row_count, digest_of_close_column)` across every symbol in the run, symbols sorted ascending. Detects silently changed history — vendor restatements, split adjustments applied retroactively, and cache corruption. |

`data_hash` matters more than it looks. yfinance's adjusted history is **not stable**: a split or a
dividend restatement rewrites the entire past series. Without `data_hash`, a run that fails to
reproduce is indistinguishable from a code bug. With it, the two cases separate immediately.

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
directory cannot silently invalidate the gate.

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
5. **Cost model is plausible, not conservative** (§3). Sensitivity to it is reported (§6) and should
   be checked before any conclusion is drawn.
6. **Intrabar path is not modelled.** Stop-versus-target ordering within a bar is resolved
   pessimistically, but real intrabar sequences (a stop touched then reversed) are not reproducible
   from daily bars.
7. **Earnings-date coverage is incomplete** (`strategy-spec.md` §7). The blackout is diluted in the
   backtest by exactly the fraction of unknown dates, and that fraction is not random.
8. **The live account's affordability constraint is not applied historically** (§4): the backtest
   sizes off simulated running equity. A backtest starting at $100 that compounds well will take
   trades the live $100 account could not have taken at that time.
9. **No dividends or cash interest** (§4). Runs pessimistic on the cash side, and cash exposure is
   large when the regime gate is off.
10. **yfinance adjusted history is mutable.** `data_hash` (§9) detects this but cannot prevent it;
    a rerun months later may legitimately produce different numbers for the same code and config.
