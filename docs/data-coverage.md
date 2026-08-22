# Data Coverage

What the local price cache actually contains, how deep it goes, and — the part that matters — which
parts of it may be used to make a claim about the strategy and which may not.

**Cache deepened on 2026-08-22.** Last bar in the cache: **2026-08-21**. Fetched from Yahoo via
`scripts/fetch_history.py`, which goes through `swing.data.get_provider(...).daily_bars(...)` and
therefore through the ordinary `BarCache` — nothing here was hand-written into the cache directory.

---

## 1. Read this before quoting any pre-2010 number

This section is the reason the document exists. The other sections are inventory.

### Pre-2010 STOCK results are survivorship-contaminated and are not validation

`src/swing/assets/universe/sp500.csv`, `sp400.csv` and `sp600.csv` are a snapshot of **today's**
index membership. Deepening those tickers to 1990 does not produce a 1990s backtest. It produces a
backtest of *the companies that were still in a US index in 2026*.

The mechanism, stated plainly:

* Every constituent that went bankrupt, was acquired, was delisted, or was simply demoted out of the
  index between 1990 and today is **absent from the sample**. There is no row for it, so it can
  never be picked, and it can never lose money.
* The excluded names are not a random sample. They are disproportionately the ones that fell.
* Our strategy is a **trend/momentum** strategy, which makes this the worst case rather than an
  average one. The filter that decided who is in the sample — "still trending well enough in 2026 to
  be in an index" — is itself a forward-looking momentum screen. Selecting on the outcome and then
  measuring a momentum edge on the survivors measures the selection, not the edge.

Consequences, which are not negotiable:

* **Do not quote a pre-2010 stock backtest as evidence of anything.** Not as validation, not as a
  "sanity check", not as a "directionally interesting" number, and not with a footnote. The number
  is biased upward by an amount nobody in this project has measured.
* **Do not use pre-2010 stock data to tune a parameter.** A parameter chosen on survivors is a
  parameter chosen on the future.
* The fix is point-in-time membership — a per-date record of who was actually in each index,
  including the names that later died. That work is in progress
  (`src/swing/assets/universe/*-membership.csv`). Until it lands and a backtest actually consumes
  it, the pre-2010 stock bars in this cache are **raw material, not results**.

Why the data was fetched at all, if it cannot be used yet: the bias lives in *universe selection*,
not in the price series. AAPL's 1997 bars are AAPL's 1997 bars. When point-in-time membership
arrives, the depth needs to already be here, because a 1,500-symbol full-history download is a
40-minute job that should not be on the critical path of that work.

**The 2010 line is a convention, not a threshold.** Membership drift is continuous: it does not
switch on in 2009. The same bias exists in the 2013-2025 window this project currently treats as its
out-of-sample validation — in reduced form, because there has been less time for the index to
reshuffle, but it is not zero. That is a known and currently accepted limitation of the existing
validation, and it should be restated whenever those results are quoted.

### Pre-2010 ETF results are clean *of the constituent problem* — with one honest caveat

An ETF is a continuously tradeable instrument. `XLF`'s December 1998 close is the real price of a
real portfolio that really held the then-real financial sector, including every bank that later
failed. The index reconstitutions are already baked into the NAV, as they happened, with no
hindsight. **There is no constituent-survivorship problem in an ETF price series.** This is what
makes the deep ETF history in this cache genuinely usable validation data.

The caveat, which is smaller but real and should not be waved away:

* **Instrument-level survivorship still applies to the list.** The 137 ETFs below are ETFs that
  exist and are liquid *in 2026*. Funds that launched and later closed are absent. A cross-sectional
  strategy ranking across this list back to 1998 is ranking across today's survivors.
* Why it is much smaller than the stock case: ETF closures are overwhelmingly concentrated in small,
  young, narrow, high-fee and leveraged products. The list below is dominated by large, cheap,
  broad, decades-old index funds from four fund families — the category with a closure rate near
  zero. Leveraged and inverse products, where the closure rate is highest, are excluded outright
  (see §3).
* It is not zero, though: a 2010-vintage version of this list would plausibly have included `RSX`,
  the VanEck Russia ETF, which was suspended in 2022 and wound down rather than recovering. Picking
  the list in 2026 cannot see instruments that ended that way.

So: **ETF results back to the 1990s are the strongest out-of-sample evidence this project has.**
They are not perfect evidence, and the paragraph above belongs in any write-up that uses them.

