# Indicator research: what the evidence supports, and what this repo actually does

**Purpose.** Every number in `config.example.toml` should trace to something
better than "it looked good on a chart". This document is that trace. For each
component it states the claim, the evidence for the claim, the honest size of
the effect, and the specific ablation in this repo that measures whether the
component earns its place *here*, on *your* universe, at *your* costs.

**Read this first.** No indicator predicts stock prices. Every effect below is
a small, noisy, time-varying tilt in a distribution — visible over hundreds of
trades and invisible over ten. Most were discovered in academic samples, and
most decayed after publication: McLean & Pontiff (2016) measured a 26% decline
in returns from the in-sample period to the post-sample, pre-publication
period, and a further 58% decline after publication, across 97 documented
predictors. Assume everything here is weaker now than in the paper that found
it. The goal of this system is not prediction; it is a **positive-expectancy,
risk-controlled process** whose losses are bounded by construction and whose
edge, if it exists at all, is small enough to be plausible.

---

## 0. How to read the ablation column

Every section ends with an **Ablation** line naming a variant in
`src/swing/backtest/runner.py::ABLATIONS`. Run:

```bash
swing backtest --ablations            # full universe
swing backtest --ablations --etf-only # survivorship-free
```

and the report writes an `ablations` table with CAGR / Sharpe / profit factor /
max drawdown for each variant against the baseline.

**The tables below are deliberately empty.** They are filled in by running the
ablations on real data, on your machine, over your universe. Nothing in this
repo ships with pre-baked results, because a number I made up would be worse
than no number at all.

The decision rule this document commits to: **a component that does not
measurably improve out-of-sample results in the ablation gets removed from the
defaults, no matter how good the citation is.** Literature justifies *trying*
a component. Only your own ablation justifies *keeping* it.

| variant | CAGR | Sharpe | PF | max DD | keep? |
|---|---|---|---|---|---|
| baseline | | | | | — |
| no_regime_filter | | | | | |
| no_trend_template | | | | | |
| no_adx_filter | | | | | |
| no_volume_confirmation | | | | | |
| no_atr_normalised_rank | | | | | |
| rank_long_only | | | | | |
| rank_short_only | | | | | |
| no_momentum_skip | | | | | |
| no_trailing_stop | | | | | |
| no_time_stop | | | | | |
| rsi2_entry | | | | | |

---

## 1. Cross-sectional momentum — the core ranking signal

**Config:** `[strategy.rank]` — `long_lookback = 126`, `long_weight = 0.6`,
`short_lookback = 63`, `short_weight = 0.4`, `skip_recent_days = 5`.

**Claim.** Stocks that have outperformed over the last 3–12 months tend to
keep outperforming over the next few weeks to months.

**Evidence.** Jegadeesh & Titman (1993, *Journal of Finance* 48:1) is the
original: 6-month formation / 6-month holding portfolios earned about 1% per
month gross on US stocks, 1965–1989. The effect replicated out of sample
(Jegadeesh & Titman 2001), across countries and across asset classes (Asness,
Moskowitz & Pedersen 2013, *JF* 68:3). Hurst, Ooi & Pedersen (2017) show
trend-following returns across a century of data.

**Honest caveats, in order of importance:**

1. **It is a monthly-rebalanced, long-short, hundreds-of-names effect.** This
   system is long-only, holds 4 positions, and rebalances on breakouts. The
   academic Sharpe does not transfer. What transfers is the *direction of the
   tilt*: ranking by momentum is better than ranking randomly.
2. **Momentum crashes.** Daniel & Moskowitz (2016) document momentum
   strategies losing enormous amounts in sharp market rebounds after
   drawdowns. The regime filter (§3) exists largely to sidestep those.
3. **Novy-Marx (2012) argues it is *intermediate* momentum that works** —
   performance 12 to 7 months ago, not 6 to 2 months ago. That is a direct
   challenge to the 126/63-day lookbacks used here, which are the "recent"
   horizons he finds weaker. The counter-argument is that this is a 1–8 week
   swing system with breakout-triggered entries, not a monthly-rebalanced
   cross-sectional portfolio, so the relevant horizon is shorter. **This
   argument is not settled by the literature; it is settled by the ablation.**
   Test 252/21 lookbacks against the defaults before trusting either.
