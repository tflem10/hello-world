# Code Audit Report — `swing`

**Commit audited:** `ec23a88ca2f8a5c3c0aaae4d862cf3ab671683c5` (branch `claude/swing-trading-picker-8rorzg`)
**Date:** 2026-08-19
**Auditor:** Claude (read-only audit; no source files were modified)

---

## 1. Executive summary

This is an unusually disciplined codebase for its size (~10.1k LOC source, ~5.9k LOC tests, 469 passing). Invariants are stated and mostly enforced, error-handling philosophy is consistent ("data degrades, execution blocks"), the journal is genuinely append-only, orders are validated recursively, reports state their own caveats, and there are **zero** TODO/FIXME markers. Most modules survived line-by-line review with nothing to report. That is the context for what follows: the findings below are real, but they sit inside a system that takes correctness seriously.

**Headline counts: 1 Critical / 2 High / 10 Medium / 12 Low.**

The five findings that matter most:

1. **[BUG-001, Critical] Every production scan will crash the moment the backtest gate passes.** `scan.py:131` evaluates `bars.get("SPY") or load_bars(...)` — and `or` on a pandas DataFrame raises `ValueError: truth value ambiguous`. SPY is in the shipped ETF universe, so in production `bars["SPY"]` always exists and the line always raises. Verified against the installed pandas. The test suite is blind to it because its fixture universe (`S00…S05`) never contains the regime symbol, so tests only ever exercise the `None or …` branch. The user has never hit it only because the gate currently blocks, which exits `run_scan` before this line. The sibling code in `runner.py:80` does the same lookup correctly with an `is None` check.

2. **[BUG-002, High] The nightly incremental update slowly corrupts the price cache — the exact corruption the codebase's own `ProviderMismatch` guard exists to prevent.** `pipeline.update()` fetches only a recent window (oldest stale bar − 5 days) and `merge_bars` grafts it onto cached history. yfinance `auto_adjust=True` re-bases the *entire* history on every dividend or split, so the fresh window arrives on a new adjustment basis while everything before the window keeps the old one. Each dividend leaves a ~0.3–0.6% step at the merge boundary; a split leaves a cliff (a 10:1 split → cached history 10× too high). Every indicator with a lookback spanning the boundary is then computed on prices that never existed. The 5-day overlap the code already fetches contains exactly the data needed to detect this; no check exists.

3. **[BUG-003, High] The Schwab provider silently defeats the cache's provider-identity enforcement.** On any client failure (e.g. the weekly token expiry) `SchwabProvider.daily_bars` falls back to yfinance and returns its bars; `pipeline.update()` then writes those dividend-adjusted bars into a cache stamped `schwab`. `ensure_provider` compares config vs stamp — both say "schwab" — and waves it through. This is the same adjustment-basis splice as BUG-002, delivered through the front door, on the exact Friday-night-token-expiry path the module's docstring advertises as safe.

4. **[BUG-004 + BUG-005, Medium] The execution layer's two headline safety nets have accounting holes.** The daily order cap counts journal *events*, but a placed order writes two `EVENT_PLACED` events (verified empirically: 1 order → count 2), and the per-order check counts only the current run — so the cap both under-allows within a day (blocks after 2 of 3) and over-allows across runs (4 orders can be placed under a cap of 3). Separately, the kill switch is checked once at startup and never re-checked in the per-order loop, which can sit indefinitely at an interactive `input()` prompt — engage `swing kill` mid-run and the remaining orders still transmit.

5. **[DEBT-001, Medium] The fallback data provider is structurally dead.** stooq.com now fronts its CSV endpoint with a JavaScript proof-of-work bot wall (verified by live probe: HTTP 200 + HTML challenge for the exact request the code sends, with and without a browser UA). The provider degrades gracefully and `swing doctor` surfaces it, but the redundancy story in the docstrings ("plain, stable CSV download… a good second source to fail over to") is no longer true, and there is currently no working fallback for a yfinance outage.

Also verified this morning against live artifacts: a transient yfinance failure during the user's real backfill marked 36 symbols absent for 7 days — including BK (BNY Mellon) and CTRA (Coterra), both S&P 500 members that certainly have data (BUG-006).

---

## 2. Scope & method

- **Reviewed exhaustively (every line):** all 42 files under `src/swing/` — strategy (`rules.py`, `sizing.py`, `indicators.py`), data layer (`provider.py`, `cache.py`, `pipeline.py`, three providers, `universe.py`, `earnings_calendar.py`), backtest (`engine.py`, `walkforward.py`, `metrics.py`, `bootstrap.py`, `gate.py`, `runner.py`, `report.py`), execution (`journal.py`, `guardrails.py`, `executor.py`, `orders.py`), live path (`scan.py`, `picks.py`, `confirm.py`), infra (`config.py`, `auth.py`, `cli.py`, `commands.py`, `doctor.py`, `schedule.py`, `alerts/*`, `logging_setup.py`).
- **Sampled:** tests (read for coverage-shape and to explain why specific bugs survived; not audited line-by-line), docs.
- **Skipped:** `.venv/`, universe CSV contents, generated reports.
- **Dynamic verification performed:** pandas `DataFrame or` behavior (BUG-001); journal event double-count via synthetic journal (BUG-004); live stooq endpoint probe ×3 request shapes (DEBT-001); this morning's real `absent.json` inspected (BUG-006); installed pandas version 3.0.5 (DEBT-003 context).
- **Blind spots:** no runtime profiling of the walk-forward (wall-time figure is the observed ~25 min run from earlier today); Schwab API paths reviewed statically only (no token on this machine — `schwab-py` not installed); alert channels not exercised; no memory profiler run (LEAK-001 figure is arithmetic, not measurement).