---

## 2. What the cache holds

Cache root: `~/.swing/cache/daily` (`cfg.data.cache_dir`). One parquet per symbol plus a JSON
sidecar. **1,644 parquet files, 442 MiB** (451 MiB as `du` reports allocated blocks), up from
1,545 files / 262 MiB before this fetch.

Two of those 1,644 files (`SLX`, `XPH`) are no longer in any universe list — see §3 — and are inert.

### coverage by source

| Source | Symbols in list | With cached bars | Earliest bar | Latest bar | Rows |
| --- | ---: | ---: | --- | --- | ---: |
| `sp500` | 503 | 503 | 1990-01-02 | 2026-08-21 | 3,659,628 |
| `sp400` | 400 | 400 | 1990-01-02 | 2026-08-21 | 2,459,169 |
| `sp600` | 603 | 602 | 1990-01-02 | 2026-08-21 | 3,431,392 |
| `etf` | 137 | 137 | 1993-01-29 | 2026-08-21 | 770,466 |
| **total** | **1643** | **1642** | **1990-01-02** | **2026-08-21** | **10,320,655** |

Before this fetch the cache held 1,545 symbols, **all** of which started on 2010-01-04, for
5,655,520 rows. Row count has grown by 82%.

The single symbol without bars is **`CWEN-A`** (Clearway Energy Class A, `sp600`). Yahoo serves
nothing for it under `CWEN-A`, `CWEN.A` or `CWENA`; only the Class C line `CWEN` resolves. This is a
pre-existing gap — it was uncached before this fetch too — and fixing it means changing `sp600.csv`,
which this document does not do.

### first bar by decade

| First cached bar | Stocks | ETFs | All |
| --- | ---: | ---: | ---: |
| 1990 or earlier | 503 | 0 | 503 |
| 1991-1999 | 336 | 22 | 358 |
| 2000-2009 | 220 | 95 | 315 |
| 2010 or later | 446 | 20 | 466 |
| **total** | **1505** | **137** | **1642** |

So **1,059 of 1,505 stocks** and **117 of 137 ETFs** now have pre-2010 history, where previously
none did.

### inception or data gap?

The fetch asked for `1990-01-01`. Of the 1,642 symbols with bars:

* **487** have their first bar on **1990-01-02**, the first trading day of the requested window.
  Their history is truncated by our request, not by the instrument — they were already trading, and
  a `--start 1980-01-01` run would go deeper still. (The "1990 or earlier" row above counts 503,
  because 16 more first appear later in 1990.)
* **1,155** start later than that. For these the first bar is *either* a genuine inception (IPO or
  fund launch) *or* the point at which Yahoo's coverage begins.

Those two cases can be told apart for ETFs and cannot reliably be told apart for stocks:

* **ETFs — verified inceptions.** Every ETF in §3 was probed independently against Yahoo's maximum
  history before it was added to the list, and the first bar recorded there matches the first bar in
  the cache for all 137. The pre-2000 dates line up with published fund launches: SPY 1993-01-29,
  MDY 1995-05-04, the WEBS/iShares MSCI single-country funds 1996-03-18, DIA 1998-01-20, the nine
  original Select Sector SPDRs 1998-12-22, QQQ 1999-03-10. Treat the ETF first-bar column as a real
  inception date, give or take a few sessions between prospectus date and first Yahoo bar (EFA, for
  example, launched mid-August 2001 and first appears 2001-08-27).
* **Stocks — do not read the first bar as an IPO date.** Yahoo's coverage of small and mid caps
  thins out going back, and a stock whose first bar is 1997 may have listed in 1997 or may simply be
  where the vendor's series starts. Nothing in this repo distinguishes the two, and no code should
  assume the first bar means "the company began here".

---

## 3. The ETF universe

`src/swing/assets/universe/etfs.csv`, expanded from 40 to **137** on 2026-08-22.

### provenance and selection rules

The list is chosen to maximise two things at once: **length of history** and **independence of
exposure**. In order:

1. **Deep, multi-family core.** Several fund families are represented for the same exposure
   specifically because their launch dates differ — SPY (1993) and MDY (1995) predate the sector
   SPDRs (1998), which predate QQQ (1999), which predates the iShares index and sector families
   (2000-2001), which predate the Vanguard funds (2004+). Taking only one family would have cost a
   decade.