4. **Short-horizon reversal contaminates it.** Jegadeesh (1990) and Lehmann
   (1990) document one-month reversal: last month's winners underperform over
   the following month. This is why `skip_recent_days = 5` exists and why the
   standard academic construction is "12-1" rather than "12-0". Five days is
   a conservative, swing-appropriate version of that skip.

**Why divide by ATR%?** Without volatility normalisation, ranking by raw return
is close to ranking by volatility: high-beta names dominate the top of the list
in every up-market. Dividing by ATR% asks "how much move per unit of risk?",
which is the question a fixed-risk-per-trade system should be asking. This is
the same logic as risk parity, applied to selection rather than weighting.

**Ablations:** `no_momentum_skip`, `no_atr_normalised_rank`, `rank_long_only`,
`rank_short_only`.

---

## 2. 52-week-high proximity — the ranking tiebreak

**Config:** `[strategy.trend_template]` — `max_pct_below_52w_high = 0.25`,
`min_pct_above_52w_low = 0.25`.

**Claim.** How close a stock trades to its 52-week high predicts future
returns, and does so at least as well as past return itself.

**Evidence.** George & Hwang (2004, *JF* 59:5) found that nearness to the
52-week high dominates both Jegadeesh–Titman momentum and Moskowitz–Grinblatt
industry momentum in explaining the cross-section; when both are included, the
52-week-high measure survives and traditional momentum largely does not. The
behavioural story is anchoring: investors under-react to good news that would
push a stock to a new high.

**Honest caveat.** This is a *ranking* signal in the literature, applied to
long-short deciles. Here it is used as a **filter** (must be within 25% of the
high) rather than a ranking input, which is a weaker use of the finding. It is
also mechanically correlated with the momentum ranking in §1, so the two are
not independent evidence.

**Ablation:** covered by `no_trend_template` (which removes the 52-week
constraints along with the moving-average stack — the components are not
individually separable without a config edit; do that edit if you want to
isolate them).

---

## 3. Moving-average regime filter — risk control, not return

**Config:** `[strategy.regime]` — `symbol = "SPY"`, `ma_len = 200`.

**Claim.** Taking new long positions only while the broad market is above its
200-day average reduces drawdown substantially, and reduces return only
slightly.

**Evidence.** Faber (2007, updated 2013) is the well-known practitioner
reference: a 10-month / 200-day SMA timing rule applied to broad indices
historically cut volatility and drawdown sharply with roughly comparable
returns. Brock, Lakonishok & LeBaron (1992) found moving-average rules
profitable on the Dow 1897–1986.

**The counter-evidence matters more than the evidence.** Sullivan, Timmermann
& White (1999) re-examined exactly those rules with a data-snooping-adjusted
bootstrap and found that once you account for the number of rules searched, the
significance largely evaporates in later samples. Zakamulin's work on moving
average timing reaches a similar conclusion: **the benefit is risk reduction,
not return enhancement**, and much of the historical outperformance came from a
handful of episodes (1929, 1937, 2000–02, 2008).

**Why keep it anyway.** For a leveraged-to-your-net-worth-by-attention retail
account, cutting drawdown is worth giving up return. This filter's job is to
stop the system from opening new positions into a 2008 or a 2022. It never
force-closes an existing position — those keep trailing — because forced
liquidation on a regime flip adds whipsaw without adding protection.

**Ablation:** `no_regime_filter`. Expect the drawdown column to move much more
than the CAGR column. If it does not, the filter is not doing its job here.

---

## 4. Trend template (50 > 150 > 200 SMA, rising 200) — practitioner heuristic

**Config:** `[strategy.trend_template]` — `sma_fast = 50`, `sma_mid = 150`,
`sma_slow = 200`, `slow_rising_lookback = 21`.

**Claim.** Stocks in sustained advances have their short averages stacked
above their long averages, with the long average itself rising.

**Evidence.** This specific stack is Minervini's (*Trade Like a Stock Market
Wizard*, 2013), derived from studying past big winners. **This is not academic
evidence.** It is a practitioner heuristic fitted to a hand-picked sample of
successes, which is close to the definition of survivorship bias.

**Why it is in the defaults anyway.** It is a cheap, monotone, non-parametric
way to express "only buy things in confirmed uptrends", and it is highly
correlated with signals that do have academic support (§1, §2). Its role here
is to *exclude* obviously wrong candidates rather than to *select* winners.