Estimated source coverage: **~100% of first-party Python read; findings verified where dynamically checkable.**

---

## 3. Findings index

| ID | Title | Category | Severity | Confidence | Effort |
|---|---|---|---|---|---|
| BUG-001 | `DataFrame or` crashes every scan once the gate passes | correctness | Critical | Confirmed | S |
| BUG-002 | Incremental update splices adjustment bases into the cache | correctness | High | Confirmed | M |
| BUG-003 | Schwab→yfinance silent fallback poisons the stamped cache | correctness | High | Confirmed | M |
| BUG-004 | Daily order cap counts events, not orders; per-run counter ignores prior runs | correctness | Medium | Confirmed | S |
| BUG-005 | Kill switch never re-checked inside the per-order loop | correctness | Medium | Confirmed | S |
| BUG-006 | Provider outage poisons the absent-list for the whole universe (7 days) | correctness | Medium | Confirmed | S |
| BUG-007 | Engine sizes entries with same-day closes of held positions (lookahead) | correctness | Medium | Confirmed | S |
| BUG-008 | Backtest applies *today's* fundamentals to all history (lookahead, undocumented) | correctness | Medium | Confirmed | S |
| BUG-009 | `confirm` re-sizes each pick against full cash; combined picks can exceed it | correctness | Medium | Confirmed | S |
| BUG-010 | Time-stop exit silently cancelled if the symbol has no bar on exit day | correctness | Low | Confirmed | S |
| BUG-011 | `true_range` raises IndexError on an empty series | correctness | Low | Confirmed | S |
| PERF-001 | Walk-forward rebuilds the full panel 378× | efficiency | Medium | Confirmed | M |
| PERF-002 | Journal fully re-read and re-parsed on every query | efficiency | Low | Confirmed | S |
| PERF-003 | `drawdown_stats` Python loop runs 1000× inside the bootstrap | efficiency | Low | Confirmed | S |
| PERF-004 | `ema()` is a Python loop (off the hot path) | efficiency | Low | Confirmed | S |
| LEAK-001 | Walk-forward feature cache is unbounded (~1.3 GB by design) | resource | Low | Likely | S |
| DEBT-001 | stooq fallback dead upstream (bot wall); docs claim a stable CSV | tech-debt | Medium | Confirmed | M |
| DEBT-002 | `account.equity` is in the gate hash; every balance edit re-locks the gate | design | Medium | Confirmed | S |
| DEBT-003 | pandas floor `>=2.1` too low for `resample("ME")` (needs 2.2) | dependencies | Low | Confirmed | S |
| DEBT-004 | Dead code: `today_orders_placed` never called | tech-debt | Low | Confirmed | S |
| DEBT-005 | Market-order guardrail scans only top-level `orderType` | defense-in-depth | Low | Confirmed | S |
| DEBT-006 | `Pick(**payload)` breaks on any sheet schema evolution | robustness | Low | Confirmed | S |
| DEBT-007 | Test blind spots map exactly onto the bugs found | test-coverage | Medium | Confirmed | M |
| DEBT-008 | Gate has no freshness or data-fingerprint bound | design | Low | Confirmed | S |
| DEBT-009 | Plaintext SMTP credential in config (documented, perms-checked) | security-note | Low | Confirmed | — |

---

## 4. Detailed findings

### Bugs

### [BUG-001] `DataFrame or` crashes every scan once the gate passes
Severity: Critical | Confidence: Confirmed | Effort: S
Location: `src/swing/scan.py:131`

**What's wrong:**
```python
benchmark = bars.get(regime_symbol) or load_bars(cfg, [regime_symbol]).get(regime_symbol)
```
`bars.get("SPY")` returns a `DataFrame` whenever the regime symbol is in the loaded universe. Python's `or` calls `bool()` on it, and pandas raises `ValueError: The truth value of a DataFrame is ambiguous` — verified against the installed pandas 3.0.5. SPY is in the shipped `data/universe/etfs.csv` and `etfs = true` is the default, so the production path always has SPY in `bars` (5,000+ cached bars, well past the `min_bars=260` filter).

**Impact:** `run_scan` raises an unhandled exception after the gate check and data refresh. Every nightly scan dies with a traceback the moment the walk-forward gate passes — or immediately, with `--force`. The system's entire output path is broken in its default configuration. It has not been observed only because this install's gate currently blocks, which returns at `scan.py:63` before reaching line 131.