2. **All 11 GICS sectors from two families** — the Select Sector SPDRs (`XL*`) and the iShares US
   sector funds (`IY*`, `IDU`). The SPDRs are the deeper series for the nine original sectors; the
   iShares set is an independent construction of the same exposures and adds a 2000-vintage
   alternative.
3. **Industry-level funds** where the industry is economically distinct and the fund is liquid:
   semiconductors, biotech, software, internet, homebuilders, banks, regional banks, insurance,
   retail, oil services, E&P, transports, aerospace and defence, gold miners, metals and mining,
   medical devices, healthcare providers, agribusiness.
4. **International**: broad developed and emerging, Europe and Pacific regional, plus 13
   single-country funds. The eight 1996-03-18 country funds are the second-deepest series in the
   entire cache after SPY and MDY.
5. **Bonds across the curve and the credit spectrum**: T-bills through 20+ year Treasuries,
   aggregate, MBS, munis, investment grade, high yield, EM sovereign, TIPS, preferred.
6. **Commodities**: gold and silver (physical), platinum and palladium (physical), crude, natural
   gas, broad commodity and agriculture (futures-based — see the caveat in §4).
7. **Factor and style**: momentum, quality, value, min-vol, low-vol, high-beta, large/small
   value/growth, dividend.
8. **Real estate** and a small number of **high-volume thematic** funds.

### what is deliberately excluded

* **Leveraged and inverse products are excluded entirely.** No 2x/3x long, no -1x/-2x/-3x short, no
  volatility-futures products. These reset their exposure daily, so their return over any period
  longer than one day is path-dependent — a 3x fund on an index that ends flat after a volatile
  month ends *down*. A momentum or trend system measures exactly the quantity that this decay
  corrupts, and including them would inject a systematic, horizon-dependent bias into every ranking
  the strategy computes. They are also the category with by far the highest fund-closure rate, which
  would worsen the instrument-survivorship caveat in §1.
* **Anything below the strategy's own liquidity gate.** `strategy.min_dollar_volume` defaults to
  **$5,000,000** of median daily dollar volume, so a fund that cannot clear that would be filtered
  out of every scan anyway. Three candidates were dropped on this rule, with their median 1-year
  daily dollar volume measured over the 250 sessions to 2026-08-21:
  * `RWX` (SPDR Dow Jones International Real Estate) — $0.53M/day. Replaced by **`VNQI`**
    ($10.7M/day), which carries the same international real-estate exposure.
  * `SLX` (VanEck Steel) — $2.98M/day. Exposure partly covered by `XME` and `IYM`.
  * `XPH` (SPDR S&P Pharmaceuticals) — $2.77M/day. Exposure partly covered by `XLV`, `IYH`, `IBB`.
* **Anything with under ~5 years of history**, or that fails to resolve on Yahoo. No candidate was
  dropped on this rule: all 139 probed tickers resolved and the youngest, `XLC` (2018-06-19), has
  eight years.

The thinnest survivor is `IYJ` at $6.4M/day; everything else clears the gate with more room.

All 40 previously-listed ETFs are retained — the new list is a strict superset.

Fund names are Yahoo's `longName` as reported on 2026-08-22, so they carry current legal branding
("State Street SPDR …", "Vanguard Morningstar …") rather than the historical marketing names.

### ETF exposure blocks

| Block | Count | Earliest | Rows |
| --- | ---: | --- | ---: |
| Broad US | 17 | 1993-01-29 | 108,551 |
| Sector (SPDR) | 11 | 1998-12-22 | 67,410 |
| Sector (iShares) | 11 | 2000-05-19 | 72,439 |
| Industry | 25 | 2000-06-05 | 132,453 |
| International | 24 | 1996-03-18 | 148,040 |
| Bond | 18 | 2002-07-30 | 92,669 |
| Commodity | 10 | 2004-11-18 | 49,520 |
| Factor/style | 15 | 2000-05-26 | 74,901 |
| Real estate | 2 | 2004-09-29 | 9,484 |
| Thematic | 4 | 2008-04-15 | 14,999 |
| **total** | **137** | **1993-01-29** | **770,466** |

History available: **11 ETFs with 30+ years**, **52 with 25+**, **98 with 20+**, **117 reaching
before 2010**.

### every ETF, by inception