**Be suspicious of this one.** It has the weakest evidential basis of anything
in the default configuration and the largest number of free parameters
(three window lengths and a slope lookback). If `no_trend_template` shows
little damage in your ablation, delete it — that would be the expected result
if it is merely restating the momentum ranking in a more constrained form.

**Ablation:** `no_trend_template`.

---

## 5. ADX — a filter, never a signal

**Config:** `[strategy.trend_template]` — `adx_len = 14`, `adx_min = 20`.

**Claim.** ADX measures trend *strength* without regard to direction; readings
above ~20–25 conventionally indicate a trending rather than a ranging market.

**Evidence.** Wilder (1978) introduced ADX along with ATR and RSI. There is
essentially **no peer-reviewed evidence that ADX alone generates positive
expected returns.** The 20 and 25 thresholds are conventions from Wilder's book
that have been repeated ever since, not estimates from data.

**Role here.** Purely a gate: it removes breakout candidates in choppy names
where a Donchian breakout is most likely to be noise. Being direction-agnostic,
it cannot bias entries long or short, which limits how much damage it can do.

**Ablation:** `no_adx_filter` (sets `adx_min = 0`). If the results barely move,
drop it and remove a parameter.

---

## 6. Donchian breakout entry — the trigger

**Config:** `[strategy.entry]` — `donchian_len = 20`, `breakout_tolerance = 0.02`.

**Claim.** Buying an N-day high is a mechanical way to participate in trends
that are already established.

**Evidence.** Richard Donchian's channel rules and the Turtle system (Faith,
*Way of the Turtle*, 2007) used 20-day and 55-day breakouts and are the best
documented public examples of the approach. Hurst, Ooi & Pedersen (2017)
provide the strongest general evidence for trend-following as a class. Note
that all the well-documented breakout track records are **futures** track
records — diversified across dozens of uncorrelated markets, which is where
most of their Sharpe comes from. A 4-position long-only equity book is a much
more concentrated animal.

**Implementation note that is a real design decision.** A signal here requires
**both** that the prior 20-day high was exceeded today **and** that the close
finished within `breakout_tolerance` (2%) of that level. Requiring only the
second condition — the naive reading of "close is within 2% of the 20-day
high" — fires continuously on any flat, going-nowhere stock, because a stock
that has not moved in a month is permanently within 2% of its own 20-day high.
Requiring only the first buys every failed breakout that spiked and closed on
its low. This was caught by a test, not by reasoning, which is the argument for
the test.

**Ablation:** vary `donchian_len` via `swing backtest --sensitivity`. A system
that only works at exactly 20 days is not a system.

---

## 7. Volume confirmation

**Config:** `[strategy.entry]` — `volume_len = 50`, `volume_mult = 1.30`.

**Claim.** A breakout on unusually heavy volume is more likely to continue than
one on quiet volume.

**Evidence.** Gervais, Kaniel & Mingelgrin (2001, *JF* 56:3) document the
"high-volume return premium": stocks experiencing unusually high trading volume
over a day or a week tend to outperform over the following month. That is
suggestive but not the same claim — their result is about volume shocks
generally, not about volume at a breakout level specifically. Evidence for the
narrower practitioner claim is thin.

**Ablation:** `no_volume_confirmation` (`volume_mult = 0`). This is one of the
components most likely to be removed after testing; treat 1.30 as a starting
point, not a finding.

---

## 8. RSI(2) mean reversion — available, off by default

**Config:** `[strategy.entry.rsi2]` — `enabled = false`, `rsi_len = 2`,
`rsi_max = 10`, `trend_ma = 200`.

**Claim.** In an established uptrend, buying extremely short-term oversold
readings produces a high win rate over 2–5 day holds.

**Evidence.** Connors & Alvarez (*Short Term Trading Strategies That Work*,
2008) documented strong historical results for RSI(2) below 10 with price above
its 200-day average. Independent replications generally confirm the historical
pattern existed and generally find it substantially weaker after roughly 2010,
consistent with McLean & Pontiff's post-publication decay.

**Why it is off by default.** Three reasons, all structural rather than
statistical:

1. The documented edge is a **2–5 day hold**. This is a 1–8 week system. The
   holding periods are not compatible, and stretching an RSI(2) signal to a
   6-week hold is not the strategy that was tested.
2. It is a **mean-reversion** signal being grafted onto a **trend-following**
   exit structure (a 3-ATR Chandelier trail). The exit will not do what the
   entry expects.
