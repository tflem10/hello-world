# swing — an evidence-based swing-trading pick system

Long-only, 1–8 week holds. Researches indicators against the literature,
**backtests before it will give you a single live pick**, then produces a
nightly pick sheet with whole-share position sizes, ATR stops, and drafted
Schwab orders — delivered to your phone, your inbox, and your desktop.

Optional v2 places those orders automatically, behind a stack of guardrails
that all have to agree before anything is transmitted.

---

## Read this before anything else

**No indicator predicts stock prices.** Every effect this system uses is a
small, noisy, time-varying tilt in a distribution, most of them measured in
academic samples and most of them weaker now than when they were published
(McLean & Pontiff 2016 found a 26% decay from in-sample to post-sample and a
further 58% after publication, across 97 documented predictors).

The goal here is not prediction. It is a **positive-expectancy, risk-controlled
process**: losses bounded by construction, position sizes derived from
volatility, and a validation gate you cannot casually talk yourself past.

The system enforces that last part mechanically. `swing scan` refuses to emit
picks until a walk-forward backtest exists whose *out-of-sample* metrics clear
the thresholds you wrote down in advance — **and** whose config hash matches
the config you are about to trade. Edit a stop multiple and the gate re-locks
until you re-validate.

If your backtest comes back with a profit factor above 2.5, a Sharpe above 2
and a sub-10% drawdown, that is a bug report, not a success. See
[docs/indicator-research.md](docs/indicator-research.md) §15.

---

## Quick start

```bash
git clone <this repo> && cd hello-world
./install.sh                       # uv venv + deps + config.toml
source .venv/bin/activate

swing data --backfill              # free daily history via yfinance (slow, once)
swing backtest --walk-forward      # REQUIRED before any live picks
swing backtest --etf-only          # the survivorship-free lower bound
swing notify-test                  # prove your alerts work before you need them
swing scan --dry-run               # a pick sheet, with nothing sent
```

If the walk-forward clears the gate, `swing scan` starts producing picks. If it
does not, the system tells you exactly which threshold failed and by how much,
and refuses. That refusal is the feature.

---

## How it fits together

```
data/universe/*.csv ─┐
                     ├─► universe ─► DataProvider (yfinance | schwab | stooq)
config.toml ─────────┘                    │
                                          ▼
                                    parquet cache
                                          │
                        ┌─────────────────┴─────────────────┐
                        ▼                                   ▼
              strategy/rules.py  ◄── the same code ──►  strategy/rules.py
                        │           in both paths            │
                        ▼                                    ▼
                backtest/engine.py                        scan.py
                        │                                    │
                        ▼                                    ▼
              walk-forward report ──── gates ────────►  pick sheet
                                                             │
                                          ┌──────────────────┼──────────────┐
                                          ▼                  ▼              ▼
                                   ntfy / email        orders/*.json    confirm.py
                                    / macOS                  │         (pre-open)
                                                             ▼
                                                    execution/executor.py
                                                     (guardrails, v2)
```

The important arrow is the horizontal one. The backtester and the live scanner
import **the same** `strategy/rules.py` and `strategy/sizing.py`. There is no
second copy of the logic to drift, which is the usual way a backtested edge
quietly stops existing in production.

Data comes from yfinance by default, Schwab once your app is approved, or
**stooq** as a free fallback when Yahoo breaks — stooq bars are split-adjusted
but *not* dividend-adjusted, so long-horizon numbers drift, and because the
cache is stamped with the provider that wrote it, switching providers is
refused until you delete `data/cache/` and re-backfill — mixing adjustment
bases is the kind of corruption that never looks wrong.

---

## The default strategy: "Trend-Momentum Core"

Every number lives in `config.toml`, and every one is justified — or explicitly
flagged as unjustified — in [docs/indicator-research.md](docs/indicator-research.md).

| stage | rule |
|---|---|
| **regime** | new entries only while SPY > its 200-day SMA (open positions keep trailing) |
| **liquidity** | price ≥ $5, 20-day average dollar volume ≥ $5M |
| **trend** | close > 50 > 150 > 200 SMA, 200-SMA rising, ≥25% above the 52-week low, within 25% of the 52-week high, ADX(14) ≥ 20 |
| **entry** | today exceeds the prior 20-day high and closes within 2% of it, on volume ≥ 1.3× its 50-day average |
| **rank** | 0.6 × (126-day return, skipping the last week) + 0.4 × (63-day return), each ÷ ATR% |
| **fundamentals** | soft filter, stocks only, fail-open when data is missing; ETFs bypass |
| **earnings** | no entry within 10 days of a known report |
| **stop** | entry − 2 × ATR(14) |
| **trail** | highest close since entry − 3 × ATR, ratcheting up only |
| **time stop** | 40 trading days |
| **size** | `floor(equity × risk% / (entry − stop))`, capped at 25% of equity and by cash |
| **book** | at most 4 concurrent positions |

---

## What the backtester does, and does not, do

**Does:**

- signals at the close of day *t*, fills at the **open of day t+1** — a test
  truncates the series mid-run and asserts no already-closed trade moves
- **gaps through the stop fill at the open, not the stop price**, so a gap
  costs more than 1R, as it does in life
- whole shares, real cash accounting, per-position and portfolio caps
- costs on both sides: commission, slippage in bps, and an ATR-proportional
  spread proxy — stop exits pay them too
- walk-forward: parameters fitted in-sample only, headline equity is the
  concatenation of out-of-sample segments, chained at realised equity so
  whole-share effects compound realistically
- ±25% parameter sensitivity tables, read for **flatness** rather than for the
  best cell