| Symbol | Name | Block | First bar | Last bar | Rows |
| --- | --- | --- | --- | --- | ---: |
| `SPY` | State Street SPDR S&P 500 ETF Trust | Broad US | 1993-01-29 | 2026-08-21 | 8,448 |
| `MDY` | State Street SPDR S&P MIDCAP 400 ETF Trust | Broad US | 1995-05-04 | 2026-08-21 | 7,877 |
| `EWA` | iShares MSCI Australia ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWC` | iShares MSCI Canada ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWG` | iShares MSCI Germany ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWH` | iShares MSCI Hong Kong ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWJ` | iShares MSCI Japan ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWL` | iShares MSCI Switzerland ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWQ` | iShares MSCI France ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWU` | iShares MSCI United Kingdom ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `EWW` | iShares MSCI Mexico ETF | International | 1996-03-18 | 2026-08-21 | 7,657 |
| `DIA` | State Street SPDR Dow Jones Industrial Average ETF Trust | Broad US | 1998-01-20 | 2026-08-21 | 7,192 |
| `XLB` | State Street Materials Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLE` | State Street Energy Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLF` | State Street Financial Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLI` | State Street Industrial Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLK` | State Street Technology Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLP` | State Street Consumer Staples Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLU` | State Street Utilities Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLV` | State Street Health Care Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `XLY` | State Street Consumer Discretionary Select Sector SPDR ETF | Sector (SPDR) | 1998-12-22 | 2026-08-21 | 6,958 |
| `QQQ` | Invesco QQQ Trust | Broad US | 1999-03-10 | 2026-08-21 | 6,906 |
| `EWY` | iShares MSCI South Korea ETF | International | 2000-05-12 | 2026-08-21 | 6,608 |
| `IVV` | iShares Core S&P 500 ETF | Broad US | 2000-05-19 | 2026-08-21 | 6,603 |
| `IWB` | iShares Russell 1000 ETF | Broad US | 2000-05-19 | 2026-08-21 | 6,603 |
| `IYW` | iShares U.S. Technology ETF | Sector (iShares) | 2000-05-19 | 2026-08-21 | 6,603 |
| `IJH` | iShares Core S&P Mid-Cap ETF | Broad US | 2000-05-26 | 2026-08-21 | 6,598 |
| `IJR` | iShares Core S&P Small-Cap ETF | Broad US | 2000-05-26 | 2026-08-21 | 6,598 |
| `IWD` | iShares Russell 1000 Value ETF | Factor/style | 2000-05-26 | 2026-08-21 | 6,598 |
| `IWF` | iShares Russell 1000 Growth ETF | Factor/style | 2000-05-26 | 2026-08-21 | 6,598 |
| `IWM` | iShares Russell 2000 ETF | Broad US | 2000-05-26 | 2026-08-21 | 6,598 |
| `IWV` | iShares Russell 3000 ETF | Broad US | 2000-05-26 | 2026-08-21 | 6,598 |
| `IYF` | iShares U.S. Financials ETF | Sector (iShares) | 2000-05-26 | 2026-08-21 | 6,598 |
| `IYZ` | iShares U.S. Telecommunications ETF | Sector (iShares) | 2000-05-26 | 2026-08-21 | 6,598 |
| `SMH` | VanEck Semiconductor ETF | Industry | 2000-06-05 | 2026-08-21 | 6,593 |
| `IYE` | iShares U.S. Energy ETF | Sector (iShares) | 2000-06-16 | 2026-08-21 | 6,584 |
| `IYH` | iShares U.S. Healthcare ETF | Sector (iShares) | 2000-06-16 | 2026-08-21 | 6,584 |
| `IYK` | iShares US Consumer Staples ETF | Sector (iShares) | 2000-06-16 | 2026-08-21 | 6,584 |
| `IYR` | iShares U.S. Real Estate ETF | Sector (iShares) | 2000-06-19 | 2026-08-21 | 6,583 |
| `IDU` | iShares U.S. Utilities ETF | Sector (iShares) | 2000-06-20 | 2026-08-21 | 6,582 |
| `IYM` | iShares U.S. Basic Materials ETF | Sector (iShares) | 2000-06-20 | 2026-08-21 | 6,582 |
| `EWT` | iShares MSCI Taiwan ETF | International | 2000-06-23 | 2026-08-21 | 6,579 |
| `IYC` | iShares US Consumer Discretionary ETF | Sector (iShares) | 2000-06-28 | 2026-08-21 | 6,576 |
| `EWZ` | iShares MSCI Brazil ETF | International | 2000-07-14 | 2026-08-21 | 6,565 |
| `IYJ` | iShares U.S. Industrials ETF | Sector (iShares) | 2000-07-14 | 2026-08-21 | 6,565 |
| `IWN` | iShares Russell 2000 Value ETF | Factor/style | 2000-07-28 | 2026-08-21 | 6,555 |
| `IWO` | iShares Russell 2000 Growth ETF | Factor/style | 2000-07-28 | 2026-08-21 | 6,555 |
| `IBB` | iShares Biotechnology ETF | Industry | 2001-02-12 | 2026-08-21 | 6,419 |
| `OIH` | VanEck Oil Services ETF | Industry | 2001-02-26 | 2026-08-21 | 6,410 |
| `VTI` | Vanguard Morningstar Total Stock Market ETF | Broad US | 2001-06-15 | 2026-08-21 | 6,333 |
| `SOXX` | iShares Semiconductor ETF | Industry | 2001-07-13 | 2026-08-21 | 6,314 |
| `IGV` | iShares Expanded Tech-Software Sector ETF | Industry | 2001-07-17 | 2026-08-21 | 6,312 |
| `EFA` | iShares MSCI EAFE ETF | International | 2001-08-27 | 2026-08-21 | 6,283 |
| `ILF` | iShares Latin America 40 ETF | International | 2001-10-26 | 2026-08-21 | 6,244 |
| `IEF` | iShares 7-10 Year Treasury Bond ETF | Bond | 2002-07-30 | 2026-08-21 | 6,055 |
| `LQD` | iShares iBoxx $ Investment Grade Corporate Bond ETF | Bond | 2002-07-30 | 2026-08-21 | 6,055 |
| `SHY` | iShares 1-3 Year Treasury Bond ETF | Bond | 2002-07-30 | 2026-08-21 | 6,055 |
| `TLT` | iShares 20+ Year Treasury Bond ETF | Bond | 2002-07-30 | 2026-08-21 | 6,055 |
| `EEM` | iShares MSCI Emerging Markets ETF | International | 2003-04-14 | 2026-08-21 | 5,877 |
| `RSP` | Invesco S&P 500 Equal Weight ETF | Broad US | 2003-05-01 | 2026-08-21 | 5,865 |
| `AGG` | iShares Core U.S. Aggregate Bond ETF | Bond | 2003-09-29 | 2026-08-21 | 5,761 |
| `DVY` | iShares Select Dividend ETF | Factor/style | 2003-11-07 | 2026-08-21 | 5,732 |
| `TIP` | iShares TIPS Bond ETF | Bond | 2003-12-05 | 2026-08-21 | 5,713 |
| `IYT` | iShares Transportation Average ETF | Industry | 2004-01-02 | 2026-08-21 | 5,695 |
| `ITOT` | iShares Core S&P Total U.S. Stock Market ETF | Broad US | 2004-01-23 | 2026-08-21 | 5,681 |
| `VB` | Vanguard Morningstar Small-Cap ETF | Broad US | 2004-01-30 | 2026-08-21 | 5,676 |
| `VO` | Vanguard Morningstar Mid-Cap ETF | Broad US | 2004-01-30 | 2026-08-21 | 5,676 |
| `VTV` | Vanguard Morningstar Value ETF | Factor/style | 2004-01-30 | 2026-08-21 | 5,676 |
| `VUG` | Vanguard Morningstar Growth ETF | Factor/style | 2004-01-30 | 2026-08-21 | 5,676 |
| `VNQ` | Vanguard Real Estate Index Fund ETF Shares | Real estate | 2004-09-29 | 2026-08-21 | 5,509 |
| `FXI` | iShares China Large-Cap ETF | International | 2004-10-08 | 2026-08-21 | 5,502 |
| `GLD` | SPDR Gold Shares | Commodity | 2004-11-18 | 2026-08-21 | 5,473 |
| `IAU` | iShares Gold Trust | Commodity | 2005-01-28 | 2026-08-21 | 5,425 |
| `VGK` | Vanguard FTSE Europe ETF | International | 2005-03-10 | 2026-08-21 | 5,397 |
| `VPL` | Vanguard FTSE Pacific Index Fund ETF Shares | International | 2005-03-10 | 2026-08-21 | 5,397 |
| `VWO` | Vanguard Emerging Markets Stock Index Fund | International | 2005-03-10 | 2026-08-21 | 5,397 |
| `IWC` | iShares Micro-Cap ETF | Broad US | 2005-08-16 | 2026-08-21 | 5,287 |
| `PPA` | Invesco Aerospace & Defense ETF | Industry | 2005-10-26 | 2026-08-21 | 5,237 |
| `KBE` | State Street SPDR S&P Bank ETF | Industry | 2005-11-15 | 2026-08-21 | 5,223 |
| `KIE` | State Street SPDR S&P Insurance ETF | Industry | 2005-11-15 | 2026-08-21 | 5,223 |
| `DBC` | Invesco DB Commodity Index Tracking Fund | Commodity | 2006-02-06 | 2026-08-21 | 5,168 |
| `XBI` | State Street SPDR S&P Biotech ETF | Industry | 2006-02-06 | 2026-08-21 | 5,168 |
| `XHB` | State Street SPDR S&P Homebuilders ETF | Industry | 2006-02-06 | 2026-08-21 | 5,168 |
| `USO` | United States Oil Fund, LP | Commodity | 2006-04-10 | 2026-08-21 | 5,124 |
| `SLV` | iShares Silver Trust | Commodity | 2006-04-28 | 2026-08-21 | 5,111 |
| `VIG` | Vanguard Dividend Appreciation Index Fund ETF Shares | Factor/style | 2006-05-02 | 2026-08-21 | 5,109 |
| `IHF` | iShares U.S. Healthcare Providers ETF | Industry | 2006-05-05 | 2026-08-21 | 5,106 |
| `IHI` | iShares U.S. Medical Devices ETF | Industry | 2006-05-05 | 2026-08-21 | 5,106 |
| `ITA` | iShares U.S. Aerospace & Defense ETF | Industry | 2006-05-05 | 2026-08-21 | 5,106 |
| `ITB` | iShares U.S. Home Construction ETF | Industry | 2006-05-05 | 2026-08-21 | 5,106 |
| `GDX` | VanEck Gold Miners ETF | Industry | 2006-05-22 | 2026-08-21 | 5,095 |
| `KRE` | State Street SPDR S&P Regional Banking ETF | Industry | 2006-06-22 | 2026-08-21 | 5,073 |
| `XES` | State Street SPDR S&P Oil & Gas Equipment & Services ETF | Industry | 2006-06-22 | 2026-08-21 | 5,073 |
| `XME` | State Street SPDR S&P Metals & Mining ETF | Industry | 2006-06-22 | 2026-08-21 | 5,073 |
| `XOP` | State Street SPDR S&P Oil & Gas Exploration & Production ETF | Industry | 2006-06-22 | 2026-08-21 | 5,073 |
| `XRT` | State Street SPDR S&P Retail ETF | Industry | 2006-06-22 | 2026-08-21 | 5,073 |
| `FDN` | First Trust Dow Jones Internet Index Fund | Industry | 2006-06-23 | 2026-08-21 | 5,072 |
| `GSG` | iShares S&P GSCI Commodity-Indexed Trust | Commodity | 2006-07-21 | 2026-08-21 | 5,053 |
| `VYM` | Vanguard High Dividend Yield Index Fund ETF Shares | Factor/style | 2006-11-16 | 2026-08-21 | 4,970 |
| `DBA` | Invesco DB Agriculture Fund | Commodity | 2007-01-05 | 2026-08-21 | 4,938 |
| `IEI` | iShares 3-7 Year Treasury Bond ETF | Bond | 2007-01-11 | 2026-08-21 | 4,934 |
| `SHV` | iShares Short Treasury Bond ETF | Bond | 2007-01-11 | 2026-08-21 | 4,934 |
| `TLH` | iShares 10-20 Year Treasury Bond ETF | Bond | 2007-01-11 | 2026-08-21 | 4,934 |
| `MBB` | iShares MBS ETF | Bond | 2007-03-16 | 2026-08-21 | 4,890 |
| `PFF` | iShares U.S. Preferred Stock ETF | Bond | 2007-03-30 | 2026-08-21 | 4,880 |
| `BND` | Vanguard Total Bond Market Index Fund | Bond | 2007-04-10 | 2026-08-21 | 4,874 |
| `HYG` | iShares iBoxx $ High Yield Corporate Bond ETF | Bond | 2007-04-11 | 2026-08-21 | 4,873 |
| `UNG` | United States Natural Gas Fund, LP | Commodity | 2007-04-18 | 2026-08-21 | 4,868 |
| `BIL` | State Street SPDR Bloomberg 1-3 Month T-Bill ETF | Bond | 2007-05-30 | 2026-08-21 | 4,839 |
| `VEA` | Vanguard FTSE Developed Markets Index Fund ETF Shares | International | 2007-07-26 | 2026-08-21 | 4,799 |
| `MOO` | VanEck Agribusiness ETF | Industry | 2007-09-05 | 2026-08-21 | 4,771 |
| `MUB` | iShares National Muni Bond ETF | Bond | 2007-09-10 | 2026-08-21 | 4,768 |
| `JNK` | State Street SPDR Bloomberg High Yield Bond ETF | Bond | 2007-12-04 | 2026-08-21 | 4,708 |
| `EMB` | iShares J.P. Morgan USD Emerging Markets Bond ETF | Bond | 2007-12-19 | 2026-08-21 | 4,697 |
| `TAN` | Invesco Solar ETF | Thematic | 2008-04-15 | 2026-08-21 | 4,618 |
| `ICLN` | iShares Global Clean Energy ETF | Thematic | 2008-06-25 | 2026-08-21 | 4,568 |
| `GDXJ` | VanEck Junior Gold Miners ETF | Industry | 2009-11-11 | 2026-08-21 | 4,219 |
| `PALL` | abrdn Physical Palladium Shares ETF | Commodity | 2010-01-08 | 2026-08-21 | 4,180 |
| `PPLT` | abrdn Physical Platinum Shares ETF | Commodity | 2010-01-08 | 2026-08-21 | 4,180 |
| `VOO` | Vanguard S&P 500 ETF | Broad US | 2010-09-09 | 2026-08-21 | 4,012 |
| `VNQI` | Vanguard Global ex-U.S. Real Estate Index Fund ETF Shares | Real estate | 2010-11-01 | 2026-08-21 | 3,975 |
| `MCHI` | iShares MSCI China ETF | International | 2011-03-31 | 2026-08-21 | 3,871 |
| `SPHB` | Invesco S&P 500 High Beta ETF | Factor/style | 2011-05-05 | 2026-08-21 | 3,847 |
| `SPLV` | Invesco S&P 500 Low Volatility ETF | Factor/style | 2011-05-05 | 2026-08-21 | 3,847 |
| `XAR` | State Street SPDR S&P Aerospace & Defense ETF | Industry | 2011-09-29 | 2026-08-21 | 3,745 |
| `USMV` | iShares MSCI USA Min Vol Factor ETF | Factor/style | 2011-10-20 | 2026-08-21 | 3,730 |
| `INDA` | iShares MSCI India ETF | International | 2012-02-03 | 2026-08-21 | 3,658 |
| `GOVT` | iShares U.S. Treasury Bond ETF | Bond | 2012-02-24 | 2026-08-21 | 3,644 |
| `IEFA` | iShares Core MSCI EAFE ETF | International | 2012-10-24 | 2026-08-21 | 3,475 |
| `IEMG` | iShares Core MSCI Emerging Markets ETF | International | 2012-10-24 | 2026-08-21 | 3,475 |
| `MTUM` | iShares MSCI USA Momentum Factor ETF | Factor/style | 2013-04-18 | 2026-08-21 | 3,357 |
| `VLUE` | iShares MSCI USA Value Factor ETF | Factor/style | 2013-04-18 | 2026-08-21 | 3,357 |
| `QUAL` | iShares MSCI USA Quality Factor ETF | Factor/style | 2013-07-18 | 2026-08-21 | 3,294 |
| `ARKK` | ARK Innovation ETF | Thematic | 2014-10-31 | 2026-08-21 | 2,968 |
| `JETS` | U.S. Global Jets ETF | Thematic | 2015-04-30 | 2026-08-21 | 2,845 |
| `XLRE` | State Street Real Estate Select Sector SPDR ETF | Sector (SPDR) | 2015-10-08 | 2026-08-21 | 2,733 |
| `XLC` | State Street Communication Services Select Sector SPDR ETF | Sector (SPDR) | 2018-06-19 | 2026-08-21 | 2,055 |

