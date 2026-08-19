# swing

A command-line system that proposes long-only swing trades (roughly one to eight
week holds) across the S&P 500, 400 and 600 plus a curated list of liquid ETFs.

**The honest framing:** no indicator predicts stock prices. Trend and momentum
rules have a modest, well-documented historical edge that is easy to destroy
with costs, over-fitting and impatience. So this tool refuses to hand you picks
on the strength of a pretty chart: `swing scan` will not emit a single pick
unless a walk-forward, out-of-sample backtest with realistic slippage and
spreads has passed the thresholds in your `[gates]` config. If the strategy
stops working, the gate closes and the scan says so instead of inventing ideas.

Everything is local. Price history comes from free yfinance data cached to
parquet, so nothing blocks on broker API approval. Execution is opt-in, dry-run
by default, and behind a kill switch.

## Quickstart

```bash
./install.sh                     # installs uv if needed, creates the venv
$EDITOR config.toml              # set [account] equity/risk and an alert channel
make test                        # full suite, runs offline
uv run swing backtest            # walk-forward run — this is the gatekeeper
uv run swing report              # read the results honestly
uv run swing scan --dry-run      # build tonight's report, notify nobody
```

If `uv run swing backtest` does not clear your gates, that is the system
working. Investigate the strategy; do not lower the gates to feel better.

## Commands

| Command | What it does |
| --- | --- |
| `swing scan` | Nightly scan; writes `reports/scan-YYYY-MM-DD/` and sends alerts. `--dry-run`, `--force`, `--asof` |
| `swing confirm` | Morning re-check of last night's picks against fresh prices. `--dry-run` |
| `swing backtest` | Walk-forward backtest with costs. `--universe full\|etf\|stocks`, `--start`, `--end`, `--no-walkforward`, `--label` |
| `swing report` | Print the summary of the most recent backtest |
| `swing auth` | Schwab OAuth login, or `--check` the stored token |
| `swing notify-test` | Send a test through every configured alert channel |
| `swing schedule` | `install`, `uninstall` or `status` for the launchd timers |
| `swing execute` | Place today's confirmed orders. Dry-run unless `--live` **and** `execution.enabled` |
| `swing positions` | Open positions, broker view vs local journal |
| `swing kill` | Engage the kill switch and cancel working orders. `--off` releases it |
| `swing universe` | Show the tradable universe this config selects. `--list` |

Global options: `--config PATH`, `--verbose`, `--version`.

## How it fits together

```
universe.py ──► data/ (yfinance → parquet cache)
                  │
                  ▼
            indicators.py ──► strategy/ (rules, scoring, regime)
                  │                        │
                  │                        ├──► backtest/ ──► gate  ─┐
                  │                        │                          │ must pass
                  └────────────────────────┴──► alerts/ (scan) ◄──────┘
                                                    │
                                                    ▼
                                        reports/ + drafted Schwab orders
                                                    │
                                                    ▼
                                          broker/ (opt-in execution)
```

Configuration is one TOML file (`config.toml`, gitignored — start from
`config.example.toml`).

## Configuration notes

- **A relative `paths.reports_dir` is resolved against the directory of the
  `config.toml` it was read from**, so `swing confirm` run from `~` finds the
  reports `swing scan` wrote from the project directory. Absolute paths are
  left alone, and running on example defaults (no config file anywhere) keeps
  the old working-directory behaviour, with the loud "EXAMPLE DEFAULTS"
  warning.
- **`paths.state_dir` is *not* anchored that way** — a relative `state_dir`
  stays relative to whatever directory you happen to run from, and you will get
  a different journal per directory. Known limitation; leave it absolute (it
  defaults to `~/.swing`).
- **Numbers must not be quoted.** `sma_fast = "50"` and `risk_pct = true` are
  refused at load time with a sentence naming the setting, rather than dying
  later inside a comparison.
