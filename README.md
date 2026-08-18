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
`config.example.toml`). Local state is one JSON journal plus a `KILL` file
under `~/.swing/`.

## Safety

- Execution needs **two** independent switches: `execution.enabled = true` in
  config *and* `--live` on the command line.
- `swing kill` writes `~/.swing/KILL`. While that file exists nothing is sent.
  It is a plain file on purpose — you can create it with `touch` at 3am.
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
  and a working token

## Disclaimer

This is research tooling, not investment advice. It is not written or reviewed
by a licensed financial advisor. Past backtest results say nothing reliable
about future returns. You are responsible for every order that leaves your
account.
