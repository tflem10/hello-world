# Forward Paper Trading (`swing shadow`)

How to record, score and compare competing strategy configurations on data that none of them has
ever seen — and how to avoid drawing a conclusion from the result before there is one to draw.

Related: [`backtest-methodology.md`](backtest-methodology.md) (how the engine measures a strategy),
[`ablation-results.md`](ablation-results.md) (the experiments that made this necessary).

---

## 1. Why this exists

Eleven backtest experiments have been run against the same 2013–2025 out-of-sample window. The
leading candidate — `combo` — is the one that came out on top **of that comparison**, on **that
window**.

That is selection contamination, and it is not a small effect. Picking the best of eleven
variants on one dataset produces a winner whose measured edge is systematically better than its
true edge, because part of what was measured is the noise that the selection rewarded. No metric
computed on the same window can undo it: the window was consumed by the act of choosing. Re-running
the walk-forward does not help either, because the *variant* was chosen with knowledge of the whole
period, whichever slice each fold happens to score.

There is exactly one clean remedy, and it is slow: **data that arrives after the choice was made**.

`swing shadow` is that. Every trading day it records what each competing configuration *would* do,
and then scores those recorded decisions against the bars that actually print afterwards. No
broker, no orders, no money.

---

## 2. What it does not do

- It does **not** trade, place orders, or talk to a broker.
- It does **not** touch the real journal. Shadow state lives in `<state_dir>/shadow/`, and
  `journal.json` is never opened by any code path in `swing.shadow`. There is a test that asserts
  this.
- It does **not** send notifications.
- It does **not** produce a verdict. See §7 — the sample is, and will remain for years, too small
  for one.

---

## 3. The three commands

```
swing shadow run    [--asof YYYY-MM-DD] [--dry-run] [--config-name NAME]
swing shadow score  [--asof YYYY-MM-DD]             [--config-name NAME]
swing shadow report                                 [--config-name NAME]
```

`--config-name` is repeatable and defaults to every tracked configuration.

### `run` — record today's hypothetical decisions

For each tracked configuration, this runs that configuration's candidate pipeline for the day and
appends the resulting picks to `<state_dir>/shadow/<name>.json`.

**It ignores the trading gate on purpose.** `swing scan` is fail-closed — no passing gate, no picks
— and every configuration currently fails the gate, so a gate-respecting shadow harness would
record nothing, forever, including the evidence the gate is asking for. What shadow will not do is
hide it: the gate is queried anyway and its verdict is written into every day's record, so
`gate_passed: false` sits next to every position it produced.

Recording is **idempotent per (config, date)**. Re-running a day replaces that day's entry and that
day's picks together; it never duplicates them. A scan that half-failed can simply be run again.

`--dry-run` does all the work, prints what would be recorded, and writes nothing.

### `score` — walk the record forward against real bars

Every position that has not finished is replayed from its signal bar against the bars that have
printed since, using the backtest engine's exit ladder (§5). Positions that met an exit are marked
closed with their date, price and reason; the rest stay open with their current effective stop and
unrealised P&L.

Scoring is a **full replay, not an increment**. Rerunning it produces the same answer, and a revised
bar cannot leave a stale exit behind. The stored outcome is a cache, never a source of truth.

`--asof` truncates the bars, so any scoring run can be reproduced exactly.

### `report` — the side-by-side comparison

Open and closed positions, realised and unrealised P&L, win rate, profit factor, average hold and
days tracked, one column per configuration — under a header that says how little any of it means
yet. The header is unconditional; there is no sample size at which it is dropped.

---

## 4. What is being tracked, and how to add an arm

Tracked configurations live in **`config/shadow/*.toml`**, one file per arm, and the filename stem
is the journal name.

| Arm | What it is |
| --- | --- |
| `baseline` | The shipping `./config.toml`. The control. 2% proximity band, 2.0/3.0 stops. |
| `combo` | Strict breakout (`breakout_proximity_pct = 0.0`) **and** wide stops (`atr_stop_mult = 3.5`, `chandelier_mult = 5.0`). |

A tracked file describes **rules only**: `[account]`, `[universe]`, `[strategy]`, `[regime]`,
`[backtest]`, `[gates]`.

It **must not** contain `[schwab]`, `[alerts]` or `[execution]`. Unlike `config.toml`, these files
are committed to git, and those three sections hold credentials — `swing shadow` refuses to load a
file that sets them and names the offending section. `[data]` and `[paths]` are likewise taken from
your real configuration rather than the tracked file, so every arm shares one cache and one state
directory and no committed file can redirect where this machine reads or writes.

