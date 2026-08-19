# Runbook

How to operate this thing day to day, and what to do when a piece of it breaks.

---

## First-run checklist

Work through this once, in order. Each step has a check you can actually see.

```bash
./install.sh
source .venv/bin/activate
swing --version                      # 1. the CLI runs
make test                            # 2. the suite is green
swing doctor                         # 3. can this machine reach a data provider?
```

`swing doctor` probes rather than assumes — it asks the provider for a few
bars of SPY instead of reading the config and hoping. Run it first on any new
machine, and again whenever something behaves oddly; every failure it reports
carries the command that fixes it. `--offline` skips the network probes.

```bash
swing universe --show 10             # 3. ~1,000 symbols across the enabled indices
swing data --backfill                # 4. SLOW: 15-45 min for the full universe
swing data --status                  # 5. history goes back to 2005, nothing stale
```

```bash
swing backtest --walk-forward        # 6. THE gate. Read the whole report.
swing backtest --etf-only            # 7. the survivorship-free lower bound
swing backtest --ablations           # 8. fills in docs/indicator-research.md §0
swing backtest --sensitivity         # 9. read for flatness, not the best cell
```

```bash
swing notify-test                    # 10. every channel reports "ok"
swing scan --dry-run                 # 11. a real sheet, nothing sent
swing scan                           # 12. it arrives on your phone
swing schedule install               # 13. launchd, both jobs
swing schedule status                # 14. both loaded, last exit 0
```

Only after step 6 passes will step 11 produce picks. That is the point.

**Set `[account] equity` to your actual balance before step 11**, or every
share count is wrong.

---

## The daily loop

### 17:30 ET — the scan

Runs automatically once scheduled. It:

1. checks the gate (and stops there if it fails),
2. updates the price cache incrementally,
3. refreshes earnings dates and fundamentals if they are stale,
4. evaluates the strategy on today's close across the whole universe,
5. sizes the survivors, drafts orders, writes `reports/scan-YYYY-MM-DD/`,
6. sends the sheet to every enabled alert channel.

**What to read on the sheet, in order:**

- the **gate** line — if it says BLOCKED, nothing else matters
- the **regime** line — risk-off means no new entries tonight, by design
- **warnings** — stale data, unknown earnings dates, a failed refresh
- **open positions** — raise-stop instructions, breached stops, earnings soon
- **picks** — shares, stop, trail, risk in dollars and as a % of the account
- **watch** — things that passed everything but did not fit

### 09:00 ET — the confirm

Re-quotes each pick and re-classifies it:

- **confirmed** — price essentially unchanged, the drafted order stands
- **adjusted** — price moved a little; shares re-sized and the stop re-derived
  so the dollar risk stays where you put it
- **cancelled** — gapped more than 1 ATR (or 3%) past the reference. Do not
  chase it. It is a different trade at a worse price.

The order files in `reports/scan-*/orders/` are **rewritten** at this point, so
a cancelled pick has no order file left to place by accident.

### Placing the orders (v1)

For each confirmed pick, in Schwab or thinkorswim:

1. **Buy LIMIT** at the limit price in the order JSON (reference + 0.3%).
2. Attach the protective **SELL STOP, GTC**, at the stop price, for the same
   quantity. Same quantity matters — a partial stop leaves shares uncovered,
   which is exactly what the order validator refuses to draft.
3. Or import the drafted `bracket_*.json` if you are using the API directly.

Then record it so the scanner knows you are in:

```bash
swing journal add AAPL 12 190.55 182.10 --trail 5.70
```

`add SYMBOL SHARES PRICE STOP`, plus optional `--trail OFFSET`, `--order-id ID`
and `--note TEXT`. If you already hold the symbol, an `add` whose stop is
*below* the stop on record is refused — re-entering at a worse stop is almost
always a typo. Pass `--force` when you actually mean it.

If you skip this, tomorrow's scan thinks you are flat and will happily suggest
a fifth position while you hold four.

When the trail ratchets and you move the resting stop at the broker, tell the
journal too:

```bash
swing journal stop AAPL 186.40
```

Stops only go up: lowering one needs `--force`, because the trailing discipline
is the thing that bounds your loss. `swing journal show [--limit N]` prints the
tail of the event log when you want to see what was recorded.

### Managing open positions

The sheet tells you what changed:

- **raise stop** — the Chandelier trail ratcheted up. Modify the resting stop
  order to the new price. It never moves down.