3. High win rate with small wins and occasional large losses is exactly the
   payoff profile that looks best in a short backtest and hurts most in a bad
   month.

It is implemented, config-switchable, and included in the ablation set so it
can be tested rather than argued about.

**Ablation:** `rsi2_entry`.

---

## 9. MACD and OBV — deliberately absent

**MACD** is implemented in `indicators.py` but is not used by any default rule.
It is a difference of two EMAs; as a standalone signal it is a slower,
noisier restatement of the moving-average information the trend template
already carries. Sullivan, Timmermann & White's data-snooping critique applies
with full force.

**OBV** (On-Balance Volume) is implemented for completeness and is **not**
recommended. It is a cumulative signed-volume sum whose level depends entirely
on an arbitrary starting point, and the published evidence for it is close to
nonexistent. It is in the codebase so that anyone who wants to test it can,
and it is in this document so that the reason it is unused is on the record.

---

## 10. ATR — stops and sizing

**Config:** `[strategy.exit]` — `atr_len = 14`, `initial_stop_atr = 2.0`,
`chandelier_atr = 3.0`, `time_stop_days = 40`.

**Claim.** Position risk should be measured in units of the instrument's own
volatility, not in fixed percentages.

**Evidence.** This is the least controversial item in the document. ATR
(Wilder 1978) as the unit of risk, with position size set so that a stop-out
costs a fixed fraction of equity, is standard across the entire systematic
trading literature and is what makes trades comparable via R-multiples (Tharp).
Volatility-scaled position sizing has direct academic support in the
time-series momentum literature (Moskowitz, Ooi & Pedersen 2012).

The **Chandelier exit** (highest close since entry minus 3 ATR) is Chuck Le
Beau's. Anchoring to the highest *close* rather than the highest *high* is a
deliberate choice: a single spiky intraday wick should not permanently ratchet
the stop up into the noise.

**Multiples of 2.0 and 3.0 are conventions, not estimates.** They are in the
sensitivity sweep for exactly that reason. What the evidence supports is the
*structure* (volatility-scaled stop, trailing exit, fixed fractional risk); the
specific multipliers are yours to test.

**The time stop has the weakest justification of the three.** Its purpose is
capital turnover — a position that has gone nowhere for 40 trading days is
consuming one of only four slots. That is a portfolio-construction argument,
not an edge argument, and it should be evaluated as one.

**Ablations:** `no_trailing_stop`, `no_time_stop`, plus
`swing backtest --sensitivity` over all three multipliers.

---

## 11. Fundamentals as a soft filter

**Config:** `[strategy.fundamentals]` — `enabled = true`,
`require_positive_eps_or_growth = true`.

**Claim.** Among technically strong candidates, those with positive earnings or
revenue growth are less likely to be story stocks that unwind violently.

**Evidence.** Profitability is a documented cross-sectional factor
(Novy-Marx 2013; Fama & French 2015 five-factor model). But the version
implemented here — "trailing EPS or revenue growth is positive" — is a very
coarse proxy for that literature, and free fundamental data is patchy and
occasionally wrong.

**Design consequence.** The filter is **soft and fail-open**: a symbol with no
fundamental data available returns `None` from `rules.fundamentals_ok()` and is
treated as "no opinion", never as "fails". ETFs bypass it entirely, since an
ETF has no EPS and treating that as a failure would silently delete the entire
ETF universe. This is the correct default for unreliable data: missing data
must not be able to change the trade set.

---

## 12. Earnings blackout

**Config:** `[strategy.earnings]` — `blackout_days_before = 10`,
`blackout_days_after = 1`, `unknown_date_policy = "allow_with_warning"`.

**Claim.** Holding through an earnings release converts a controlled 1R risk
into an uncontrolled overnight gap.

**Evidence.** The earnings announcement premium is real (Frazzini & Lamont
2007 — stocks earn higher returns in announcement months), so this filter
plausibly *costs* return. It is not adopted for return; it is adopted because a
stop order does not protect you across a gap. §"gap through the stop" in the
engine models exactly this: the fill is the open, not the stop, and such trades
routinely lose more than 1R. A 10-day blackout removes the single largest
source of that risk.

**By default this is the one place where backtest and live deliberately
differ**, and it is called out in every report:

> Free data has no historical earnings calendar reaching back to 2010.
> The backtest therefore runs **without** the earnings blackout, while the live
> scanner applies it. The live system will take **fewer** trades than the
> backtest implies. The engine emits this as a warning on every run rather than
> silently ignoring it.