One override is applied in code: **`account.equity` is replaced by `backtest.initial_equity`**
before scanning, exactly as `swing.backtest.runner` does it and for the same reason. On a real $100
account, whole-share rounding sizes almost every candidate to zero and a forward record of
zero-share decisions would compare nothing to nothing. See
[`backtest-methodology.md` §4.1](backtest-methodology.md#41-reference-capital).

**To add an arm**, drop a new `.toml` into `config/shadow/`. **Do not edit an arm that already has
history** — changing the rules under a running experiment means the journal no longer records one
thing, and there is no way to tell afterwards which trades came from which rules.

### The `combo` arm, and why its stops are hard-coded

"Combo" in the backtest programme means two ideas stacked: **strict entry** and **wide stops**. In
the backtest, wide stops were expressed by widening `[backtest.tuning_grid]` and letting the
walk-forward tuner select a value per fold.

That does not carry over. **A tuning grid is invisible to a daily scan.** The live scanner — and the
shadow scanner, which is the same code — reads `strategy.atr_stop_mult` and
`strategy.chandelier_mult` straight from the config; only the walk-forward's per-fold parameter
search ever looks at `[backtest.tuning_grid]`. An arm that carried the wide-stops idea only in the
grid would quietly run baseline's 2.0/3.0 stops and test strict entry alone — a year of data
answering a question nobody asked.

So `config/shadow/combo.toml` writes the stops out as fixed values the scanner actually reads.
Forward trading cannot tune: there is no in-sample fold to select from, so one fixed pair has to
stand in for whatever the tuner would have chosen per fold.

- **`atr_stop_mult = 3.5`** — across the wide-stops and combo runs the tuner chose 3.5 more often
  than any other value. It is the modal selection, so it is the least arbitrary single number
  available.
- **`chandelier_mult = 5.0`** — the modal pick was 6.0, but 6.0 was the **top of the offered grid**
  `[4.0, 5.0, 6.0]`. A tuner pinned to the boundary of its own grid is not reporting a discovered
  optimum; it is reporting that the in-sample optimum wants to run off the edge, which is a classic
  overfitting signature. The interior value 5.0 is the deliberately conservative reading.

**Both numbers are a defensible judgement call, not a measured optimum.** If this arm ever wins,
that win belongs to the specific pair 3.5/5.0 and to nothing else. The `[backtest.tuning_grid]`
block is kept in the file for lineage; it is inert for the scanner.

### What the comparison can and cannot claim

Entry and exit now differ **together**. That makes this a legitimate A/B of two whole
configurations — "strict breakout + wide stops" against "2% band + default stops" — and that is the
only claim the forward record can support.

It **cannot** attribute a future difference to the entry rule or to the stops individually. If you
want that attribution, it needs a third and fourth arm (strict entry with default stops; 2% band
with wide stops), added as new files before the clock starts — and each new arm costs another
multi-year wait for its own sample. Adding one is cheap; getting an answer out of it is not.

---

## 5. How scoring stays comparable to the backtest

Scoring does not re-derive the exit rules. It borrows the engine's pieces:

- **Stop levels** come from `swing.strategy.rules.initial_stop` and `.chandelier_stop` — the same
  functions the engine feeds into its arrays.
- **Frictions** come from `swing.backtest.costs.CostModel`, charged per side.
- **The trade is booked** by the engine's own `_close_position`, so `pnl`, `pnl_pct`, `hold_days`
  and the cost split are computed by engine code, not by a copy of it.
- **The ATR used for an exit** is the ATR of the last bar whose close was known before the fill,
  never the bar being filled into.

What shadow does write out for itself is the *order* of the ladder, which
[Contract 11](backtest-methodology.md#2-fill-model-and-gap-through-handling) fixes as:

1. **Gap through at the open** — `open(t) <= stop(t-1)`: fills at the open, not at the stop.
2. **Time stop** — armed at the close of `t-1`, fills at `open(t)`.
3. **Intraday touch** — `low(t) <= stop(t-1)`: a resting stop order, so it fills *at* the stop.

The reason is reported as `chandelier` when the effective stop has ratcheted above where it started
and `stop` when it has not. The ratchet is per position and monotonic:
`stop(t) = max(stop(t-1), chandelier(t), initial_stop_at_entry)`.

**Signals at close fill at the next open**, exactly as the engine does.

There is a test (`test_the_private_pipeline_and_engine_seams_still_exist`) pinning every private
helper shadow borrows, so a refactor of `pipeline.py` or `engine.py` fails loudly here rather than
letting the two drift apart quietly.

### Three deliberate differences from the engine

1. **No `end_of_data` exit.** A backtest knows its data has ended; a shadow position whose last bar
   is yesterday is simply still open. Shadow never marks a position out because the future has not
   happened yet.
2. **Sizing happens at the signal close, not at the fill.** The live scanner sizes off the closing
   price; the engine sizes off the fill price it is about to pay. Shadow records a *live* decision,
   so it keeps the live behaviour — and inherits the live discrepancy.
3. **`hold_days` is counted on the symbol's own bars**, not on the master calendar of the whole
   universe. Identical for a liquid name; one or two days shorter for a halted one.

One consequence worth knowing when reading a journal: `picks[].initial_stop` and
`picks[].outcome.stop` are different numbers on purpose. The first is the scanner's, rounded to the
cent, because that is the price a human would have written on the order ticket. The second is the
unrounded level the engine's ladder actually uses, because comparability with the backtest is the
point. The gap is fractions of a cent and it is recorded rather than reconciled away.

---

## 6. Storage

```
<state_dir>/shadow/
  baseline.json
  baseline.json.lock
  combo.json
  combo.json.lock
```

One JSON document per arm, atomic writes (`swing.state.atomic_write_text`) serialised across
processes by an advisory lock (`swing.state.file_lock`). Reads take no lock; a reader sees one whole
document or the other, never a torn one.

```jsonc
{
  "version": 1,
  "name": "baseline",
  "days": [
    {
      "date": "2026-08-22",
      "recorded_at": "2026-08-22T15:31:04-06:00",
      "gate_passed": false,
      "gate_reasons": ["..."],   // why, verbatim from the gate
      "regime_ok": true,
      "n_picks": 2,
      "watch": ["XYZ"],          // sized to zero shares
      "notes": ["..."],          // the scanner's own explanations
      "error": ""                // set instead of picks when the scan failed
    }
  ],
  "picks": [
    {
      "symbol": "AAA",
      "signal_date": "2026-08-22",  // decided at THIS close
      "kind": "pick",
      "shares": 21,
      "signal_close": 118.44,
      "initial_stop": 109.02,
      "atr": 4.71,
      "score": 0.3812,
      "thesis": "...",
      "outcome": {                  // derived; rewritten by every `score` run
        "status": "open",           // pending | open | closed | lapsed
        "scored_asof": "2026-09-04",
        "entry_date": "2026-08-23", // the NEXT open after the signal
        "entry_price": 118.90,      // raw market price; costs are separate
        "entry_cost": 6.19,
        "stop": 112.30,             // effective, after the ratchet
        "last_date": "2026-09-04",
        "last_close": 121.10,
        "hold_days": 8,
        "exit_date": "", "exit_price": 0.0, "exit_reason": "", "exit_cost": 0.0,
        "pnl": 0.0, "pnl_pct": 0.0,
        "unrealized_pnl": 39.99,
        "note": "..."
      }
    }
  ]
}
```

A day with no picks is still recorded. The difference between "this configuration found nothing"
and "nobody looked" is exactly what a forward record has to preserve.

A corrupt shadow journal is **refused**, not reset — unlike the real journal, nothing in here is
safety state, and forward evidence cannot be regenerated. Move the file aside by hand if you really
want to start over.

---

## 7. How to read the report — and when

**The sample is meaningless right now, and will be for years.**

A profit-factor estimate does not start to carry information until roughly **300 closed trades**,
and a few hundred is the low end of what anyone should want. At about **50 trades a year**, that is
**five years or more** of forward tracking before the comparison can support a conclusion.

Until then:

- Whichever arm leads the table is not the better arm; it is the luckier one.
- Do **not** change the shipping configuration because of this report.
- Do **not** treat a shadow position as validated. Every one of them was recorded while the gate
  was failing, and the record says so.
- A profit factor of `n/a` means it could not be computed — no closed trades, or no losing trade.
  It is deliberately not printed as `inf`, because "inf" reads like a score.

The report prints this warning at the top every single time, at every sample size. That is not
decoration and it is not to be removed as the numbers fill in.

---

## 8. Scheduling it

Shadow is part of the nightly job. `swing schedule install` wires it in:

```sh
swing schedule install     # writes both plists, the wrapper, and loads them
```

Nothing about the real scan changes — same time, same weekday-only schedule, same logs, same exit
code — but the evening agent now runs a generated wrapper instead of `swing scan` directly:

```
~/.swing/bin/swing-nightly.sh
    swing scan            # the real, gated scan — unchanged
    swing shadow run      # record what each tracked config would have done
    swing shadow score    # walk yesterday's and older positions forward
```

`score` is cheap: it only fetches the symbols with open shadow positions.

### Why one job and not two

launchd runs exactly one `ProgramArguments` list, so chaining needs a wrapper or a second timed job.
It is a wrapper, for one reason: **ordering**. Scoring reads the bars the scan has just downloaded,
and a second job "five minutes later" is a bet on how long a 1,500-symbol scan takes *tonight*. When
that bet loses, the two run concurrently, each pulling the universe, and scoring reads a cache that
is mid-refresh.

What a separate job would have given for free — failure isolation — is written into the wrapper
instead, and each property has a test that runs the generated script for real:

- **`;`, never `&&`.** A failed scan still records the shadow arms. A night nobody records is a
  night the forward experiment can never get back, and it is exactly the night something was
  already going wrong.
- **The job exits with the *scan's* status.** That exit code is load-bearing: with strict delivery,
  non-zero means nobody heard about tonight's picks. A shadow failure must not be able to raise that
  flag, and must not be able to clear it either.
- **Shadow gets its own log pair**, `~/.swing/logs/shadow.out.log` and `shadow.err.log`, so neither
  stream buries the other. Each step's exit code is written into the log next to its output.

The wrapper is generated, not hand-written: `swing schedule install` overwrites it, and
`swing schedule uninstall` deletes it along with the plists. Edit `config.toml` and re-install
rather than editing the script. Running it by hand does exactly what launchd does:

```sh
sh ~/.swing/bin/swing-nightly.sh
```

### Notes

- Running the chain twice is safe. Recording is idempotent per `(configuration, date)` and scoring
  is a full replay, so a launchd double-fire on wake rewrites the same day rather than
  double-counting it.
- Shadow exits `2` when it refuses (no tracked configurations, an unreadable tracked file) and `0`
  otherwise. A scan that failed for one arm is recorded as an `error` on that day rather than
  being raised, so one bad night never loses the other arms' record.
- Missing a day is not fatal to the experiment, but it is not free either: fewer observations means
  an even longer wait for a sample that means something.

### When it stops running

A forward experiment dies quietly — nothing raises, nothing pages, the file simply stops growing —
and three weeks of silence look exactly like three weeks of a flat market. Two things make that
visible:

- **`swing shadow report` leads with a gap warning.** Above even the small-sample banner, it names
  any arm whose last record is two or more weekdays old, any weekday hole inside the series, and any
  day whose record carries a failure. It prints the log to read and the backfill command. Two or
  three weekdays of silence is usually a market holiday; more than that is a scheduler that stopped.
- **`swing schedule status`** shows the wrapper's path (flagged `(MISSING)` if it has been deleted),
  the shadow log paths, and launchd's own last-exit status for the job.

If it has stalled, `tail -n 40 ~/.swing/logs/shadow.err.log` is the first thing to read, and a
single missed day can be recorded from cached bars with `swing shadow run --asof YYYY-MM-DD`.

---

## 9. Checking it by hand

```sh
swing shadow run --dry-run          # see today's decisions, write nothing
swing shadow run                    # record them
swing shadow score                  # walk the record forward
swing shadow report                 # compare — gap warning first, then the small print
cat ~/.swing/shadow/baseline.json   # the raw record
sh ~/.swing/bin/swing-nightly.sh    # exactly what the scheduler runs each evening
tail -n 40 ~/.swing/logs/shadow.err.log   # what it said last time it ran
```

Every command takes `--asof YYYY-MM-DD`, so a past day can be recorded or re-scored deterministically
— useful for backfilling a missed night from cached bars, and the reason nothing in this module
reads the wall clock below its entry points.

### On backfilling more than a missed night

`--asof` will happily record any date the cache covers, and it is tempting to backfill a year and
have a "forward" sample tomorrow. Be careful about what that sample would actually be.

Backfilling a night the scheduler dropped is fine: the decision was already determined by data that
existed at the time, and recording it late changes nothing about it.

Backfilling a long stretch is a different thing, and the honest test is one question: **could the
data have influenced the choice of configuration?** The eleven experiments scored a window ending in
2025, so 2026 bars were not in the scored window — but they were sitting in the cache while those
experiments were run, and nobody can now demonstrate that no one looked. Data that was *available*
during selection is weaker evidence than data that did not exist yet, and a backfilled stretch
should be labelled and counted separately if it is used at all, never silently merged into the
forward record.

If you do backfill, do it into a separate `state_dir` first (`--config` a throwaway file) and decide
what it is worth before letting it near the real journals.
