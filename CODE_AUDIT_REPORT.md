# Code Audit Report — swing

**Repo:** `swingtrader2` at commit `eb403f4` (branch `claude/swing-trading-picker-mqo51q`)
**Date:** 2026-08-19 · **Method:** five parallel module auditors + lead cross-file deep dives; every Critical/High finding re-verified line-by-line by the lead; ~20 findings carry executed probe reproductions.

---

## 1. Executive summary

**Overall health: a genuinely disciplined codebase with a specific, consistent blind spot.** The numeric core (indicators, sizing, engine arithmetic) is reference-tested and clean — no lookahead, no cash drift, no determinism gaps, zero TODO/FIXME markers across 13k LOC, and docstrings that explain *why*. What the build's package-scoped QA could not see is exactly what this audit found: **cross-process concurrency, cross-file contract drift, and edge-of-domain data** (NaN bars, delistings, dead-flat prices, hostile-but-plausible JSON). Headline counts after dedup: **5 Critical / 12 High / 26 Medium / 24 Low** (67 findings from ~95 raw, aggressively merged).

**Important context:** the strategy currently fails its own backtest gate, so *no live trading is possible today* — every Critical here is about what happens **when** live use begins or **as** data drifts, not about money being lost right now. That is lucky, not safe.

The five most important findings:

1. **[BUG-001] The journal has no cross-process locking, and the nightly scan holds a stale copy for minutes.** Reproduced: an order recorded by `swing execute` mid-scan was silently erased when the scan saved. The journal feeds the duplicate-order guardrail, so a lost order record can let the system re-send an order already working at Schwab.
2. **[BUG-006/007] Live execution is broken against its own designed workflow — it would refuse everything, permanently.** The `duplicate` guardrail refuses every scan-tonight/execute-tomorrow pick (tests only ever dated picks "today"), and `reconciliation` can never pass on a funded account (nothing ever sets a pick to `"filled"`, and Schwab cash-sweep rows like `MMDA1` aren't filtered). Fails safe, but `--live` is unusable, and the first live attempt would be a confusing wall of refusals.
3. **[BUG-003] The momentum ranking inverts for stocks that stop moving.** ATR% is an unguarded divisor; Wilder ATR decays geometrically on a pinned price, so a halted/merger-pinned stock climbs to **rank #1** (measured score 294.7 vs 5.41 for a healthy trend) and consumes a real position slot with dead capital. Silent under default config.
4. **[BUG-002] A post-placement failure tells the operator the opposite of the truth.** If Schwab accepts the order but the response lacks a `Location` header and has a non-JSON body — or the journal write fails — the CLI prints "*so it was not sent*" and exits, with a live, unrecorded order at the broker.
5. **[BUG-004/013/014] The bar cache can serve wrong data while reporting "fully cached, zero network."** Three reproduced mechanisms: parquet/meta sidecar desync under concurrent writers; a full refetch that deletes cached bars newer than the requested end (49 live-tail bars lost in the probe); and sub-0.1% dividend re-adjustments splicing two price bases permanently — the exact discontinuity the overlap probe exists to prevent, just under its detection floor.

---

## 2. Scope & method

- **Audited exhaustively (every line):** `src/swing/**` (30 files, 13,116 LOC incl. `scripts/ablations.py`) via five parallel auditors — broker; backtest; data+universe; alerts+scheduler+state+cli; strategy+indicators+config — each applying the correctness/performance/leak/debt catalogs.
- **Lead verification:** all 5 Critical and all 12 High findings were re-verified by the lead against the cited lines (two batched sweeps, part 1/part 2, excerpts matched verbatim). ~20 findings additionally carry probe reproductions executed against the real modules in a scratchpad (lost-update, cache truncation, ranking inversion, NaN P&L corruption, gate truthiness, etc.).
- **Sampled only:** `tests/**` (13,636 LOC) — consulted to confirm/refute findings, not audited for quality. `docs/**` — checked only where a finding claims doc/code contradiction.
- **Skipped:** `.venv`, `uv.lock`, cached parquet data, report artifacts.
- **Blind spots (honest):** no runtime profiling of live yfinance/Schwab network behavior (Schwab endpoints never exercised — no credentials exist); no live launchd soak test; test-suite quality itself not audited; findings marked *Latent* are unreachable with today's data/config but confirmed reachable by code path.
- **Known accepted designs** (ruled during the build, not re-reported): SystemExit refusal codes; fail-closed gate; dry-run-by-default; TR bar-0 NaN; hold_days=41 recording; compounded-return stitching; `ablate-*` label guard; capped_by precedence; calendar-day blackout windows; reference-capital backtesting.

Severity: **Critical** = money/data corruption or operator deception · **High** = malfunction in normal designed use · **Medium** = edge-case wrongness or meaningful waste · **Low** = latent hazard or notable debt. Effort: S < 1 h · M < 1 day · L multi-day.

---

## 3. Findings index

| ID | Title | Sev | Conf | Effort |
|---|---|---|---|---|
| BUG-001 | Journal lost-update race; no cross-process locking anywhere | Critical | Confirmed (repro) | M |
| BUG-002 | Post-placement failure reported as "not sent"; placement/journal not atomic | Critical | Confirmed | M |
| BUG-003 | ATR%→0 inverts momentum ranking; scanner/engine disagree on non-finite | Critical | Confirmed (repro) | S |
| BUG-004 | Cache parquet/meta pair desync serves truncated data as fully-cached | Critical | Confirmed (repro) | M |
| BUG-005 | Engine: unvalidated NaN OHLC corrupts P&L/equity/drawdown silently | Critical | Confirmed (repro; latent) | S |
| BUG-006 | `duplicate` guardrail refuses every overnight pick (the designed flow) | High | Confirmed | S |
| BUG-007 | `reconciliation` can never pass: `"filled"` never set; broker rows unfiltered | High | Confirmed | M |
| BUG-008 | Kill switch / trading hours not rechecked across interactive prompts | High | Confirmed | S |
| BUG-009 | Transmitted payload never validated against the guardrail-approved plan | High | Confirmed | M |
| BUG-010 | Same-day `swing scan` rerun wipes the night's report via self-dedupe | High | Confirmed (repro) | S |
| BUG-011 | Watch-list entries block their symbols for 7 days | High | Confirmed (repro) | S |
| BUG-012 | Weekend/holiday scans shadow Friday's picks; Monday confirm checks nothing | High | Confirmed (repro) | M |
| BUG-013 | Full refetch truncates cached bars newer than the requested end | High | Confirmed (repro) | S |
| BUG-014 | Overlap guard fails open: sub-tolerance re-adjustments splice bases; empty intersection = "no conflict" | High | Confirmed (repro) | M |
| BUG-015 | Batch symbol loss is silent; missing regime symbol reported as "regime OFF" | High | Confirmed (repro) | S |
| BUG-016 | A symbol whose bars end mid-run freezes its slot and capital for the fold | High | Confirmed (repro; latent) | M |
| BUG-017 | Gate trusts untrusted JSON: `bool("false")`=True opens it; `Infinity` crashes `swing report`; ablate labels unchecked | High | Confirmed (repro) | S |
| BUG-018 | Confirm verdicts never reach `picks.json`; executor and reruns trust the stale file | Medium | Confirmed (repro) | S |
| BUG-019 | Morning confirm consumes `--dry-run` scan reports and notifies real picks | Medium | Confirmed (repro) | S |
| BUG-020 | Report files written non-atomically; readers can see truncated `picks.json` | Medium | Likely | S |
| BUG-021 | `scan`/`confirm` exit 0 when every channel failed; `ScanError` leaks tracebacks | Medium | Confirmed (repro) | S |
| BUG-022 | `paths.reports_dir` is CWD-relative; journal is absolute — split anchoring | Medium | Confirmed (repro) | S |
| BUG-023 | `schedule install` prints success when `launchctl` failed (EIO conflation) | Medium | Confirmed (repro) | S |
| BUG-024 | Corrupt-journal recovery silently resets positions; warning invisible under launchd | Medium | Likely | M |
| BUG-025 | No intra-run duplicate-symbol suppression in `plan_orders` | Medium | Confirmed | S |
| BUG-026 | Scan freshness judged by editable file body, not the selected directory | Medium | Confirmed | S |
| BUG-027 | `quote_price` can return previous close or a batch-payload mismatch as "current" | Medium | Likely | S |
| BUG-028 | Token age understated: future timestamps clamp to 0; mtime tracks refresh, not creation | Medium | Confirmed | S |
| BUG-029 | `breakout_proximity_pct=100` validates and makes the breakout test always true | Medium | Confirmed (repro) | S |
| BUG-030 | `size_position` unguarded in scanner; `min_price` floor permits entry=0.0 crash | Medium | Confirmed (repro) | S |
| BUG-031 | Interior NaN bar permanently shifts ATR/ADX with no NaN in the output | Medium | Confirmed (repro) | M |
| BUG-032 | Config values that validate cleanly but silently disable the whole strategy | Medium | Confirmed (repro) | M |
| BUG-033 | Quoted numbers in TOML raise raw `TypeError`, not the promised `ConfigError` | Medium | Confirmed (repro) | S |
| BUG-034 | Schwab request bounds are naive datetimes — encoded in machine TZ, decoded in ET | Medium | Confirmed | S |
| BUG-035 | Empty/all-NaN tail response recorded as "covered through end" — retries suppressed all day | Medium | Confirmed | S |
| BUG-036 | No as-of earnings data: the backtest's earnings blackout is a no-op on history | Medium | Confirmed | L |
| BUG-037 | Fundamentals silently mix quarterly-YoY and annual-YoY; non-adjacent periods compared | Medium | Confirmed | M |
| BUG-038 | Schwab `time`/`timestamp` keys decoded as ms without magnitude check (1970 bars) | Medium | Confirmed (repro; latent) | S |
| BUG-039 | Yahoo-form symbols sent verbatim to Schwab; dual-class names would vanish | Medium | Needs investigation | M |
| BUG-040 | `by_year` drawdown excludes each year's first bar from its own drawdown | Medium | Confirmed (repro) | S |
| BUG-041 | Walk-forward tuner maximizes the 9999.0 PF sentinel, ignoring `profit_factor_capped` | Medium | Confirmed | S |
| BUG-042 | `--label` with a path separator relocates `latest.json` and defeats the ablate guard | Medium | Confirmed (repro) | S |
| BUG-043 | Report headline shows the data span, not the OOS span the numbers cover | Medium | Confirmed | S |
| BUG-044 | `quote_drift` uses raw `float()` on pick fields — traceback instead of refusal | Low | Confirmed | S |
| BUG-045 | `earnings_blackout` raises on tz-aware index; scanner catches it and **fails open** | Low | Confirmed (latent) | S |
| BUG-046 | `draft_orders` coercions raise bare `TypeError` outside its documented contract | Low | Confirmed | S |
| BUG-047 | `_ascii_header` keeps embedded newlines; 2dp prices wrong for sub-$1 (unreachable today) | Low | Confirmed (latent) | S |
| BUG-048 | `_flatten_columns` heuristic fooled by a ticker named `OPEN` (public-API path only) | Low | Confirmed (repro; latent) | S |
| BUG-049 | Same-trading-date Schwab candles deduped (keep-last) instead of aggregated | Low | Confirmed (repro; latent) | S |
| BUG-050 | Lost meta sidecar narrows recorded coverage → full refetch instead of warm serve | Low | Confirmed | S |
| BUG-051 | Negative earnings answers cached 3 days against a 10-day blackout window | Low | Confirmed | S |
| BUG-052 | `max_drawdown` duration contradicts its docstring (stops at last underwater bar) | Low | Confirmed (repro) | S |
| BUG-053 | Zero-share picks past the `max_positions` cut are not replaced (contradicts engine contract) | Low | Confirmed | S |
| BUG-054 | No risk-per-share floor: near-flat ATR sizes to the notional cap with fictional risk | Low | Likely | S |
| BUG-055 | Crash between journaling and notify suppresses that alert for the 7-day dedupe window | Low | Confirmed | S |
| PERF-001 | Engine rebuilds numpy arrays on all 1,053 calls — 63–71% of a warm walk-forward (~30 min) | High | Confirmed (measured) | M |
| PERF-002 | `latest_quotes` (yfinance) is one serial Ticker round-trip per symbol | High | Confirmed | M |
| PERF-003 | Cold earnings/fundamentals: thousands of serial calls, nothing persisted until all complete | High | Confirmed | M |
| PERF-004 | Per-day `Timestamp` extraction (30× slower) and per-combo recomputation of fold invariants | Medium | Confirmed (measured) | S |
| PERF-005 | `SignalCache` unbounded — ≈520 MB per fold at 1,545 symbols, no eviction/diagnostic | Medium | Confirmed (measured) | M |
| PERF-006 | Every warm read re-normalizes parquet (~½ GB churn/scan); provider rebuilt at 5 call sites | Medium | Confirmed | M |
| PERF-007 | Schwab price history strictly serial (~1,500 requests cold) | Medium | Confirmed | M |
| PERF-008 | Scan-loop waste: `inspect.signature` per candidate; O(candidates×journal) dedupe | Low | Confirmed (measured) | S |
| PERF-009 | ATR computed twice per ranked symbol (scoring) and twice per sized pick (pipeline) | Low | Confirmed | S |
| PERF-010 | `trend_template` always pays ADX (80% of its cost) even when `adx_min=0` disables it | Low | Confirmed (measured) | S |
| PERF-011 | Universe CSVs re-parsed per call; `etfs.csv` parsed twice per `load()` | Low | Confirmed | S |
| LEAK-001 | Journal grows without bound; each confirm rewrites the whole file N times | Medium | Confirmed | M |
| LEAK-002 | Report dirs accumulate forever; orphaned `.tmp` files (cache + state) never swept | Low | Confirmed | S |
| LEAK-003 | TTL JSON caches never pruned; whole-file rewrite grows monotonically | Low | Confirmed | S |
| LEAK-004 | schwab-py httpx clients (connection pools) constructed and never closed | Low | Confirmed | S |
| LEAK-005 | Two SMTP connect/auth/teardown cycles per delivery when email+SMS both configured | Low | Confirmed | S |
| LEAK-006 | Matplotlib figure + BytesIO leak on `savefig` failure (not in `finally`) | Low | Confirmed (repro) | S |
| DEBT-001 | Two divergent `latest_scan_dir` implementations disagree on real states | Medium | Confirmed | S |
| DEBT-002 | The 210-line order validator is never called on the operational path | Medium | Confirmed | S |
| DEBT-003 | Executor docstring promises "touches no network" — dry run always calls the data provider | Medium | Confirmed | S |
| DEBT-004 | Money-path failures logged at INFO; provider errors never reach refusal text | Medium | Confirmed | S |
| DEBT-005 | `ablations.py` prints a factually false warning that the sweep poisoned the gate | Medium | Confirmed | S |
| DEBT-006 | Confirm drift threshold duplicated: module constant vs config knob | Low | Confirmed | S |
| DEBT-007 | Orders/day budget and token-age ladder each implemented twice | Low | Confirmed | M |
| DEBT-008 | Naive-datetime semantics contradict between auth (local) and guardrails (ET) | Low | Confirmed | S |
| DEBT-009 | Ablation `change` strings hardcode baseline values the runner doesn't use | Low | Confirmed | S |
| DEBT-010 | Spec/code contradiction on score ATR window; `MIN_HISTORY_ROWS` undocumented in spec | Low | Confirmed | S |
| DEBT-011 | `obv`'s deliberate non-use documented in research doc, not in its docstring | Low | Confirmed | S |
| DEBT-012 | Dead first disjunct in the breakout test (proximity band subsumes it) | Low | Confirmed | S |
| DEBT-013 | Retry helper duplicated across providers; zero network-tuning config surface | Low | Confirmed | S |
| DEBT-014 | Small data-layer inconsistencies (Traversable stringify, BOM, `__all__`, dead branches, `warnings` vs `log`) | Low | Confirmed | S |
| DEBT-015 | Dead `index_of_symbol` parameter; misdirecting `_finite(stop)` guards beside the real gap | Low | Confirmed | S |
| DEBT-016 | HTML equity chart titled "Out-of-sample" even on in-sample runs | Low | Confirmed | S |
| DEBT-017 | Dead `inspect.signature` compat shim; 7 unused view-model keys in render | Low | Confirmed | S |

---

## 4. Detailed findings

### 4.1 Correctness bugs — Critical

### [BUG-001] Journal lost-update race; no cross-process locking anywhere
Severity: Critical | Confidence: Confirmed (reproduced) | Effort: M
Location: `src/swing/state.py:176-188,258-265` (atomic-for-readers save), `src/swing/alerts/pipeline.py:667→712` (minutes-wide window), `src/swing/broker/executor.py:761,778,916` (concurrent writers); same pattern: `src/swing/data/cache.py:470-493` (TTL JSON read-all→write-all)

**What's wrong:** `Journal.save()` serializes the whole in-memory list and `os.replace`s it — atomic for readers, but last-writer-wins between writers; no `flock`/`fcntl` exists anywhere in `src/` (grep-verified). `run_scan` loads the journal at `pipeline.py:667`, then spends **minutes** in network fetch before `add_picks` at `:712` persists the stale snapshot. Three processes overlap by design: the 15:30 launchd scan, the 07:00 confirm, and any user-run `swing execute`/`kill`/`confirm`. Reproduced: a `record_order` fired mid-scan → `LOST UPDATE: True`, order gone from disk. The TTL JSON caches have the identical read-all→mutate→write-all shape. Bonus hazard: two processes hitting a corrupt journal race `_backup_corrupt` (`state.py:191-199`) and the loser dies on uncaught `FileNotFoundError`.
**Impact:** A lost `record_order` blinds the `duplicate` guardrail (`guardrails.py:494`) → the next `swing execute --live` re-sends an order already working at Schwab. A lost `update_status("filled")` frees a slot and inflates cash in the next scan. This is the highest-leverage fix in the report because four other findings (LEAK-001, BUG-020, LEAK-003, BUG-024's TOCTOU) shrink or vanish behind the same lock.
**Fix:** Advisory `fcntl.flock(LOCK_EX)` on a `journal.lock` sidecar held around load→mutate→save; have each mutator re-read and merge under the lock rather than overwriting from a stale snapshot; move `Journal.load` in `run_scan` to just before `add_picks`. Give `_backup_corrupt` a pid/timestamp suffix and tolerate `FileNotFoundError`.

### [BUG-002] Post-placement failure reported as "not sent"; placement/journal not atomic
Severity: Critical | Confidence: Confirmed | Effort: M
Location: `src/swing/broker/executor.py:749-784`

**What's wrong:** Two windows. (A) `_order_id_of(response)` sits inside the same `try` as `place_order`; it calls `response.json()` when the `Location` header is absent (`:458-475`), so a 201-with-non-JSON-body raises **after the order is live**, and the handler prints "*so it was not sent*" + `SystemExit(1)`, journaling nothing. (B) `journal.record_order` runs after placement; an `OSError` from the journal write (full disk, permissions) or Ctrl-C kills the process with a live, unrecorded order.
**Impact:** A real order exists at Schwab that the local safety state has never seen; `duplicate` and `orders_today` are blind to it; and in window A the operator is told the opposite of the truth. Reconciliation would not catch it either — it compares positions, not open orders (see BUG-007).
**Fix:** Narrow the `try` to `place_order` alone; default `order_id=None` on extraction failure and *say the order may be live*. Journal a `status:"pending"` row **before** the network call and flip it after; wrap post-placement journal writes so failures print a loud, order-id-bearing message.

### [BUG-003] ATR%→0 inverts the momentum ranking; scanner and engine disagree on non-finite scores
Severity: Critical | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/strategy/scoring.py:72-79` (unguarded divisor), `:127-131` (`pd.isna` filter passes `inf`); divergence vs `src/swing/backtest/engine.py:663-666`

**What's wrong:** `score = w·(return / atr_pct)` with no floor on `atr_pct`, unlike every other division in the numeric layer. Wilder ATR decays ×13/14 per flat bar, so a pinned price sends ATR%→0 while the trailing 126-day return stays large: the score is monotonically increasing in how long the name has been dead. Measured on an acquisition-pinned fixture: score 10.8 → **294.7** over 38 flat sessions vs 5.41 for a healthy trend, passing template+entry+liquidity on 24 of those days, ranked #1. Secondary: `pd.isna(np.inf)` is False, so an infinite score survives the filter and sorts **first** in the scanner, while the engine's `_rank_key` sorts non-finite **last** — the shared-rules contract's one job, violated.
**Impact:** Under default config, pick lists silently favor halted stocks, pinned merger targets, and stale vendor series — consuming real slots with dead capital. Affects both live scans and every backtest's candidate selection.
**Fix:** Floor the divisor via a named `MIN_ATR_PCT` (≈0.05) → NaN → excluded (matches documented warm-up policy); change the filter to `np.isfinite`. One test for the large-return/near-zero-ATR quadrant.

### [BUG-004] Cache parquet/meta pair desync serves truncated data as "fully cached"
Severity: Critical | Confidence: Confirmed (reproduced) | Effort: M
Location: `src/swing/data/cache.py:218-228` (two independent atomic writes), `:276-280` (read trusts sidecar; `rows`/`last_bar` never checked)

**What's wrong:** Parquet and its JSON sidecar are each written atomically, but the *pair* is not, and no lock exists. Interleave two processes on one symbol (scan + manual backtest — documented usage) and the surviving meta can describe the other writer's frame. `get_bars` then trusts `covered_start/covered_end` with zero cross-check against the actual frame. Reproduced: **0 network calls, 150 rows served ending 2020-07-29** when the true range was 200 rows ending 2020-10-07.
**Impact:** SMA-200/ATR/momentum silently computed on a series that stops months early; for most symbols the asof row is missing so they just vanish from the scan; for SPY the failure is *misreported as a market condition* (BUG-015). The one data-layer failure that produces wrong numbers with no error and no self-healing.
**Fix:** Three-line self-healing first: on read, discard the sidecar unless `meta.rows == len(frame)` and `meta.last_bar == frame.index[-1].date()`, falling back to `_meta_from_frame`. Then the BUG-001 lock over `get_bars`. Longer-term: store coverage inside the parquet metadata so there is one atomic artifact.

### [BUG-005] Engine: unvalidated NaN OHLC silently corrupts P&L, equity, drawdown; loses positions
Severity: Critical | Confidence: Confirmed (reproduced) — **latent** (0 partial-NaN rows in today's 5.65M cached bars) | Effort: S
Location: `src/swing/backtest/engine.py:722-733` (exit ladder), `:826` (close mark), `:888-889` (snapshot filter); boundary gap: `src/swing/data/provider.py:285` (`dropna(how="all")` admits partial-NaN rows)

**What's wrong:** The entry path validates its fill price; the exit ladder and daily mark do not. `NaN <= x` is False, so: a NaN open on a time-stop bar → `exit_price=NaN` → `pnl=NaN`, cash=NaN, **59/141 equity rows NaN** in the probe, and the poisoned trade *vanishes from profit factor* (NaN fails both `>0` and `<0`) while still counting in `trades` and the win-rate denominator. A NaN close on the final bar → position filtered at `:888` → **$26,436 of a $100k account silently disappeared**, booked nowhere. One NaN close mid-hold fabricates a one-day equity crater: measured max drawdown **25.67% vs 0.029%** on identical clean data — two such bars flip the 35% gate. A NaN open with a valid low below the stop skips the gap-through branch and fills *at the stop* — a silent optimistic bias.
**Impact:** Gate-feeding metrics corrupted with no exception, warning, or artifact trace, the first night the vendor ships a partial row.
**Fix:** Validate at the boundary: in `_build_plan`, treat any row with non-finite OHLC as "no bar" (`row_of_day=-1` — already a handled state) with one `log.warning`; additionally never overwrite `last_close` with a non-finite value at `:826`. Or tighten Contract 3 so `normalize_bars` drops partial-NaN rows. Add a no-NaN-reaches-artifacts engine test.

### 4.1 Correctness bugs — High

### [BUG-006] `duplicate` refuses every overnight pick — the designed workflow
Severity: High | Confidence: Confirmed | Effort: S
Location: `src/swing/broker/guardrails.py:511`; pick dated at `src/swing/alerts/pipeline.py:467`; boundary at `src/swing/state.py:309`

**What's wrong:** `recently_picked(sym, within_days, asof=asof - timedelta(days=1))` intends to exempt "today's own pick" — but picks are stamped with the **scan** date (17:30, after close), so at execution time they are always dated the *previous* day: `delta=0` → refused. Tests never caught it because every executor fixture dates picks `TODAY`. `stale_scan` explicitly blesses the Friday-scan→Monday-execute case that `duplicate` then refuses.
**Impact:** With any populated journal, `swing execute --live` refuses 100% of orders in the scan-tonight/execute-tomorrow flow. Fails safe; makes live mode unusable.
**Fix:** Exempt by identity, not date arithmetic: skip journal picks whose `date == scan_date` of the bundle under execution. Add a test with the journal pick dated `TODAY-1` and scan dir `scan-<TODAY-1>`.

### [BUG-007] `reconciliation` can never pass on a funded account
Severity: High | Confidence: Confirmed | Effort: M
Location: `src/swing/broker/guardrails.py:530-572,719-724`; `src/swing/broker/executor.py:370-395`; `src/swing/state.py:406-426`

**What's wrong:** (a) `Journal.positions()` returns picks with `status=="filled"` — and grep-verified, **nothing in the codebase ever sets "filled"** (writers stop at `"ordered"`/`"confirmed"`/`"invalidated"`), so the journal side is structurally empty. (b) `_extract_positions` keeps every broker row — no `assetType` filter, no `quantity==0` drop — so cash sweeps (`MMDA1`, `SWVXX`) and pre-existing holdings always appear.
**Impact:** Non-empty broker set vs empty journal set → permanent refusal of every live run on any real account; the `acknowledged` escape is unreachable from the CLI. Also makes `duplicate`'s already-held branch dead code and `swing positions` always print "(none)". The missing half of the order state machine (`open→filled`) is the root cause — it also underlies BUG-002's blindness.
**Fix:** Add fill-tracking: poll order status via the client and move picks/orders to `filled`/`closed`. Filter broker rows to `assetType in {"EQUITY","ETF"}`, drop zero quantities. Until fills exist, reconcile against journal *orders* too, not positions alone.

### [BUG-008] Kill switch / trading hours evaluated once, then never rechecked across interactive prompts
Severity: High | Confidence: Confirmed | Effort: S
Location: `src/swing/broker/executor.py:633-642,731-750`

**What's wrong:** Guardrails run once before the placement loop; per-order confirmation then blocks on `input()` indefinitely. Nothing re-reads the KILL file or the clock between prompt and `place_order` — while the docstring promises the kill switch "is checked before anything else happens."
**Impact:** The panicking-human scenario the kill switch exists for — `swing kill` from a second terminal mid-run — does not stop queued orders. A run started 15:55 can place after the close.
**Fix:** Re-evaluate `kill_switch(cfg)` and `trading_hours(now)` at the top of each loop iteration, immediately before placement; break with a message.

### [BUG-009] The transmitted payload is never validated against the plan the guardrails approved
Severity: High | Confidence: Confirmed | Effort: M
Location: `src/swing/broker/executor.py:276-297,750`

**What's wrong:** `plan.order` is the raw dict from `orders/<SYMBOL>.json`, transmitted verbatim; only `limit_only` inspects it. Symbol, instruction (BUY vs SELL_SHORT), quantity, price are never cross-checked against the pick. Worse, `or`-chained fallbacks (`leg quantity or pick shares or 0`) mean the table/prompt/journal can describe a trade the wire payload does not encode (`quantity: 0` payload displays as the pick's 50 shares).
**Impact:** Any drift between file and pick — stale dir, partial write, hand edit — sends a trade nobody checked, and the eleven other guardrails validated a *model* of the order, not the order.
**Fix:** Assert payload↔plan equivalence after `_select_variant` (symbol, BUY, exact quantity, exact price string); mismatch → `problems`, never a fallback.

### [BUG-010] Same-day scan rerun wipes the night's report
Severity: High | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/state.py:309` (`0 <= delta`), `src/swing/alerts/pipeline.py:556-560,632,698-705`

**What's wrong:** Run 1 journals picks; run 2 (same evening — the most natural user action: re-run to see output or after a notification hiccup) dedupes every one of them (`delta==0`) and **overwrites the same `scan-YYYY-MM-DD/` dir with an empty report**, leaving run 1's `orders/*.json` orphaned beside a "Nothing is tradable tonight" sheet. Reproduced end-to-end. The existing byte-identity test only passes because it uses `dry_run=True`.
**Impact:** The night's tradable output is destroyed; executor and 07:00 confirm find nothing.
**Fix:** Exclude the current asof from dedupe (`0 < delta`) or filter `pick.date == asof`; clear `orders/` before drafting so the dir can never disagree with `picks.json`.

### [BUG-011] Watch-list entries block their symbols for 7 days
Severity: High | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/alerts/pipeline.py:712`; `src/swing/state.py:303` (no `kind` filter)

**What's wrong:** `add_picks([*picks, *watch])` journals watch entries, and `recently_picked` ignores `kind` — so printing a symbol on the watch list suppresses it (from picks *and* the watch list itself) for a week. On the configured $100 account **every** qualifying name is a watch entry — the live `scan-2026-08-19` report shows exactly this (0 picks / 4 watch).
**Impact:** Within a week the watch list — the feature that makes a $100 account useful — degrades to empty, and if capital arrives on day 2, the day-1 qualifiers are precisely the names that cannot surface. Dominant failure mode for the current user.
**Fix:** Dedupe should mean "we committed capital": pass only `picks` to `add_picks` (record watch separately) or filter `kinds=("pick",)` in the dedupe path.

### [BUG-012] Weekend/holiday scans shadow Friday's picks; Monday's confirm checks nothing
Severity: High | Confidence: Confirmed (reproduced) | Effort: M
Location: `src/swing/scheduler/launchd.py:206` (no `Weekday` key); `src/swing/alerts/pipeline.py:235-247` (latest dir = lexicographic name, only requires `picks.json` to exist)

**What's wrong:** Both agents fire 7 days/week. A Saturday run re-derives Friday's candidates, dedupes them all away (BUG-010/011), and writes an empty `scan-<Saturday>/` — which becomes `latest_scan_dir`. Monday 07:00 confirm re-quotes an empty pick list. Reproduced.
**Impact:** Friday's picks are never re-checked the one morning they'd be traded; "0 confirmed, 0 invalidated" notifications look normal. Same for market holidays.
**Fix:** Both guards: `"Weekday": [1,2,3,4,5]` in the plist, **and** make `latest_scan_dir` skip empty-pick reports (or refuse to write a scan dir when asof has no new trading bar) — the second covers holidays and manual runs.

### [BUG-013] Full refetch truncates cached bars newer than the requested end
Severity: High | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/data/cache.py:281-283,300,310-321`

**What's wrong:** Both `full_from` paths refetch `[from_date, end]` and **replace** the file. The start side is widened (`min(start, covered_start)`); the end side is not — `covered_end` isn't even tracked. Reproduced: warm cache through 2020-10-07; a backtest with an earlier `--end` left the parquet ending 2020-07-30 — **49 live-tail bars deleted**. Reachable via `swing backtest --end <past>` or an uncommented `backtest.end`, for all ~1,500 symbols.
**Impact:** The nightly scan then runs on a short series until the next successful tail fetch heals it; a vendor outage in that window makes the loss stick.
**Fix:** Track `covered_end` and fetch/record the union: `end' = max(end, covered_end)`; also widen the missing-head branch's start.

### [BUG-014] The overlap guard fails open two ways: sub-tolerance splices and empty intersections
Severity: High | Confidence: Confirmed (reproduced) | Effort: M
Location: `src/swing/data/cache.py:68-70,338-359` (threshold + merge), `:342-344` (empty intersection → "no conflict")

**What's wrong:** (a) Re-adjustments under the 0.1% tolerance are *merged*, not reconciled — reproduced with a 0.05% dividend: old-basis close 87.317 sitting beside new-basis 83.742 (true old-basis 83.784), the exact "step discontinuity" the module docstring exists to prevent; low-yield names accumulate ~0.2%/yr of splice error that only a >0.1% single event ever resets. (b) A tail fetch that shares **zero** dates with the cache returns "no conflict" and concatenates — but tail fetches start *at a cached bar* by construction, so an empty intersection means the vendor ignored the request (exactly when its basis is most suspect; BUG-034 produces this).
**Impact:** Silently basis-mixed series biasing long-window returns and total-return metrics the gate reads.
**Fix:** Test the *uniformity* of the overlap ratio (a real re-adjustment scales all bars equally: `relative.std() small ∧ mean > ~1e-6` ⇒ refetch), lower the tolerance, and add a staleness-forced refetch (e.g. >90 days). Treat non-empty-fresh/empty-intersection on a tail fetch as a conflict.

### [BUG-015] Silent batch symbol loss; a missing regime symbol is reported as "regime OFF"
Severity: High | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/data/yf_provider.py:352-353` (flat-frame multi-symbol → `{}` with no warning); `src/swing/alerts/pipeline.py:306-316` (`spy_bars empty → regime_ok=False`)

**What's wrong:** When Yahoo flattens the header (all-but-one ticker in a batch failed), a 200-symbol batch evaporates with only a DEBUG line. And the scan translates an *absent* SPY into the user-facing note "*The market regime gate is OFF (SPY is not above its 200-day average)*" — a data failure presented as a confident market statement. The backtest runner warns loudly in the same situation; the scan does not.
**Impact:** A transient data failure can silently no-op the scan indefinitely while looking exactly like a bear-market signal.
**Fix:** `log.warning` in the flat-multi-symbol branch; in the pipeline, distinguish "regime symbol missing" (its own note + notification) from "regime off."

### [BUG-016] A symbol whose bars end mid-run freezes its slot and capital for the rest of the fold
Severity: High | Confidence: Confirmed (reproduced) — latent (no truncated histories in today's cache) | Effort: M
Location: `src/swing/backtest/engine.py:705-709,818-827,885-901`

**What's wrong:** `row_of_day == -1` (correctly) carries a position through a one-day halt — but it is `-1` for *every* remaining day once a symbol's history ends, and no "data ran out" exit exists; only the global end-of-run close-out. Reproduced: a position whose symbol stopped printing held the book's only slot for **4.5 months**; the trade list says `hold_days=3` while the equity curve says the capital was locked all along; a later signal never traded.
**Impact:** With survivorship-free data (delistings/acquisitions mid-period — the exact upgrade path this system wants), one dead symbol deletes 25% of book capacity per fold, and the trade list disagrees with the equity curve.
**Fix:** Precompute each plan's last valid day; when `di` passes it, close at `last_close` with `EXIT_END_OF_DATA`, free the slot, log a warning.

### [BUG-017] The gate trusts untrusted JSON: truthiness opens it, `Infinity` crashes it, ablate labels pass it
Severity: High | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/backtest/gate.py:118` (`bool(summary.get("walkforward"))`), `:160-174` (`_as_float` NaN-only; `_as_int` uncaught `OverflowError`); no label check

**What's wrong:** `bool("false") is True` — a hand-edited/script-generated `latest.json` with `"walkforward": "false"` opens the gate on an in-sample fit (verified: `passed=True`). `json.load` accepts `Infinity`/`NaN` literals: `"trades": Infinity` raises uncaught `OverflowError` — `swing report` dies with a traceback (guardrails absorb it; report does not) — and `"profit_factor": Infinity` *passes* the check that documents itself as fail-closed. Copying any `ablate-*/summary.json` over `latest.json` opens the gate on a deliberately crippled variant: rule 3 is enforced only at write time, never at read time.
**Impact:** The single artifact standing between the user and live picks is defended by Python truthiness.
**Fix:** Require `is True`; add `_as_bool`; make `_as_float` require `math.isfinite`, catch `OverflowError` in `_as_int` (or reject `Infinity`/`NaN` via `parse_constant`); add a fail reason when the label starts with the ablation prefix.

### 4.1 Correctness bugs — Medium

### [BUG-018] Confirm verdicts never reach `picks.json`; every downstream reader trusts the stale file
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/alerts/pipeline.py:785-789` (confirm candidates from picks.json), `:799-807` (journal-only writeback); `src/swing/broker/executor.py:170-180` (status filter reads picks.json — unreachable branch)
**What's wrong/Impact:** `picks.json` is written once, always `"drafted"`. Confirm updates only the journal, so (a) re-running confirm *resurrects* an invalidated pick (reproduced: invalidated → confirmed when the price came back), and (b) the executor's skip-invalidated branch can never fire — an explicitly killed pick is still planned and sent; today the coincidence `CONFIRM_DRIFT_ATR_MULT == max_quote_drift_atr` (see DEBT-006) masks it.
**Fix:** Make one store authoritative: confirm rewrites `picks.json` statuses (and the executor cross-checks the journal for terminal statuses). Skip journal-terminal picks in confirm reruns.

### [BUG-019] The morning confirm consumes `--dry-run` scan reports and sends real notifications
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/alerts/pipeline.py:707-709,770-789,804-807`
**What's wrong/Impact:** Dry-run writes a full report dir (becomes `latest_scan_dir`) with no marker; `run_confirm` re-quotes it, notifies "1 confirmed" for picks that were never journaled, and the journal-miss `KeyError` is swallowed to a log file.
**Fix:** Stamp `"dry_run": true` in the payload; confirm refuses or downgrades itself; surface the swallowed count in `confirm.json` and the notification.

### [BUG-020] Report files are written non-atomically into a shared per-day directory
Severity: Medium | Confidence: Likely | Effort: S
Location: `src/swing/alerts/pipeline.py:230-232` (`write_text` truncate-in-place), ordering `:698-705`
**What's wrong/Impact:** A concurrent reader (confirm/executor) can see a truncated `picks.json`; a crash between orders written (`:698`) and picks.json written (`:700`) leaves orders without a report. Contrast `state._atomic_write` one module over.
**Fix:** Promote `_atomic_write` to a shared helper; write `picks.json` last as the directory's commit point (which `latest_scan_dir` already assumes).

### [BUG-021] `scan`/`confirm` exit 0 when every notification channel failed; `ScanError` leaks tracebacks
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/cli.py:192-210` (no `ScanError` handler); `src/swing/alerts/pipeline.py:713,815` (delivery dict discarded)
**What's wrong/Impact:** For a system whose entire value is the notification, all-channels-failed is indistinguishable from success (`launchctl` shows `last exit 0`); meanwhile a `ScanError` buries its plain-English sentence in a traceback, and `notify-test` *does* exit 1 — inconsistent convention.
**Fix:** Catch `ScanError` → one sentence, exit 2. Propagate the delivery dict; exit non-zero (or print loudly) when every configured channel returned False.

### [BUG-022] `paths.reports_dir` is CWD-relative while `state_dir` is absolute
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/config.py:642`; consumers `pipeline.py:237,632`, `executor.py:115`
**What's wrong/Impact:** `swing confirm` from `~` finds "no scan to confirm"; `swing scan` from elsewhere writes a report tree the executor can't find, while the journal (absolute) records the picks. launchd's pinned `WorkingDirectory` hides it until the first manual run.
**Fix:** Resolve `reports_dir` against the config file's directory at load, or default to `~/.swing/reports`.

### [BUG-023] `schedule install` reports success when `launchctl` actually failed
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/scheduler/launchd.py:250-253,281-292`
**What's wrong/Impact:** `_already()` treats exit code 5 (`EIO`) as "already loaded," but macOS returns 5 for genuine bootstrap failures too (SIP/TCC, malformed plist, missing binary) — reproduced printing "Installed…" on a failure. The user finds out days later when no alert ever arrives — the exact failure the module exists to prevent.
**Fix:** Verify with `launchctl print gui/<uid>/<label>` after bootstrap; only then print Installed. Drop 5 from the exit-code allowlist.

### [BUG-024] Corrupt-journal recovery silently resets open positions; the warning is invisible under launchd
Severity: Medium | Confidence: Likely | Effort: M
Location: `src/swing/state.py:191-199,226-254`; blast radius `pipeline.py:568-582`
**What's wrong/Impact:** Recovery-by-reset is documented, but the consequence — `positions()==[]` → full slots + full cash proposed while real positions are open, duplicate guardrail blinded — surfaces only as a `UserWarning` on stderr → `scan.err.log`. Plus the cross-process `_backup_corrupt` TOCTOU (uncaught `FileNotFoundError` for the losing racer).
**Fix:** Propagate `recovered=True` into a top-of-report note + high-priority notification line; pid-suffix the backup name; tolerate the race.

### [BUG-025] No intra-run duplicate-symbol suppression in `plan_orders`
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/broker/executor.py:261-298`
**What's wrong/Impact:** Two `AAPL` entries in `picks.json` → two identical live orders; the journal-based `duplicate` guardrail runs before either is journaled. `new_exposure` caps but does not prevent.
**Fix:** `seen: set[str]` in `plan_orders`; repeats become `problems`.

### [BUG-026] Scan freshness judged by the editable file body, not the selected directory
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/broker/executor.py:157-166`
**What's wrong/Impact:** `payload["asof"]` overrides the directory date and feeds `stale_scan` and journal keys; an edited/corrupted `asof` defeats staleness entirely (or refuses a genuinely fresh scan), and a malformed one silently reverts with no note.
**Fix:** Refuse on dir/body disagreement; use the directory date for selection *and* staleness; treat `asof` as corroboration.

### [BUG-027] `quote_price` can return the previous close — or a mismatched batch payload — as "current"
Severity: Medium | Confidence: Likely | Effort: S
Location: `src/swing/broker/auth.py:421-438`
**What's wrong/Impact:** `closePrice` (Schwab's *previous* close) sits in the current-price preference list, and `payload.get(symbol, payload)` falls back to the whole batch payload when the symbol key is missing — so `quote_drift`, the run-away-price guardrail, can validate against yesterday's number exactly when data is already degraded.
**Fix:** Drop `closePrice` (or tag it stale → refuse); missing symbol key → `None`.

### [BUG-028] Token age understated two ways, defeating the 7-day guardrail
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/broker/auth.py:110-154`
**What's wrong/Impact:** A future `creation_timestamp` (clock skew, partial write) clamps to 0.0 = "brand new, forever"; the mtime fallback tracks last *refresh* (schwab-py rewrites the file on every refresh), so a 6-day-old token refreshed five minutes ago measures five minutes old — the day-6 warning never fires and the failure surfaces at Schwab instead of as a local sentence.
**Fix:** Future timestamp → `None` (already refused); mtime fallback → `None` or explicitly labeled lower bound.

### [BUG-029] `breakout_proximity_pct = 100` validates and makes the breakout test unconditionally true
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/strategy/rules.py:169-170`; `src/swing/config.py:359-367`
**What's wrong/Impact:** `proximity=0.0` ⇒ `close >= 0.0` always true — entry degenerates to a bare volume filter (fired on a 39% downtrend in the probe), with no documentation that the knob has a cliff.
**Fix:** Cap the range (e.g. ≤25) or document 100 as an explicit disable like `adx_min=0`.

### [BUG-030] `size_position` is the one strategy call the scanner leaves unguarded
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/alerts/pipeline.py:453-462`; `src/swing/strategy/sizing.py:162-171`
**What's wrong/Impact:** The pre-guard checks finiteness and positive risk but not `entry > 0` — a legal `min_price=0.001` config with a sub-cent close rounds entry to 0.00, `size_position` raises, and the whole scan dies with a traceback (its neighbors are all try/except-wrapped).
**Fix:** Extend the guard (`entry <= 0 or stop < 0`) and wrap the call like its neighbors.

### [BUG-031] An interior NaN bar permanently shifts ATR/ADX instead of producing NaN
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: M
Location: `src/swing/indicators.py:109-121` (`_seeded_ewm`); boundary at `provider.py:285`
**What's wrong/Impact:** `ewm(adjust=False).mean()` forward-fills across NaN inputs and the seed `mean()` skips NaN — one bad vendor bar (partial-NaN row survives Contract 3) permanently rescales ATR with **zero NaNs in the output**, silently moving stops, sizes, trailing exits, and the BUG-003 divisor. The docstring's "NaN through warm-up" guarantee doesn't hold for interior gaps.
**Fix:** Require a complete seed window; re-mask output where input was NaN. (Or fix once at the boundary alongside BUG-005.)

### [BUG-032] Config values that validate cleanly but silently disable the strategy
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: M
Location: `src/swing/config.py:318-320,328-334,402-408,446-454`
**What's wrong/Impact:** No upper bounds vs the hard history limits (600-day fetch window, 260-row ranking floor): `sma_slow=450`, `volume_avg_window=5000`, `mom_skip_days=5000` all load fine and produce a permanently empty scan indistinguishable from a quiet market. `backtest.start` is validated against `end` only when `end` is set — a future start with unset end yields an empty backtest.
**Fix:** Cross-section validation in `Config.__post_init__` against a shared `MAX_LOOKBACK_BARS`; validate `start` unconditionally.

### [BUG-033] A quoted number in `config.toml` raises a raw `TypeError`, not `ConfigError`
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/config.py:162-198`
**What's wrong/Impact:** `sma_fast = "50"` — among the most common TOML mistakes — produces `TypeError: '>=' not supported…` as a stack trace, violating the module's central plain-English promise (bool and int-float mismatches *are* handled; str-for-number is not).
**Fix:** Symmetric guard in `_coerce_value` for numeric fields.

### [BUG-034] Schwab request bounds are naive datetimes — encoded in machine TZ, decoded in ET
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/data/schwab_provider.py:265-274` vs decode at `:334-340`
**What's wrong/Impact:** `datetime.combine(start, time.min)` is naive; schwab-py epoch-encodes it via the host's local zone (Denver = 2h behind ET), while decoding assumes ET — a permanent one-bar hole at the head of every cold fetch on non-ET hosts (and a 4-bar overlap probe instead of 5, feeding BUG-014's empty-intersection path). Existing test asserts only the naive date, so it can't catch it.
**Fix:** `datetime.combine(start, time.min, tzinfo=ZoneInfo(EXCHANGE_TZ))`; test the epoch millis under a monkeypatched TZ.

### [BUG-035] An empty/all-NaN tail response is recorded as "covered through end"
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/data/cache.py:302-308`
**What's wrong/Impact:** "Nothing traded" and "vendor returned junk" are indistinguishable; a transient failure at 21:00 marks the symbol covered through today, so the natural rerun-an-hour-later stays offline on stale bars.
**Fix:** Only advance coverage when the batch genuinely succeeded (sentinel from `_call`), or cap the advance at the last known trading day.

### [BUG-036] No as-of earnings data: the backtest's earnings blackout is a no-op on history
Severity: Medium | Confidence: Confirmed | Effort: L
Location: `src/swing/data/provider.py:118-120`; `yf_provider.py:283-288`; consumers `backtest/runner.py:283`, `rules.py:228-229`
**What's wrong/Impact:** The contract returns only the *next upcoming* date; the engine feeds that single 2026 date into every historical bar's blackout check → nothing is ever blocked. The gate measures a strategy (no blackout) the live scan does not run (10-day blackout) — a genuine live/backtest divergence, the one thing the shared-rules design is meant to prevent.
**Fix:** Add `earnings_history()` to the contract (yfinance already returns past dates; they're currently discarded), or stamp "earnings blackout not simulated" into the report so the divergence is at least declared.

### [BUG-037] Fundamentals silently mix quarterly-YoY and annual-YoY; non-adjacent periods compared
Severity: Medium | Confidence: Confirmed | Effort: M
Location: `src/swing/data/yf_provider.py:54-57,302-317,463-490`
**What's wrong/Impact:** Primary source is quarterly growth; the fallback income statement is annual, invisibly; and NaN periods are dropped without preserving adjacency, so a two-period gap masquerades as one. Bounded blast radius (sign-only consumer) but the screen rejects candidates on an unlabeled basis.
**Fix:** `freq="quarterly"` fallback or a `basis` field on `Fundamentals`; adjacent-periods-only in `_growth_from_statement`.

### [BUG-038] Schwab `time`/`timestamp` keys decoded as milliseconds without a magnitude check
Severity: Medium | Confidence: Confirmed (reproduced; latent) | Effort: S
Location: `src/swing/data/schwab_provider.py:60,328-340`
**What's wrong/Impact:** A seconds-based `timestamp` decodes to 1970 bars; normalize accepts, cache stores, window-slice returns empty — symbol vanishes with no diagnostic.
**Fix:** Magnitude sniff (`< 1e11 ⇒ seconds`) or drop the ambiguous keys and fail loudly.

### [BUG-039] Yahoo-form symbols sent verbatim to Schwab
Severity: Medium | Confidence: Needs investigation | Effort: M
Location: `src/swing/universe.py:59-65`; `schwab_provider.py:264,283-286`
**What's wrong/Impact:** The universe normalizes to `BRK-B`; Schwab's symbology for share classes is generally `BRK/B`. If so, every dual-class name silently vanishes under `data.provider="schwab"` (one warning per symbol per run, no aggregate). Needs a live quote check to confirm the exact form before fixing.
**Fix:** `to_schwab_symbol()` translation applied in the provider (mapping back for result keys); aggregate "N of M returned no history" log.

### [BUG-040] `by_year` drawdown excludes each year's first bar from its own drawdown
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/backtest/metrics.py:277-285`
**What's wrong/Impact:** `cumprod` seeds the peak *after* the year's first move: a year opening −20% and recovering reports `max_dd 0.0`. Zero delta on the 13 shipped years (recomputed both ways), but the table exists to answer "was the bad year survivable."
**Fix:** Seed the year curve at 1.0, mirroring `compute_metrics`'s `initial_equity` prepend.

### [BUG-041] The walk-forward tuner maximizes the 9999.0 profit-factor sentinel
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/walkforward.py:225-235,446-448`
**What's wrong/Impact:** `objective_key` reads the capped PF and ignores `profit_factor_capped`: 8 lucky zero-loss IS trades (≥ `MIN_IS_TRADES`) outrank 300 honestly measured ones, and that parameter set drives a full OOS year. Hasn't fired in shipped reports (max IS PF 2.23); becomes live on narrow universes/short IS windows.
**Fix:** Capped ⇒ treat PF as 0 for ranking (fall back to trades/DD) or raise the floor for capped points; document in `OBJECTIVE_DESCRIPTION`.

### [BUG-042] A `--label` with a path separator relocates `latest.json` and defeats the ablate guard
Severity: Medium | Confidence: Confirmed (reproduced) | Effort: S
Location: `src/swing/backtest/runner.py:419,493-502,329-330`
**What's wrong/Impact:** `label` is an unvalidated path component and `latest.json` is written to `directory.parent`: `--label sub/run-1` leaves the real gate file **stale** while the user believes they refreshed it; `--label sub/ablate-x` bypasses the prefix guard entirely; `../escaped` writes outside the tree.
**Fix:** Validate `^[A-Za-z0-9._-]+$`; compute the latest path from `gate.latest_path(cfg)`, never from the run directory.

### [BUG-043] The report headline shows the data span, not the OOS span the numbers cover
Severity: Medium | Confidence: Confirmed | Effort: S
Location: `src/swing/backtest/runner.py:388-389,421-422`; `report.py:96,292,494`
**What's wrong/Impact:** "Period: 2010-01-01 to 2026-08-18" sits directly above OOS metrics that actually cover 2013-01-02→2025-12-31 — three years of warm-up and eight unused months presented as measured record, in every artifact a human reads before deciding to trade.
**Fix:** Add `oos_start`/`oos_end` to the summary and render those in the headline; keep the data span in provenance.

### 4.1 Correctness bugs — Low

### [BUG-044] `quote_drift` parses pick fields with raw `float()` — traceback instead of refusal. `guardrails.py:300-302`; use the executor's `_as_float`. (S)
### [BUG-045] `earnings_blackout` raises on a tz-aware index; the scanner catches broadly and **fails open** (entry permitted inside a blackout) while the engine would crash. Latent — all providers strip tz. `rules.py:228`; normalize defensively, fail closed. (S)
### [BUG-046] `draft_orders`' three pre-coercions (`float(pick.entry)` etc.) raise bare `TypeError` outside the documented `OrderDraftError` contract, and the drafting loop runs before `picks.json` is written — a bad hand-built record loses the whole report. `orders.py:194-204`. (S)
### [BUG-047] `_ascii_header` strips only edge whitespace (embedded `\n` reaches the ntfy header; requests rejects it — fails closed); price strings hard-code 2dp while Schwab wants 4dp under $1 (unreachable at `min_price=5.0`, silent if lowered). `channels.py:110-112`, `orders.py:73,89-102`. (S)
### [BUG-048] `_flatten_columns` level heuristic (unlike `_ticker_level`) is fooled by a ticker literally named `OPEN` — public `normalize_bars` path only. `provider.py:190-204` vs `yf_provider.py:368-377`; share one subtractive heuristic. (S)
### [BUG-049] Same-trading-date Schwab candles are deduped keep-last instead of OHLCV-aggregated — volume halves if the endpoint ever returns intraday rows. `schwab_provider.py:334-344`; groupby-agg before normalize. (S)
### [BUG-050] A lost meta sidecar rebuilds coverage from the frame's own bounds, converting a warm cache into a full refetch for late-IPO symbols. `cache.py:276,377-379`; seed `covered_start=min(start, first_bar)`. (S)
### [BUG-051] `None` earnings answers cached 3 days against a 10-day blackout — a date appearing inside the TTL window can admit an entry the blackout exists to block. `yf_provider.py:50,186-192`; shorter negative-TTL. (S)
### [BUG-052] `max_drawdown` duration measures to the last underwater bar; docstring and test comment say "to recovery." `metrics.py:99-127`; make all three agree. (S)
### [BUG-053] Zero-share picks inside the `ranked[:max_positions]` cut are not replaced by the next affordable candidate, contradicting the engine contract at `engine.py:78-79`. `engine.py:859-863`; carry a deeper bench. (S)
### [BUG-054] No minimum risk-per-share: near-flat ATR (0.002) sized 249 shares, `risk_amount=$10.96` on a $24,910 notional — risk-first sizing becomes fiction on pegged instruments. `engine.py:771-776`; floor `entry−stop ≥ max(0.01, 0.1%·entry)`, ideally in `size_position` so the scanner is covered too. (S)
### [BUG-055] A crash between `add_picks` (`pipeline.py:712`) and `deliver_scan` (`:713`) journals picks whose alert never went out — dedupe then suppresses them for 7 days. Notify-then-journal, or tolerate re-notification. (S)

---

### 4.2 Performance

### [PERF-001] The engine rebuilds numpy arrays on all 1,053 calls — 63–71% of a warm walk-forward
Severity: High | Confidence: Confirmed (measured) | Effort: M
Location: `src/swing/backtest/engine.py:379-485`
**What's wrong:** `SignalCache` memoizes pandas Series (keys verified complete against every field the builders read — no staleness), but every `run_engine` call re-runs eleven `reindex→astype→to_numpy` round-trips per symbol plus `index.get_indexer(calendar)`. Measured: `_build_plan` is 23.1/36.6 ms at 30 symbols, 117.7/166.8 ms at 150 (71%); cProfile concurs.
**Impact:** ≈30 minutes of the shipped 1,545-symbol walk-forward is redundant array reconstruction.
**Fix:** Cache arrays, not Series (builders return `.to_numpy()` under the same keys) plus one `("plan_static", ident, calendar_ident)` entry for OHLC/row_of_day.

### [PERF-002] `latest_quotes` (yfinance) is one serial `Ticker` round-trip per symbol, with a 5-day-history fallback per miss. `yf_provider.py:157-171,261-279`. High for arbitrary lists (~200-400 ms/symbol); executor/confirm lists are small today. Batch via `yf.download`/`Tickers` + small thread pool. (M)
### [PERF-003] Cold earnings/fundamentals: one monolithic serial dict-comprehension over ~1,500 symbols (thousands of round-trips), and `TtlJsonCache` persists **nothing until all complete** — a Ctrl-C at symbol 1,400 discards everything. `yf_provider.py:186-210`, `cache.py:484-493`. Chunk (50) + persist per chunk + bounded pool. (M)
### [PERF-004] Per-day `calendar[di]` Timestamp materialization is 30× `list()` (≈15-20% of `_simulate`); `_master_calendar`/`_regime_array`/`row_of_day` are grid-invariant but rebuilt per combo (≈18 s/run); `max_drawdown` has the same per-item pattern (43% of `compute_metrics`). `engine.py:697,605-650`; `metrics.py:119-127`. Hoist/vectorize/cache-per-fold. (S)
### [PERF-005] `SignalCache` is unbounded: 21 Series/symbol measured ⇒ ≈520 MB per fold at 1,545 symbols atop ~250 MB of bars, no eviction, no size metric. `engine.py:252-289`. Byte-budget LRU + size in stats. (M)
### [PERF-006] Every warm `get_bars` re-reads and fully re-normalizes every parquet (~5 frame copies each ⇒ ≈½ GB churn per scan; 3,000 file opens), and `get_provider(cfg)` is rebuilt at 5 call sites. `cache.py:184-206,271-277`, `provider.py:254-286`. Fast-path already-normalized frames; mtime-keyed memo; share one provider. (M)
### [PERF-007] Schwab daily history is strictly serial (~1,500 requests cold; no bulk endpoint exists, but no concurrency either). `schwab_provider.py:258-281`. Bounded thread pool. (M)
### [PERF-008] Scan loop: `inspect.signature` re-probed per candidate (7.4 µs × universe) and `recently_picked` is O(candidates × journal) — 121 ms at 5k journal rows and growing (LEAK-001). `pipeline.py:219-227,556-560`. Hoist probe; index journal by symbol per scan. (S)
### [PERF-009] ATR computed twice per ranked symbol (`scoring.py:128,139`) and twice per sized pick (`pipeline.py:447-448`) — 0.94 ms/symbol. Pass the series. (S)
### [PERF-010] `trend_template` always computes ADX (3.75 of 4.66 ms; ≈5.8 s/scan; every `adx_off` ablation fold) even when `adx_min=0` makes it a provable no-op. `rules.py:139`. Short-circuit on `adx_min <= 0`. (S)
### [PERF-011] Universe CSVs re-parsed per `load()`; `etfs.csv` parsed twice per call. `universe.py:98-102,126-148`. `lru_cache` the immutable snapshots. (S)

---

### 4.3 Memory & resource leaks

### [LEAK-001] The journal grows without bound and every mutation rewrites the whole document
Severity: Medium | Confidence: Confirmed | Effort: M
Location: `src/swing/state.py:258-347`; `pipeline.py:799-805` (one full rewrite per confirmed pick)
**Impact:** ~1,000 records/year — small on disk, but dedupe cost grows linearly (PERF-008), the BUG-001 race window widens with document size, and stale `"drafted"` rows accumulate forever.
**Fix:** Retention policy in `save()` (archive drafted/watch > 90 days; keep ordered/filled); batch `update_statuses()`.

### [LEAK-002] Report dirs accumulate forever (one per calendar day incl. weekends, ~4 MB/yr, linear `latest_scan_dir` scans); orphaned `.tmp` files from SIGKILL survive in both `~/.swing` and the cache (each cache orphan is a full symbol history). `pipeline.py:632-640`, `state.py:176-188`, `cache.py:80-91`. Prune on scan start; sweep stale tmps. (S)
### [LEAK-003] TTL JSON caches never evict departed symbols and rewrite whole-file (growth + the BUG-001-family lost-update race). `cache.py:438-493`. Drop entries older than ~10×TTL under the shared lock. (S)
### [LEAK-004] schwab-py clients (httpx pools) never closed at any of five call sites — `check()` builds one just to print, `login()` discards one. Harmless in a short CLI; leaks per call in any long-lived use. `auth.py:265-270,325-330,464`; `executor.py:614,816,948`. `contextlib.closing`/`finally`. (S)
### [LEAK-005] Email + SMS each open their own SMTP connect/STARTTLS/auth/teardown per delivery (2× per notification). `channels.py:134-178`. One connection per `deliver`. (S)
### [LEAK-006] `plt.close(figure)` not in `finally`; `BytesIO` never closed — figure stays registered if `savefig` raises (verified). Only matters for long-lived processes. `report.py:193-201`. (S)

*Verified clean:* no unclosed file handles anywhere (context managers throughout); subprocess pipes reaped with timeouts; `requests` calls carry timeouts; matplotlib happy path leaves zero figures after 25 renders; no module-level accumulators in the numeric layer.

---

### 4.4 Code quality & tech debt (significant only)

### [DEBT-001] Two divergent `latest_scan_dir` implementations. Pipeline: strict regex, requires `picks.json`, lexicographic sort. Executor: loose prefix, no content check, parsed-date max. They disagree on real states (crash-mid-write dirs), so `swing confirm` and `swing execute` minutes apart can target *different scans*. `pipeline.py:235-247` vs `executor.py:100-126`. One shared function: parsed-date max + `picks.json` required. (Medium, S)
### [DEBT-002] The 210-line order validator (`validate_order_draft` et al.) is exercised only by tests — `run_scan` writes drafts to disk unvalidated, so the one artifact a human might hand a broker is never actually checked. `orders.py:232-444`; `pipeline.py:691-698`. Three lines to call it and skip+note failures. (Medium, S)
### [DEBT-003] Executor module docstring promises the dry run "touches no network" — but `fetch_quotes` unconditionally calls the configured data provider (yfinance HTTP), stalling through per-symbol retries offline. The suite can't see it (socket-block + broad except ⇒ SKIP). `executor.py:5-6` vs `:631,418-427`. Gate the fetch on `live` or add `--offline`; make prose match. (Medium, S)
### [DEBT-004] Money-path degradations logged at INFO and invisible: the journal-couldn't-record-a-live-order case, and provider errors behind `quote_drift`'s "fix the data provider" refusal. `executor.py:442-454,777-780`; `auth.py:219-224`. WARNING + print post-placement; carry provider error text into the refusal. (Medium, S)
### [DEBT-005] `ablations.py` ends every sweep with a **factually false** warning that it overwrote `latest.json` ("the gate now reflects the LAST variant") — the `ablate` label guard makes that impossible (verified in the shipped run). Erodes trust in the warnings that are real. `scripts/ablations.py:549-554`. Invert it to confirm the guard held. (Medium, S)
### [DEBT-006] The 1×ATR invalidation threshold exists twice: frozen `CONFIRM_DRIFT_ATR_MULT` (confirm) and config `execution.max_quote_drift_atr` (executor). Retuning the knob desynchronizes the two verdicts on the same pick — and BUG-018's masking depends on their current equality. `pipeline.py:71-73`; `config.py:574`. Confirm reads the config knob. (Low, S)
### [DEBT-007] Orders/day budget implemented in both `orders_today` and `_place_orders`; token-age ladder implemented in both `auth.token_status` and `guardrails.token_age`. Enforcement rules in two places drift expensively. `guardrails.py:362-379`/`executor.py:727-738`; `auth.py:172-216`/`guardrails.py:164-193`. Single helpers. (Low, M)
### [DEBT-008] Naive-datetime semantics contradict: auth says "local", guardrails say "ET", `run_execute` hands the same `moment` to both and mixes two notions of "today" near midnight. Latent (CLI always passes aware ET). `auth.py:135-146`; `guardrails.py:201-211`; `executor.py:566-567`. Reject naive at the boundary; derive `today` once. (Low, S)
### [DEBT-009] Ablation `change` strings hardcode assumed baselines ("20.0 -> 0.0") while the runner ablates the *user's* config — the committed results table can misstate its own baseline. `scripts/ablations.py:134-198`. Render from `getattr(base_cfg, …)`. (Low, S)
### [DEBT-010] `docs/strategy-spec.md` still says the score ATR uses `strategy.atr_window`; code hardcodes `SCORE_ATR_WINDOW=14` (ruled) — and neither `SCORE_ATR_WINDOW` nor the 260-row ranking floor appears in the spec, so a maintainer trusting the doc would silently rescale every score. Amend the spec; pin with a test. (Low, S)
### [DEBT-011] `obv`'s deliberate non-use is documented only in the research doc; its own docstring — the place the misreading happens — says nothing. One line. `indicators.py:437-455`. (Low, S)
### [DEBT-012] `(close > prior_high)` is provably dead beside the proximity disjunct for every legal config; reads as carrying the strict-breakout case. `rules.py:170`. (Low, S)
### [DEBT-013] `_with_retry` duplicated across both providers; retries/backoff/batch sizes are inline literals with zero config surface — a rate-limited user must edit source. `yf_provider.py:132-149`; `schwab_provider.py:205-218`. Shared helper + `DataCfg` knobs. (Low, S)
### [DEBT-014] Data-layer smalls: `asset_dir()` stringifies a `Traversable` (breaks zipped installs); universe CSVs opened `utf-8` (BOM → misleading header error; use `utf-8-sig`); `universe.__all__` omits `UniverseError`/`symbols`; `daily_bars` alone lacks `now=` injection; dead upper-case fallback `schwab_provider.py:240`; yfinance-fallback ignores configured retries; `warnings.warn` in a 1,500-iteration loop where siblings use `log`. (Low, S)
### [DEBT-015] Engine: `index_of_symbol` built, threaded, never read; `_finite(stop)` guards at `:722,:730` are unreachable-false while the genuinely unguarded `open_px` on the same lines has none — misdirection that helped BUG-005 survive review. `engine.py:591,672,722,730`. (Low, S)
### [DEBT-016] The HTML equity chart is hard-titled "Out-of-sample equity" even when the banner above it says "NOT a walk-forward run." `report.py:212,301`. (Low, S)
### [DEBT-017] Dead `inspect.signature` compat shim (real journal always accepts `asof`); 7 of 18 view-model keys unused by any template (`notional_pct` computed and used nowhere); `_autoescape` matches ".html" by substring; `template_dir()` breaks under zipimport. `pipeline.py:219-227`; `render.py:53-60,128-157`. (Low, S)

**TODO/FIXME inventory: zero markers across all of `src/` and `scripts/` — genuinely unusual, and consistent with the no-dead-code discipline observed everywhere except DEBT-015/017.**

---

## 5. Remediation roadmap

**Phase 0 — quick wins (all S; ~a day total; do before any further live-mode work):**
1. Gate hardening (BUG-017) — `is True`, finite-only numerics, ablate-label check.
2. Dedupe semantics (BUG-010, BUG-011) — `0 < delta`, picks-only journaling. *Unblocks daily operation immediately.*
3. `Weekday` in plists + empty-report skip in `latest_scan_dir` (BUG-012, DEBT-001 shared helper).
4. Ranking floor + `np.isfinite` (BUG-003).
5. Cache self-heal on read (`meta.rows`/`last_bar` check — BUG-004 mitigation) + union-range refetch (BUG-013).
6. Call `validate_order_draft` in the scan loop (DEBT-002); delete the false ablations warning (DEBT-005).
7. Label validation + `gate.latest_path` (BUG-042); `reports_dir` anchoring (BUG-022); `ScanError` handling + delivery exit codes (BUG-021).
8. Engine NaN boundary guard (BUG-005) — small, prevents the worst silent-corruption class.

**Phase 1 — concurrency (M; one focused day):** one `fcntl.flock` discipline for journal + TTL caches + cache directory (BUG-001, LEAK-003, BUG-024's TOCTOU); atomic report writes with `picks.json` as commit point (BUG-020); load-journal-late in `run_scan`. This phase removes an entire finding family.

**Phase 2 — live-execution readiness (M/L; required before the first `--live` order, in order):** duplicate-by-identity (BUG-006) → broker-position filtering + order-level reconciliation (BUG-007, also mitigates BUG-002's blindness) → pending-then-open placement journaling + honest post-placement errors (BUG-002) → per-order kill/hours recheck (BUG-008) → payload↔plan assertion (BUG-009) → intra-run dedupe (BUG-025) → token-age `None`-on-unknown (BUG-028) → drop `closePrice` (BUG-027). Then a paper-account end-to-end rehearsal.

**Phase 3 — backtest fidelity (M):** dead-symbol exit (BUG-016); earnings history or declared no-blackout (BUG-036); tuner cap handling (BUG-041); OOS-span headline (BUG-043); by-year seed (BUG-040); risk floor (BUG-054); bench depth (BUG-053).

**Phase 4 — performance (M; optional but big):** plan-array caching (PERF-001, ~30 min/run back) → fold-invariant caching + Timestamp hoists (PERF-004) → warm-read fast path + shared provider (PERF-006) → chunked/parallel earnings+quotes (PERF-002/003) → `SignalCache` budget (PERF-005).

**Phase 5 — hygiene sweep (S items):** remaining Lows and DEBT-006..017, config cross-validation (BUG-032/033/029), data-layer smalls (BUG-034/035/037/038, DEBT-013/014), retention policies (LEAK-001/002).

Dependencies: Phase 1's lock makes several Phase 0 mitigations belt-and-braces rather than load-bearing; Phase 2 items 2–3 both depend on introducing order-status polling (the missing `filled` half of the state machine) — build that once.

---

## 6. Appendix

**Metrics.** Source: 13,116 LOC across 30 files (largest: `engine.py` 996, `executor.py` 982, `config.py` 835, `pipeline.py` 816, `guardrails.py` 783). Tests: 13,636 LOC (1,426 passing at `eb403f4`). TODO/FIXME/XXX/HACK/`type: ignore`/`noqa`: **0**. Cached data at audit time: 1,545 symbols, 5.65M bars, 0 partial-NaN rows.

**Duplication hotspots:** `latest_scan_dir` ×2 (DEBT-001); `_with_retry` ×2 (DEBT-013); orders/day + token ladder ×2 each (DEBT-007); drift threshold ×2 (DEBT-006); column-level heuristics ×2 (BUG-048).

**Method/commands (representative):** five parallel module auditors with checklists + probe scripts in an out-of-repo scratchpad; lead verification sweeps via `sed -n`/`grep -n` on every Critical/High citation; `grep -rn "flock\|fcntl" src/` (no locking); `grep -rn '"filled"' src/` (never assigned); `find`+`wc` sizing; probes exercised real modules (`BarCache`, `run_engine`, `run_scan`, `gate.check`, `size_position`, `draft_orders`) with synthetic fixtures. No repo files modified; no tests/builds run as part of the audit; no live Schwab endpoints exercised.

**Honest blind spots.** Schwab-side behavior (symbology BUG-039, candle semantics BUG-049, epoch encoding BUG-034) is verified against schwab-py source, not the live API. launchd behavior verified by plist inspection and mocked runners, not a multi-day soak. Performance figures are measured on synthetic universes and extrapolated linearly to 1,545 symbols. Test-suite quality was not audited.
