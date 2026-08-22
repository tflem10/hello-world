# Strategy Experiment Log

A dated record of deliberate strategy experiments: what was changed, what happened, and what
may honestly be concluded. This is a research record, not a recommendation. Nothing here is
advice to trade any security or any configuration.

**Contents**

- [Programme: 2026-08-21 — diagnosing the gate failure](#programme-2026-08-21--diagnosing-the-gate-failure)
- [Experiment definitions](#experiment-definitions)
- [Results](#results)
- [Findings](#findings)
- [Limitations — read before quoting any number above](#limitations--read-before-quoting-any-number-above)
- [What would actually settle this](#what-would-actually-settle-this)
- [Reproducibility and verification notes](#reproducibility-and-verification-notes)

---

## Programme: 2026-08-21 — diagnosing the gate failure

**Date run:** 2026-08-21. **Logged:** 2026-08-22.

**Purpose.** The shipping configuration fails its own deployment gate. Over the out-of-sample
window it returns a profit factor of 1.18 against a required 1.30, and a maximum drawdown of
52.88% against a tolerated 35.0% (`config.example.toml` `[gates]`: `min_profit_factor = 1.3`,
`max_drawdown_pct = 35.0`, `min_trades = 30`). The programme asks a diagnostic question — *which
part of the rule set is responsible?* — and not a search question. It was not run to find a
configuration that passes.

**Code.** Experiments were executed across three commits, all ancestors of `c90e407`
(`backtest: make the walk-forward tuning grid configurable`). That commit is what made two of the
experiments possible at all: the walk-forward tuning grid was hardcoded before it, so stop widths
and channel lengths could not be varied. See
[Reproducibility and verification notes](#reproducibility-and-verification-notes) for the exact
per-run code and data provenance, which is not uniform and matters.

**Method.** Every run is a walk-forward: 3-year in-sample tuning window, 1-year out-of-sample
window, stepped annually, out-of-sample stretches concatenated on returns. Thirteen folds cover
2013-01-01 to 2025-12-31. Each fold's tuner searches 81 parameter combinations in-sample and
applies the single winner, untouched, to the following year. All headline metrics below are the
**out-of-sample** (`oos`) block of each run's `summary.json` — the numbers the gate reads.

**Scope.** Eleven experiments on the full stock universe, plus one replication of the combined
configuration on the ETF universe. Two pre-existing baselines (`full-walkforward-r1`,
`etf-walkforward-r1`, both run 2026-08-19) serve as controls. The stock runs drew on a
1546-instrument universe snapshot and loaded 1545 of them (`CWEN-A` is delisted); the ETF runs used
the 40-instrument ETF list of the time. **That universe snapshot has since been enlarged** — see
[Reproducibility and verification notes](#reproducibility-and-verification-notes) — so quote each
run's own `n_symbols`, not the current universe file.

### The `ablate` prefix, and why it is not cosmetic

Every experiment label begins with `ablate`. That prefix is what stops the backtest runner
writing `reports/backtest/latest.json`, and `latest.json` is the file the deployment gate reads
before `swing scan` will emit a single pick. Had these runs been labelled anything else, the last
one to finish would have become the gate's opinion of the strategy — and the best-scoring
experiment in a twelve-run search is precisely the number that must never be allowed to authorise
trading.

Verified: `reports/backtest/latest.json` still carries `"label": "full-walkforward-r1"`. The
experiment programme did not touch the gate.

---

## Experiment definitions

Each run's `summary.json` stores a `config_hash` — a SHA-256 over the strategy, backtest, gates,
account and regime settings — but not the configuration itself. The table below was therefore
**verified by reconstruction**: the shipping `config.toml` was loaded, each described change
applied via `dataclasses.replace`, and the resulting hash compared against the recorded one. All
twelve matched exactly, and the unmodified `config.toml` reproduces the baseline hash
`c6782f8db70b`. The changes below are confirmed, not assumed.

| Run directory | Change from shipping defaults | `config_hash` |
|---|---|---|
| `full-walkforward-r1` | *(control — shipping defaults)* | `c6782f8db70b` |
| `ablate-exp-strict-entry` | `breakout_proximity_pct` 2.0 → 0.0 | `a4eee387cee5` |
| `ablate-exp-wide-stops` | tuning grid `atr_stop_mult` [2.5, 3.5, 4.5], `chandelier_mult` [4.0, 5.0, 6.0] | `4383fcfb2c4a` |
| `ablate-exp-long-breakout` | tuning grid `donchian_window` [30, 45, 60] | `5ec2839f9db1` |
| `ablate-exp-strong-trend` | `adx_min` 20 → 30 | `1ca451968bde` |
| `ablate-exp-near-high` | `max_below_high_pct` 25 → 8 | `f9bf58c3544c` |
| `ablate-exp-concentrate` | `max_positions` 4 → 2, `max_position_pct` 25 → 40 | `f566aa4e054f` |
| `ablate-exp-diversify` | `max_positions` 4 → 10, `max_position_pct` 25 → 12 | `06a3705c15ea` |
| `ablate-exp-hold-longer` | `time_stop_days` 40 → 120 | `06a31ef746ea` |
| `ablate-exp-regime-fast` | `regime.sma_window` 200 → 50 | `9f15d37f1c75` |
| `ablate-exp-quality` | `min_dollar_volume` 5M → 25M, `min_price` 5 → 15 | `c33cf73aa320` |
| `ablate-exp-combo` | strict-entry **and** wide-stops together | `06680ceb9545` |
| `ablate-exp-combo-etf` | same as combo, ETF universe | `06680ceb9545` |

The `strict-entry` change addresses an audit observation: with a 2% proximity band, the breakout
condition is close to always true for a name that has already passed the trend template in an
uptrend, which makes the volume test the de-facto trigger. Setting the band to zero forces a real
new high.

---

## Results

Out-of-sample, 2013-01-01 to 2025-12-31, full stock universe (1545 symbols loaded). Sorted by
profit factor.

| Run | Profit factor | CAGR | Max drawdown | Win rate | Trades | Avg hold (days) |
|---|---:|---:|---:|---:|---:|---:|
| `ablate-exp-strict-entry` | 1.25 | 7.80% | 49.43% | 32.66% | 689 | 16.3 |
| `ablate-exp-combo` | 1.21 | 4.69% | 34.65% | 46.49% | 413 | 27.9 |
| **`full-walkforward-r1`** *(baseline)* | **1.18** | **4.97%** | **52.88%** | **33.82%** | **692** | **16.3** |
| `ablate-exp-concentrate` | 1.17 | 2.95% | 54.11% | 32.80% | 375 | 15.3 |
| `ablate-exp-strong-trend` | 1.14 | 4.79% | 41.96% | 30.94% | 695 | 16.0 |
| `ablate-exp-near-high` | 1.14 | 3.82% | 48.90% | 34.26% | 686 | 16.5 |
| `ablate-exp-wide-stops` | 1.11 | 2.92% | 33.13% | 41.94% | 422 | 27.5 |
| `ablate-exp-long-breakout` | 1.11 | 2.53% | 39.86% | 34.38% | 672 | 16.8 |
| `ablate-exp-diversify` | 1.03 | 0.08% | 54.17% | 33.64% | 1644 | 17.0 |
| `ablate-exp-hold-longer` | 1.02 | −0.66% | 55.47% | 32.62% | 650 | 17.3 |
| `ablate-exp-regime-fast` | 1.00 | −1.43% | 56.13% | 34.66% | 629 | 17.3 |
| `ablate-exp-quality` | 0.96 | −3.26% | 57.41% | 32.86% | 770 | 14.6 |
| *SPY buy-and-hold* | — | *14.56%* | *33.72%* | — | — | — |

ETF universe, identical window:

| Run | Profit factor | CAGR | Max drawdown | Win rate | Trades | Avg hold (days) |
|---|---:|---:|---:|---:|---:|---:|
| `etf-walkforward-r1` *(ETF baseline)* | 1.02 | 0.06% | 28.56% | 37.35% | 332 | 16.5 |
| `ablate-exp-combo-etf` | 1.08 | 0.71% | 22.47% | 45.19% | 208 | 27.7 |

**The SPY reference row** is buy-and-hold on the dividend- and split-adjusted cached SPY series
(`~/.swing/cache/daily/SPY.parquet`), measured over exactly the same 3270 trading days
(2013-01-02 to 2025-12-31) as the strategy equity curves, using the project's own
`swing.backtest.metrics` functions so the conventions match: CAGR 14.5587%, maximum drawdown
33.7173%, total return 484.79%. Profit factor, win rate and hold are undefined for a single
never-closed position.

**Gate status.** Every configuration fails. Profit factor is the binding constraint in all twelve
cases; the best is `strict-entry` at 1.25 against the required 1.30. Two configurations —
`wide-stops` (33.13%) and `combo` (34.65%) — bring maximum drawdown inside the 35.0% tolerance;
the other ten do not. All clear `min_trades = 30`.

---

## Findings

### Three hypotheses refuted outright

Nine of the ten single-change experiments left profit factor at or below the baseline's 1.18.
Only `strict-entry` improved it. Five made things worse on *all three* headline metrics
simultaneously — profit factor, CAGR and maximum drawdown: `concentrate`, `diversify`,
`hold-longer`, `regime-fast`, `quality`.

Three of these were direct tests of stated hypotheses about why the strategy underperforms, and
each is **refuted**:

- **"Holds are too short; the time stop is cutting winners."** `ablate-exp-hold-longer`
  (`time_stop_days` 40 → 120) took profit factor from 1.18 to 1.02 and CAGR from +4.97% to
  −0.66%. Tripling the horizon bound moved the system from marginally profitable to losing.
  Average hold rose only from 16.3 to 17.3 days, which is itself informative: the time stop was
  not the binding exit for most positions, so relaxing it mostly retained losers.
- **"The 200-day regime gate is too slow; it re-enters late after drawdowns."**
  `ablate-exp-regime-fast` (`regime.sma_window` 200 → 50) gave profit factor 1.00 and CAGR
  −1.43%, with drawdown *worse* at 56.13%. A faster gate whipsawed.
- **"The universe contains too much junk; a stricter quality screen will help."**
  `ablate-exp-quality` (`min_dollar_volume` 5M → 25M, `min_price` 5 → 15) was the worst run in
  the programme: profit factor 0.96, CAGR −3.26%, drawdown 57.41%, and *more* trades (770 vs
  692). Restricting the universe to larger, more liquid names removed the dispersion the
  cross-sectional ranking needs.

Refuted hypotheses are the most trustworthy output of this programme. They are the results least
exposed to the selection problem described under [Limitations](#limitations--read-before-quoting-any-number-above):
nobody was hoping for them, and they are not candidates for adoption.

Four further changes reduced drawdown while lowering profit factor — `wide-stops` (−19.76pp),
`long-breakout` (−13.02pp), `strong-trend` (−10.92pp) and `near-high` (−3.99pp). They are
discussed below and are not "neutral", but none improved the gate's binding metric.

### `strict-entry` was the only single change to improve profit factor

Forcing a genuine new high (`breakout_proximity_pct` 2.0 → 0.0) raised profit factor from 1.18 to
1.25, CAGR from +4.97% to +7.80%, and *reduced* drawdown from 52.88% to 49.43% — with essentially
the same number of trades (689 vs 692) and the same average hold (16.3 days).

That last detail is the interesting part. The change did not filter the book down; it changed
*which* names were bought while leaving turnover almost identical. This contradicts the design
intent recorded in
[`indicator-research.md` §7](indicator-research.md#7-donchian-channel-breakouts-and-volume-confirmation),
which held that the breakout is a timing device and deliberately not load-bearing. On this window
it is load-bearing, and the 2% tolerance was costing rather than buying anything.

### `wide-stops`: the whipsaw hypothesis was mechanically right and financially wrong

The hypothesis was that tight ATR stops whipsaw out of positions that would otherwise have
worked. Every mechanical prediction of that hypothesis came true:

| | Baseline | `wide-stops` | Change |
|---|---:|---:|---:|
| Max drawdown | 52.88% | 33.13% | −19.76pp |
| Win rate | 33.82% | 41.94% | +8.13pp |
| Average hold | 16.3d | 27.5d | +11.1d |
| Trades | 692 | 422 | −39.0% |
| **Profit factor** | **1.18** | **1.11** | **−0.07** |
| **CAGR** | **4.97%** | **2.92%** | **−2.05pp** |

Positions survived longer, were stopped out less often, and the equity curve became far less
violent. And the strategy made *less money*. The tight stops were net-positive for returns: they
were cutting losers faster than they were cutting winners, and paying for a calmer ride with a
third of the return.

This is the cleanest result in the programme, because the mechanism and the outcome point in
opposite directions. A hypothesis that predicts a mechanism can be confirmed on the mechanism and
still be wrong about the thing that matters.

### The position-count curve is a positive finding about the ranking

| `max_positions` | Run | Profit factor |
|---:|---|---:|
| 2 | `ablate-exp-concentrate` | 1.17 |
| 4 | `full-walkforward-r1` | 1.18 |
| 10 | `ablate-exp-diversify` | 1.03 |

Profit factor is flat between two and four positions and collapses toward breakeven at ten. If
the momentum ranking were noise, spreading capital across more names would not systematically
degrade the per-dollar result — it would look roughly like sampling from the same distribution.
Instead, positions five through ten are materially worse than positions one through four.

That is evidence the ranking carries genuine signal for the top few names, and then dilutes. It
is a positive finding about the component the whole strategy rests on, and it arrives from an
experiment that was not designed to test the ranking at all. Note that concentrating further
(two positions) bought nothing: profit factor was flat, CAGR fell to 2.95% and drawdown rose to
54.11% — the ranking's edge does not sharpen below four names, it just gets less diversified.

### `combo`: the levers overlap rather than add

Combining `strict-entry` and `wide-stops` gives profit factor 1.21 — *between* strict's 1.25 and
wide's 1.11, not above either. The two changes are not additive; they overlap. Both reduce the
population of marginal entries, and applying both does not remove twice as many.

What the combination does change is the *shape* of the return stream:

| | Baseline | `combo` |
|---|---:|---:|
| CAGR | 4.97% | 4.69% |
| CAGR excluding each run's own best year | 0.85%/yr | 3.02%/yr |
| Trailing four years (2022–2025) | −8.22%/yr | +0.36%/yr |
| Standard deviation of annual returns | 23.75pp | 13.61pp |
| Max drawdown | 52.88% | 34.65% |
| Worst single-year drawdown | 34.45% | 24.74% |

The baseline's entire out-of-sample record depends on one year. Strip 2021 (+69.64%) and what
remains compounds at 0.85%/yr over the other twelve years. Strip `combo`'s best year (2013,
+26.96%) and it still compounds at 3.02%/yr. The crisis years move in the same direction:

| Year | Baseline | `combo` |
|---|---:|---:|
| 2018 | −18.29% | −3.21% |
| 2022 | −17.33% | −13.25% |
| 2024 | −19.80% | +10.78% |

And `combo`'s 34.65% drawdown is the second of only two configurations in the programme to fall
inside the gate's 35.0% tolerance.

**This is not a uniform improvement, and should not be described as one.** `combo` is worse than
baseline in six of thirteen years. Its worst single year (−21.38% in 2015) is *deeper* than the
baseline's worst (−19.80% in 2024). Both have five losing years. It gives up the upside — 2021
falls from +69.64% to +16.76%, 2019 from +25.65% to +17.02% — and its headline CAGR is slightly
*lower* than baseline. What it removes is the dependence on a single year, not the losses.

### The ETF replication reproduced the mechanical signature

`ablate-exp-combo-etf` applies the identical configuration to the ETF universe (40 symbols at the
time of the run) — a different instrument set that played no part in choosing these two levers.
Against the ETF baseline:

| | ETF baseline | `combo-etf` | Change |
|---|---:|---:|---:|
| Profit factor | 1.02 | 1.08 | +0.06 |
| Win rate | 37.35% | 45.19% | +7.84pp |
| Trades | 332 | 208 | −37.3% |
| Average hold | 16.5d | 27.7d | +11.2d |
| Max drawdown | 28.56% | 22.47% | −6.09pp |

Every mechanical effect reproduced with the same sign and a similar magnitude: roughly a third
fewer trades, eleven more days per hold, a substantially higher win rate, a shallower drawdown.

Two honest qualifications. First, the ETF universe is a weaker habitat by design — forty
correlated baskets give a cross-sectional ranking little dispersion — and both runs sit near
breakeven, so the profit-factor movement (1.02 → 1.08) is small in absolute terms and on a small
sample. Second, this universe is **not untouched data**: the ten-variant ablation sweep recorded
in [`ablation-results.md`](ablation-results.md) was run on it, and it has informed component
decisions before. It is an independent *instrument set*, not an independent *sample*. The
replication raises confidence that the mechanism is real rather than an artefact of the stock
universe. It does not constitute out-of-sample validation.

### Methodological red flag: the tuner pins to the edge of whatever range it is offered

When the in-sample tuner is given wider stops, it does not settle in the middle of them. Counting
the parameter chosen in each of the thirteen folds:

| Run | Grid offered | Top value chosen in |
|---|---|---|
| `full-walkforward-r1` | `chandelier_mult` [2.5, 3.0, 3.5] | 9 of 13 folds (3.5) |
| `full-walkforward-r1` | `atr_stop_mult` [1.5, 2.0, 2.5] | 4 of 13 folds (2.5) |
| `ablate-exp-wide-stops` | `chandelier_mult` [4.0, 5.0, 6.0] | 9 of 13 folds (6.0) |
| `ablate-exp-wide-stops` | `atr_stop_mult` [2.5, 3.5, 4.5] | 7 of 13 folds (4.5) |
| `ablate-exp-combo` | `atr_stop_mult` [2.5, 3.5, 4.5] | 8 of 13 folds (4.5) |

The default grid's widest chandelier is already the majority in-sample choice. Widen the grid and
the tuner walks to the new edge — and the preference for the edge *strengthens* for
`atr_stop_mult`, from 4 folds out of 13 to 7 and 8.

An in-sample optimum sitting on a grid boundary means the search has not found an interior
optimum; it has found the limit of what it was allowed to consider. And out-of-sample results got
*worse* as in-sample preference widened (profit factor 1.18 → 1.11). That combination — the tuner
wanting more of something that hurts out-of-sample — is a signature of in-sample overfitting on
the stop-width axis, and it applies to the shipping default grid as much as to the widened one.
It should be read as a caution about the tuning procedure itself, not only about these
experiments.

---

## Limitations — read before quoting any number above

**The combo result is selection-contaminated. Its specific numbers are biased upward.**

Twelve configurations were evaluated against one out-of-sample window. The window was then
inspected, two levers were chosen because they looked best on it, and they were combined and
evaluated on the same window again. Every quantity reported for `combo` — the 1.21 profit factor,
the 34.65% drawdown, the 3.02%/yr ex-best-year figure, the 2024 reversal — is the outcome of a
search over that window, and the expected out-of-sample value of a searched maximum is lower than
the searched maximum. The by-year detail that makes `combo` persuasive is the *same data* the
selection used; it is not independent corroboration.

The honest status of `combo` is **leading hypothesis**, not validated result. The only claims here
that survive the selection problem intact are the refutations, because nothing was selected *for*
them.

**Defaults were deliberately not changed on the strength of this.** No value in `config.toml` was
modified. Adopting the best-scoring configuration from a twelve-run search over a single
out-of-sample window is exactly the failure mode that
[`indicator-research.md` §13](indicator-research.md#13-cross-cutting-caveat-data-snooping-and-edge-decay)
exists to prevent, and doing it *while documenting that it is being done* would not make it
sound. A change to the shipping defaults needs evidence from data that no configuration has been
selected against. This programme produced no such evidence, by construction.

**The gate refuses every configuration tested.** Best profit factor 1.25 (`strict-entry`) against
the required 1.30. Profit factor is the binding constraint in all twelve runs. Not one of them is
deployable under the rules the project set for itself before running them, and no configuration
here should be read as "nearly passing" — 1.25 is a selected maximum, and the honest expectation
for an unselected re-run is below it.

**Every configuration also underperforms simply holding SPY over the same window.** On return the
gap is not close: the best CAGR in the programme is 7.80% (`strict-entry`) against SPY's 14.56%,
and `combo` returns 4.69%. On drawdown, eleven of twelve configurations are deeper than SPY's
33.72%; the single exception is `wide-stops` at 33.13%, which is 0.59pp shallower — and it earns
2.92%/yr while doing it. `combo`'s 34.65% is deeper than SPY. A strategy that takes single-name
concentration risk, pays spread and slippage on 413–692 round trips, and demands nightly
attention should be measured against the alternative of doing nothing, and on this window it
loses to it on both axes.

**Other standing caveats.** Thirteen annual out-of-sample folds is a small number of independent
periods regardless of trade count. The window contains no bear market of the 2000–2002 or
2007–2009 kind, which under-samples exactly the states the regime gate and time stop exist for.
And the universe is current index membership, discussed next.

---

## What would actually settle this

**Forward paper trading on data no configuration has seen.** This is the only thing that
addresses the selection problem directly. A configuration selected on 2013–2025 and then traded
forward on bars that did not exist when it was selected produces a result that no amount of
searching can have contaminated. A harness for this is being built separately under its own work
package; see [`paper-trading.md`](paper-trading.md) for its design and status, which are not
asserted here. The relevant property is simply that the data arrives *after* the selection, and
the cost is calendar time — a meaningful read needs enough forward trades to distinguish a
profit-factor difference of the size in dispute, which on these trade rates is quarters, not
weeks.

**The survivorship problem, which is not fixed by more history.** The universe is current S&P
500/400/600 membership. Every symbol in it is a company that exists today and is in an index
today. The backtest therefore never buys a 2015 constituent that was delisted in 2018, and the
2013–2025 results are measured on a population selected by having survived to 2026. See
[`survivorship.md`](survivorship.md) for the treatment of this, which is likewise not asserted
here.

The point worth stating plainly in this log, because it bears directly on how these experiments
could be strengthened: **extending price history backwards does not provide clean validation
while the universe is current index membership.** Running the same twelve configurations over
2000–2012 would add folds and would sample a real bear market, but it would sample it using only
companies that survived to 2026 — and the further back the window extends, the more severe that
distortion becomes, because more of the era's actual constituents are missing. More history on a
survivor-selected universe buys statistical power at the cost of a growing bias whose sign is
known (favourable) and whose magnitude is not. Point-in-time index membership would fix it;
backfilling prices for today's members would not.

Until one of those exists, the correct reading of this programme is: three hypotheses refuted,
one positive finding about the ranking, one mechanically-confirmed-but-financially-wrong
hypothesis about stops, one leading hypothesis that has not been validated, and a strategy that
does not pass its own gate.

---

## Reproducibility and verification notes

**What was verified for this log.** Every figure quoted above was read from the `oos` or
`by_year` block of the relevant `reports/backtest/<run>/summary.json`, or computed from them.
Specifically checked:

- `reports/backtest/latest.json` carries `"label": "full-walkforward-r1"` — the experiment
  programme did not overwrite the gate's input.
- All twelve experiment `config_hash` values were reproduced by applying the described change to
  the shipping `config.toml`, and the unmodified `config.toml` reproduces the baseline hash
  `c6782f8db70b`. Every change in [Experiment definitions](#experiment-definitions) is confirmed.
- `by_year` is the out-of-sample breakdown: for every run, the annual trade counts sum exactly to
  the `oos` trade count, and compounding the annual returns reproduces the reported `oos` CAGR to
  within 0.002pp.
- The SPY benchmark was computed from the cached series with the project's own metric functions
  over the same 3270 bars as the strategy equity curves.
- Fold-by-fold tuner selections were read from each run's `windows[].params`.

**Provenance is not uniform across runs.** Two axes vary — the code commit and the data
fingerprint. The first is a genuine open caveat; the second is explained below and does not affect
the measured window.

| Runs | `code_ref` |
|---|---|
| `full-walkforward-r1`, `etf-walkforward-r1` (baselines) | `94e90f5` |
| `strict-entry`, `strong-trend`, `near-high`, `diversify`, `hold-longer`, `regime-fast`, `quality` | `5f11721` |
| `wide-stops`, `long-breakout`, `concentrate`, `combo`, `combo-etf` | `c90e407` |

Every comparison against the baseline therefore crosses at least one commit boundary. The
intervening commits are `5f11721` (HTML report contrast — cosmetic) and `c90e407` (making the
tuning grid configurable), and `c90e407`'s default grid is the same grid `94e90f5` hardcoded, so
no behavioural difference is expected. Expected is not verified: no run reproduces the baseline
configuration under the later code.

**Eleven distinct `data_hash` values appear across the fourteen runs — explained, and benign for
the measured window.** `data_hash` is a SHA-256 over each symbol's `(last bar date, row count)`.
The experiments were launched concurrently while the bar cache was still topping up that day's
trailing bars, so each run fingerprinted the cache at a slightly different moment and picked up a
slightly different `(last date, row count)` per symbol. The run logs corroborate the concurrency
directly: `exp-concentrate`, `exp-wide-stops` and `exp-long-breakout` finished at 16:26:57,
16:26:59 and 16:27:01 respectively, four seconds apart, for runs that take on the order of two
hours each.

The differing bars are all at the trailing end of the series — a cache top-up appends recent bars,
it does not rewrite history — and the measured window closes on 2025-12-31. Checked directly: all
fourteen runs report `oos_start` 2013-01-01, `oos_end` 2025-12-31, thirteen folds, and **byte-identical
fold boundaries** (every `is_start` / `is_end` / `oos_start` / `oos_end` tuple matches across all
fourteen). No run measured a different window from any other. Full-period metrics, which do extend
to the data edge, would legitimately differ between these runs; the out-of-sample metrics quoted
throughout this log cannot.

**Lesson for the next programme: quiesce the cache before launching a comparison batch** — one
warm-up fetch, then no further top-ups — so that every run in the comparison shares a single
`data_hash`. That is achievable and was achieved by accident here: `wide-stops`, `long-breakout`,
`concentrate` and `combo` all carry `data_hash` `5e9e608295`, because by the time they ran the
cache had settled.

**Universe vintage — these results are tied to a 1546-instrument snapshot.** Every full-universe
run logs `Loading bars for 1546 symbols (full universe)... Loaded 1545 symbols with data`. The one
symbol lost is `CWEN-A`, delisted, and the data layer reports it as an `ERROR` line in each run's
log (`reports/full-r1.log` and the `reports/exp-*.log` files) rather than dropping it silently.
That is the whole of the discrepancy.

The universe has since been expanded — the ETF list grew from 40 instruments to 139, taking the
full universe to 1645 — so **the results in this log are not directly comparable to future runs
made against the enlarged universe.** Any re-run intended as a comparison against the numbers here
must either pin the universe to the 1546-instrument snapshot or re-run the baseline alongside it.

**Cache side effect from writing this log.** Recomputing `data_hash` to test reproducibility caused
the loader to fetch the 99 newly added ETFs, so `~/.swing/cache/daily/` grew from 1545 to 1644
symbols on 2026-08-22. This is additive — no existing series was truncated and no bars inside the
measured window changed — but combined with the universe expansion it means a re-run of any
experiment above will load a larger universe and produce a different `data_hash` than those
recorded here.

**Corrections applied while checking.** Four claims in the working notes circulated when the runs
finished did not survive comparison with the JSON, and this log states the checked versions:

1. "Seven of ten single-change experiments made things worse or were neutral" — **nine** of ten
   left profit factor at or below baseline; only `strict-entry` improved it. Five were worse on
   all three headline metrics.
2. "`combo`'s drawdown clears the 35% limit for the first time" — `wide-stops` (33.13%) also
   clears it, ran earlier, and is shallower than `combo`'s 34.65%.
3. "Every configuration underperforms SPY on both return and drawdown" — true on return for all
   twelve; on drawdown `wide-stops` (33.13%) is marginally shallower than SPY (33.72%), and every
   other configuration is deeper.
4. "Twelve walk-forward runs on the full universe plus one ETF check" — **eleven** experiments on
   the full universe plus one on the ETF universe; the two baselines predate the programme
   (2026-08-19).

Minor rounding: `combo`'s 2022 return is −13.25%, quoted in the working notes as −13.2%.