**Why tests miss it:** the `scan_config` fixture (`tests/test_scan.py:50`) sets every index list false and uses `extra_symbols=["S00".."S05"]`. SPY is written to the *cache* but never enters the *universe*, so `load_bars(cfg, universe, ...)` never includes it, `bars.get("SPY")` is `None` in every test, and `None or …` short-circuits safely.

**Fix:** the codebase already contains the correct pattern at `src/swing/backtest/runner.py:79-81`:
```python
benchmark = bars.get(benchmark_symbol)
if benchmark is None:
    benchmark = load_bars(cfg, [benchmark_symbol]).get(benchmark_symbol)
```
Apply the same two-line form in `scan.py`, and add a scan test whose universe includes the regime symbol (see DEBT-007).

---

### [BUG-002] Incremental update splices adjustment bases into the cache
Severity: High | Confidence: Confirmed | Effort: M
Location: `src/swing/data/pipeline.py:160-176` (window construction), `src/swing/data/cache.py:262-273` (`merge_bars`)

**What's wrong:** `update()` fetches one window per night — `start = min(stale.values()) - 5 days` — and `merge_bars` grafts it onto cached history, fresh rows winning only *inside* the window:
```python
oldest = min(stale.values())
start = oldest - timedelta(days=5)
...
merged = cache.upsert(sym, bars)   # merge_bars: old rows outside the window survive as-is
```
The yfinance provider requests `auto_adjust=True`, which re-bases the **entire** history whenever a dividend or split occurs. After any corporate action, the fresh window is on the new basis and everything before the window stays on the old one. The in-code comment ("Overlap is harmless: merge prefers fresh rows, which is also how vendor revisions get picked up") is true only within the 5-day overlap; the revision extends over the whole series.

**Impact:** for a dividend, a spurious ~0.3–0.6% step at the merge boundary per event, compounding with each ex-date — a 200-day SMA, 126-day momentum, or 52-week-high test spanning the boundary is computed on a price path that never existed. For a split, a cliff: after a 10:1 split, cached pre-window closes are 10× the post-window closes; ATR explodes, `trend_ok` fails against a phantom 52-week high, the symbol silently drops out of the tradable set for up to a year, and any backtest over the cache inherits the corruption. This is precisely the failure mode the `ProviderMismatch` docstring (`cache.py:40-49`) describes as "silent and total" — occurring *within* one provider. Today's cache is clean (full backfill this morning, single snapshot); corruption starts accruing with the first nightly `update()` that crosses a corporate action.

**Fix:** the overlap fetched every night is the detector. In `update()`, compare the overlap rows against the cached rows for the same dates; if any close differs by more than a tolerance (say 0.1%), discard the merge and re-backfill that symbol's full history (`cache.write` replaces atomically). Cheap, targeted, and turns every corporate action into one extra full download for one symbol.

---

### [BUG-003] Schwab→yfinance silent fallback poisons the stamped cache
Severity: High | Confidence: Confirmed | Effort: M
Location: `src/swing/data/schwab_provider.py:66-90` (`daily_bars`, `_fall_back`), consumed by `src/swing/data/pipeline.py:98-101, 178-181`

**What's wrong:** with `[data] provider = "schwab"`, any client failure — expired weekly token, `schwab-py` missing, transport error — makes `daily_bars` return **yfinance** bars:
```python
except (SchwabNotConfigured, Exception) as exc:
    return self._fall_back("price history", exc).daily_bars(symbols, start, end)
```
`pipeline.update()`/`backfill()` then `cache.write`/`upsert` those bars into a cache stamped `schwab`. `ensure_provider` compares the *configured* provider against the stamp — both say "schwab" — so the enforcement added in commit `b328ec6` ("Enforce provider identity on the bar cache") never sees the substitution. Schwab candles and yfinance `auto_adjust` closes are on different adjustment bases (dividends), so this is the same splice as BUG-002, on the exact "Friday-night token expiry" path the module docstring presents as a safe degradation.

**Impact:** one expired token + one nightly scan = mixed-basis series in a cache that attests to a single provider, with no warning at the cache layer and a `data_fingerprint` that happily hashes the spliced series into reports.

**Fix options (either closes it):** (a) make the fallback quote-/earnings-/fundamentals-only — `daily_bars` raises or returns `{}` on client failure so the scan degrades to "stale cache with a loud warning" (which `run_scan` already handles gracefully); or (b) have `daily_bars` results carry the *actual* source and make `pipeline` refuse to write bars whose source ≠ stamp. Option (a) is smaller and matches the house rule that bars are load-bearing while quotes are advisory.

---