- component ablations, so each rule has to earn its place
- a buy-and-hold **benchmark** (the regime symbol, SPY by default) plotted on
  the equity curve and reported as excess CAGR — beating a flat line is the
  minimum bar
- **block-bootstrap confidence intervals** on the out-of-sample curve (p5/p50/p95
  CAGR and max drawdown, plus P(CAGR ≤ 0)) — a floor on the uncertainty, not an
  estimate of it
- byte-identical reruns; every report carries the config hash, a fingerprint of
  the exact bars consumed, and the git commit

**Does not:**

- correct for **survivorship bias** in the stock universe. The CSVs list
  *current* index members, so companies that went to zero are simply absent.
  `swing backtest --etf-only` is the survivorship-free lower bound; read the
  pair as a range and trust the floor.
- apply the **earnings blackout** out of the box, because free historical
  earnings calendars do not reach back to 2010. The live scanner does apply it,
  so live takes fewer trades than the backtest implies, and every report says
  so. This one is now closable: point `[data] earnings_calendar` at a
  historical calendar CSV and the backtest applies the same blackout live uses
  (symbols missing from your file stay unprotected — partial calendar, partial
  fix). Without a calendar the caveat above stands unchanged.
- undo the fact that a human chose these indicators, this universe and this
  period after reading about what worked historically. Walk-forward bounds how
  much you can fool yourself; it does not eliminate it.

---

## Daily rhythm

| when | command | what happens |
|---|---|---|
| 17:30 ET | `swing scan` | refresh cache → rank → size → sheet → phone/email/desktop |
| 09:00 ET | `swing confirm` | re-quote; picks that gapped >1 ATR are cancelled, small moves re-sized so the dollar risk stays put |
| you | place the orders | v1: from the drafted JSON or by hand in thinkorswim |
| you | `swing journal add ...` | record the fill, or tomorrow's scan thinks you are flat |
| weekly | `swing auth` | Schwab refresh tokens last 7 days. Not negotiable. |

```bash
swing schedule install     # launchd jobs for both times (macOS)
swing schedule status
```

Full operational detail — including what to do when something breaks — is in
[docs/runbook.md](docs/runbook.md).

---

## Small accounts

At **$100** of equity with 2% risk you are risking $2 per trade. With a 25%
per-position cap that is a $25 maximum position, so almost everything the
strategy finds lands in the **watch** section marked *unaffordable*, with the
shortfall stated in dollars.

That is arithmetic, not a defect, and it is shown rather than hidden — an empty
sheet would wrongly suggest the strategy found nothing.

At **$500** cheaper stocks and ETFs become tradable and sizing scales
automatically. Nothing needs to change but `[account] equity`.

Schwab's Trader API does not accept fractional shares, so `floor()` is an API
constraint, not a rounding preference. Rounding 0.5 shares up to 1 would
silently double the risk budget, so the system reports zero instead.

---

## v2: automated execution

Off by default, and deliberately hard to turn on:

```bash
swing execute                 # dry run: prints the exact JSON, sends nothing
swing execute --live          # also requires [execution] enabled = true
swing kill                    # engage the kill switch, immediately
swing kill --release
```

Every guardrail must pass: kill switch clear, both switches on, Schwab token
fresh, market open, sheet fresh and matching the current config hash, gate
passed (a `--force` sheet can never be auto-executed), broker equity within
tolerance of the config, broker positions reconciled against the journal, no
duplicate for that symbol today, under the daily order and exposure caps, and
the live quote within 1 ATR / 3% of the analysed price. Limit orders only —
market orders are refused at every layer.

Unless `autopilot = true`, you also confirm each order interactively.

**First real trade: one share of a liquid ETF, in confirm mode, then look at it
in thinkorswim before doing anything else.**

---

## Layout

```
config.example.toml         every knob, annotated; copy to config.toml (gitignored, 600)
CLAUDE.md                   orientation for agent sessions: commands, invariants, conventions
src/swing/
  cli.py commands.py        entry points
  config.py                 layered config + strategy-only hash
  indicators.py             hand-rolled pandas (Wilder smoothing done properly)
  data/                     provider seam, parquet cache, universe, pipeline
  strategy/rules.py         THE definition of a trade — imported by both paths
  strategy/sizing.py        whole-share sizing; zero shares is a real answer
  backtest/                 engine, metrics, walk-forward, reports, the gate
  scan.py confirm.py        nightly picks, pre-open re-quote
  picks.py orders.py        pick sheets, drafted Schwab orders + validator
  alerts/                   ntfy, email, SMS gateway, macOS
  execution/                journal, guardrails, executor
  auth.py schedule.py       Schwab OAuth, launchd jobs
docs/
  indicator-research.md     every default traced to evidence or an ablation
  schwab-setup.md           developer account → first live quote
  runbook.md                daily/weekly operation and failure recovery
tests/                      455 tests, no network
```

---

## Commands

```
swing universe [--refresh] [--fetch]   inspect / rebuild the tradable universe
swing data --backfill | --update | --status
swing backtest [--walk-forward] [--full] [--etf-only] [--ablations] [--sensitivity]
swing scan [--dry-run] [--force] [--date YYYY-MM-DD]
swing confirm [--dry-run]
swing execute [--live] [--yes]
swing auth [--check] [--force]
swing notify-test
swing schedule install|uninstall|status|print
swing journal add|exit|stop|show      record manual fills; inspect the event log
swing positions
swing kill [--release]
```

---

## Disclaimer

This is a personal tool, not financial advice, and not a product. It drafts
orders; you are responsible for every one that reaches a broker. Backtested
results are not predictive, and the caveats above are real limitations rather
than boilerplate. Trade only money you can afford to lose entirely.