That divergence is now closable rather than permanent. Point `[data]
earnings_calendar` at a historical calendar CSV (`symbol,date` header, ISO
dates; format and failure modes in `src/swing/data/earnings_calendar.py`) and
the backtest applies the same blackout the live scanner does — the warning
above disappears and the report manifest records the file, its symbol and date
counts, and its coverage of the traded universe. The programmatic route still
exists: `run_backtest(..., earnings={"AAPL": [date(2024, 2, 1), ...]})`.

Keep the caveat that survives it: **coverage is on you.** Symbols absent from
your file get no blackout at all, and the loader cannot tell "never reported"
from "missing from my file". A calendar covering 50 of 500 names buys
protection for 50 and none for the other 450, so the run replaces the
all-or-nothing warning above with a specific one naming those counts. Read it:
a supplied calendar is not the same as a covered universe. Prefer a calendar
spanning the whole universe and the whole backtest window.

---

## 13. Survivorship bias — the largest single distortion

The universe CSVs list **current** index members. Every company that was in the
S&P 500 in 2012 and subsequently went to zero, was acquired at a discount, or
was quietly dropped is absent from the backtest, so the strategy never gets the
chance to lose money on it. The standard estimate for US large-cap strategies
is on the order of 1–4 percentage points of annual return, and materially more
for small caps, where index deletion is more common and more brutal.

**Two mitigations ship with this repo:**

1. `swing backtest --etf-only` runs the same strategy on the ETF universe. The
   ETFs in `data/universe/etfs.csv` existed throughout the test period, so that
   run has **no survivorship problem**. It is the honest lower bound.
2. Every report states the bias in its body.

**How to read the pair:** the ETF run is a floor, the stock run is a ceiling,
and the truth sits between them — closer to the floor than feels comfortable.
If the ETF-only run does not clear the gate, do not trade the stock universe on
the strength of the stock-universe backtest alone.

---

## 14. Overfitting — the risk that dwarfs all of the above

This system has roughly two dozen tunable parameters. Bailey, Borwein, López de
Prado & Zhu (2014) show that with enough trials you can produce an
in-sample Sharpe above 1 from **pure noise**, and that the expected maximum
Sharpe from N random strategies grows roughly with sqrt(2 ln N). Harvey, Liu &
Zhu (2016) argue that the appropriate significance hurdle for a new predictor,
after accounting for the number of factors researchers have collectively tried,
is a t-statistic above 3.0 rather than the conventional 2.0.

**What this repo does about it:**

- **Walk-forward is the headline number.** Parameters are fitted on the
  in-sample block only; the reported equity curve is the concatenation of
  out-of-sample segments the optimiser never saw.
- **The grid is small on purpose** — 27 points, not 27,000. A large grid
  searched by walk-forward is still a large search.
- **Sensitivity tables are read for flatness**, not for the best cell. If
  profit factor collapses when a parameter moves 25%, the backtest is measuring
  the edge of a knife.
- **The gate binds to a config hash.** Change a parameter and the gate re-locks
  until you re-validate. This is specifically designed to make "just nudge the
  stop and re-run" cost something.

**A measured example of how fragile grid selection is.** While optimising an
indicator implementation in this repo, `wilder_smooth` was rewritten from a
Python loop to an equivalent EWM call. The two agree to about **1 part in
10^13** — pure floating-point associativity, not a logic change, and every
exact-value indicator test still passed. Re-running the walk-forward with that
change produced a **materially different set of out-of-sample trades**.

The mechanism is worth understanding, because it is not a bug and it will not
be fixed by better code. The metric surface is *discontinuous*: a boundary
comparison somewhere (a stop touched at exactly the low, a close exactly at the
channel high) flips, one trade changes, profit factor moves by a hundredth, and
a different point in the parameter grid wins the in-sample block. Everything
downstream follows from that.

What this tells you is not "the code is unreliable" — reruns are byte-identical
and that is verified. It tells you that **the differences between grid points
are smaller than the noise**, which is exactly the condition under which
optimisation finds nothing but noise. Two consequences are baked into the code:
near-ties are reported as warnings on the walk-forward report, and the
incumbent is held rather than displaced by an immaterial improvement. The third
consequence is for the reader: treat the specific parameters a walk-forward
window "chose" as arbitrary among the plausible set, and judge the strategy by
the *flatness* of the sensitivity table instead.