### [BUG-004] Daily order cap counts events, not orders; per-run counter ignores prior runs
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/execution/executor.py:236-249` (`_place` writes two `EVENT_PLACED` lines), `src/swing/execution/guardrails.py:233-239` (preflight counts events), `src/swing/execution/executor.py:148-151` (per-order check receives this-run count)

**What's wrong:** `_place` journals `EVENT_PLACED` twice per successful order (`status="submitting"`, then `status="accepted"`). Preflight's cap check is `len(placed_today(cfg)) < cap` — an *event* count. Verified empirically against a synthetic journal: one order → `placed_today()` returns 2; two orders → 4, which already blocks a cap of 3. Meanwhile `check_order`'s cap check receives `placed` — a counter of orders placed *in this run only*, starting at 0 regardless of what earlier runs did today.

**Failure scenario:** cap = 3. Run A places 1 order (2 events) and is interrupted. Run B: preflight sees 2 < 3 → passes; `check_order` counts 0,1,2 → places 3 more. **4 orders placed under a cap of 3.** Conversely on a clean day the cap effectively becomes 2, because 2 orders → 4 events ≥ 3 blocks the next run — the documented semantics ("a bug that generates orders in a loop stops at 3") hold in neither direction. `today_orders_placed()` (guardrails.py:391) looks like it was written to be this single source of truth and is never called (DEBT-004).

**Fix:** count *orders*, once, from one source: e.g. count distinct `(symbol, date)` with `status="accepted"` (or only the "submitting" events, one per attempt — pick one and document it), use it in preflight, and pass `that_count + placed_this_run` to `check_order`.

---

### [BUG-005] Kill switch never re-checked inside the per-order loop
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/execution/executor.py:75-80` (sole check), `executor.py:213-222` (`_confirm` blocks on `input()`), loop at `executor.py:140-190`

**What's wrong:** `kill_switch_engaged` is checked once at the top of `run_execute`. The per-order loop then runs guardrails, prompts (`input()` — which waits indefinitely), and transmits. Neither `check_order` nor `_place` re-consults the kill file.

**Failure scenario:** `swing execute --live` with three picks. Order 1 transmits; something looks wrong; the user runs `swing kill` from another terminal (its stated purpose: "stops everything, without needing the network, the config, or the API"). The executor is sitting at the order-2 confirmation prompt. The user returns later, types `yes` — orders 2 and 3 transmit with the kill switch engaged. With `autopilot = true` the window is seconds instead of minutes, but the guarantee is still violated.

**Fix:** re-check `kill_switch_engaged(cfg)` at the top of each loop iteration (and ideally immediately before `client.place_order`), breaking with the same message and exit code 6.

---

### [BUG-006] Provider outage poisons the absent-list for the whole universe (7 days)
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/data/pipeline.py:104-113` (`backfill` marks everything missing), `src/swing/data/yfinance_provider.py:56-76` (batch failure → per-symbol absence)

**What's wrong:** `backfill` marks every requested-but-unreturned symbol absent for `absent_retry_days` (7). Nothing distinguishes "this ticker is delisted" from "yfinance had a bad five minutes": a failed batch falls back to per-symbol downloads, and symbols that fail both ways land in `absent.json`.

**Evidence from this machine, this morning:** the user's real backfill left 36 symbols in `data/cache/absent.json` dated 2026-08-19 — including **BK** (Bank of New York Mellon) and **CTRA** (Coterra Energy), current S&P 500 members that unquestionably have price history. Those names are now invisible to every scan and backtest until 2026-08-26 unless manually cleared. In the worst case (fresh install, provider fully down for the duration of one backfill), the *entire universe* is marked absent and `swing data --backfill` becomes a silent no-op ("0 to download") for a week.

**Fix:** treat absence as suspicious when it is epidemic: if more than a small fraction of a batch (say 10–20%) comes back empty, log an outage warning and *don't* mark that batch absent. Cheap complement: a `swing data --clear-absent` escape hatch; today the only remedy is hand-editing `absent.json`. (`cache.py:239-244` already acknowledges the ambiguity in a comment; the blast radius is what makes it a bug.)

---

### [BUG-007] Engine sizes entries with same-day closes of held positions (lookahead)
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/engine.py:361-372`

**What's wrong:** entries fill at the open of day *i*, but the equity used for risk sizing marks the *other* open positions at `_last_price(cl, i, …)` — day *i*'s **close**:
```python
equity_now = cash + sum(
    p.shares * _last_price(cl, i, p.col) for p in positions.values()
)
size = size_position(entry=fill, stop=stop, equity=equity_now, ...)
```
At the open, that close is hours in the future. This violates the engine's own charter ("Nothing that happens on day t+1 is visible when the order is chosen" — the *size* of the order is part of the choice).

