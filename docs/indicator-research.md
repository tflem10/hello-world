# Indicator Research

Evidence review behind the **Trend-Momentum Core** strategy implemented in `swing`.

**Scope and honesty statement.** Nothing in this document predicts prices. Every component below is
either (a) a *tilt* — a filter or ranking that historically shifted the distribution of forward
returns modestly in our favour, or (b) *risk control* — a rule that bounds loss per trade and per
portfolio. Effect sizes in the published literature are small, are measured on long-short academic
portfolios that we do not trade, and shrink after publication (McLean & Pontiff 2016 find published
predictors return **26% less out-of-sample and 58% less post-publication**). This document is a
record of why each rule is in the system and what would have to be true for it to be removed. It is
not investment advice and recommends no security.

Every default in `StrategyCfg` and `RegimeCfg` (SPEC Contract 1) is traced to a section here; see
[Parameter traceability](#parameter-traceability) at the end.

---

## Table of contents

1. [Intermediate-horizon momentum and the skip effect](#1-intermediate-horizon-momentum-and-the-skip-effect)
2. [52-week-high proximity](#2-52-week-high-proximity)
3. [Moving-average trend filters and the trend template](#3-moving-average-trend-filters-and-the-trend-template)
4. [Regime filter (index above its 200-day SMA)](#4-regime-filter-index-above-its-200-day-sma)
5. [RSI(2) short-horizon mean reversion](#5-rsi2-short-horizon-mean-reversion)
6. [MACD and ADX](#6-macd-and-adx)
7. [Donchian channel breakouts and volume confirmation](#7-donchian-channel-breakouts-and-volume-confirmation)
8. [On-Balance Volume](#8-on-balance-volume)
9. [ATR stops and position sizing](#9-atr-stops-and-position-sizing)
10. [Liquidity and price filters](#10-liquidity-and-price-filters)
11. [Fundamentals as a soft filter](#11-fundamentals-as-a-soft-filter)
12. [Earnings blackout](#12-earnings-blackout)
13. [Cross-cutting caveat: data snooping and edge decay](#13-cross-cutting-caveat-data-snooping-and-edge-decay)
14. [Ablation plan](#ablation-plan)
15. [Parameter traceability](#parameter-traceability)
16. [Bibliography](#bibliography)

---

## 1. Intermediate-horizon momentum and the skip effect

### Evidence

Jegadeesh & Titman (1993, *Journal of Finance* 48(1):65–91) documented that buying past 3–12 month
winners and selling past losers produced significant positive returns over 3–12 month holding
periods on US stocks, 1965–1989, and that the effect was not explained by systematic risk or by
delayed reaction to common factors. They also documented that part of the first-year abnormal return
dissipates over the following two years — momentum is a medium-horizon phenomenon that eventually
reverses.

The *skip* convention comes from the same literature. Raw last-month returns are contaminated by
short-horizon **reversal**: the most recent weeks tend to mean-revert (bid-ask bounce, liquidity
provision, institutional flow). The standard academic formation window is therefore "12 months
skipping the most recent month" (12-2), not "12 months". Jegadeesh & Titman ran both, and the
skip-a-week/skip-a-month variants avoid the reversal contamination.

Novy-Marx (2012, *"Is momentum really momentum?"*, *Journal of Financial Economics*
103(3):429–453) sharpened this: **intermediate**-horizon past performance — the six months ending
twelve months before formation — predicts future returns *better* than recent past performance
(the last six months skipping one). He shows this on US equities 1926–2010 and reports similar
patterns for international equity indices, commodities and currencies, on average returns, four-
factor alphas, and Sharpe ratios.

Barroso & Santa-Clara (2015, *"Momentum has its moments"*, *JFE* 116(1):111–120) show momentum's
risk is highly variable *and predictable*, and that scaling exposure by recent realised volatility
nearly doubles the strategy's Sharpe ratio and largely removes its crash tail. This is the direct
justification for dividing our momentum measure by ATR% rather than ranking on raw return.

### Verdict for this system

Adopted, with adaptations, and with expectations set low.

- We rank on a **blend of a ~6-month and a ~3-month** lookback rather than a single window. A pure
  Novy-Marx intermediate window (t-12m to t-6m) is a poor fit for a 1–8 week hold on a four-position
  book: it selects stocks whose move is already a year old. The 126-day (≈6 month) leg carries the
  bulk of the weight (0.6) as the closest tradeable analogue to the documented medium-horizon
  effect; the 63-day (≈3 month) leg (0.4) keeps the ranking responsive on a swing timescale.
- We **skip the most recent 5 trading days** on the 126-day leg to avoid short-horizon reversal
  contamination, following the spirit of the skip convention scaled to our horizon. We do *not*
  skip on the 63-day leg — at that length the skip removes 8% of the window for little benefit, and
  the breakout entry already requires recent strength.
- Both legs are divided by **ATR%** (`100 × ATR(14) / close`), i.e. we rank on *risk-adjusted*
  momentum. This is the Barroso–Santa-Clara insight applied cross-sectionally: without it the
  ranking systematically selects the highest-volatility names, which on a whole-share $100–$500
  account translates into the worst position-count and slippage outcomes.
- **Known deviation from the literature:** the academic effect is measured on a long-short decile
  portfolio rebalanced monthly across thousands of names. We hold at most four long positions for
  1–8 weeks. We should expect a fraction of the documented effect, dominated by idiosyncratic noise.
  The ranking's job here is *candidate ordering when slots are contested*, not alpha generation.

**Measured (ETF, 2010–2026):** both parameters were weakly supported. Removing the skip
(`mom_skip_days = 0`) cost 0.06 PF and pushed the system below breakeven (0.96 vs 1.02); flattening
the blend to 0.5/0.5 cost 0.02 PF. Both differences are inside the noise band and neither is
evidence on its own — but both moved in the direction Jegadeesh & Titman (1993) and Novy-Marx (2012)
predict. Consistent-with, not confirmation-of.

### Where it appears in config

`strategy.mom_weight_126 = 0.6`, `strategy.mom_weight_63 = 0.4`, `strategy.mom_skip_days = 5`.

The ATR% denominator is **not** a config key: the score always divides by `ATR(SCORE_ATR_WINDOW)`
with `SCORE_ATR_WINDOW = 14` fixed in `swing.strategy.scoring`, so that a score means the same thing
across ablation variants and sensitivity cells. `strategy.atr_window` sizes stops; it does not touch
the ranking ([`strategy-spec.md` §8.1](strategy-spec.md#81-score-formula)). A name whose ATR% falls
below `MIN_ATR_PCT = 0.05` has no measurable volatility to normalise by and scores `NaN`, which drops
it from the ranking rather than sending it to the top.

---

## 2. 52-week-high proximity

### Evidence

George & Hwang (2004, *"The 52-Week High and Momentum Investing"*, *Journal of Finance*
59(5):2145–2176): nearness to the 52-week high, combined with current price, explains a large
portion of momentum profits, **dominates and improves upon** the forecasting power of past returns
(both individual and industry), and — critically — the returns it forecasts **do not reverse in the
long run**, unlike classic momentum. The behavioural story is anchoring: traders treat the 52-week
high as a reference point and under-react to good news that pushes price through it.

Follow-on work extended the result to international stock indices (Du, 2008, *Journal of
International Financial Markets, Institutions & Money*), though as with all published anomalies the
post-publication decay caveat in §13 applies.

### Verdict for this system

Adopted in two places, both deliberately weak-form:

1. As a **hard gate** inside the trend template — a candidate must be within 25% of its 52-week
   high. This is a coarse "is this a stage-2 uptrend" test, not a fine-grained signal.
2. As the **tiebreaker** in `rank_candidates`: when two symbols score identically on risk-adjusted
   momentum, the one closer to its 52-week high wins (`high_prox`, 0..1, 1 = at the high).

We do *not* rank primarily on 52-week-high proximity. The measure is bounded and highly clustered —
in a strong tape a third of the universe sits within 5% of its high, so it has poor discriminating
power as a primary sort, but excellent properties as a deterministic tiebreak.

### Where it appears in config

`strategy.max_below_high_pct = 25.0` (trend-template gate). The tiebreak itself is not
parameterised — it is fixed behaviour of `swing.strategy.scoring.rank_candidates` (SPEC Contract 7).

---

## 3. Moving-average trend filters and the trend template

### Evidence

**Faber (2007, *"A Quantitative Approach to Tactical Asset Allocation"*, Journal of Wealth
Management, Spring 2007; SSRN 962461)** tested a single rule — hold when monthly close > 10-month
SMA, else cash — on the S&P 500 back to 1900 and on MSCI EAFE, GSCI, NAREIT and 10-year Treasuries
since 1973. Risk-adjusted returns improved almost universally; the mechanism is drawdown truncation,
not return enhancement. Faber's own 10-years-later revisit reports the rule continued to behave as
designed. This is the single best-evidenced piece of technical machinery in the whole system, and
notably it is a *risk* rule, not a *selection* rule.

**Hurst, Ooi & Pedersen (2017, *"A Century of Evidence on Trend-Following Investing"*, Journal of
Portfolio Management; SSRN 2993026)** extend time-series momentum evidence back to 1880 across
global markets: positive average returns in **every decade since 1880**, low correlation to
traditional assets, and positive performance in 8 of the 10 largest 60/40 drawdowns. Trend
following as a category has about as much out-of-sample support as anything in finance.

**Minervini (2013, *Trade Like a Stock Market Wizard*, McGraw-Hill)** is a practitioner source, not
peer-reviewed, and we treat it as such. Its "trend template" is an eight-criterion screen for a
confirmed stage-2 uptrend:

| # | Minervini criterion | Our config key |
|---|---------------------|----------------|
| 1 | Price above 150-day and 200-day SMA | `sma_mid=150`, `sma_slow=200` |
| 2 | 150-day SMA above 200-day SMA | `sma_mid`, `sma_slow` |
| 3 | 200-day SMA trending up ≥ 1 month | `sma_slow_rising_days=21` |
| 4 | 50-day SMA above both 150-day and 200-day | `sma_fast=50` |
| 5 | Price above 50-day SMA | `sma_fast=50` |
| 6 | Price ≥ 25–30% above 52-week low | `min_above_low_mult=1.25` |
| 7 | Price within 25% of 52-week high | `max_below_high_pct=25.0` |
| 8 | IBD Relative Strength rank ≥ 70 | **not implemented** — see below |

Criterion 8 requires Investor's Business Daily's proprietary RS rank. We do not have it and do not
approximate it with a home-made percentile, because doing so would double-count: our momentum score
*is* a relative-strength measure and is already applied at the ranking stage. Documented deviation.

The template has no published out-of-sample validation. What it does have is strong *construct*
overlap with two things that are well evidenced: the Faber/Hurst trend-following result (criteria
1–5) and the George & Hwang 52-week-high result (criteria 6–7). We use it as a structured
composition of those two effects rather than as an independently credible claim.

### Verdict for this system

Adopted as the **primary entry gate**, with the honest framing that it is doing filtering work
(refusing to buy downtrends), not forecasting work. It should be expected to reduce trade count
substantially and to improve drawdown more than it improves CAGR — exactly Faber's finding.

ETFs take a **relaxed path**: the trend template still applies in full, but the fundamentals soft
filter (§11) is skipped, since EPS/revenue growth is undefined for a basket.

### Where it appears in config

`strategy.sma_fast = 50`, `strategy.sma_mid = 150`, `strategy.sma_slow = 200`,
`strategy.sma_slow_rising_days = 21`, `strategy.min_above_low_mult = 1.25`,
`strategy.max_below_high_pct = 25.0`.

---

## 4. Regime filter (index above its 200-day SMA)

### Evidence

Same Faber (2007) result as §3, applied to the index rather than the name. The additional and more
specific evidence is **Daniel & Moskowitz (2016, *"Momentum Crashes"*, *JFE* 122(2):221–247)**: they
show momentum strategies suffer infrequent, persistent strings of large negative returns; that these
crashes are **partly forecastable**; and that they occur specifically in "panic" states — *following
market declines and when market volatility is high* — and coincide with market rebounds. Their
examples include the post-1932 and March–May 2009 rebounds.

That is a precise statement about *when* a momentum-selection system is most dangerous, and it maps
directly onto a cheap observable: is the broad index above its long-term average.

### Verdict for this system

Adopted as an **entry-only** gate. When SPY closes below its 200-day SMA, `entries_allowed` is
False and the scanner emits no new picks; **existing positions are still managed by their own stops**
(initial, chandelier, time). Gating exits on the regime would convert a risk filter into a
market-timing bet, and would create a discontinuity where a regime flip liquidates a healthy book.

Costs of this rule, stated plainly: it will keep us flat through the first leg of every V-shaped
recovery, and it whipsaws around the 200-day line. We accept both. The ablation (`regime_off`)
quantifies exactly what we pay for it.

**Measured (ETF, 2010–2026):** disabling the gate *improved* the sample — PF 1.24 vs 1.02, max
drawdown 21.3% vs 28.6%. The default stays `True`. This window contains no extended bear market, so
it prices the premium without ever paying the claim; see
[Ablation plan → Interpretation](#three-removals-improved-this-sample--and-the-defaults-are-kept-anyway).

Default reference symbol is SPY rather than a broader index because SPY is the most liquid, longest,
cleanest free-data series available and correlates ~0.95+ with any reasonable alternative.

### Where it appears in config

`regime.enabled = True`, `regime.symbol = "SPY"`, `regime.sma_window = 200`.

---

## 5. RSI(2) short-horizon mean reversion

### Evidence

Wilder (1978, *New Concepts in Technical Trading Systems*, Trend Research) introduced RSI with a
default 14-period lookback. Connors & Alvarez (2008, *Short Term Trading Strategies That Work*,
TradingMarkets Publishing; and Connors' earlier *Street Smarts*, 1996) popularised a **2-period**
RSI as a pullback trigger: buy when RSI(2) drops below ~5–10 while price is above its 200-day SMA,
exit on RSI(2) recovery or a close above a short MA. Their published backtests cover roughly the
1990s–2010 window on US stocks and index ETFs (SPY, QQQ) and reported high hit rates.

**The decay evidence is the important part.** Chordia, Subrahmanyam & Tong (2014, *"Have Capital
Market Anomalies Attenuated in the Recent Era of High Liquidity and Trading Activity?"*, *Journal of
Accounting and Economics* 58(1):41–58) find that daily-frequency anomaly profits have essentially
vanished since the early 2000s, and that average returns from prominent anomaly portfolios roughly
**halved after decimalization**; they attribute the decline to growth in hedge-fund AUM, short
interest and share turnover. Chordia, Roll & Subrahmanyam (2011, *"Recent trends in trading activity
and market quality"*, *JFE* 101(2):243–263) document the underlying microstructure shift. McLean &
Pontiff (2016) supply the general publication-decay result. Short-horizon reversal is precisely the
kind of edge those forces erode first: it is capacity-constrained, it is cheap to compute, and it
was published in a mass-market book.

There is also a structural mismatch. Connors' rules are 2–5 day holds. Ours are 5–40 day holds.

### Verdict for this system

**Ships OFF by default (`rsi2_enabled = False`).** Three reasons, in order of weight:

1. **Horizon mismatch.** A 2-day mean-reversion trigger inside a 1–8 week trend-following system is
   a different strategy wearing the same config file. It would fire on pullbacks that our chandelier
   stop is designed to sit through.
2. **Documented decay.** The specific edge lives at the daily frequency in liquid large caps — the
   exact segment Chordia et al. show has attenuated most.
3. **Cost sensitivity.** Short holds amortise our cost model (§9 of `backtest-methodology.md`:
   5 bps + 0.05×ATR per side) over a much smaller expected move. On a $100–$500 whole-share account
   this is decisive.

It remains *implemented and configurable* rather than deleted, because it is a legitimate hypothesis
worth measuring on our own data with our own costs. The `rsi2_on` ablation exists to test it, against
a criterion registered before the run: enable it only if a walk-forward OOS run with
`rsi2_enabled=True` beats baseline on profit factor **and** max drawdown.

**Measured (ETF, 2010–2026): it did neither.** PF 0.99 vs 1.02, max drawdown 32.5% vs 28.6%, 553
trades vs 332, average hold 11.7 days vs 16.5 — more trades, shorter holds, no profit-factor gain,
worse drawdown. Exactly the predicted signature. `rsi2_enabled` stays `False`, and that decision now
rests on an out-of-sample result from our own data rather than on inference from a 2008 book.

### Where it appears in config

`strategy.rsi2_enabled = False`.

---

## 6. MACD and ADX

### Evidence

**The broad survey result is unflattering.** Park & Irwin (2007, *"What Do We Know About the
Profitability of Technical Analysis?"*, *Journal of Economic Surveys* 21(4):786–826) reviewed 95
modern studies: 56 found positive results, but they stress that positive findings concentrate in
**futures and foreign-exchange markets, not equities**, and that many studies suffer from
data-snooping, ex-post rule selection and inadequate transaction-cost treatment. Their earlier
review (Park & Irwin, 2004, *"The Profitability of Technical Analysis: A Review"*, AgMAS Project
Research Report; SSRN 603481) covers 130+ studies to the same effect.

Sullivan, Timmermann & White (1999, *"Data-Snooping, Technical Trading Rule Performance, and the
Bootstrap"*, *Journal of Finance* 54(5):1647–1691) applied White's Reality Check bootstrap to a
universe of ~7,846 trading rules over 100 years of DJIA data, extending Brock, Lakonishok & LeBaron
(1992). Their headline finding: once you correct for the full universe of rules from which the best
rule was selected, and once you look at the subsequent 10-year out-of-sample period, profitability
is low. Rule-mining produces impressive in-sample numbers essentially for free.

**MACD specifically**: the published record is thin and inconsistent. Studies that report MACD
profitability generally do so *after optimising the (12, 26, 9) parameters per market* — which is
Sullivan/Timmermann/White's exact failure mode. Several papers report that the traditional
(12, 26, 9) settings fail to beat buy-and-hold on equity indices.

**ADX specifically**: Wilder (1978) introduced ADX as a *trend-strength* measure with no directional
information — ADX rises in strong downtrends as well as uptrends. Wilder himself proposed it as a
filter for deciding whether to run a trend system at all, not as a signal. There is no credible
peer-reviewed evidence of standalone ADX profitability, and given the construction there could not
be: it is direction-blind.

### Verdict for this system

- **MACD: rejected.** Not used in any rule. It contributes no information that the 50/150/200 SMA
  structure does not already carry, and adds three more parameters to overfit. It is not
  implemented, not in config, and not in the ablation plan.
- **ADX: adopted as a filter only, at a low threshold.** `adx_min = 20.0` is Wilder's own
  conventional "trending" boundary and is used as a *pass/fail gate inside the trend template*, never
  as a signal, never as a ranking input, and never with an upper bound. Direction is supplied
  entirely by the trend template and the breakout; ADX only answers "is there enough directional
  movement here for a breakout to mean anything."

The threshold is deliberately at the low end. A higher `adx_min` would look better in-sample
(it selects for realised trend) and is a classic overfitting trap. The `adx_off` ablation
(`adx_min=0.0`) tests whether this filter earns its place at all; we should be prepared to find that
it does not.

**Measured (ETF, 2010–2026): it did not.** Disabling ADX improved the sample slightly — PF 1.09 vs
1.02 — while adding 127 trades (459 vs 332) and lifting exposure from 41.6% to 53.5%. The +0.07 PF
is well inside the noise band on 332 trades, so the filter is retained as a turnover and
chop-avoidance control rather than as a demonstrated edge. This is the weakest-supported gate in the
system after the fundamentals filter, and a reasonable person could disable it.

### Where it appears in config

`strategy.adx_min = 20.0`. The smoothing period is Wilder's 14 and is a fixed constant
(`ADX_WINDOW` in `swing.strategy.rules`), not `strategy.atr_window`. Setting `adx_min = 0.0` disables
the filter outright — the code then skips the ADX computation entirely, which is also what the
`adx_off` ablation does. MACD: intentionally absent.

---

## 7. Donchian channel breakouts and volume confirmation

### Evidence

**Breakouts.** Richard Donchian's channel rules, and the Dennis/Eckhardt "Turtle" programme of the
early 1980s that used them (System 1: enter on a break of the 20-day high, exit on a 10-day low
break; System 2: 55-day), are the canonical trend-following entry. The Turtles reportedly earned
substantial profits over roughly five years. That is the heritage. It is also, in evidentiary terms,
a single non-replicated live experiment in 1980s commodity futures — a different asset class, a
different decade, with leverage and shorting, run by a group selected and coached by Dennis.

The honest reading is: **the breakout trigger is the weakest-evidenced load-bearing component in
this system.** After the Turtle rules were published, reported performance of the naive rules fell.
Sullivan/Timmermann/White's (1999) result applies directly — channel-breakout rules were in their
tested universe, and survived correction for data snooping poorly. Park & Irwin (2007) find what
equity-market evidence exists for breakout rules to be weak relative to futures/FX. Mechanically,
breakouts in liquid US equities in the 2010s–2020s face far more competition than in 1983: the
signal is trivially computable, widely known, and the failed-breakout / stop-run pattern is itself a
well-known counter-strategy.

Hurst, Ooi & Pedersen (2017) is the strongest defence available, but note what it actually supports:
*time-series trend following as a category*, over 12-month-ish horizons, across futures markets. It
supports "trend exposure pays"; it does not specifically vindicate a 20-day equity breakout.

**Volume confirmation.** Gervais, Kaniel & Mingelgrin (2001, *"The High-Volume Return Premium"*,
*Journal of Finance* 56(3):877–919) is the real evidence here: stocks experiencing unusually high
trading volume over a day or a week tend to experience higher returns over the subsequent month.
They attribute it to a visibility/investor-recognition effect. Kaniel, Li & Starks later confirmed
the premium internationally. This is genuine, peer-reviewed, and it is exactly the effect we are
invoking when we require breakout volume ≥ 1.3× the 50-day average.

### Verdict for this system

Adopted, with the entry deliberately **not** load-bearing:

- Entry requires a close at or above the prior-20-day Donchian high **or within
  `breakout_proximity_pct` (2%) of it**, *and* volume ≥ `volume_mult` (1.3) × the 50-day average
  volume. The proximity tolerance exists because a strict "must exceed by a tick" rule on a nightly
  batch scan produces a lottery on which names happened to close a cent higher, and because we fill
  at the *next open* (see `backtest-methodology.md`) — demanding a tick-perfect break at close and
  then paying an overnight gap is the worst of both worlds.
- **Design intent: the trend template and the ranking carry most of the weight; the breakout is a
  timing device.** By the time a candidate has passed the trend template (§3), the regime gate (§4)
  and the liquidity filter (§10), and then ranked in the top four by risk-adjusted momentum (§1),
  the breakout is mainly answering "is today a reasonable day to start the position?"
- **The ablation could not test this, and we should not pretend otherwise.** `volume_off` returned
  metrics identical to baseline because `volume_mult` is grid-tuned and the tuner overwrites the
  config default per fold — a structural no-op, not a finding (see
  [Ablation plan → Structural](#structural--three-000-rows-are-an-artefact-of-the-tuner-not-evidence-of-inert-components)).
  What the in-sample tuning *does* show is that folds split between `volume_mult` 1.6 and 1.0 with no
  stable preference, and settled on `donchian_window` 15 rather than the default 20 — i.e. the tuner
  wants a shorter channel and has no firm opinion on volume confirmation. That is weak evidence for
  the design intent above, not the confirmation originally anticipated here.
- `donchian_window = 20` is Turtle System 1, retained for horizon fit (20 trading days ≈ 4 weeks,
  the middle of our 1–8 week hold) rather than for its pedigree. `volume_avg_window = 50` matches
  `sma_fast` so the same 50-day window governs both the trend and volume baselines — one fewer free
  parameter.

### Where it appears in config

`strategy.donchian_window = 20`, `strategy.breakout_proximity_pct = 2.0`,
`strategy.volume_mult = 1.3`, `strategy.volume_avg_window = 50`.

---

## 8. On-Balance Volume

### Evidence

OBV was introduced by Joseph Granville (1963, *Granville's New Key to Stock Market Profits*,
Prentice-Hall), built on the premise that "volume precedes price" — that a rising cumulative
signed-volume line foreshadows price advances and that OBV/price divergence is a reversal warning.

**There is no body of peer-reviewed evidence supporting OBV as a standalone predictor.** This is a
statement about the literature, not a claimed refutation: OBV is largely absent from the academic
technical-analysis surveys (Park & Irwin 2004, 2007) that catalogue moving averages, filter rules,
channel breakouts, momentum and oscillators. What volume-based evidence does exist is about
*abnormal volume levels* (Gervais, Kaniel & Mingelgrin 2001) and about volume as a state variable
(Lo & Wang 2000, *"Trading Volume: Definitions, Data Analysis, and Implications of Portfolio
Theory"*, *Review of Financial Studies* 13(2):257–300) — not about Granville's cumulative signed sum.

Structurally, OBV is a running cumulative total with an arbitrary origin, so its *level* is
meaningless; only its slope and divergences carry claimed information, and divergence rules are
notoriously subject to ex-post visual selection — the Sullivan/Timmermann/White (1999) problem in
its purest form.

### Verdict for this system

**Omitted from all rules.** OBV appears in no gate, no signal, no ranking, and no exit.

It *is* implemented in `src/swing/indicators.py` (SPEC Contract 5: `obv(bars) -> pd.Series`) and
that is deliberate: the indicators module is a tested, reusable numerical library with reference-
value unit tests, and OBV is cheap to provide there for exploratory work, chart annotation, and
future ablation hypotheses. Shipping it in the library is not an endorsement of trading it. If a
future ablation wants to test an OBV-slope filter, the primitive is present and verified.

**Anyone reading `indicators.py` and inferring that the strategy uses OBV is reading it wrong.**

### Where it appears in config

Nowhere. No `StrategyCfg` field governs OBV, by design.

---

## 9. ATR stops and position sizing

### Evidence

**ATR.** Wilder (1978) introduced Average True Range alongside RSI, ADX/DMI and Parabolic SAR,
defining true range as max(high−low, |high−prev close|, |low−prev close|) and smoothing it with his
own recursive (Wilder) smoother. ATR is not a predictor — it is a *measurement* of realised
volatility that happens to be robust to gaps. Its evidentiary status is different in kind from
everything above: we are not claiming ATR forecasts anything. We are claiming that a stop placed a
fixed *number of typical daily ranges* below entry produces a more consistent distribution of
per-trade loss than a stop placed a fixed *percentage* below entry, because it normalises across a
$8 utility and a $300 semiconductor name.

**Chandelier exit.** Chuck LeBeau's chandelier exit — highest high since entry minus a multiple
(conventionally 3) of ATR — was popularised through Alexander Elder's *Come Into My Trading Room*
(2002, Wiley). It is a practitioner construct with no independent academic validation. Its
theoretical justification is the same as any trailing stop in a trend-following system: it enforces
the asymmetry (cut losses, let winners run) that Hurst/Ooi/Pedersen's century of trend-following
evidence depends on, and it is monotone — the stop only ratchets up, never down.

**Position sizing.** Van Tharp (*Trade Your Way to Financial Freedom*, 2nd ed. 2007, McGraw-Hill)
sets out four sizing models — units per fixed money amount, equal units, **percent risk**, and
percent volatility — and argues that position sizing dominates signal accuracy in determining a
system's return distribution. The percent-risk (fixed-fractional) model sets shares so that the
distance from entry to stop equals a fixed fraction of equity. This is the model we use. Note that
combining ATR stops with percent-risk sizing produces something close to Tharp's percent-volatility
model automatically, since the risk-per-share *is* an ATR multiple.

The fixed-fractional idea has a firmer theoretical footing than most of this document — it is the
practical, drawdown-constrained cousin of Kelly (1956) growth-optimal betting, run at a small
fraction of full Kelly precisely because expectancy is uncertain.

### Verdict for this system

Adopted wholesale. This is the part of the system we have the most confidence in, and it is
notably the part that makes no prediction at all.

- **Initial stop**: `close − atr_stop_mult × ATR(14)` with `atr_stop_mult = 2.0`. 2× is tighter than
  the Turtle 2× *of a 20-day ATR on futures* and tighter than LeBeau's 3×; the justification is
  horizon. On a 1–8 week hold, a 3× initial stop on a whole-share $100 account makes almost every
  candidate unaffordable (risk per share too large ⇒ zero shares).
- **Trailing stop**: chandelier at `chandelier_mult = 3.0` × ATR below the highest close of the last
  22 bars (`CHANDELIER_WINDOW`), ratcheted per position so the stop in force never falls. Wider than
  the initial stop on purpose: the initial stop answers "was I wrong immediately?", the trailing stop
  answers "is the trend over?" and needs room. **Two deliberate deviations from LeBeau above:** we
  take the highest *close*, not the highest high, so one intraday spike cannot set the anchor; and the
  window is a fixed 22 bars rather than "since entry", which makes the series depend only on the bars
  and not on when a position was opened — that is what lets the same function serve the nightly
  scanner and any historical date in the backtest. The ratchet is applied by the consumer;
  `swing.strategy.rules.chandelier_stop` returns the unratcheted rolling series (SPEC Contract 7).
- **Time stop**: `time_stop_days = 40` trading days ≈ 8 weeks, the upper bound of the stated holding
  horizon. This is a *definitional* parameter, not an empirical one: a position that has neither
  stopped out nor trended after 8 weeks is not the trade we intended to take, and it is occupying
  one of four slots. Capital turnover matters disproportionately on a four-slot book.
  **Measured (ETF, 2010–2026):** removing it improved the sample (PF 1.16 vs 1.02, maxDD 24.7% vs
  28.6%, average hold 18.0 days vs 16.5). The default is kept regardless — +0.13 PF is inside the
  noise band on 332 trades, and because the parameter is definitional, abandoning it would mean
  abandoning the 1–8 week horizon that the rest of this document is built around, not merely
  retuning a knob. The chandelier stop is what should capture an extended winner, not the absence
  of a horizon bound.
- **Risk sizing**: `shares = floor(equity × risk_pct/100 / (entry − stop))` with `risk_pct = 2.5`.
  2.5% is aggressive by institutional standards and appropriate only because the absolute stake is
  tiny ($2.50 on a $100 account) and because a smaller fraction would round to zero shares on
  essentially every candidate. **This is a documented consequence of account size, not a claim that
  2.5% is optimal.** As equity grows, this should come down.
- **A floor under the divisor.** The whole model divides by `entry − stop`, so an instrument whose
  stop sits a fraction of a cent below its entry — a halt, a merger target pinned at the deal price —
  would size a five-figure position against fictional risk. Anything below
  `max($0.01, 0.1% of entry)` returns zero shares with `capped_by = "risk_floor"` instead
  ([`strategy-spec.md` §10.2](strategy-spec.md#102-sizing-formula)). It is the sizing analogue of the
  ATR% floor in §1: where the denominator stops describing risk, the answer is "no size", not a very
  large number.
- **Caps**: `max_position_pct = 25.0` and `max_positions = 4` together mean a fully invested book is
  4 × 25% = 100% of equity with no leverage. The notional cap binds far more often than the risk
  formula at small equity — see `strategy-spec.md` for the worked examples.

### Where it appears in config

`strategy.atr_window = 14`, `strategy.atr_stop_mult = 2.0`, `strategy.chandelier_mult = 3.0`,
`strategy.time_stop_days = 40`, `account.risk_pct = 2.5`, `account.max_positions = 4`,
`account.max_position_pct = 25.0`, `account.equity = 100.0`.

---

## 10. Liquidity and price filters

### Evidence

This is a microstructure and execution constraint rather than a return-predictability claim, but it
has real literature behind it. Amihud (2002, *"Illiquidity and stock returns: cross-section and
time-series effects"*, *Journal of Financial Markets* 5(1):31–56) establishes illiquidity as a
priced characteristic and provides the standard price-impact measure. Chordia, Roll & Subrahmanyam
(2011) document the modern liquidity landscape. More directly relevant: essentially every anomaly
survey finds that raw anomaly returns concentrate in microcaps and low-priced stocks, where they are
unimplementable after realistic spreads — this is one of the main criticisms Park & Irwin (2007)
level at the technical-analysis literature.

Sub-$5 stocks in particular carry structural problems independent of any strategy: wide relative
spreads, reverse-split and delisting risk, higher margin requirements at most brokers, and price
levels where a one-cent tick is a meaningful percentage.

### Verdict for this system

Adopted as a hard pre-filter, applied before the trend template so that expensive computation is
never spent on names we cannot trade.

- `min_price = 5.0` — the conventional institutional floor; also removes the segment where
  backtested returns are most likely to be spread-illusory.
- `min_dollar_volume = 5_000_000.0` — 50-day average dollar volume. At $5M/day our maximum position
  ($125 at $500 equity) is ~0.0025% of daily volume, so our own market impact is nil. The filter is
  not protecting us from our own size; it is excluding names whose *quoted spreads* would invalidate
  the cost model used in the backtest, and whose data quality on a free provider is worst.

The filter is applied identically to stocks and ETFs.

### Where it appears in config

`strategy.min_price = 5.0`, `strategy.min_dollar_volume = 5_000_000.0` (window: `volume_avg_window`).

---

## 11. Fundamentals as a soft filter

### Evidence

The relevant academic anchor is **post-earnings announcement drift** and earnings-momentum: Bernard
& Thomas (1989, *"Post-Earnings-Announcement Drift: Delayed Price Response or Risk Premium?"*,
*Journal of Accounting Research* 27:1–36) documented that prices continue to drift in the direction
of an earnings surprise for months afterwards. Chan, Jegadeesh & Lakonishok (1996, *"Momentum
Strategies"*, *Journal of Finance* 51(5):1681–1713) showed price momentum and earnings momentum are
related but distinct, and that both predict returns.

Two caveats matter for us. First, the well-evidenced effect is driven by *earnings surprise*
(actual vs. expectation), not by trailing EPS/revenue growth — and surprise data is not reliably
available from a free provider. Second, Chordia, Subrahmanyam & Tong (2014) include PEAD among the
anomalies they find attenuated in the modern high-liquidity era.

### Verdict for this system

Adopted as a **soft** filter and explicitly labelled as the weakest gate in the system.

`fundamentals_ok(f, rank_below_median, cfg)` (SPEC Contract 7) uses trailing EPS growth and revenue
growth from the provider's `Fundamentals` record. Its semantics are deliberately permissive:

- **Missing data never disqualifies.** Free fundamentals coverage is patchy and stale. `None` passes.
- It is a *disqualifier for the clearly deteriorating*, not a selector for the best. A name is
  rejected only when it has data *and* that data is negative on both growth measures *and* it ranks
  below the median candidate — the `rank_below_median` argument. Something great on price action but
  merely mediocre on fundamentals is not excluded.
- **ETFs skip it entirely** (`is_etf` relaxed path). EPS growth for a sector SPDR is not a
  meaningful quantity.
- It is disableable via `fundamentals_filter = False`, and any measured contribution should be
  treated with suspicion given the data quality.

**We should not be surprised if this filter contributes nothing measurable.** It is retained mainly
as a sanity guard against buying a technically pristine chart on a business in visible decline.

### Where it appears in config

`strategy.fundamentals_filter = True`.

---

## 12. Earnings blackout

### Evidence

Earnings announcements are the largest scheduled idiosyncratic-variance events in a stock's
calendar. The volatility fact is uncontroversial: Beaver (1968, *"The Information Content of Annual
Earnings Announcements"*, *Journal of Accounting Research* 6:67–92) established that return variance
spikes around announcements, and it has been replicated continuously since. Jegadeesh & Titman
(1993) themselves observed a distinct return pattern around the earnings announcements of past
winners and losers.

The relevant point for us is not directional. It is that overnight earnings gaps **defeat stop
orders**. A stop at 2×ATR below entry provides no protection against a −20% gap; the fill happens at
the open, far through the stop. Our backtest models this honestly (gap-through fills at the open, see
`backtest-methodology.md`), which means an unmanaged earnings exposure shows up as a genuine fat left
tail in the equity curve rather than being quietly hidden.

### Verdict for this system

Adopted as an entry blackout, with two honestly-stated limitations.

- New entries are blocked when a known upcoming earnings date falls within
  `earnings_blackout_days = 10` of the signal bar. Ten days is chosen to be short enough not to
  exclude a large fraction of the universe (each name is "blacked out" roughly 10 of every ~63
  trading days ≈ 16% of the time) and long enough to cover the pre-announcement drift window where
  entering means near-certainly holding through the event.
- **Limitation 1 — the hold outlives the blackout.** With a 1–8 week hold and a quarterly earnings
  cycle, many positions *will* carry through an announcement regardless. The blackout prevents
  deliberately opening into an event; it does not make the system earnings-neutral. Contract 11
  allows an optional earnings-tighten exit for this reason.
- **Limitation 2 — unknown dates.** Free earnings-date data is incomplete. When nothing is known,
  `earnings_blackout` returns all-False (no block) and the pick is **tagged**:
  `PickRecord.earnings_known = False`, surfaced as a visible warning on the pick sheet. We chose
  fail-open plus loud warning over fail-closed because failing closed would silently delete a large,
  non-random slice of the universe (coverage gaps correlate with smaller names) — and it would do so
  invisibly, inside a backtest where there is no operator to notice.
- **How the backtest handles it now.** The runner asks the provider for each symbol's *historical*
  announcement dates and feeds the whole sequence to the same rule, so a historical entry inside a
  blackout is blocked the way the scanner would have blocked it. When no historical source is
  available at all, the run stamps `earnings_blackout_simulated: false` in `summary.json` and every
  report says in plain language that it took entries the live scanner would have blocked. The
  measured effect is still diluted by whatever the provider does not know, but the *divergence
  between backtest and scanner* is now declared rather than assumed
  ([`backtest-methodology.md` §11](backtest-methodology.md#11-known-limitations)).

### Where it appears in config

`strategy.earnings_blackout_days = 10`.

---

## 13. Cross-cutting caveat: data snooping and edge decay

Three results should be read as applying to *every* section above.

1. **Sullivan, Timmermann & White (1999)** — searching a large universe of technical rules and
   reporting the best one produces impressive in-sample results by construction. Their bootstrap
   correction, applied to 100 years of DJIA data, substantially deflates apparent performance, and
   the subsequent 10-year OOS period showed low profitability. *Our defence:* the walk-forward design
   (`backtest-methodology.md`), a deliberately small parameter set, and the fact that most parameters
   here are inherited from published conventions rather than tuned by us.

2. **McLean & Pontiff (2016, *Journal of Finance* 71(1):5–32)** — across 97 reconstructed
   cross-sectional predictors, returns are **26% lower out-of-sample and 58% lower post-publication**.
   Every effect cited in this document has been published, most of them decades ago. *Our defence:*
   none, really. This is a haircut we should mentally apply to every number in the ablation table.

3. **Bailey, Borwein, López de Prado & Zhu (2014/2015, *"Pseudo-Mathematics and Financial
   Charlatanism"*, *Notices of the AMS* 61(5):458–471; *"The Probability of Backtest Overfitting"*,
   *Journal of Computational Finance*, SSRN 2326253)** — with enough trials, an in-sample Sharpe of 2
   is achievable from pure noise; they formalise this as the probability of backtest overfitting
   (PBO) via combinatorially symmetric cross-validation, and derive a "minimum backtest length" for
   a given number of trials. *Our defence:* the gate thresholds in `GatesCfg` are set once, in
   advance, and the number of ablation variants is small and fixed (ten), not searched.

**Practical consequence.** The ablation table below is a *diagnostic* tool for understanding which
components move which metrics, not a menu for picking the best-scoring variant. Selecting the
top-scoring ablation and shipping it is exactly the failure mode all three papers describe.

---

## Ablation plan

Run by `scripts/ablations.py`, which loads the config, applies **one change at a time** via
`dataclasses.replace`, and calls `swing.backtest.runner.run_backtest` (SPEC Contract 2) with
`walkforward=True` and a per-variant `label`. It tabulates the `oos` block of each run's
`summary.json` (SPEC Contract 11) into `docs/ablation-results.md`.

Two properties of the runner make the sweep safe to run whenever you like:

- **It cannot touch the trading gate.** Every label starts with `ablate`, which is what stops the
  runner writing `reports/backtest/latest.json`, and the script verifies that prefix on every variant
  before the first backtest starts rather than trusting it. `swing scan` keeps gating on your last
  real `swing backtest` (audit DEBT-005).
- **The "Change" column is rendered from the config the sweep actually loaded**, not from the
  shipping defaults, so a table produced against a customised `config.toml` states that config's real
  baseline (audit DEBT-009).

`--validate-only` applies every variant to the config and exits in about a second, which catches a
variant that has drifted outside the config's allowed range without burning a multi-hour sweep.

All runs are walk-forward, so the reported metrics are **out-of-sample** (3-year IS / 1-year OOS
stepped annually, concatenated OOS equity).

### Results — ETF universe, 2010-01-01 to 2026-08-18

Executed 2026-08-18. Full metrics, per-variant detail and the reproducibility triple
(`config_hash` / `code_ref` / `data_hash`) are in [`ablation-results.md`](ablation-results.md).

> **Stale as of 2026-08-19 — this sweep predates the remediation pass.** These numbers were produced
> on 2026-08-18, before commits `10f6673..94e90f5` fixed the findings in
> [`CODE_AUDIT_REPORT.md`](../CODE_AUDIT_REPORT.md). Several of those fixes change what the engine
> computes, so every row below can move: the ATR% floor that drops a pegged name from the ranking
> (BUG-003), the sizing risk floor that refuses a sub-tick stop (BUG-054), the deeper pending-order
> queue that stops a zero-share candidate from consuming a slot (BUG-053), the earnings blackout now
> being simulated from historical announcement dates (A12), and the `end_of_data` exit for a symbol
> that stops printing bars (BUG-016). **No number here has been adjusted by hand.** The table will be
> replaced wholesale by the next `uv run python scripts/ablations.py --universe etf` run; until then,
> read the `code_ref` in `ablation-results.md` as the statement of which code produced it, and treat
> the qualitative conclusions below — noise-dominated sample, defaults retained — as the durable part.

**Baseline: PF 1.02, CAGR 0.06%, max drawdown 28.56%, 332 OOS trades, 41.6% exposure.** That is a
system scraping breakeven on this universe, and every delta below should be read against that
near-zero reference rather than against a healthy baseline.

| # | Component | Research section | Config flag / param varied | Baseline → variant | OOS result vs baseline |
|---|-----------|------------------|----------------------------|--------------------|------------------------|
| 0 | *Baseline (all components on)* | — | *none* | — | PF 1.02, CAGR 0.06%, maxDD 28.56%, 332 trades |
| 1 | Market regime gate | [§4](#4-regime-filter-index-above-its-200-day-sma) | `regime.enabled` | `True` → `False` | **PF +0.21** (1.24), CAGR +1.93pp, maxDD **−7.26pp** (21.30), 332 trades — *improved; default kept, see below* |
| 2 | ADX trend-strength filter | [§6](#6-macd-and-adx) | `strategy.adx_min` | `20.0` → `0.0` | PF +0.07 (1.09), CAGR +1.03pp, maxDD −4.37pp, **+127 trades** (459), exposure 53.5% — *improved; default kept* |
| 3 | Breakout volume confirmation | [§7](#7-donchian-channel-breakouts-and-volume-confirmation) | `strategy.volume_mult` | `1.3` → `1.0` | **±0.00 on every metric — grid-tuned, see "Structural" below.** Not evidence of inertness |
| 4 | Momentum skip-recent window | [§1](#1-intermediate-horizon-momentum-and-the-skip-effect) | `strategy.mom_skip_days` | `5` → `0` | PF −0.06 (0.96, below breakeven), CAGR −0.68pp, maxDD +1.13pp — *mildly supports the default* |
| 5 | Momentum horizon weighting | [§1](#1-intermediate-horizon-momentum-and-the-skip-effect) | `strategy.mom_weight_126` / `mom_weight_63` | `0.6/0.4` → `0.5/0.5` | PF −0.02 (1.00), CAGR −0.19pp, maxDD +1.17pp — *mildly supports the default* |
| 6 | Trailing stop width (tighter) | [§9](#9-atr-stops-and-position-sizing) | `strategy.chandelier_mult` | `3.0` → `2.0` | **±0.00 on every metric — grid-tuned, see "Structural" below** |
| 7 | Trailing stop width (wider) | [§9](#9-atr-stops-and-position-sizing) | `strategy.chandelier_mult` | `3.0` → `4.0` | **±0.00 on every metric — grid-tuned, see "Structural" below** |
| 8 | Time stop | [§9](#9-atr-stops-and-position-sizing) | `strategy.time_stop_days` | `40` → `10_000` (sentinel = off; `0` is invalid and would mean "exit immediately") | PF +0.13 (1.16), CAGR +1.24pp, maxDD −3.84pp, −18 trades, hold 18.0d — *improved; default kept* |
| 9 | RSI(2) mean-reversion overlay | [§5](#5-rsi2-short-horizon-mean-reversion) | `strategy.rsi2_enabled` | `False` → `True` | PF −0.03 (0.99), maxDD **+3.90pp** (32.46), **+221 trades** (553), hold 11.7d — *supports shipping OFF* |

### Interpretation

*(Every figure quoted below comes from the pre-remediation sweep — see the staleness note above.)*

**Outcome in one line: all shipping defaults are retained. None were changed by this sweep.**

#### Structural — three `±0.00` rows are an artefact of the tuner, not evidence of inert components

`volume_off`, `chandelier_2` and `chandelier_4` returned results identical to baseline **to the last
decimal on every metric**. This is by construction. `strategy.volume_mult` and
`strategy.chandelier_mult` are both members of the walk-forward in-sample tuning grid
(`backtest-methodology.md` §5); the tuner re-selects them from that grid on every fold, so the
config default these variants edit is **overwritten before a single out-of-sample bar is traded**.
The ablation changed an input that the walk-forward machinery does not read.

Evidence for those two components must therefore come from **which values the folds actually chose**
during in-sample tuning, not from the OOS delta:

| Grid parameter | What the folds selected | Reading |
|----------------|-------------------------|---------|
| `volume_mult` | split between 1.6 and 1.0 across folds | no stable preference — volume confirmation is neither consistently earning its keep nor consistently harmful |
| `donchian_window` | 15 dominant across folds (default is 20) | the tuner prefers a **shorter** channel than the Turtle-inherited default of §7 |

`donchian_window` has no ablation row at all for the same reason — it is grid-tuned, so an ablation
of it would also have returned `±0.00`.

**Do not read the `+0.00` rows as "this component does nothing."** They say only that the tuner, not
the config file, is in charge of those two knobs. Anyone extending this table should first check
whether the parameter they intend to vary is in the tuning grid; if it is, the variant is a no-op.

#### Three removals *improved* this sample — and the defaults are kept anyway

`regime_off` (+0.21 PF, −7.3pp maxDD), `adx_off` (+0.07 PF) and `time_stop_off` (+0.13 PF) all beat
baseline. The shipping configuration nonetheless keeps all three enabled, for four reasons:

1. **The differences are inside the noise band.** 332 OOS trades; the entire table spans PF
   0.96–1.24 around a baseline of 1.02. Per §13 and `backtest-methodology.md` §5, PF differences
   below roughly 0.2 on this sample size are not distinguishable from noise. `regime_off` sits right
   at that boundary; the other two are well inside it.
2. **All three are risk-policy components, and this window under-samples the states they exist
   for.** 2010–2026 contains no extended bear market of the 2000–2002 or 2007–2009 kind. The regime
   gate and the time stop are insurance premiums, and a sample without a fire shows insurance as
   pure cost. Daniel & Moskowitz (2016) is specifically about momentum's behaviour in panic states —
   the states this window barely visits.
3. **The ETF universe is the strategy's weakest habitat**, and its documented lower bound
   (`backtest-methodology.md` §7): ~40 correlated, diversified baskets give a cross-sectional
   ranking very little dispersion to exploit. Re-specifying risk policy from the weakest-habitat run
   would be backwards.
4. **Selecting the three best rows is the exact failure mode §13 exists to prevent** — post-hoc
   selection over 10 trials on a near-breakeven, noise-dominated sample (Sullivan, Timmermann &
   White 1999).

A user who wants the higher-PF configuration can flip these — they are supported config keys, not
hidden switches. What they would be giving up:

| Flip | Gains on this sample | Gives up |
|------|----------------------|----------|
| `regime.enabled = false` | +0.21 PF, +1.93pp CAGR, −7.3pp maxDD | the only rule that moves the book to cash in a sustained downtrend — fully exposed through the next 2008-style regime, which this sample does not contain |
| `strategy.adx_min = 0.0` | +0.07 PF, +1.03pp CAGR | 38% more trades (459 vs 332) at 53.5% exposure vs 41.6% — more turnover, more cost, and no screen against non-trending chop |
| `strategy.time_stop_days = 10_000` | +0.13 PF, +1.24pp CAGR | the 8-week horizon bound itself — slots occupied indefinitely, average hold stretching to 18.0 days, and the system ceasing to be the 1–8 week strategy this spec describes |

#### Two removals mildly degraded — weak support for the momentum specification

`skip_off` (−0.06 PF, dropping the system below breakeven at 0.96) and `weights_equal` (−0.02 PF).
Both are inside the noise band, and neither is evidence on its own. What they do is **fail to
contradict the prior**: the 5-day skip and the 0.6/0.4 tilt toward the ~6-month leg each moved the
result in the direction Jegadeesh & Titman (1993) and Novy-Marx (2012) predict — recent-window
contamination hurts, weighting the intermediate horizon helps. Consistent-with, not confirmation-of.

#### RSI(2) is empirically rejected on the terms set in advance

`rsi2_on`: PF 0.99 (−0.03), max drawdown 32.46% (+3.90pp), 553 trades (+221), average hold 11.7 days
versus 16.5. That is precisely the signature §5 anticipated — many more, much shorter trades, no
profit-factor improvement, and a worse drawdown.

§5 committed in advance to a falsifiable condition: enable the overlay only if a walk-forward OOS run
beats baseline on profit factor **and** max drawdown. It did neither. `strategy.rsi2_enabled = False`
stands, and it now rests on an out-of-sample result from our own data and our own cost model rather
than on an inference from the post-2010 decay literature (Chordia, Subrahmanyam & Tong 2014).

#### Scope — ETF universe only, deliberately

This sweep is ETF-only. Stock-universe ablations were **not** run: at roughly two hours per
walk-forward run, ten variants is ~20 hours of compute to resolve differences the ETF sweep already
shows are noise-dominated. The stock universe gets the headline walk-forward validation and the
survivorship haircut instead (`backtest-methodology.md` §7–§8), which is where the deployment
decision is actually made.

Two consequences worth stating plainly:

- These ETF results are **survivorship-free** and take no haircut, but they are also the strategy's
  documented *lower bound*, not its expected performance.
- Baseline PF 1.02 clears deployment-rule condition 3 (`backtest-methodology.md` §8: ETF-only
  `PF_oos ≥ 1.0`) by the thinnest possible margin. That is a floor being scraped, not a good result,
  and it should temper any reading of the stock-universe headline.

#### Why `regime_off` has an identical trade count — verified, not a glitch

`regime_off` reports **exactly** the same trade count (332) and exposure (41.58%) as baseline while
win rate, average win and profit factor all move. This was checked directly against the two runs'
`trades.csv` and is benign: the lists share 261 `(symbol, entry_date)` pairs and differ by exactly
**71 swapped entries each way**, with total P&L of $292.92 (baseline) versus $2,910.77
(`regime_off`). The swaps cluster precisely where a SPY-above-200-SMA gate binds — `regime_off`
enters ARKK on 2020-04-28, mid V-bottom with SPY still below its 200-day, where baseline waits until
2020-06-02 after the reclaim; `regime_off` also takes five 2022 bear-market entries and several
2015/2021-correction entries that baseline has none of (baseline contributes zero differing 2022
entries). The identical count is **slot saturation**: with four slots effectively always full,
turnover is exit-driven, so a 71-for-71 entry swap preserves the count, and near-identical exposure
follows for the same reason. The regime gate is wired correctly; it is changing *which* names occupy
slots, not how many slot-days are filled.

### Components deliberately not ablated

Components deliberately **not** ablated, and why:

| Component | Why not in the ablation set |
|-----------|------------------------------|
| Trend template (`sma_*`, `min_above_low_mult`, `max_below_high_pct`) | Disabling it does not produce a variant of this strategy; it produces a different strategy (unfiltered breakout). Its ±25% parameter sensitivity is covered by the walk-forward sensitivity tables in `backtest-methodology.md`. |
| Liquidity filters (`min_price`, `min_dollar_volume`) | Relaxing them makes the cost model invalid, so the resulting metrics would not be comparable. |
| ATR stops (`atr_stop_mult`) | There is no "off" — every position needs a stop. Width is covered by sensitivity tables. |
| Fundamentals soft filter (`fundamentals_filter`) | Data quality on the free provider is too poor for a measured difference to be interpretable. Can be run manually. |
| Earnings blackout (`earnings_blackout_days`) | Same data-coverage problem; the unknown-date fail-open path (§12) means the measured effect would be diluted by missing dates. |
| MACD / OBV | Not implemented in any rule (§6, §8). Nothing to ablate. |
| `donchian_window` (and any other grid member) | In the walk-forward tuning grid, so the tuner overwrites the config default per fold and the variant is a guaranteed no-op — see "Structural" above. `volume_mult` and `chandelier_mult` have rows only because their no-op status was discovered by running them. |

**How to read the results.** Look for components whose removal *degrades* OOS profit factor or
materially *worsens* max drawdown — those are earning their keep. A component whose removal barely
moves anything *is* a candidate for deletion on parsimony grounds (fewer parameters ⇒ lower PBO per
Bailey et al.) — **unless** it is grid-tuned, in which case a flat row says nothing at all. A
component whose removal *improves* results should be scrutinised, not deleted: at 332 OOS trades on
a near-breakeven baseline, most differences in this table are inside the noise band. Per §13,
**do not select the best-scoring variant as the shipping configuration.**

**What this sweep actually decided.** Nothing was changed. Three components (regime gate, ADX
filter, time stop) scored better switched off on this sample and were kept on anyway, for the
reasons set out above; two momentum parameters were weakly supported; one optional overlay was
rejected on a pre-registered criterion; three rows carried no information at all. An ablation sweep
is a diagnostic that can *fail* to move the defaults, and on a noise-dominated sample that is the
expected result rather than a disappointing one. The honest summary is **"defaults retained, and
here is why"** — not "defaults updated."

---

## Parameter traceability

Every default in `StrategyCfg`, `RegimeCfg` and the risk-relevant `AccountCfg` fields
(SPEC Contract 1) maps to a section above.

| Config key | Default | Research section |
|------------|---------|------------------|
| `strategy.min_price` | `5.0` | [§10 Liquidity](#10-liquidity-and-price-filters) |
| `strategy.min_dollar_volume` | `5_000_000.0` | [§10 Liquidity](#10-liquidity-and-price-filters) |
| `strategy.sma_fast` | `50` | [§3 Trend template](#3-moving-average-trend-filters-and-the-trend-template) |
| `strategy.sma_mid` | `150` | [§3 Trend template](#3-moving-average-trend-filters-and-the-trend-template) |
| `strategy.sma_slow` | `200` | [§3 Trend template](#3-moving-average-trend-filters-and-the-trend-template) |
| `strategy.sma_slow_rising_days` | `21` | [§3 Trend template](#3-moving-average-trend-filters-and-the-trend-template) (Minervini criterion 3) |
| `strategy.min_above_low_mult` | `1.25` | [§3 Trend template](#3-moving-average-trend-filters-and-the-trend-template) (criterion 6) |
| `strategy.max_below_high_pct` | `25.0` | [§2 52-week high](#2-52-week-high-proximity) + [§3](#3-moving-average-trend-filters-and-the-trend-template) (criterion 7) |
| `strategy.adx_min` | `20.0` | [§6 MACD and ADX](#6-macd-and-adx) |
| `strategy.donchian_window` | `20` | [§7 Breakouts](#7-donchian-channel-breakouts-and-volume-confirmation) |
| `strategy.breakout_proximity_pct` | `2.0` | [§7 Breakouts](#7-donchian-channel-breakouts-and-volume-confirmation) |
| `strategy.volume_mult` | `1.3` | [§7 Breakouts](#7-donchian-channel-breakouts-and-volume-confirmation) (Gervais et al. 2001) |
| `strategy.volume_avg_window` | `50` | [§7 Breakouts](#7-donchian-channel-breakouts-and-volume-confirmation) + [§10](#10-liquidity-and-price-filters) |
| `strategy.atr_window` | `14` | [§9 ATR stops](#9-atr-stops-and-position-sizing) (Wilder 1978) |
| `strategy.atr_stop_mult` | `2.0` | [§9 ATR stops](#9-atr-stops-and-position-sizing) |
| `strategy.chandelier_mult` | `3.0` | [§9 ATR stops](#9-atr-stops-and-position-sizing) (LeBeau) |
| `strategy.time_stop_days` | `40` | [§9 ATR stops](#9-atr-stops-and-position-sizing) |
| `strategy.earnings_blackout_days` | `10` | [§12 Earnings blackout](#12-earnings-blackout) |
| `strategy.mom_weight_126` | `0.6` | [§1 Momentum](#1-intermediate-horizon-momentum-and-the-skip-effect) |
| `strategy.mom_weight_63` | `0.4` | [§1 Momentum](#1-intermediate-horizon-momentum-and-the-skip-effect) |
| `strategy.mom_skip_days` | `5` | [§1 Momentum](#1-intermediate-horizon-momentum-and-the-skip-effect) |
| `strategy.rsi2_enabled` | `False` | [§5 RSI(2)](#5-rsi2-short-horizon-mean-reversion) |
| `strategy.fundamentals_filter` | `True` | [§11 Fundamentals](#11-fundamentals-as-a-soft-filter) |
| `regime.enabled` | `True` | [§4 Regime filter](#4-regime-filter-index-above-its-200-day-sma) |
| `regime.symbol` | `"SPY"` | [§4 Regime filter](#4-regime-filter-index-above-its-200-day-sma) |
| `regime.sma_window` | `200` | [§4 Regime filter](#4-regime-filter-index-above-its-200-day-sma) (Faber 2007) |
| `account.equity` | `100.0` | [§9 ATR stops](#9-atr-stops-and-position-sizing) |
| `account.risk_pct` | `2.5` | [§9 ATR stops](#9-atr-stops-and-position-sizing) (Van Tharp percent-risk model) |
| `account.max_positions` | `4` | [§9 ATR stops](#9-atr-stops-and-position-sizing) |
| `account.max_position_pct` | `25.0` | [§9 ATR stops](#9-atr-stops-and-position-sizing) |

---

## Bibliography

Peer-reviewed and working-paper sources, with stable links where available.

**Momentum and cross-sectional predictability**

- Jegadeesh, N. & Titman, S. (1993). "Returns to Buying Winners and Selling Losers: Implications for
  Stock Market Efficiency." *Journal of Finance* 48(1), 65–91.
  https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1993.tb04702.x
- Novy-Marx, R. (2012). "Is momentum really momentum?" *Journal of Financial Economics* 103(3),
  429–453. https://www.sciencedirect.com/science/article/abs/pii/S0304405X11001152
- George, T.J. & Hwang, C.-Y. (2004). "The 52-Week High and Momentum Investing." *Journal of Finance*
  59(5), 2145–2176. https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.2004.00695.x
- Chan, L.K.C., Jegadeesh, N. & Lakonishok, J. (1996). "Momentum Strategies." *Journal of Finance*
  51(5), 1681–1713.
- Asness, C.S., Moskowitz, T.J. & Pedersen, L.H. (2013). "Value and Momentum Everywhere." *Journal of
  Finance* 68(3), 929–985. https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12021
- Daniel, K. & Moskowitz, T.J. (2016). "Momentum crashes." *Journal of Financial Economics* 122(2),
  221–247. https://www.kentdaniel.net/papers/published/jfe_16.pdf
- Barroso, P. & Santa-Clara, P. (2015). "Momentum has its moments." *Journal of Financial Economics*
  116(1), 111–120. https://www.sciencedirect.com/science/article/abs/pii/S0304405X14002566

**Trend following and moving averages**

- Faber, M.T. (2007). "A Quantitative Approach to Tactical Asset Allocation." *Journal of Wealth
  Management*, Spring 2007. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=962461
- Hurst, B., Ooi, Y.H. & Pedersen, L.H. (2017). "A Century of Evidence on Trend-Following Investing."
  *Journal of Portfolio Management* 44(1), 15–29.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2993026
- Brock, W., Lakonishok, J. & LeBaron, B. (1992). "Simple Technical Trading Rules and the Stochastic
  Properties of Stock Returns." *Journal of Finance* 47(5), 1731–1764.

**Technical analysis surveys and data snooping**

- Park, C.-H. & Irwin, S.H. (2007). "What Do We Know About the Profitability of Technical Analysis?"
  *Journal of Economic Surveys* 21(4), 786–826.
  https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-6419.2007.00519.x
- Park, C.-H. & Irwin, S.H. (2004). "The Profitability of Technical Analysis: A Review." AgMAS
  Project Research Report 2004-04. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=603481
- Sullivan, R., Timmermann, A. & White, H. (1999). "Data-Snooping, Technical Trading Rule
  Performance, and the Bootstrap." *Journal of Finance* 54(5), 1647–1691.
  https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00163
- McLean, R.D. & Pontiff, J. (2016). "Does Academic Research Destroy Stock Return Predictability?"
  *Journal of Finance* 71(1), 5–32. https://onlinelibrary.wiley.com/doi/abs/10.1111/jofi.12365
- Bailey, D.H., Borwein, J.M., López de Prado, M. & Zhu, Q.J. (2014). "Pseudo-Mathematics and
  Financial Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample Performance."
  *Notices of the American Mathematical Society* 61(5), 458–471.
- Bailey, D.H., Borwein, J.M., López de Prado, M. & Zhu, Q.J. (2015). "The Probability of Backtest
  Overfitting." *Journal of Computational Finance* 20(4), 39–69.
  https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253

**Volume, liquidity and microstructure**

- Gervais, S., Kaniel, R. & Mingelgrin, D.H. (2001). "The High-Volume Return Premium." *Journal of
  Finance* 56(3), 877–919. https://onlinelibrary.wiley.com/doi/abs/10.1111/0022-1082.00349
- Lo, A.W. & Wang, J. (2000). "Trading Volume: Definitions, Data Analysis, and Implications of
  Portfolio Theory." *Review of Financial Studies* 13(2), 257–300.
- Amihud, Y. (2002). "Illiquidity and stock returns: cross-section and time-series effects."
  *Journal of Financial Markets* 5(1), 31–56.
- Chordia, T., Roll, R. & Subrahmanyam, A. (2011). "Recent trends in trading activity and market
  quality." *Journal of Financial Economics* 101(2), 243–263.
  https://www.sciencedirect.com/science/article/abs/pii/S0304405X11000730
- Chordia, T., Subrahmanyam, A. & Tong, Q. (2014). "Have capital market anomalies attenuated in the
  recent era of high liquidity and trading activity?" *Journal of Accounting and Economics* 58(1),
  41–58. https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2029057

**Earnings**

- Beaver, W.H. (1968). "The Information Content of Annual Earnings Announcements." *Journal of
  Accounting Research* 6 (Supplement), 67–92.
- Bernard, V.L. & Thomas, J.K. (1989). "Post-Earnings-Announcement Drift: Delayed Price Response or
  Risk Premium?" *Journal of Accounting Research* 27 (Supplement), 1–36.

**Data quality and survivorship**

- Shumway, T. (1997). "The Delisting Bias in CRSP Data." *Journal of Finance* 52(1), 327–340.
  https://www.tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf
- Elton, E.J., Gruber, M.J. & Blake, C.R. (1996). "Survivorship Bias and Mutual Fund Performance."
  *Review of Financial Studies* 9(4), 1097–1120.
- Brown, S.J., Goetzmann, W., Ibbotson, R.G. & Ross, S.A. (1992). "Survivorship Bias in Performance
  Studies." *Review of Financial Studies* 5(4), 553–580.

**Practitioner sources (not peer-reviewed; used as construct definitions, not as evidence)**

- Wilder, J.W. (1978). *New Concepts in Technical Trading Systems*. Trend Research. — RSI, ATR,
  ADX/DMI, Parabolic SAR and Wilder smoothing.
- Granville, J.E. (1963). *Granville's New Key to Stock Market Profits*. Prentice-Hall. — OBV.
- Minervini, M. (2013). *Trade Like a Stock Market Wizard: How to Achieve Super Performance in Stocks
  in Any Market*. McGraw-Hill. — trend template, SEPA.
- Connors, L. & Alvarez, C. (2008). *Short Term Trading Strategies That Work*. TradingMarkets
  Publishing. — RSI(2) rules.
- Connors, L. & Raschke, L.B. (1996). *Street Smarts: High Probability Short-Term Trading
  Strategies*. M. Gordon Publishing.
- Elder, A. (2002). *Come Into My Trading Room*. Wiley. — popularised Chuck LeBeau's chandelier exit.
- Faith, C. (2007). *Way of the Turtle*. McGraw-Hill. — Turtle/Donchian breakout system as taught by
  Richard Dennis and William Eckhardt.
- Tharp, V.K. (2007). *Trade Your Way to Financial Freedom*, 2nd ed. McGraw-Hill. — position-sizing
  models including percent-risk and percent-volatility.
- Kelly, J.L. (1956). "A New Interpretation of Information Rate." *Bell System Technical Journal*
  35(4), 917–926. — theoretical basis for fractional-of-equity betting.