- **STOP BREACHED** — the last close is at or below your stop and the stop did
  not fill. Check the position immediately; something is wrong (order
  cancelled, wrong quantity, halted symbol).
- **earnings soon** — tighten or close. A stop does not protect you across an
  overnight gap; that is the whole reason for the entry blackout.
- **time stop** — held 40 trading days. Close it and free the slot.

---

## Weekly

```bash
swing auth                # Schwab refresh tokens expire after 7 days. Always.
swing auth --check        # confirm the account and a live quote
swing positions           # what the journal thinks you hold
```

Update `[account] equity` whenever your balance moves meaningfully. Everything
is sized off that number, and `swing execute` blocks when it drifts more than
20% from the broker's own figure.

## Monthly / after any strategy change

```bash
swing universe --fetch            # refresh index constituents from Wikipedia
swing backtest --walk-forward     # re-validate; the gate is bound to the config hash
```

Any edit to `[account]`, `[universe]`, `[strategy]` or `[backtest]` changes the
config hash, which re-locks the gate until you re-run the walk-forward. This is
deliberate: it makes "just nudge the stop and see" cost something.

### Closing the earnings gap in the backtest

By default the backtest runs *without* the earnings blackout the live scanner
applies, and says so on every report. If you get hold of a historical earnings
calendar — a paid export, a broker download, a hand-built file — point
`[data] earnings_calendar` at it (`symbol,date` header, ISO dates, `#` comments
allowed; the format is spelled out in `src/swing/data/earnings_calendar.py`)
and the backtest applies the same blackout, the warning disappears, and the
report manifest records the file, its symbol and date counts, and how much of
the traded universe it covers. Two things to know: a path that does not exist
aborts the run rather than quietly falling back, and **symbols missing from the
file get no protection at all**. The run is explicit about the second — a
partial calendar produces a warning naming the numbers ("covers 50 of 500
universe symbols; the other 450 get NO earnings blackout") — so read that line
instead of assuming a supplied calendar means a covered universe. The key lives
under `[data]`, so adding it does not change the config hash or re-lock the
gate.

---

## Turning on v2 execution

Do these in order, and stop at the first surprise.

1. Run `swing execute` (dry run) for at least a week. Read the JSON. Compare
   the guardrail output to what you would have done by hand.
2. Set `[execution] enabled = true`. Leave `autopilot = false`.
3. Set `max_orders_per_day = 1` and `max_new_exposure_pct = 0.05` for the first
   live session.
4. Run `swing execute --live` and confirm **one** order — one share of a liquid
   ETF. Type `yes` deliberately.
5. Open thinkorswim. Verify: the buy limit is resting, the child stop exists,
   it is GTC, and the quantity matches.
6. Only after that trade closes cleanly, relax the caps.

`autopilot = true` removes the per-order prompt. Do not set it until you have
watched confirm mode do the right thing repeatedly.

---

## When something breaks

### The gate is blocking and I want picks

That is the system working. Your options, best first:

1. Improve the strategy and re-run the walk-forward.
2. Decide the thresholds in `[backtest.gate]` were wrong *and write down why*
   before changing them. Lowering a threshold to get past your own gate is the
   exact behaviour the gate exists to make visible.
3. `swing scan --force` — produces picks, stamps "GATE OVERRIDDEN" on the
   sheet, the alert and the log, and **still refuses to auto-execute them**.

### No alert arrived

```bash
swing notify-test                    # which channel is broken?
cat ~/.swing/logs/scan.err.log       # what did the job say?
ls reports/scan-$(date +%F)/         # did the sheet get written at all?
```

The sheet is always written before any alert is attempted, so "no alert" and
"no picks" are different failures. A broken channel never stops the others.

### The scheduled job did not run

```bash
swing schedule status
launchctl list | grep com.swing
```

launchd does **not** wake a sleeping Mac. If the machine was asleep at 17:30 the
job runs when it next wakes. Either keep it awake (System Settings → Energy) or
accept late picks. Check the `last exit status` column — anything non-zero
means the job ran and failed, and `~/.swing/logs/` has the reason.

### yfinance stopped working

Yahoo's endpoints are undocumented and change without notice. Symptoms: the
scan warns "data refresh failed" and the sheet is based on yesterday's prices.

- The cache means you still get picks, just stale ones. **Do not trade a sheet
  with a staleness warning** without checking prices yourself.
- Try `uv pip install --python .venv/bin/python -U yfinance` — breakage is
  usually fixed upstream within days.
- If you have Schwab approved, set `[data] provider = "schwab"`.
- Otherwise set `[data] provider = "stooq"` — a free daily CSV source that
  needs no key. **Delete `data/cache/` and re-backfill when you switch**: the
  cache does not record which provider wrote a file, so mixing sources splices
  two adjustment bases into one series and manufactures a price jump out of
  nothing. Stooq bars are split-adjusted but **not dividend-adjusted**, so
  numbers from a long backtest drift from yfinance's; it also publishes no
  earnings dates or fundamentals, so those soft filters fail open. Fine for
  keeping the nightly scan alive, not a substitute for re-validating on
  yfinance or Schwab data.

### The Schwab token expired

Data falls back to yfinance and the pick sheet carries a warning. Order
placement does **not** fall back — `swing execute` refuses outright. Run
`swing auth`.

### An order was rejected

```bash
swing execute            # dry run prints the exact JSON that would be sent
```

Compare against the current schwab-py docs. The built-in validator catches
structural errors (mismatched quantities, a DAY-duration protective stop, a
market order) but cannot know that Schwab renamed a field. `tests/test_orders.py`
pins our JSON against schwab-py's own builder, so `make test` will tell you if
the vendor moved something.

### I need everything to stop, now

```bash
swing kill
```

Creates `~/.swing/KILL`. No orders will be placed while it exists, and the
check runs before the config is even consulted, so it works when other things
are broken.

**It does not cancel orders already resting at Schwab.** Cancel those in
thinkorswim or the Schwab app. Then:

```bash
swing positions          # what the journal thinks
swing kill --release     # when you are ready
```

### The journal and the broker disagree

`swing execute` blocks on this and names the difference. It is the right
response: if the journal is wrong, then the free-slot count, the available
cash and the duplicate check are all wrong too.

Fix it by appending corrective events:

```bash
swing journal exit AAPL 12 195.00 --reason "closed by hand"
swing journal show --limit 20      # check what you just wrote
```

`exit SYMBOL SHARES PRICE` handles partial exits too — pass the shares you
actually sold.

The journal is append-only on purpose. Correct it by adding events, not by
editing history.

### A pick's earnings date says UNKNOWN

Free earnings data is patchy, especially for small caps. The default policy
(`allow_with_warning`) lets it through with a tag. **Check it yourself** before
buying — 30 seconds on the company's IR page. If you would rather not think
about it, set `unknown_date_policy = "block"`.

---

## Native trailing stop vs the Chandelier exit

The system drafts a `bracket_trailing` variant using Schwab's native
`TRAILING_STOP` with a fixed dollar offset from the **last price**, tracked
continuously.

That is **not** what the backtest models. The Chandelier exit trails the
**highest daily close** and ratchets **once a day**. They diverge: the native
trail reacts to intraday spikes the Chandelier ignores, and it can be stopped
out by a wick the backtest would have held through.

Both are offered because the native version needs no daily maintenance, which
is a real advantage if you cannot reliably adjust stops each evening. Just know
that placing it means trading a slightly different system than the one that
passed the gate. The plain `bracket_stop` plus the nightly "raise stop"
instructions is the variant the backtest actually validated.

---

## Files worth knowing about

| path | what |
|---|---|
| `config.toml` | every setting. Secrets. chmod 600, gitignored. |
| `data/cache/bars/*.parquet` | price history. Safe to delete; re-backfill. |
| `data/cache/absent.json` | symbols that returned nothing, skipped for a week |
| your `[data] earnings_calendar` CSV | optional; the backtest reads it and records it in the report manifest |
| `reports/YYYY-MM-DD-walkforward/` | the report the gate reads |
| `reports/scan-YYYY-MM-DD/` | pick sheet, renderings, drafted order JSON |
| `~/.swing/journal.jsonl` | append-only record of everything. **Back this up.** |
| `~/.swing/schwab_token.json` | live credential, mode 600 |
| `~/.swing/KILL` | kill switch. Exists = no orders. |
| `~/.swing/logs/` | scheduled-job stdout and stderr |

---

## Things that should make you stop and look

- a backtest with a profit factor above 2.5 or a sub-10% drawdown — that is a
  bug report, not a success
- a scan that produces picks while the sheet says the data is days stale
- a stop that did not fill when the close was below it
- realised losses consistently worse than 1R (costs or slippage are wrong, or
  you are holding through gaps)
- the journal disagreeing with the broker more than once
- any urge to lower a gate threshold rather than fix the strategy