---

## 4. Data-quality caveats

These are properties of the data, independent of the survivorship discussion in §1.

* **`SMH` and `OIH` splice two different instruments.** Both series begin as Merrill Lynch HOLDRS
  (`SMH` 2000-06-05, `OIH` 2001-02-26) and become VanEck index ETFs when VanEck took the tickers over
  in December 2011. A HOLDRS trust held a **fixed** basket set at launch that was never rebalanced,
  only shrunk by mergers; the ETFs that replaced them track rebalancing indices. The exposure is
  broadly the same and both structures were continuously tradeable, so the price series is real
  throughout — but a pre-2012 `SMH` bar is the price of a decaying 2000-vintage basket, not of a
  current semiconductor index. Anything that reasons about long-horizon `SMH`/`OIH` behaviour should
  say so.
* **Futures-based commodity funds carry roll return.** `USO`, `UNG`, `DBC`, `DBA` and `GSG` hold
  futures and roll them. In contango the roll is a persistent drag that has nothing to do with the
  spot commodity — `UNG` in particular has lost the overwhelming majority of its value since 2007
  while natural gas has not. They are legitimate tradeable instruments and are included as such, but
  they are not proxies for their commodity, and a trend signal on them is partly a trend signal on
  the futures curve. The physical funds (`GLD`, `IAU`, `SLV`, `PPLT`, `PALL`) do not have this
  problem.
