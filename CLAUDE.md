# CLAUDE.md — orientation for agent sessions

## What this is

`swing` is a personal, evidence-based swing-trading pick system (long-only, 1–8
week holds). It builds a universe from index CSVs, pulls daily bars through a
provider seam, runs a trend/momentum strategy, and refuses to emit live picks
until a walk-forward backtest has passed a gate for *this exact config*. Output
is a nightly pick sheet with whole-share sizes, ATR stops and drafted Schwab
orders; v2 can place those orders behind a stack of guardrails. Every strategy
number lives in `config.toml` and is justified in `docs/indicator-research.md`.

## Commands

```bash
make install                            # ./install.sh — uv venv, deps, config.toml
make test                               # or: .venv/bin/pytest -q -p no:warnings
.venv/bin/ruff check src tests          # or: make lint
swing doctor|universe|data|backtest|scan|confirm|execute|auth|journal|positions|kill|schedule
swing backtest --walk-forward           # THE gate; also --etf-only --ablations --sensitivity
swing scan --dry-run                    # a pick sheet, nothing sent
swing journal add|exit|stop|show        # record manual fills, ratchet stops, read the log
```

Tests are fast (~20s, 612 of them) and hermetic. Run the full suite after any
change: it layers `config.example.toml` under its fixtures, so editing that
file breaks tests far away from it.

## Architecture map

- `src/swing/strategy/rules.py` + `sizing.py` — **the** definition of a trade.
  `backtest/engine.py` and `scan.py` both import them. Never fork this logic:
  one copy is the reason a backtested edge survives into production.
- `src/swing/data/provider.py` — the `DataProvider` seam (`get_provider`);
  `yfinance_provider.py`, `schwab_provider.py`, `stooq_provider.py` implement
  it. `cache.py` is a parquet bar store that does **not** record which provider
  wrote a file, and `ensure_provider` REFUSES to write into a cache stamped
  with a different one — switching providers means deleting `data/cache/` and
  re-backfilling.
- `src/swing/backtest/` — `engine` (bar-by-bar, next-open fills), `walkforward`,
  `metrics`, `bootstrap` (block-bootstrap CIs), `report`, `gate`.
- `src/swing/execution/` — append-only `journal.jsonl`, `guardrails.py`,
  `executor.py`. `scan.py` → `picks.py` → `orders.py` → alerts.
- The gate: `swing scan` reads the newest walk-forward report and refuses picks
  unless its out-of-sample metrics clear `[backtest.gate]` **and** its config
  hash matches the current config.

## THE config-hash rule

`Config.hash` (src/swing/config.py) hashes `[account] [universe] [strategy]
[backtest]` only. Adding a key to any of those four sections changes every
user's hash and **re-locks the gate until they re-run the walk-forward**. Put
reporting, data-source and operational knobs under `[data]`, `[reports]`,
`[execution]`, `[alerts]`, `[schedule]` instead — see `[data]
earnings_calendar` and `[reports.bootstrap]`, both deliberately placed to keep
the hash stable.

The converse binds just as hard: a knob that changes the trades, or that moves
the floor a report is validated against, **belongs inside the hash**, and its
arrival costs every user one re-run. `[backtest.gate] min_excess_cagr` and
`[strategy.regime] exit_on_regime_off` both landed there on purpose and
re-locked every existing gate once. A gate threshold outside the hash would let
a report drift out of sync with the floor being applied to it — pinned by
`tests/test_config.py::test_gate_thresholds_are_inside_the_hashed_backtest_section`.
Reporting knob → outside the hash; trade or validation knob → inside it, and say
which in the annotation.

Three `[account]` keys are excluded (`HASH_EXCLUDED_ACCOUNT_KEYS` in
config.py): `equity`, `stale_equity_tolerance_pct` and `currency`. Backtests
size from `backtest.initial_equity`, so recording a deposit cannot move an
out-of-sample number and no longer re-locks the gate. The rest of `[account]`
— `risk_pct`, `max_position_pct`, `max_concurrent_positions` — still does.

`config.example.toml` layers *under* `config.toml`, so deleting a key there —
a `[backtest.walk_forward.grid]` axis, say — restores the shipped default
instead of removing it; pin the axis to one value to neutralise it.

## Conventions

- **Indicators are hand-rolled pandas** (`indicators.py`, Wilder smoothing done
  properly) and pinned by exact-value tests. Do not swap in a TA library.
- **No network in tests.** Providers keep a single I/O method that tests
  monkeypatch with canned payloads; everything above it is pure parsing.
- **Data paths degrade, they do not die**: a symbol that fails to fetch is
  absent from the result and warned about, never an exception that kills the
  run. Soft filters fail *open* when the data is missing.
- **Execution paths block, they do not warn**: a guardrail that cannot verify
  something refuses the order. Limit orders only; market orders are refused at
  every layer. The gate reads the same way: a criterion it cannot evaluate —
  `excess_cagr` absent, null or non-finite — FAILS, it is not skipped, because
  0.0 would *clear* that particular floor.
- **Whole shares only** — `floor()`, and zero shares is a real, reported answer,
  not an error to round away.
- **The journal is append-only.** Correct it by recording new events (`swing
  journal exit`, `swing journal stop`), never by editing history. Stops ratchet
  up; lowering one needs `--force`.
- Reports are byte-reproducible: fixed seeds, and each carries the config hash,
  a bar fingerprint and the git commit. Caveats (survivorship, earnings
  coverage, bootstrap width, overfitting) are stated plainly, not softened.

## Where to read more

`README.md` (what it does), `docs/runbook.md` (operating it and failure
recovery), `docs/indicator-research.md` (every default traced to evidence or an
ablation), `docs/schwab-setup.md` (broker OAuth), `config.example.toml` (every
knob, annotated).