**Impact:** small in expectation — it perturbs `floor(equity·risk% / risk_per_share)` by intraday drift of *other* holdings, occasionally ±1 share — but it is a genuine information leak in the module whose central claim is "no look-ahead", and share-count divergence vs. live compounds through the whole walk-forward (live sizing uses last night's close via the pick sheet).

**Fix:** mark holdings at `_last_price(cl, i-1, …)` (yesterday's close — what the drafted overnight order actually knew), or at today's open where finite.

---

### [BUG-008] Backtest applies *today's* fundamentals to all history (lookahead, undocumented)
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/runner.py:73-79` (meta built from the current fundamentals cache), `src/swing/strategy/rules.py:96-111` (blocks all bars for the symbol)

**What's wrong:** `load_universe_bars` evaluates `fundamentals_ok` from `cache.read_fundamentals()` — a snapshot fetched this week — and the engine applies the verdict uniformly to every bar back to 2010. A company with negative EPS *today* is excluded from a 16-year backtest even for years when its fundamentals were fine, and vice versa.

**Impact:** the same shape as survivorship bias (conditioning the past on the present), softened by the filter's design — it only blocks on a known-negative verdict, and free-data gaps mean most symbols evaluate to `None` ("no opinion"). What elevates it: the report caveats survivorship and the earnings-blackout divergence loudly and specifically, but not this one — a reader of `report.md` is told about the other two biases and left to assume the fundamentals filter is unbiased.

**Fix (cheapest honest option):** disable the fundamentals filter in backtests (`meta` without verdicts) and let the scan-only filter be a stated live/backtest divergence — mirroring exactly how the earnings blackout is handled, warning included. Alternatively add the caveat line to `report.py` alongside `SURVIVORSHIP_NOTE`.

---

### [BUG-009] `confirm` re-sizes each pick against full cash; combined picks can exceed it
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/confirm.py:166-176`

**What's wrong:** `run_scan._size_and_draft` correctly decrements `cash_left` pick-by-pick. The pre-open re-size does not: every adjusted pick is sized with `available_cash=sheet.available_cash` — the same full amount each time. Two picks that both gap down (each individually affordable, shares increased by the re-size) can sum to more than the account holds.

**Failure scenario:** equity $10,000, two picks at ~$4,800 notional each after scan. Both gap down 2.5% pre-open; each re-sizes to ~$5,100 against the full $10,000 → combined $10,200 > cash. The executor's `max_new_exposure_pct` (default 0.5) may catch the excess incidentally, and the broker would reject the second fill on buying power — but "the broker will catch it" is the failure mode this codebase everywhere else refuses to rely on.

**Fix:** iterate picks in rank order in `run_confirm`, threading a decremented `cash_left` into `_apply_quote` exactly as `_size_and_draft` does.

---

### [BUG-010] Time-stop exit silently cancelled if the symbol has no bar on exit day
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/engine.py:342-349` (queued exit skipped, queue cleared), `engine.py:407-411` (`time_stop_queued` never resets)

**What's wrong:** a queued exit whose symbol has no bar at the open (halt, missing data) is `continue`d — carried, correctly — but `pending_exits = []` then clears the queue, and step 4 will never re-queue the time stop because `pos.time_stop_queued` is already `True`. The position's time stop is permanently cancelled; it exits only via trail/stop or end-of-backtest.

**Impact:** rare (needs a missing bar on the exact exit morning) and bounded (the trail still protects), but it contradicts the time-stop contract and can extend a 40-day hold indefinitely on thinly traded names with data gaps.

**Fix:** on the no-bar branch, re-append `(sym, reason)` to the next day's queue (or reset `time_stop_queued = False`).

---

### [BUG-011] `true_range` raises IndexError on an empty series
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/indicators.py:145`

**What's wrong:** `tr.iloc[0] = float(...) if len(high) else np.nan` — the conditional guards the *value*, not the assignment; on an empty series `tr.iloc[0] = np.nan` still executes and raises `IndexError`.

**Impact:** contained in practice — both the engine (`engine.py:180-184`) and the scanner wrap `compute_features` in try/except, and empty frames are filtered by `min_bars` upstream — but any direct caller of `atr()`/`true_range()` on an empty frame crashes, and the module contract says indicators return NaN-padded frames, not exceptions.

**Fix:** `if len(high): tr.iloc[0] = float(high.iloc[0] - low.iloc[0])`.

---

### Performance

### [PERF-001] Walk-forward rebuilds the full panel 378×
Severity: Medium | Confidence: Confirmed | Effort: M
Location: `src/swing/backtest/engine.py:157-199` (`_build_panel` per `run()`), `src/swing/backtest/walkforward.py:239-306` (14 windows × 27 combos)

**What's wrong:** `feature_cache` correctly memoizes `compute_features` per `(symbol, feature_key)` — 3 distinct feature passes for the shipped grid, as designed. But `_build_panel` — the per-symbol `reindex` onto the union date axis plus 8 dense `(dates × symbols)` array fills — runs once per `run()`: 14 windows × 27 combos + 14 OOS runs = **392 full panel constructions** over ~960 symbols, even though only 3 distinct feature-frame sets exist and window bounds are pure slices.

**Impact:** the dominant cost of the observed ~25-minute walk-forward on this machine (960 symbols, 2010–2026). Panels are also where the peak transient memory goes (a 3-year IS window ≈ 8 arrays × 756×960×8B ≈ 46 MB each, rebuilt 392 times — pure allocator churn).

**Fix:** cache the *aligned full-history panel* per `feature_key` (3 of them) beside the feature frames, and give `run()` window views by row-slicing `dates` and the arrays. Estimated wall-time reduction: several-fold on the optimize loop; exact-value tests already pin the numbers, so a refactor is verifiable.

---

### [PERF-002] Journal fully re-read and re-parsed on every query
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/execution/journal.py:89-103` (`read_events`), callers throughout `guardrails.py`/`executor.py`/`scan.py`

**What's wrong:** every `open_positions`, `placed_today`, and per-order duplicate check re-reads and re-JSON-parses the whole journal file. One `swing execute` over N picks does ~2N+3 full reads.

**Impact:** negligible today (personal scale, small file), grows linearly forever with an append-only file. Not worth infrastructure — worth a single read per command invocation passed down, if it ever shows up.

---

### [PERF-003] `drawdown_stats` Python loop runs 1000× inside the bootstrap
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/metrics.py:262-273`, called from `src/swing/backtest/bootstrap.py:180-186`

**What's wrong:** the underwater-streak counter is a pure-Python loop over every equity point; the bootstrap calls it per resample (1000 × ~3,400 points ≈ 3.4M iterations, plus a `pd.Series` construction per resample whose index is never used by the max-drawdown half).

**Impact:** seconds per report — visible but not painful. A vectorized `np.maximum.accumulate` drawdown (the streak length is only needed for the headline metrics, not the bootstrap) would make it disappear.

---

### [PERF-004] `ema()` is a Python loop (off the hot path)
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/indicators.py:61-88`

**What's wrong:** commit `068dffd` vectorized `wilder_smooth` via `ewm` (~20× per its comment) but `ema` kept the per-bar Python loop. Currently only `macd` (ablation-only) consumes it, so no production impact — noted so a future strategy that leans on `ema`/`macd` doesn't inherit a surprise. The same seed-then-`ewm` trick used in `wilder_smooth` applies directly.

---

### Memory & resource leaks

**This category is essentially clean.** File I/O uses context managers or atomic temp-file replaces throughout (`cache.py`, `picks.py`, `journal.py`); SMTP connections use `with`; matplotlib figures are explicitly closed (`report.py`); no listeners, timers, threads, or temp files left behind. One bounded observation:

### [LEAK-001] Walk-forward feature cache is unbounded (~1.3 GB by design)
Severity: Low | Confidence: Likely | Effort: S
Location: `src/swing/backtest/engine.py:150-155, 163-175`; shared across runs in `walkforward.py:239`

**What's wrong:** `feature_cache` holds full-history feature frames per `(symbol, feature_key)` for the life of the walk-forward: ~960 symbols × 3 feature keys × (~4,180 rows × 14 columns float64 ≈ 470 KB) ≈ **1.3 GB** steady-state, never evicted (arithmetic, not measured — hence Likely). Single process, released at exit, and it is exactly what makes the 27-combo grid affordable — but it is the number to know before anyone widens the grid to more feature-relevant parameters (each new `donchian_len`-class value adds ~450 MB) or grows the universe.

**Fix if it ever matters:** evict frames whose `feature_key` isn't in the current window's combo set, or store only the `FEATURE_COLUMNS` the panel actually consumes (9 of 14 columns are copied into the panel; the frame also duplicates the 5 OHLCV columns already held in `raw_bars`).

---

### Code quality & tech debt

### [DEBT-001] stooq fallback dead upstream (bot wall); docs claim a stable CSV
Severity: Medium | Confidence: Confirmed | Effort: M
Location: `src/swing/data/stooq_provider.py` (module docstring, `_fetch_csv`, `_parse_csv:127-152`)

**What's wrong:** live probes today (exact request shape the code sends; with a browser UA; without date params) all return HTTP 200 with an HTML JavaScript proof-of-work challenge — stooq now bot-walls `/q/d/l/`. `_parse_csv` handles it gracefully (HTML → no `Date` column → `None`), and `swing doctor` correctly reports "reachable but returned no bars". But the module's premise ("Stooq publishes a plain, stable CSV download… a good second source to fail over to") is no longer true, the rate-limit/no-data markers it sniffs for never match this response (it's logged at *debug* as an unparseable frame, not surfaced as "provider is bot-walled"), and the system currently has **no working fallback** for a yfinance outage.

**Fix:** (1) short term, teach `_parse_csv` to recognize an HTML challenge body and log it loudly once per run as "stooq is blocking automated clients", so the failure is named; update the docstrings and runbook to say the fallback is currently unavailable. (2) Structurally, pick a replacement fallback with sanctioned API access (e.g. Tiingo or Alpha Vantage free tiers, both keyed). Working around the bot wall itself is not recommended — it's explicitly what the operator is signaling against, and it would be fragile anyway.

---

### [DEBT-002] `account.equity` is in the gate hash; every balance edit re-locks the gate
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/config.py:97-102`; consumed by `src/swing/backtest/gate.py:118-127`

**What's wrong:** `Config.hash` covers all of `[account]`, including `equity`. But backtests size from `backtest.initial_equity` (`engine.py:311`), not `account.equity` — the walk-forward's out-of-sample results are byte-identical for any `equity` value. Meanwhile the config's own comment instructs "Update as the account grows", and doing so changes the hash and re-locks the gate until a full ~25-minute walk-forward re-run.

**Impact:** either the operator re-validates after every deposit (pure ritual — the report cannot change), or they learn to leave `equity` stale, which corrupts every live share count and eventually trips the executor's 20% `equity match` guardrail. The other `[account]` keys (`risk_pct`, `max_position_pct`, `max_concurrent_positions`) genuinely belong in the hash; `equity` alone does not.

**Fix:** exclude `account.equity` (and arguably `stale_equity_tolerance_pct`, `currency`) from the hashed subset. One-time migration cost: every existing hash changes — ship it with a release note, or grandfather by hashing both forms and accepting either.

---

### [DEBT-003] pandas floor `>=2.1` too low for `resample("ME")`
Severity: Low | Confidence: Confirmed | Effort: S
Location: `pyproject.toml:8` (`pandas>=2.1`), `src/swing/backtest/metrics.py:326`

**What's wrong:** the `"ME"` offset alias was introduced in pandas 2.2; on a 2.1 install `monthly_returns` raises `ValueError` and every report build fails. This venv has pandas 3.0.5, so it's latent, but the declared floor advertises support the code doesn't have. **Fix:** `pandas>=2.2`.

---

### [DEBT-004] Dead code: `today_orders_placed` never called
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/execution/guardrails.py:391-394`

Defined, exported, never referenced by source or tests. Notably it is the "count of orders placed today from one source" helper whose absence from the executor is half of BUG-004 — fixing that bug will either give it its one caller or justify deleting it.

---

### [DEBT-005] Market-order guardrail scans only top-level `orderType`
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/execution/guardrails.py:358-364`

The `no market orders` guard iterates `pick.orders` values and checks each dict's top-level `orderType`; a `MARKET` child inside `childOrderStrategies` would pass this check. Cover exists: `validate_order` (`orders.py:210-214`) recurses and hard-rejects `MARKET` anywhere, and the executor always runs it before transmitting — so the invariant holds end-to-end. But the guardrail's *report line* ("limit only") can assert something it didn't verify, and the drafted-orders schema explicitly warns it may change. **Fix:** recurse into `childOrderStrategies` in the guardrail scan — three lines, makes the printed evidence true.

---

### [DEBT-006] `Pick(**payload)` breaks on any sheet schema evolution
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/picks.py:186-188` (`from_json`)

`PickSheet.from_json` constructs `Pick(**p)` from stored JSON. Any field added to `Pick` in a future version makes *newer* sheets unreadable by older code, and any field ever *removed* makes existing sheets on disk raise `TypeError` in `swing confirm`/`execute` after an upgrade. The sheet-level fields already use tolerant `.get(...)` access; the picks don't. **Fix:** filter the payload to `Pick.__dataclass_fields__` before splatting.

---

### [DEBT-007] Test blind spots map exactly onto the bugs found
Severity: Medium | Confidence: Confirmed | Effort: M
Location: `tests/` (19 files, 469 tests — strong on strategy/indicators/orders/journal/gate; thin on integration seams)

The suite is excellent where it looks — indicators are pinned to worked examples, sizing/orders/journal validation are thorough — and every Critical/High finding above lives precisely where it doesn't:

1. **No scan test whose universe contains the regime symbol** (`test_scan.py` fixture universe is `S00–S05`; SPY exists only in the cache) → BUG-001 invisible.
2. **No `pipeline.update()` test crossing a vendor re-adjustment** (fresh bars that disagree with cached bars in the overlap) → BUG-002 invisible. `test_cache.py` covers `merge_bars` mechanics, not the adjustment semantics.
3. **No cross-run executor tests** — cap accounting with a pre-populated journal from an "earlier run", kill-switch engagement mid-loop → BUG-004/005 invisible (`test_execution.py` exercises single runs).
4. **No backfill-under-outage test** asserting that a mostly-failed fetch does *not* poison `absent.json` → BUG-006 invisible.
5. **No multi-pick confirm test with correlated gaps** asserting combined notional ≤ cash → BUG-009 invisible.

Each is one focused test, and each would have caught a confirmed bug. Priority order as listed.

---

### [DEBT-008] Gate has no freshness or data-fingerprint bound
Severity: Low | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/gate.py:100-127`

`check_gate` accepts the newest report whose `config_hash` matches — with no age limit and no comparison of the manifest's `data_hash` against the current cache. A two-year-old passing report opens the gate forever while the cache underneath it churns nightly (and, per BUG-002, drifts). Config changes re-lock the gate; time and data do not. This is a deliberate-looking design gap rather than a bug — but a `max_report_age_days` warning (or hard limit) in `[backtest.gate]`-adjacent, non-hashed config would close the "validated once in 2026, trading it in 2028" hole for one line of code. (Placement note: put the knob under `[reports]` or `[data]`, not `[backtest]` — the hash rule in CLAUDE.md.)

---

### [DEBT-009] Plaintext SMTP credential in config (documented, perms-checked)
Severity: Low | Confidence: Confirmed | Effort: —
Location: `src/swing/alerts/email.py` (module docstring), `config.toml` (`alerts.email.password`)

Recorded for completeness per audit rules, not as a discovery: the Gmail app password lives in plaintext in `config.toml`. The codebase already states this trade-off explicitly, `.gitignore` covers `config.toml`, and `swing doctor` verifies the file is chmod 600 (currently is). No secret values appear in this report. Accepted risk, correctly handled for a single-user system; flagged so it's on the record.

---

## 5. Remediation roadmap

**Phase 0 — before the gate ever passes (hours, all S-effort):**
1. **BUG-001** — two-line `is None` fix in `scan.py:131` + the missing test (DEBT-007 item 1). Without this, everything downstream of a passing gate is theater.
2. **BUG-006** — outage-rate guard on `mark_absent` + `--clear-absent` escape hatch; then clear BK/CTRA and the other 34 from this morning's `absent.json` and re-backfill them.
3. **DEBT-003** — bump the pandas floor to 2.2.

**Phase 1 — before trusting nightly data (the cache-integrity pair, M-effort):**
4. **BUG-002** — overlap-revision detection in `update()` with per-symbol full re-download on mismatch. This is the highest-value fix in the report: it protects every number the system produces from tomorrow onward.
5. **BUG-003** — remove bars from the Schwab provider's silent fallback (quotes/earnings/fundamentals may keep it). Depends on nothing; pairs naturally with 4 since both defend the same invariant.
6. **DEBT-001** — name the stooq bot-wall in logs/docs now; choose a replacement fallback provider as a separate decision.

**Phase 2 — before `swing execute --live` (execution safety, all S-effort):**
7. **BUG-004** — single order-count source (resurrect `today_orders_placed`, count accepted orders, thread prior-run count into `check_order`). Add the cross-run test.
8. **BUG-005** — kill-switch re-check per loop iteration and before `place_order`.
9. **BUG-009** — thread decremented cash through `run_confirm`.
10. **DEBT-005** — recurse the guardrail's market-order scan (keeps the printed evidence honest).

**Phase 3 — backtest fidelity (next re-validation cycle):**
11. **BUG-007** (prior-close marks for sizing) and **BUG-008** (drop or caveat the fundamentals filter in backtests) — both change backtest numbers slightly, so land them together and re-run the walk-forward once. **BUG-010** rides along.
12. **DEBT-002** — take `equity` out of the hash in the same re-validation window (the hash changes anyway once any of `[strategy]` moves, and you're re-running regardless).
13. **DEBT-008** — add a report-age warning.

**Phase 4 — when walk-forward iteration speed starts to hurt:**
14. **PERF-001** — panel caching per feature-key. Do it before any grid expansion; verify against the byte-reproducible reports (same config + cache must produce identical `trades.csv`).
15. PERF-002/003/004, LEAK-001, DEBT-004/006 — opportunistic.

Dependency notes: 1 unblocks meaningful use of everything else; 4+5 should land before accumulating months of nightly updates (the corruption is cumulative and a re-backfill resets the clock); 11+12 batch into one gate re-lock cycle by design.

---

## 6. Appendix

**Size and shape**

| Metric | Value |
|---|---|
| Source files / LOC | 42 files / 10,060 LOC (`src/swing/`) |
| Test files / LOC / results | 19 files / 5,867 LOC / 469 passed, 2 skipped (~15 s) |
| Largest files | engine.py 621 · scan.py 585 · report.py 577 · journal.py 488 · picks.py 426 |
| TODO/FIXME/HACK/XXX | **0** |
| Highest-churn source | commands.py (6) · cli.py (5) · scan.py, journal.py, earnings_calendar.py, runner.py (3 each) |
| Dependency floors | pandas>=2.1 (should be 2.2 — DEBT-003), numpy>=1.26, yfinance>=0.2.40; installed: pandas 3.0.5 |

**Live artifacts consulted (read-only)**
- `data/cache/absent.json` — 36 symbols dated 2026-08-19 incl. BK, CTRA (BUG-006 evidence)
- `data/cache/meta.json` — provider stamp `yfinance` (BUG-003 context)
- `reports/2026-08-19-walkforward/` — observed ~25-min wall time, metrics/manifest shape (PERF-001, DEBT-008)

**Dynamic checks run**
- `pd.DataFrame or None` → `ValueError` (BUG-001)
- Synthetic journal: 1 order → `placed_today()` = 2; 2 orders → 4 → preflight blocks at cap 3 (BUG-004)
- `curl` × 3 request shapes against `stooq.com/q/d/l/` → HTTP 200 JS proof-of-work challenge each time (DEBT-001)

**Primary search/inspection methods**: full-file reads of all 42 source files; `grep -rn` for cross-references (`mark_absent`, `EVENT_PLACED`, `today_orders_placed`, `run_scan`, `resample("ME")`, orderType scans); `git log --format= --name-only | sort | uniq -c` for churn; fixture inspection in `tests/test_scan.py` to explain test blindness.

**Categories affirmatively clean** (read in full, nothing to report): `indicators.py` numerical conventions (Wilder seeding, warm-up NaNs, no lookahead in rolling windows — matches its pinned tests); `orders.py` validation (recursive, quantity-matched, GTC-enforced children); `journal.py` CLI input validation; `bootstrap.py` (deterministic, honestly caveated); `earnings_calendar.py` (malformed-row and majority-garbage guards); `auth.py` token lifecycle; `alerts/` fan-out isolation; `config.py` layered loading and validation; atomic file writes throughout the cache and report layers.