* **Prices are auto-adjusted and are therefore retroactively rewritten.** Every bar is
  `auto_adjust=True`, so every dividend and split rewrites the entire prior history. Two runs of the
  same backtest a month apart will see slightly different 1998 closes. `BarCache` detects this with
  an overlap check and refetches whole histories rather than splicing two price bases together
  (audit BUG-014); the practical consequence is that a result is reproducible against *a cache*, not
  against *a date*, unless the cache is pinned.
* **Yahoo is an unsupported, best-effort source.** During the 2026-08-22 run, 11 consecutive `sp600`
  symbols (`APAM` … `ATEN`) came back empty on the first pass and were recovered by an immediate
  retry — a transient rate-limit burst, not a data gap. A run of this length should always be
  checked with `--dry-run` afterwards and the shortfall re-fetched with `--symbols`.
* **Deep history is thinner and noisier than recent history.** Pre-2000 small caps in particular have
  wider spreads, lower volume, and vendor coverage that degrades going back. The dollar-volume
  filters in `StrategyCfg` are stated in today's dollars and are not inflation-adjusted, so applying
  them unchanged to 1995 excludes more of the universe than intended.

---

## 5. Reproducing and refreshing

```sh
# What the cache holds right now — reads the sidecars, touches no network.
uv run python scripts/fetch_history.py --dry-run

# Deepen everything to the earliest bar the vendor serves (~20 min for 1,600 symbols).
uv run python scripts/fetch_history.py --start 1990-01-01

# Just the clean half.
uv run python scripts/fetch_history.py --etf-only

# Repair the symbols a run reported as empty.
uv run python scripts/fetch_history.py --symbols APAM,APLE,APOG
```

