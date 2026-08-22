# Survivorship bias: how much of it free data can actually fix

**Fetch date: 2026-08-22.** Every number below was measured on that day against live Wikipedia and
live Yahoo. Both sources change without notice; re-run
[`scripts/build_membership.py`](../scripts/build_membership.py) before quoting any figure.

This document answers one question that [`backtest-methodology.md`](backtest-methodology.md) §7
asserts without evidence: *the survivorship bias in the stock backtests cannot be fixed with free
data.* That assertion turns out to be **correct on the price side and overstated on the membership
side**, and the difference matters, so both halves are quantified here.

---

## Verdict, up front

| Question | Answer |
|---|---|
| Can point-in-time **membership** be reconstructed free? | **Partly**, and further than this study first found. From Wikipedia alone: the S&P 500 back to ~2012, the S&P 400 to ~2016, the S&P 600 only to 2020. Adding SEC EDGAR fund filings ([§11](#11-sec-edgar-fund-filings-as-a-membership-source)) reaches **2005 at quarterly resolution for all three**, at the cost of dates that are quarter-bounds rather than exact. |
| Can the **price history** of departed companies be recovered free? | **No.** 24 of 736 departed tickers (3.3%) yield a clean delisted series. |
| Is the recovered fraction enough to change any conclusion? | **No.** It is far below the 50% bar, it is concentrated almost entirely in one calendar year, and the data Yahoo *does* return for delisted tickers is wrong more often than it is right. |
| Should point-in-time membership be wired into the engine? | **Not for survivorship repair** — the prices are not there to repair it with. See [§8](#8-recommendation). |
| Does the §7 haircut convention need changing? | **No.** The measurements support it; two of §7's supporting claims need correcting (see [§9](#9-corrections-to-backtest-methodologymd-7)). |
| Is the *look-ahead* half fixable? | **Largely yes**, and it is a separate question from survivorship. Join-date coverage is now 100% / 92% / 88% across the three indices rather than 100% / 76% / 57% ([§11.8](#118-coverage-before-and-after)). |

---

## Table of contents

1. [What was reconstructed](#1-what-was-reconstructed)
2. [Sources evaluated, with their real depth](#2-sources-evaluated-with-their-real-depth)
3. [How complete are the change tables really?](#3-how-complete-are-the-change-tables-really)
4. [Wikipedia revision snapshots as a point-in-time source](#4-wikipedia-revision-snapshots-as-a-point-in-time-source)
5. [The size of the hole](#5-the-size-of-the-hole)
6. [The recovery test: can Yahoo serve the departed?](#6-the-recovery-test-can-yahoo-serve-the-departed)
7. [Ticker recycling, measured](#7-ticker-recycling-measured)
8. [Recommendation](#8-recommendation)
9. [Corrections to backtest-methodology.md §7](#9-corrections-to-backtest-methodologymd-7)
10. [Reproducing this](#10-reproducing-this)
11. [SEC EDGAR fund filings as a membership source](#11-sec-edgar-fund-filings-as-a-membership-source)

---

## 1. What was reconstructed

Three new files sit next to the existing universe snapshots.

```
                                                as first built     today
src/swing/assets/universe/sp500-membership.csv    903 rows       1,004 rows,   955 distinct symbols
src/swing/assets/universe/sp400-membership.csv  1,043 rows       1,303 rows, 1,199 distinct symbols
src/swing/assets/universe/sp600-membership.csv  1,107 rows       1,799 rows, 1,686 distinct symbols
```

> **Two things about these files have changed since this section was written; the findings below
> have not.**
>
> **They are read by code now.** When this was written nothing in `src/swing/` knew they existed and
> they were research output. They are now a **simulation input**: `swing.universe` loads them
> (`membership()`, `members_asof()`, `membership_windows()`), and with
> `[backtest] membership = "point_in_time"` the backtest engine enforces eligibility from them, so
> a wrong date in these files is a wrong trade rather than a wrong paragraph. Treat them
> accordingly. The default mode still measures rather than enforces.
>
> **The schema has four more columns.** [§11](#11-sec-edgar-fund-filings-as-a-membership-source)
> added SEC EDGAR dates alongside the Wikipedia ones, and they are labelled rather than blended
> (see the provenance table below). The extra rows in the table above are EDGAR memberships
> Wikipedia never recorded.

Schema is `symbol,name,added,removed,added_bound,removed_bound,added_source,removed_source`, one row
per membership *interval*, so a company that left and rejoined has two rows. Symbols are
Yahoo-style (`BRK-B`), matching `swing.universe.to_yahoo_symbol`.

**The four original columns are unchanged and existing readers are unaffected.** They keep their
position, their names and their meaning; a read/write round-trip leaves them byte-identical; the
`unknown` sentinel and the empty-`removed`-means-still-a-member convention are exactly as described
below. The four new columns are additive, optional and trailing — a consumer reading
`symbol,name,added,removed` positionally or by name sees no difference.

| Column | Values | Meaning |
|---|---|---|
| `added_bound` / `removed_bound` | `exact` | The source states this date. |
| | `no_later_than` | Derived by diffing quarterly fund filings, so the true date is at or before it ([§11.4](#114-from-snapshots-to-dates-and-why-they-are-bounds)). |
| | empty | The date column is empty or `unknown`; there is nothing to qualify. |
| `added_source` / `removed_source` | `wikipedia`, `edgar`, empty | Which source produced the date. Wikipedia's exact dates win wherever it has one. |

The date columns carry five distinguishable states, and keeping them distinct is the point of the
exercise — an inferred date would otherwise be indistinguishable from a sourced one:

| Value | Meaning |
|---|---|
| an ISO date | **A source states this date.** Nothing else produces a date. |
| `added` empty | No source states an addition. The symbol was already a member when the change table's coverage begins. |
| `removed` empty | The symbol is in today's committed snapshot and no later removal is recorded: **still a member as of the fetch date.** |
| `removed` = `unknown` | The sources record an addition but no removal, *and* the symbol is absent from today's snapshot. It left on a date nobody wrote down. |
| `added` = `unknown` | The symbol is a current member but its last recorded event is a *removal*. An unrecorded re-addition happened. Two rows when this was written; **zero today** — EDGAR dated both — but the state is still part of the schema and a reader must still handle it. |

Nothing is interpolated, and no membership is inferred from a company's mere existence.

**Consistency check.** The rows with `removed` empty are exactly the committed snapshots —
503/503, 400/400, 603/603 symbols, no additions, no omissions. The new files are a strict superset
of `sp500.csv`/`sp400.csv`/`sp600.csv`, which are left untouched.

### The gaps, counted

| | S&P 500 | S&P 400 | S&P 600 |
|---|---|---|---|
| change-table rows parsed | 407 | 618 | 491 |
| rows that failed to parse | 1 | 0 | 0 |
| removals with no matching addition | 250 | 336 | 359 |
| additions with no matching removal, symbol not a current member | 17 | 33 | 13 |
| current members whose last recorded event is a removal | 2 | 1 | 1 |
| duplicate additions with no intervening removal | 2 | 1 | 0 |
| ticker/name-change rows that are not real index changes | 11 | 6 | 11 |
| join dates that disagree with the constituent table by >7 days | 12 | n/a | n/a |
| current members with a stated join date | **503/503** | 302/400 | 344/603 |

The large "removals with no matching addition" counts are not errors — they are the expected
signature of a change table whose coverage starts partway through the index's life. A company
removed in 2021 that joined in 2003 has no addition row because the table does not reach 2003.

---

## 2. Sources evaluated, with their real depth

### 2.1 Wikipedia change tables — the primary source

The brief assumed the S&P 500 changes live in a "Selected changes" section of
**List of S&P 500 companies**. As of this fetch date **they do not**. On **2026-08-11 — eleven days
before this study — the table was split out into a separate article, `Historical components of the
S&P 500`.** Any code that scrapes "the second table on the S&P 500 list page" broke this month.
That is worth stating plainly as a durability warning about this entire class of source.

| Index | Article carrying the change table | Table `id` | Data rows | Earliest event |
|---|---|---|---|---|
| S&P 500 | `Historical components of the S&P 500` | `#changes` | 407 | **1976-07-01** |
| S&P 400 | `List of S&P 400 companies` | `#changes` | 618 | **2012-01-13** |
| S&P 600 | `List of S&P 600 companies` | `#changes` | 491 | **2019-12-17** |

The earliest-event dates are the headline constraint, and the S&P 600 one is severe: **the S&P 600
change table does not reach back into 2010–2019 at all.** That is 600 of the 1,500 names, and it is
the smallest-cap, highest-failure-rate third of the universe — exactly where survivorship bias is
largest. Ten of the sixteen backtest years have no S&P 600 departure data whatsoever.

The S&P 500 table's 1976 start is misleading. Its density by year:

```
1976: 2   1994: 1   1997: 1   1998: 3   1999: 3   2000: 7   2003: 1   2005: 2   2006: 1
2007: 11  2008: 8   2009: 13  2010: 11  2011: 19  2012: 18  2013: 18  2014: 16  2015: 29
2016: 30  2017: 29  2018: 23  2019: 24  2020: 20  2021: 20  2022: 22  2023: 18  2024: 21
2025: 21  2026: 15
```

One event in 2003 and one in 2006 is not a record of a year in which the S&P 500 changed once. The
article says so itself: *"Between January 1, 1963, and December 31, 2014, 1,186 index components
were replaced"* — against roughly 50 rows in the table for that entire span. The source is openly a
selection, not a census, before about 2007.

Only the current-constituent table for the **S&P 500** carries a `Date added` column. The S&P 400
and S&P 600 constituent tables have no join dates at all, which is why join-date coverage collapses
to 76% and 57% for those two indices.

### 2.2 Reason text is prose, not a field

Every change row carries a free-text reason. Bucketing it by regex
(`terminal` / `index_migration` / `spin_off` / `ticker_or_name_change`) gets the gist right and
individual rows wrong, because the reason describes the whole multi-leg transaction rather than the
removed company. `CYH` (Community Health Systems, still trading) is labelled with a sentence about
Cousins Properties acquiring Parkway. **No conclusion in this document depends on the reason
classifier** — the recovery test in §6 uses only the bars themselves.

### 2.3 The tables contain outright errors

Not merely gaps. One found by inspection: the S&P 600 change table's row for 18 December 2023 reads
`ALSK || Alaska Air Group`. Alaska Air Group is `ALK`, and it is in today's S&P 400; `ALSK` was
Alaska Communications, delisted in 2021. The row pairs a real company with a ticker that was already
dead. A naive reconstruction would have carried a phantom `ALSK` membership into the universe and
then fetched whatever Yahoo happens to serve under it.

This one was contained by accident rather than by design — `ALSK` has an addition with no matching
removal, so it lands in the gap counts rather than the departure list and never reaches the price
probe. **No systematic search for typos of this kind was made**, so treat the ticker column as
mostly-right rather than right.

### 2.4 What is not available free

- **Point-in-time constituent files from S&P Dow Jones Indices** — licensed, not public.
- **CRSP / Compustat index-constituent history** — the academic standard; institutional licence.
- **Norgate Data, Sharadar, QuantConnect's map/factor files** — vendors that do carry delisted
  price history keyed by permanent identifiers rather than by ticker, which is what would actually
  solve §6. **Not evaluated here**: pricing and licence terms were not checked, so they are listed
  as leads, not as recommendations.

### 2.5 One free source this study did *not* exhaust: SEC EDGAR

Worth recording because it is the only free route that survives the objections above. The S&P
500/400/600 index ETFs are registered funds and file their full holdings with the SEC:

- iShares Trust (CIK `0001100663`) has filings on EDGAR from **1999**, including 1,433 `NPORT-P`
  filings plus 44 amendments in its most recent submissions block alone.
- A retrieved example — accession `0001752724-21-040685`, the `NPORT-EX` exhibit — is the
  **iShares Core S&P Small-Cap ETF Schedule of Investments as of December 31, 2020**, 1.9 MB of
  line-by-line holdings with security names, share counts and values, grouped by GICS sub-industry.
  A genuine, dated, primary-source S&P 600 roster from a year Wikipedia's S&P 600 article cannot
  reach.
- Quarterly `N-Q` filings covered the 2004–2019 era before `NPORT-P` took over in 2019, so the
  route plausibly extends back past the start of the backtest window.
- The structured `NPORT-P` XML identifies each position by CUSIP/ISIN/LEI rather than by ticker,
  which is precisely the defence against the recycling failure measured in §7.

Caveats: the HTML exhibit gives names, not tickers, so it needs an identifier crosswalk; public
`N-PORT` discloses only the quarter-end month; and an ETF's holdings are the fund's, not the
index's (close for a full-replication tracker, not identical). **This was validated far enough to
say the source is real and usable, and no further.** It does not change the verdict, because it
solves membership — and membership was never the binding constraint. Prices are (§6).

> **Followed up in [§11](#11-sec-edgar-fund-filings-as-a-membership-source).** The route works. Two
> of the hopes above did not survive contact: the `N-Q` era gives issuer *names* and nothing else,
> so only the 2019-onwards quarter of the archive is identifier-keyed, and the HTML exhibit needed a
> crosswalk built from four separate SEC sources. The conclusion in the last sentence stands
> unchanged.

---

## 3. How complete are the change tables really?

Rather than take the change tables' word for it, they were graded against an independent artefact
of the same wiki: the **constituent table as it stood on 1 January of two consecutive years**. The
symmetric difference of two year-end rosters implies how many changes actually happened in between;
the change table says how many it recorded. Both count one replacement as two changes, so they are
comparable. Within-year round trips cancel in the roster diff, so the ratio is a floor on
completeness rather than a point estimate — which is why a few years exceed 1.00.

`uv run python scripts/build_membership.py crosscheck`

**S&P 500** — mean coverage 0.85 across 16 years:

| year | 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 |
|---|---|---|---|---|---|---|---|---|
| roster implies | 44 | 56 | 38 | 46 | 32 | 62 | 61 | 62 |
| table records | 22 | 37 | 36 | 36 | 30 | 54 | 59 | 58 |
| coverage | **0.50** | **0.66** | 0.95 | 0.78 | 0.94 | 0.87 | 0.97 | 0.94 |

| year | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|---|---|---|
| roster implies | 56 | 60 | 40 | 42 | 46 | 38 | 38 | 38 |
| table records | 46 | 48 | 34 | 38 | 38 | 32 | 36 | 38 |
| coverage | 0.82 | 0.80 | 0.85 | 0.90 | 0.83 | 0.84 | 0.95 | 1.00 |

**S&P 400** — usable from 2016 (mean ~0.90 thereafter). The 2012 and 2014 rows are nonsense
(coverage 34.0 and 43.0) for an informative reason: the roster diff for those years is 1 and 2
symbols, meaning **Wikipedia's S&P 400 constituent table sat essentially unmaintained through
2012–2015** and was then bulk-refreshed, which is why 2013 and 2015 show 173 and 150 implied
changes in a single step. The rosters are stale, not point-in-time, before ~2016.

**S&P 600** — only 2022–2025 are measurable at all; coverage there averages 0.96.

**Summary of usable depth:**

| Index | Change table usable from | Constituent rosters trustworthy from |
|---|---|---|
| S&P 500 | ~2012 (2010–11 at 50–66%) | 2010 |
| S&P 400 | ~2016 | ~2016 |
| S&P 600 | 2020 | 2021 Q2 |

---

## 4. Wikipedia revision snapshots as a point-in-time source

The other route: ask the MediaWiki API for the article *as it existed* on a past date. That is a
genuine contemporaneous roster rather than a backwards reconstruction, and it is immune to the
change tables' omissions.

`uv run python scripts/build_membership.py snapshots --granularity quarterly` — 64 grid points per
index, quarter-starts from 2010 through 2025:

| Index | parses plausibly | earliest | median revision lag | why the rest fail |
|---|---|---|---|---|
| S&P 500 | **64/64** | 2010-01-01 | 3 days | — (article created 2005-09-14) |
| S&P 400 | 59/64 | 2011-04-01 | 8 days | article created **2010-12-31**; 4 points predate it, 1 has no usable table |
| S&P 600 | **19/64** | 2021-04-01 | 6 days | article created **2018-08-27**; 35 points predate it, and 10 more parse to **961–1,057** symbols for a 600-name index |

Two things follow. First, the revision route strictly beats the change-table route for the S&P 500,
covering the whole window at quarterly resolution with a ~3-day lag. Second, it does nothing for the
S&P 600 before 2021 — the article did not exist, and for its first two and a half years it was not a
correct list.

**"Parses plausibly" is a weaker claim than "is point-in-time", and the S&P 400 is where the
difference bites.** A table that nobody has edited for three years still has 400 rows and still
passes the row-count check. §3's crosscheck is the authority here: the S&P 400 roster diffs for
2012→2013 and 2014→2015 are 1 and 2 symbols, so those "ok" snapshots are stale copies, not
contemporaneous rosters. Read the 59/64 as an upper bound and ~2016 as the real start.

The column layout drifts across revisions (in the 2022 S&P 400 revision the ticker is column 1, not
column 0; the `id="constituents"` anchor only appears from ~2021), so any consumer must locate
columns by header name. Row count alone is not a safe way to pick the constituent table either — by
2024 the S&P 400 change table had grown larger than the constituent table.

**Practicality at quarterly granularity.** Each grid point costs two API calls (revision lookup plus
render), so a 2010–2025 quarterly grid across three indices is ~380 requests, and the rendered S&P
600 article alone is ~300 KB per revision. The first attempt at this walk **was rejected mid-run
with HTTP 429** at one request per second; the retry, with a partly warmed cache and exponential
backoff added, completed without further rate limiting. Call it a viable one-off build of roughly
half an hour — not something to put in CI, and not something to run without a cache.

---

## 5. The size of the hole

Window: 2010-01-01 to 2025-12-31, matching `backtest.start`. Current universe: 1,506 symbols, so
the current backtests trade a nominal **24,096 member-years**.

### 5.1 Missing departures (the classic survivorship hole)

**875 departure events, 736 distinct symbols** left an index inside the window and are absent from
today's committed universe. Companies that merely moved between the three indices and are still in
one of them are excluded — they are already in the CSVs and cost us nothing.

| | events |
|---|---|
| from the S&P 500 | 213 |
| from the S&P 400 | 318 |
| from the S&P 600 | 344 |

Member-years those companies were index members but are missing from the backtest:

- **3,627 member-years** clamping each index's contribution to the date its change table starts.
  This is the defensible lower bound.
- **6,679 member-years** clamping only to the window start, i.e. assuming an undated pre-coverage
  member was there from 2010. Larger, but partly assumed.

Departures per year climb from 10 (2010) to 111 (2023) — that climb is the change tables' coverage
improving, not the indices becoming more volatile, so the early years are undercounted.

### 5.2 Look-ahead inclusion — the larger, and separately measurable, half

The mirror-image error. A company that joined the S&P 500 in 2019 is nevertheless tradable from
2010 in every backtest run so far, and index inclusion is itself an outcome of past growth. Where a
source states a join date this is measurable **exactly**:

| Index | current members with a stated join date | of those, joined mid-window | member-years of pre-membership exposure |
|---|---|---|---|
| S&P 500 | 503 of 503 | 237 | **2,147** |
| S&P 400 | 302 of 400 | 302 | **3,564** |
| S&P 600 | 344 of 603 | 344 | **4,858** |
| **total** | | **883** | **10,569** |

(For the S&P 400 and 600 the two middle columns coincide: their change tables start in 2012 and
2019, so every join date they record is inside the window by construction.)

> **Correction (2026-08-22).** The 10,569 total above is arithmetic *per index*, and that is the
> wrong denominator for this question. It scores a company against the index it sits in **today**,
> so a name that was in the S&P 500 from 2010 and moved down to the S&P 400 in 2021 is counted as
> having eleven years of "pre-membership exposure" — when in truth it never left the tradable
> universe for a day. What the backtest actually selects from is the *union* of the three indices,
> and on that basis the figure is **7,199 member-years, 30% of the nominal 24,096**, not 44%.
>
> | measure | member-years | share of 24,096 |
> |---|---|---|
> | per index, each symbol against its current index only | 10,569 | 44% |
> | **union across indices, earliest stint start wins** | **7,199** | **30%** |
> | difference — time spent as a member of a *different* S&P index | 3,370 | 14 pp |
>
> The two reconcile exactly: a follow-up package reproduced 10,569 from the same CSVs by per-index
> arithmetic, and the 3,370-member-year gap is entirely index-to-index migration. **7,199 / 30% is
> the number to quote**; 10,569 is kept here because it is what the per-index tables above sum to
> and because the gap between them is itself the finding — a seventh of the apparent look-ahead
> bias was double counting, not bias. Both figures remain lower bounds for the reason given below.

**7,199 of 24,096 nominal member-years — 30% — are periods in which the company was demonstrably
not in any index it is being backtested as a member of.** That figure is a *lower* bound: the 98
S&P 400 and 259 S&P 600 names with no recorded join date contribute zero to it, and some of them
certainly joined mid-window too. [§11](#11-sec-edgar-fund-filings-as-a-membership-source) closes
most of that gap and re-measures the figure.

Two honest qualifications. The measured symbols are a subsample selected by data availability — a
name has a recorded join date largely because it joined recently — so the per-symbol average is not
representative even though the total is a valid floor. And the `min_dollar_volume` filter already
screens out some pre-inclusion exposure, since companies are smaller before they are promoted; how
much is not measured here.

---

## 6. The recovery test: can Yahoo serve the departed?

This is the number the whole question turns on. All 736 departed symbols were requested from Yahoo
(`yf.download`, 2008-01-01 through the fetch date, batches of 40). Each result is bucketed **using
only the bars** — never the reason text, because a misclassified reason would move a symbol between
"recovered" and "recycled", the one error this study cannot afford.

| verdict | symbols | share | meaning |
|---|---|---|---|
| `no_data` | **467** | 63.5% | Yahoo serves nothing at all |
| `still_listed` | 174 | 23.6% | continuous bars through the exit and beyond — a demotion, not a rescued failure, **and contaminated** (§7) |
| `recycled` | 71 | 9.6% | bars exist but essentially none predate the exit: a different company |
| `delisted_recovered` | **24** | **3.3%** | series stops at the exit with ≥200 prior bars — a genuine recovery |

**Clean-delisting recovery rate: 3.3%.** Even counting `still_listed` as usable, 26.9%. Against the
brief's 50% bar, both fail.

### The 3.3% is not even usable as a partial fix

The 24 clean recoveries by removal year:

```
2010 0   2011 0   2012 1   2013 2   2014 1   2015 0   2016 0   2017 0
2018 19  2019 1   2020 0   2021 0   2022 0   2023 0   2024 0   2025 0
```

**Nineteen of twenty-four come from 2018.** No other year contributes more than two. Whatever
retention quirk keeps that cohort alive on Yahoo is not a property of delistings in general.
Splicing this subset into the universe would not reduce survivorship bias by 3% — it would replace a
known, uniform, documented bias with an unknown one that over-weights 2018 acquisitions.

The 24 are real data, at least. Five were checked against known deal terms and all five match:

| ticker | last close | deal |
|---|---|---|
| AET | $212.70 on 2018-11-29 | CVS paid ~$207/sh |
| ESRX | $92.33 on 2018-12-21 | Cigna paid ~$96/sh |
| ANDV | $153.50 on 2018-10-01 | Marathon paid ~$152/sh |
| DST | $83.99 on 2018-04-16 | SS&C paid $84/sh |
| CAA | $53.12 on 2018-02-12 | Lennar paid ~$50/sh |

### Where the loss is worst, by reason

| stated reason | symbols | `no_data` | `recycled` | `still_listed` | `delisted_recovered` |
|---|---|---|---|---|---|
| terminal (acquired, merged, bankrupt) | 362 | 276 (76%) | 44 | 23 | **19 (5.2%)** |
| index migration (demoted, still listed) | 272 | 123 | 15 | 134 | 0 |
| spin-off | 26 | 17 | 3 | 6 | 0 |
| ticker/name change (not a real change) | 15 | 11 | 1 | 3 | 0 |
| unclassified | 61 | 40 | 8 | 8 | 5 |

The terminal row is the one that matters — those are the failures and takeouts whose absence
inflates the backtest. **76% return nothing, and among those that return something, Yahoo hands
back a wrong-company series more often than a correct one** (44 provably recycled plus 23
continuous-past-a-takeout, versus 19 genuine).

### The `no_data` bucket is real, not a bulk-download artefact

63.5% resting on a batch call in which most tickers are dead deserves a check. 25 `no_data` symbols
were re-requested individually (`yf.Ticker(...).history(...)`): **0 of 25 returned any data**, while
a live control (AAPL) returned 11,515 bars in the same session. The bulk path is not dropping live
symbols.

`uv run python scripts/build_membership.py verify --bucket no_data --sample 25`

---

## 7. Ticker recycling, measured

`backtest-methodology.md` §7 warns that "ticker reuse poisons naive reconstruction". It does, and
it is worse than the warning implies, because **the corruption is often invisible to any automated
test.**

71 symbols were caught by the obvious signature — bars exist, but almost none predate the index
exit. `BEAM` (Beam Inc., acquired by Suntory in 2014) returns 1,644 bars and **zero** before the
2014 exit; the ticker belongs to Beam Therapeutics now. `CAM`, `CCE`, `BMR`, `BEAT` and `CAB` are
the same shape — all five return bars, none of them return a single bar from before the company
disappeared.

The dangerous cases are the other kind. A gap detector was built for series that resume after a
hole; **it fired zero times.** There are no gaps. Instead, seven of the 23 "acquired but still
trading" cases were spot-checked against what the company actually traded at, and **all seven
carried prices inconsistent with the delisted company, with a fully continuous series across the
exit date**:

| ticker | company, fate | what Yahoo returns |
|---|---|---|
| GENZ | Genzyme, acquired by Sanofi April 2011 at $74/sh | $23.69 (2010) → $40.77 (2025), fully continuous |
| KG | King Pharmaceuticals, Pfizer paid $14.25/sh in 2011 | $135.20 (2010), $213.60 (2013) |
| EP | El Paso, acquired by Kinder Morgan 2012, traded ~$14–30 | $5.28 (2010) → $0.58 (2013) |
| SII | Smith International, acquired by Schlumberger Aug 2010 | $33.70 (2010) → $98.42 (2025) |
| VVC | Vectren, acquired by CenterPoint 2019, traded ~$25–72 | $0.19 (2010) → $0.02 (2025); 4 unique closes in the last 300 bars |
| RPT | RPT Realty, acquired by Kimco Jan 2024, traded ~$10–15 | $77.70 (2019) |
| B | Barnes Group, acquired by Apollo Jan 2025 | 5.96 **billion** shares of volume in the last 300 bars |

Seven of seven. The remaining 16 were not adjudicated, so the honest statement is that the
`still_listed` bucket cannot be used without per-symbol human review — which means the 26.9%
"any usable" figure should be read as an upper bound with no support behind it, and 3.3% as the
number that survives scrutiny.

Two further hazards, noted but not quantified: eight dead tickers share an identical final bar of
**2022-03-02** (`ATW`, `BMC`, `CLC`, `HAR`, `SUG`, `TEG`, `TNB`, `VCI`) despite index exits spread
across 2012–2017, which looks like a Yahoo-side artefact rather than eight coincident delistings;
and Yahoo's adjusted prices are back-adjusted from the present, so even a correct delisted series
carries split and dividend factors that were not knowable at the time it was trading.

---

## 8. Recommendation

**Do not wire point-in-time membership into the engine as a survivorship fix.** The recovery rate
is 3.3%, an order of magnitude below the threshold that would justify the complexity, and the
recoverable subset is concentrated in a single year in a way that would introduce a fresh bias in
place of the documented one. There is no version of this that makes the stock backtest honest,
because the missing companies' prices do not exist at any free source.

**Keep the §7/§8 convention exactly as it is.** The 4.0 pp CAGR haircut, the 0.75 profit-factor
scaling and the 1.25× drawdown multiplier were set as conventions in advance; nothing measured here
gives a reason to move them, and the measurements do support the *direction* and rough magnitude.
The ETF run remains the lower bound and the stock run the upper bound.

**Keep the three CSVs.** They are a strict superset of the committed snapshots and the evidence
base for this document. The only cost is that they sit under `src/swing/assets/`, so they ship in
the wheel. *(Written when nothing imported them, and it recommended moving them to `docs/` or
`research/` if the wheel size was unwanted. That option has since closed: `swing.universe` reads
them at runtime — see [§1](#1-what-was-reconstructed) — so `src/swing/assets/` is now where they
belong.)*

**One finding is left on the table deliberately.** §5.2 shows the look-ahead half of the bias
(7,199 member-years on the union rule, 30% of the backtest; 10,569 / 44% by the per-index arithmetic
this document originally quoted) is separately measurable *and* separately fixable without any new
price data — the join dates are already in these CSVs. That is a different decision from the one
this brief gated on the 50% recovery bar, so no interface is sketched here. If it is worth pursuing
it should be scoped as its own package, with the explicit caveat that join-date coverage is
100% / 76% / 57% across the three indices, so the fix would be partial and unevenly distributed
across the universe. **[§11](#11-sec-edgar-fund-filings-as-a-membership-source) revisits exactly
that caveat** and lifts the S&P 600 from 57% to 88%.

---

## 9. Corrections to `backtest-methodology.md` §7

§7 reaches the right conclusion on two imprecise premises. It is another package's file, so nothing
was edited; the corrections are recorded here for whoever owns it.

1. **"Point-in-time index membership is a paid product… There is no free, complete, licence-clean
   source."** Half right. There is no *complete* free source, but there is a substantially complete
   one for the S&P 500 back to ~2012 and a genuine primary-source route (SEC N-PORT/N-Q, §2.5) that
   is free, licence-clean and identifier-keyed. The correct statement is that free membership data
   exists and is uneven, and that membership was never the binding constraint.
2. **"yfinance serves currently-listed tickers. A request for a delisted symbol returns empty or
   errors."** Understated in the direction that matters. 63.5% return empty, as §7 expects — but
   9.6% return a *different company's* series, and an unmeasured share of the remaining 23.6%
   return a wrong series that is continuous across the delisting and therefore indistinguishable
   from correct data by any automated check. "Returns empty" is the safe failure. The unsafe one is
   the one §7 does not mention.

§7's third claim — that ticker reuse poisons naive reconstruction — is confirmed, with numbers.

---

## 10. Reproducing this

```bash
# Reconstruct the membership CSVs and the gap report (4 live Wikipedia requests, then cached)
uv run python scripts/build_membership.py build

# Grade the change tables against contemporaneous rosters (§3)
uv run python scripts/build_membership.py crosscheck

# Are past revisions a usable point-in-time source? (§4)
uv run python scripts/build_membership.py snapshots --granularity quarterly

# The decisive test: Yahoo recoverability of every departed ticker (§6)
uv run python scripts/build_membership.py probe

# Re-check the no_data bucket one symbol at a time (§6)
uv run python scripts/build_membership.py verify --bucket no_data --sample 25
```

Responses are cached under `$TMPDIR/swing-membership-cache` (`--cache-dir` to move it); delete it
to force a re-fetch. Wikipedia is polled at one request per second with a descriptive User-Agent
and exponential backoff on 429. The script writes nothing outside its cache directory and the three
`*-membership.csv` files, and importing it has no side effects.

**Everything above is a measurement of two third-party sources on 2026-08-22.** Wikipedia articles
get restructured — one of them was, eleven days before this was written — and Yahoo's retention of
dead tickers is undocumented and unstable. Re-measure before relying on any of it.

---

## 11. SEC EDGAR fund filings as a membership source

**Fetch date: 2026-08-22.** [§2.5](#25-one-free-source-this-study-did-not-exhaust-sec-edgar) left
EDGAR as the one free source this study had not exhausted, validated only far enough to say it was
real. This section turns that spot-check into coverage, and it is the answer to the caveat §8 ends
on: join-date coverage of 100% / 76% / 57% across the three indices, with the worst-covered index
also being the highest-turnover one.

Everything here comes from [`scripts/build_membership_edgar.py`](../scripts/build_membership_edgar.py),
which is separate from `build_membership.py` and writes the same three CSVs.

### 11.1 The source

An index-tracking ETF is a registered fund, so it must file its complete holdings with the SEC. Its
filings are therefore a dated, primary-source, licence-clean roster of the index it tracks. Three
iShares trackers cover exactly the three indices this repo backtests:

| index | tracker | series id | holdings filings found | earliest | latest |
|---|---|---|---|---|---|
| S&P 500 | IVV | `S000004310` | 96 | 2006-03-01 | 2026-06-04 |
| S&P 400 | IJH | `S000004307` | 95 | 2006-03-01 | 2026-06-04 |
| S&P 600 | IJR | `S000004313` | 95 | 2006-03-01 | 2026-06-04 |

EDGAR's browse endpoint accepts a **series id** where it normally wants a CIK, which is the only
free way to ask "what did this *fund* file" rather than "what did its 380-fund trust file".

Four form types carry a Schedule of Investments, and it takes all four to get quarterly resolution
across twenty years — a point worth stating because `NPORT-P` alone, the form §2.5 named, starts in
2019 and would have added almost nothing to the S&P 600 that Wikipedia's change table does not
already cover from 2019-12-17:

| form | era | what it is | identifiers |
|---|---|---|---|
| `N-Q` | 2006–2019 | quarterly holdings, retired when N-PORT arrived | **name only** |
| `N-CSR` / `N-CSRS` | 2006–present | annual and semi-annual reports | **name only** |
| `NPORT-P` | 2019–present | structured XML, quarterly | CUSIP + ISIN + LEI |

**The pre-2019 half of the archive has no identifier at all** — just "AAR Corp." and a share count.
That is the single most important correction to §2.5, which hoped the route would be
identifier-keyed throughout. It is identifier-keyed for a quarter of its span and name-keyed for
three quarters of it.

### 11.2 Getting a roster out of a filing

The `N-Q`/`N-CSR`/`N-CSRS` documents are 17–58 MB of HTML covering every fund in the trust, so one
fund's section has to be cut out of it. Three things had to be right:

- **Two document eras.** Reports from 2019 on put the "Schedule of Investments" banner and the fund
  name in table cells; before that they sit in bare paragraphs between the tables. A table-only
  reader finds *nothing* in the older half of the archive.
- **Summary schedules.** An annual report carries each fund twice — a "Summary Schedule of
  Investments" listing only the fifty largest positions plus a line called "Other securities", and
  then the real one. Taking whichever extraction yields more names picks the real one.
- **The affiliate-transaction table.** It repeats every holding with five to eight numeric columns.
  A holding line has exactly two (shares, value); anything else is not a holding. Getting this
  wrong doubles every roster.

A section that cannot be located unambiguously is refused rather than guessed at, because picking
up the Growth or Value sibling by mistake would silently corrupt every date derived from it. Each
roster is then checked against the index size it should have; 7 of 243 fall outside tolerance and
are dropped.

### 11.3 The crosswalk: CUSIP and name to ticker

This is the central engineering problem, and the answer is that **SEC publishes a CUSIP-to-ticker
crosswalk without ever calling it one**. The fails-to-deliver files, released twice a month since
2009-07, are plain pipe-delimited text:

```
SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
20241202|B38564108|CMBT|165|CMB.TECH NV (BEL)|11.22
```

That is a CUSIP, a ticker, and a company name, dated. Because each file is dated the mapping is
genuinely point-in-time: `724078100` correctly answers `PJC` in 2019 and `PIPR` in 2020, and a
ticker reassigned later cannot leak backwards into an older roster — which is precisely the defence
against the recycling failure measured in [§7](#7-ticker-recycling-measured).

Five tiers, each recorded per symbol so the weak ones can be discounted:

| tier | rule | applies to |
|---|---|---|
| `cusip` | CUSIP or ISIN-embedded CUSIP → fails-to-deliver ticker for the nearest fortnight | the NPORT era |
| `nport` | issuer name → the ticker its CUSIP already settled in the NPORT era | the HTML era |
| `former` | issuer name → ticker via EDGAR's record of the company's **former names** | the HTML era |
| `sec` | issuer name → `company_tickers.json` title, exact match | current filers only |
| `ftd` | issuer name → fails-to-deliver *description*, 12-character prefix | anything trading since 2009 |

Every tier is an exact rule with a uniqueness test. A name two tickers answer to is recorded as
**ambiguous and left unresolved** — a dual-class issuer is one name in `NPORT-P` and two in the HTML
schedules, and neither answer is safe from the name alone. **No holding is ever guessed into a
ticker.** Unmatched holdings are counted and reported.

Four fixes mattered more than the tier design:

- **Footnote markers glued to names.** The older schedules write `AeroVironment Inc.(a)(b)` with no
  `<sup>` to strip. Left in, they halved the match rate on every August-filed `N-Q`.
- **Punctuation.** "D.R. Horton", "DR Horton" and "D R HORTON" are three spellings of one company
  across three sources. Comparison keys drop spaces entirely.
- **Truncated descriptions.** Fails-to-deliver descriptions are cut at 30 characters, so long names
  can only be compared on a prefix and short ones only in full.
- **Share-class spelling.** NSCC writes Berkshire class B as `BRKB`; this repo writes `BRK-B`. Four
  symbols are affected and each would otherwise both fail to fill its own row *and* invent a second
  row beside it. The alias map is derived from the universe files rather than hand-written.

### 11.4 From snapshots to dates, and why they are bounds

Diffing consecutive rosters gives events, but the filings are quarterly, so an event's date is a
**bracket, not a moment**. A symbol absent from the roster of 2014-03-31 and present in that of
2014-06-30 joined somewhere in between. What is written is `2014-06-30`, and the adjacent
`added_bound` column says `no_later_than`. Wikipedia's dates keep `exact`. Nothing merges a bound
into a column that claims to be exact without saying so beside it.

The bound errs in the safe direction for the look-ahead question. The true join is at or before the
date written, so a consumer that excludes the symbol until that date excludes it for slightly *too
long* — under-including rather than over-including, which is the direction that does not reintroduce
the bias being fixed.

Two guards stop the diff from inventing events:

**Absence has to be worth something.** "Missing from the 2011 roster" is only evidence of
non-membership if a membership would have been *recognised*. In the name-keyed era it often would
not have been: an issuer the crosswalk cannot place is indistinguishable from one that is not there.
So a roster below a resolution threshold (`--min-resolution`, default 0.60) can witness presence but
never absence, and a join date is only written where a **decisive earlier roster shows the symbol
absent**. "First seen in 2014" on its own says nothing about 2013 and produces no date.

**A gap is not always a departure.** Where two runs of presence are separated only by rosters that
are not decisive, they are one membership with a crosswalk outage in the middle, and they are
merged. Without this a single unresolved quarter would manufacture a departure and a re-admission.

### 11.5 Renames: the failure mode this method is most exposed to

A corporate rename is the one event a name-keyed diff cannot see for what it is. Apple's 2006
schedule says "Apple Computer Inc."; nothing in a 2019-vintage crosswalk answers to that, so the
2006 roster looks like a roster without Apple in it and the diff reports that Apple joined the
S&P 500 in 2007. It is a manufactured join date in exactly the direction that would corrupt a
look-ahead fix — and, being indistinguishable from a real arrival, it would not announce itself.

The fix is that EDGAR's own company record lists former names with the dates they were dropped, and
that record is free and per-company:

```
Apple Inc.  <- 2007-01-04 APPLE COMPUTER INC  <- 1997-07-28 APPLE COMPUTER INC/ FA
CVS HEALTH Corp  <- 2014-09-03 CVS CAREMARK CORP  <- 2007-03-22 CVS CORP  <- 1997-01-10 MELVILLE CORP
```

Fetching that for each current member turns the whole class of error into an exact lookup. The
measured effect is in [§11.7](#117-what-the-rename-crosswalk-was-worth).

### 11.6 The crosswalk match rate

Pooled over all 243 rosters: **118,934 holding-lines, 111,537 resolved to a ticker — 93.8%.**

| tier | lines | share of resolved |
|---|---|---|
| `nport` | 50,262 | 45.1% |
| `cusip` | 39,764 | 35.7% |
| `ftd` | 15,695 | 14.1% |
| `former` | 4,068 | 3.6% |
| `sec` | 1,748 | 1.6% |

The pooled number hides the shape that matters, which is that **the rate is a function of how long
ago the roster was**:

| S&P 600 roster | 2005-12-31 | 2007-06-30 | 2009-12-31 | 2012-06-30 | 2014-12-31 | 2017-06-30 | 2018-12-31 | 2019-09-30 on |
|---|---|---|---|---|---|---|---|---|
| resolved | 68.2% | 77.8% | 90.3% | 90.8% | 92.2% | 95.2% | 96.8% | **99.3–100%** |

Two different regimes. From 2019 the CUSIP is in the filing and the match is essentially total.
Before that it is a name match, and what it is really measuring is *survival*: the unresolved
remainder is dominated by companies that were gone before the fails-to-deliver archive starts in
2009-07, so nothing free records what they traded as. The 2005 roster is one third unresolvable for
that reason alone.

This matters less than it looks, because **the names that go unresolved are not the names the
join-date question is about**. A current member is resolvable by construction — it is in a recent
CUSIP-keyed roster. The persistent failures are all long-departed: Whole Foods, Harman
International, C.R. Bard, du Pont, Burlington Northern. 146 / 232 / 592 distinct names across the
three indices are never resolved in any roster, and they are counted, not guessed at.

### 11.7 What the rename crosswalk was worth

Running the whole pipeline twice, with and without EDGAR's former-name records
(`build_membership_edgar.py renames`):

| | S&P 500 | S&P 400 | S&P 600 | total |
|---|---|---|---|---|
| holding-lines only a former name could resolve | 2,275 | 947 | 841 | **4,063** |
| spurious membership breaks stitched back together | 10 | 4 | 5 | **19** |
| symbols whose first appearance moved **earlier** | 47 | 23 | 23 | **93** |

The middle row is renames caught mid-membership — a break the diff would have published as a
departure and a re-admission. The bottom row is the more damaging kind: 93 symbols that would have
carried a **manufactured late join date**, including AAPL, CVS, ADBE, BKNG, CME and CBRE. Those are
exactly the errors that would have propagated into a look-ahead fix as fake pre-membership exposure.

It also shows up in the cross-check against Wikipedia's exact dates, holding everything else
constant:

| join-date disagreement | S&P 500 | S&P 400 | S&P 600 | all |
|---|---|---|---|---|
| without former names | 60/403 = 14.9% | 3/561 = 0.5% | 4/438 = 0.9% | 67/1,429 = **4.7%** |
| with former names | 36/374 = 9.6% | 5/557 = 0.9% | 3/438 = 0.7% | 44/1,395 = **3.2%** |

The gain is concentrated in the S&P 500, which is where it should be: its Wikipedia dates reach back
to the 1950s, so renames have had the most time to accumulate. It is **not monotonic** — the S&P 400
goes from 3 disagreements to 5, and the S&P 600 from 4 to 3. At those counts that is noise, and it
is quoted rather than smoothed because a crosswalk that only ever helped would be a suspicious
crosswalk.

**93 corrected late joins against 2 newly disagreeing rows is the honest summary.**

### 11.8 Coverage before and after

The headline. "Join date" means an ISO date in the `added` column of a row that is still a member:

| | S&P 500 | S&P 400 | **S&P 600** | total |
|---|---|---|---|---|
| current members | 503 | 400 | 603 | 1,506 |
| with a join date, before | 503 (100%) | 302 (76%) | **344 (57%)** | 1,149 (76%) |
| with a join date, after | 503 (100%) | 369 (92%) | **533 (88%)** | **1,405 (93%)** |
| rows in the file | 903 → 1,004 | 1,043 → 1,303 | 1,107 → 1,799 | 3,053 → 4,106 |
| rows with a removal date | 383 → 489 | 610 → 890 | 491 → 1,193 | 1,484 → 2,572 |

**The S&P 600 goes from 57% to 88%**, which was the binding constraint §8 flagged, and it is the
index EDGAR helps most because it is the one Wikipedia covers worst. The S&P 500 gains nothing on
join dates — Wikipedia was already at 100% — but EDGAR still adds 101 departed memberships it never
recorded and cross-checks 374 of its dates.

The 1,053 new rows are memberships the Wikipedia reconstruction never saw at all: companies that
joined and left inside a window its change table does not reach. They carry bounded dates from
EDGAR, and every one is labelled as such.

The merge is **idempotent** — running it twice adds nothing — and the invariant §1 relies on still
holds: the rows with an empty `removed` are exactly the committed snapshots, 503 / 400 / 603, no
additions and no omissions.

**A caveat on the look-ahead figure.** Re-running §5.2's calculation over the enriched CSVs gives
**7,593 union member-years, up from 6,982**. The bias did not grow — the *measurement* did, because
727 symbols now have a datable join where 630 did before. It remains a lower bound. (That 6,982 is
this script's own recomputation of the pre-merge union figure; the follow-up package quoted in §5.2
measured 7,199 on the same CSVs under a slightly different treatment of undated stints. The 3%
spread between the two is worth knowing about and does not move any conclusion.)

### 11.9 Where Wikipedia and EDGAR disagree

Both sources are cross-checked against each other wherever both speak — Wikipedia states an exact
date, EDGAR brackets it — because the disagreement rate is the only honest bound on how far either
can be trusted. A bracket edge is given 7 days of slack (`--bracket-tolerance`), since an index
change is effective before the open on a stated date while a fund's schedule is dated at a quarter
end, and the two straddle harmlessly: CBRL joined the S&P 400 on 2015-06-29 and appears in the
roster dated 2015-06-30.

| | testable | disagree | rate | within 7d of the edge |
|---|---|---|---|---|
| join dates, S&P 500 | 374 | 36 | **9.6%** | |
| join dates, S&P 400 | 557 | 5 | **0.9%** | |
| join dates, S&P 600 | 438 | 3 | **0.7%** | |
| **join dates, all** | **1,395** | **44** | **3.2%** | +26 (5.0% scored strictly) |
| removal dates, all | 1,278 | 50 | **3.9%** | |

The S&P 500's 9.6% is not a worse reconstruction, it is a harder test: its Wikipedia dates reach
back decades, so any rename or share-class event in forty years breaks the bracket, and the residue
after the former-name fix is dominated by ticker changes (`MRSH` from `MMC`, `FISV` to `FI`,
`BALL` from `BLL`) and by share-class re-listings. `GOOG` disagrees because Wikipedia's 2014-04-03
is the Class C creation while the ETF held Google from 2006 — both are right about different
questions.

**The S&P 600's 0.7% is the number that matters** for this package, since the S&P 600 is where the
new dates are, and it says the derived brackets and Wikipedia agree almost always. It has to be read
with its limit stated: Wikipedia's S&P 600 change table only starts 2019-12-17, so those 438 tests
all fall in the CUSIP-keyed era, and they say nothing about the name-keyed years where most of the
new S&P 600 dates actually come from. **The S&P 400 is the honest proxy for those years** — its
table starts in 2012, its 557 tests span the whole window on both sides of the 2019 boundary, and it
says **0.9%**.

One more contradiction, left standing rather than resolved: **24 rows the CSVs call current do not
appear in the trackers' newest rosters** (8 / 2 / 14). The committed snapshots are the authority on
who is in the index today, so those rows are untouched, but a silent contradiction would be worse
than a counted one.

### 11.10 Verdict

**EDGAR works, and it is the first free source in this study that materially improves on
Wikipedia.** It lifts join-date coverage of the S&P 600 from 57% to 88% and of the S&P 400 from 76%
to 92%, adds 1,053 previously unrecorded memberships, dated removals from 1,484 to 2,572, and
cross-checks 1,395 Wikipedia dates at a 3.2% disagreement rate. It reaches back to 2005-12-31,
five years before the backtest window opens, at quarterly resolution throughout.

Four limits belong next to that, none of them fatal and all of them measurable:

1. **Dates are bounds, not events** — accurate to the filing quarter, and marked `no_later_than`.
2. **The pre-2019 crosswalk is name-based**, 68% resolved at the far end rising to 97% by 2018. The
   unresolved remainder is long-dead companies, not current members.
3. **A rename is still the dominant residual error**, reduced but not eliminated: 93 fixed, and the
   9.6% S&P 500 disagreement rate is mostly what is left.
4. **A tracker's holdings are the fund's, not the index's.** IVV, IJH and IJR are full-replication
   funds, so the two are near-identical, but they are not the same object.

**This does not change §8's recommendation, and it was never going to.** Survivorship is blocked on
*prices*, and EDGAR has no prices. The 1,053 recovered memberships name companies whose price
history [§6](#6-the-recovery-test-can-yahoo-serve-the-departed) has already shown is not available,
so knowing they were members does not make them tradable in a backtest.

What it does change is the caveat §8 attached to the *look-ahead* fix: that fix would have been
unevenly distributed across the universe, weakest exactly where the bias was largest. It is now
roughly even — 100% / 92% / 88% — which removes the main objection to scoping it as its own package.

### 11.11 Reproducing this

```bash
# What the three trackers filed, by form and year
uv run python scripts/build_membership_edgar.py filings

# Build every roster and report the crosswalk match rate
uv run python scripts/build_membership_edgar.py snapshots

# A/B the former-name crosswalk against going without it (§11.7)
uv run python scripts/build_membership_edgar.py renames

# Fold the dates into the CSVs — report first, then write
uv run python scripts/build_membership_edgar.py merge --dry-run
uv run python scripts/build_membership_edgar.py merge
```

Responses and parsed rosters are cached under `$TMPDIR/swing-edgar-cache` (`--cache-dir` to move
it), gzipped, about 350 MB. **The first run takes roughly 45 minutes**, most of it the ~1,500
former-name lookups against EDGAR's `browse-edgar` CGI, which answers in about 3.7 s each;
`--no-former-names` skips them at the cost of §11.7. Re-runs are free.

SEC's access rules are enforced and are respected here: the descriptive User-Agent with a contact
address that SEC requires, and one request every 0.25 s — a quarter of SEC's documented 10/second
limit — with exponential backoff on 403 and 429. Neither was triggered during this study. The script
writes nothing outside its cache directory and the three `*-membership.csv` files, and importing it
has no side effects.

**The ordering rule is now enforced rather than remembered.** `scripts/build_membership.py build`
writes the same three CSVs from Wikipedia alone, which would discard the EDGAR columns. It no longer
does that silently: it **refuses, exits 3 and writes nothing** when a target file carries columns it
does not manage, or when the rebuild would drop more than 5% of the rows. `--force` overrides it and
says so loudly. So the rule — rebuild from Wikipedia first, then re-run the EDGAR merge — is checked
by the tool rather than by whoever read this paragraph last.

One warning that is not automated: these numbers are a measurement of SEC's holdings archive on
2026-08-22. The fund names, the report layouts and the fails-to-deliver URL scheme have all changed
at least once inside the span this study reads, and they will change again.