- **Lookback windows are capped** so a setting cannot quietly produce a
  permanently empty scan: see
  [`docs/strategy-spec.md` §12.2](docs/strategy-spec.md#122-config-bounds-that-exist-to-stop-a-silent-no-op).
- **`[data]` has three politeness knobs** — `retries` (1–10), `retry_backoff`
  (seconds, doubling, capped at 8) and `download_batch` (10–500 symbols per
  request). They tune how hard the tool leans on a free data source; they do
  not change any result.

## Local state, retention and locking

By default everything lives under `~/.swing/`:

| Path | What it is | Config key |
| --- | --- | --- |
| `journal.json` | picks and orders — the record of what was proposed and sent | `paths.state_dir` |
| `journal.archive.json` | retired picks and orders (see below) | `paths.state_dir` |
| `KILL` | the kill switch; while it exists nothing is sent | `paths.state_dir` |
| `logs/` | launchd stdout/stderr for the scheduled scan and confirm | `paths.state_dir` |
| `cache/` | parquet price history plus small JSON TTL caches | `data.cache_dir` |
| `schwab_token.json` | the Schwab OAuth token, `chmod 600` | `schwab.token_path` |

What gets cleaned up, and what does not:

- **Journal.** On every write, picks older than 90 days that are `watch` or
  still `drafted`, and terminal orders older than 90 days, are moved to
  `journal.archive.json`. Confirmed, ordered and filled picks stay. **The
  archive is never pruned** — it only grows, on purpose, because it is the
  audit trail. Delete it yourself if you ever need to.
- **Scan reports.** `reports/scan-YYYY-MM-DD/` directories older than 90 days
  are removed at the start of each scan. **Backtest report directories are
  never removed** — they are evidence.
- **Price cache.** A symbol normally refreshes incrementally (a short overlap
  window is re-fetched and compared, so a retroactive split adjustment
  triggers a full re-download). A symbol whose cache has not been *written* for
  90 days is re-fetched in full. In daily use that never fires; after a long
  break, expect the first run back to be a slow one.
- **Locking.** Concurrent writes to the journal and the price cache are
  serialised with **advisory `fcntl` locks** on `.lock` sidecar files. Advisory
  locking is reliable on a local disk and may be a no-op on a network share, so
  **keep `~/.swing` (and the cache directory) on local storage** — not NFS, not
  SMB, not a synced folder. Reads take no lock; a lock held too long degrades to
  a warning and the data already on disk, never to a crash. The `.lock` files
  are empty and safe to leave in place.

## Scheduling

`swing schedule install` writes two launchd jobs, `com.swing.scan` and
`com.swing.confirm`, at `schedule.scan_time` and `schedule.confirm_time`.

- They fire **weekdays only** (one calendar entry per day, Monday to Friday).
  Holidays are not modelled — a scan on a market holiday simply finds no new
  bars.
- Times are interpreted in the **machine's local time**, not
  `schedule.timezone`.
- `install` verifies with `launchctl` that the job is really loaded before it
  claims success, and prints what launchd said when it is not.
- `swing scan` and `swing confirm` exit **2** when every configured
  notification channel fails. The report is still written; the exit code is
  there so a scheduled run that reached nobody is not silently green.

## Safety

- Execution needs **two** independent switches: `execution.enabled = true` in
  config *and* `--live` on the command line.
- `swing kill` writes `~/.swing/KILL`. While that file exists nothing is sent.
  It is a plain file on purpose — you can create it with `touch` at 3am.
- A dry-run `swing execute` opens **no** broker connection and fetches no
  quotes, so the three checks that need one (quote drift, reconciliation,
  equity match) print `SKIP` rather than a pass. Everything offline still runs.
- Orders are journalled as `pending` *before* they are sent and flipped to
  `open` after, so a crash mid-flight leaves a record. If something fails after
  Schwab accepted an order, the tool says the order **may be live** and tells
  you to check the app — it never says "not sent" when it does not know.
- Secrets live only in `config.toml` and the Schwab token file, both gitignored.
- Every guardrail (quote drift, order caps, trading hours, token age,
  reconciliation) has a test proving it blocks.

## Development

```bash
make install   # uv sync
make test      # uv run pytest -q   (the network is blocked inside tests)
make lint      # ruff check + ruff format --check
make fmt       # apply formatting and safe fixes
```

Tests never touch the network: an autouse fixture in `tests/conftest.py` makes
`socket.connect` raise, so an accidental live fetch fails loudly instead of
making the suite slow and flaky.

## Documentation

Deeper write-ups live in [`docs/`](docs/):

- [`docs/indicator-research.md`](docs/indicator-research.md) — what the
  literature actually supports, and what it does not
- [`docs/strategy-spec.md`](docs/strategy-spec.md) — the rule set, precisely
- [`docs/backtest-methodology.md`](docs/backtest-methodology.md) — walk-forward
  design, cost model, survivorship-bias haircut
- [`docs/schwab-setup.md`](docs/schwab-setup.md) — getting a Schwab developer app
  and a working token, what execution writes to the journal, and how to unstick
  an order row
- [`docs/ablation-results.md`](docs/ablation-results.md) — the current
  one-change-at-a-time sweep, regenerated by `scripts/ablations.py`
- [`CODE_AUDIT_REPORT.md`](CODE_AUDIT_REPORT.md) — the full code audit (67
  findings) that drove the remediation pass, kept as the audit trail

## Disclaimer

This is research tooling, not investment advice. It is not written or reviewed
by a licensed financial advisor. Past backtest results say nothing reliable
about future returns. You are responsible for every order that leaves your
account.