**What none of that fixes:** every choice made while *writing* this strategy —
which indicators to include, which universe, which period, which exit — was
made by a human who has read about what worked historically. That is
selection, it happened before the first backtest ran, and no amount of
walk-forward machinery can undo it. Treat out-of-sample results as an upper
bound on what to expect, not an estimate.

---

## 15. Practical expectations

If, after running the walk-forward on real data, the out-of-sample numbers look
like:

- **Profit factor 1.2–1.5, Sharpe 0.4–0.8, max drawdown 20–35%, win rate
  35–45%** — that is a plausible, honest result for a long-only retail swing
  system after costs. Trend systems make money on a minority of trades.
- **Profit factor above 2.5, Sharpe above 2, drawdown under 10%** — something
  is wrong. Look for look-ahead, survivorship, or a parameter that was tuned on
  the test period. This is a bug report, not a success.
- **Below the gate thresholds** — the system will refuse to give you picks, and
  that is the correct outcome. Fix the strategy or lower your ambitions; do not
  lower the gate.

At $100 of equity with 2% risk, you are risking $2 per trade. Most picks will
land in the "watch (unaffordable)" section because whole-share math does not
work at that size. That is arithmetic, not a defect — and the amount you can
learn from the process at $100 while risking almost nothing is the actual
value of running it at that size.

---

## References

- Asness, Moskowitz & Pedersen (2013). "Value and Momentum Everywhere." *Journal of Finance* 68(3).
- Bailey, Borwein, López de Prado & Zhu (2014). "Pseudo-Mathematics and Financial Charlatanism." *Notices of the AMS* 61(5).
- Brock, Lakonishok & LeBaron (1992). "Simple Technical Trading Rules and the Stochastic Properties of Stock Returns." *Journal of Finance* 47(5).
- Connors & Alvarez (2008). *Short Term Trading Strategies That Work.*
- Daniel & Moskowitz (2016). "Momentum Crashes." *Journal of Financial Economics* 122(2).
- Faber (2007, rev. 2013). "A Quantitative Approach to Tactical Asset Allocation." *Journal of Wealth Management.*
- Faith (2007). *Way of the Turtle.*
- Fama & French (2015). "A Five-Factor Asset Pricing Model." *Journal of Financial Economics* 116(1).
- Frazzini & Lamont (2007). "The Earnings Announcement Premium and Trading Volume." NBER WP 13090.
- George & Hwang (2004). "The 52-Week High and Momentum Investing." *Journal of Finance* 59(5).
- Gervais, Kaniel & Mingelgrin (2001). "The High-Volume Return Premium." *Journal of Finance* 56(3).
- Harvey, Liu & Zhu (2016). "...and the Cross-Section of Expected Returns." *Review of Financial Studies* 29(1).
- Hurst, Ooi & Pedersen (2017). "A Century of Evidence on Trend-Following Investing." *Journal of Portfolio Management* 44(1).
- Jegadeesh (1990). "Evidence of Predictable Behavior of Security Returns." *Journal of Finance* 45(3).
- Jegadeesh & Titman (1993, 2001). "Returns to Buying Winners and Selling Losers"; "Profitability of Momentum Strategies." *Journal of Finance* 48(1), 56(2).
- Lehmann (1990). "Fads, Martingales, and Market Efficiency." *Quarterly Journal of Economics* 105(1).
- Lo, Mamaysky & Wang (2000). "Foundations of Technical Analysis." *Journal of Finance* 55(4).
- McLean & Pontiff (2016). "Does Academic Research Destroy Stock Return Predictability?" *Journal of Finance* 71(1).
- Minervini (2013). *Trade Like a Stock Market Wizard.*
- Moskowitz, Ooi & Pedersen (2012). "Time Series Momentum." *Journal of Financial Economics* 104(2).
- Novy-Marx (2012). "Is Momentum Really Momentum?" *Journal of Financial Economics* 103(3).
- Novy-Marx (2013). "The Other Side of Value: The Gross Profitability Premium." *Journal of Financial Economics* 108(1).
- Sullivan, Timmermann & White (1999). "Data-Snooping, Technical Trading Rule Performance, and the Bootstrap." *Journal of Finance* 54(5).
- Wilder (1978). *New Concepts in Technical Trading Systems.*
- Zakamulin (2017). *Market Timing with Moving Averages.*