The script fetches one symbol per call and writes after each, so it is safe to interrupt and rerun;
already-deep symbols are served from the cache with no network at all. Exit code is `1` if any
requested symbol ended without bars, `2` for a bad invocation.

Note that the first deep run is a **full re-download**, not an append: a cached symbol whose
`covered_start` is later than `--start` is missing its head, and `BarCache` correctly refetches the
whole union range rather than trying to prepend (audit BUG-013).

---

## 6. Consequences for existing results

* Any backtest, ablation table or gate artifact computed on the **40-ETF** universe describes a
  universe that no longer exists. `--universe etf` is now a 137-instrument cross-section, so
  cross-sectional rankings, position turnover and every resulting metric will differ. Prior ETF
  results should be regenerated before they are compared with anything new.
* The full universe grew from 1,546 to **1,643** instruments (+6.3%), so full-universe scan runtime
  and API load rise slightly.
* The ETF list now contains cash-equivalent and near-cash instruments (`BIL`, `SHV`, `SHY`) whose
  realised volatility is a small fraction of an equity's. ATR-based position sizing on such an
  instrument wants an enormous share count; the notional cap (`account.max_position_pct`, default
  25%) is what stops it, and it will bind rather than the risk budget. That is correct behaviour,
  but it means those names size differently from everything else in the book and their presence in a
  ranked cross-section deserves a look before the next live run.
