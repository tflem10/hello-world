#!/usr/bin/env bash
# swing first-run — everything between a fresh install and a gate verdict.
#
#   ./scripts/first-run.sh 500        # 500 = your real account equity, in dollars
#
# Safe to re-run. The backfill is incremental, so an interrupted run picks up
# where it left off rather than starting over. Everything is teed to a log you
# can paste back for review.
set -uo pipefail

cd "$(dirname "$0")/.."

# -- preconditions -----------------------------------------------------------
if [ ! -x .venv/bin/swing ]; then
  echo "no .venv found — run ./install.sh first" >&2
  exit 1
fi
SWING=.venv/bin/swing

EQUITY="${1:-}"
if [ -z "$EQUITY" ]; then
  echo "usage: $0 <equity-in-dollars>    e.g. $0 500" >&2
  echo "  every share count is sized off this number, so it must be real." >&2
  exit 2
fi
if ! printf '%s' "$EQUITY" | grep -Eq '^[0-9]+(\.[0-9]+)?$' || [ "${EQUITY%%.*}" -lt 1 ]; then
  echo "equity must be a positive number (got '$EQUITY')" >&2
  exit 2
fi

mkdir -p reports
LOG="reports/first-run-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }
started=$(date +%s)

echo "swing first-run   equity=\$$EQUITY   log=$LOG"

# -- 1. equity ---------------------------------------------------------------
step "1/5  set [account] equity = $EQUITY"
.venv/bin/python - "$EQUITY" <<'PY'
import re, sys, pathlib
equity = float(sys.argv[1])
p = pathlib.Path("config.toml")
if not p.exists():
    sys.exit("config.toml missing — run ./install.sh")
text = p.read_text()
# Replace `equity = ...` only inside the [account] table, not anywhere else.
def sub_account(match):
    body = re.sub(r"(?m)^(\s*equity\s*=\s*).*$", rf"\g<1>{equity}", match.group(0), count=1)
    return body
new = re.sub(r"(?ms)^\[account\].*?(?=^\[|\Z)", sub_account, text, count=1)
if new == text:
    print(f"  warning: no equity line changed — check [account] in config.toml")
p.write_text(new)
PY
grep -A1 '^\[account\]' config.toml | head -2

# -- 2. universe -------------------------------------------------------------
step "2/5  refresh index constituents (needs network)"
echo "the shipped S&P 400/600 lists are partial seeds; this replaces them with"
echo "the real membership so the backfill downloads the right set once."
if ! $SWING universe --fetch; then
  echo "!! universe --fetch failed — continuing with the shipped seed lists."
  echo "   (not fatal: you get a smaller universe, and can re-run this later.)"
fi
$SWING universe --show 8

# -- 3. backfill -------------------------------------------------------------
step "3/5  backfill daily history  (SLOW: 15-45 min)"
echo "warnings about symbols returning no data are expected and by design:"
echo "delisted or renamed tickers are logged, skipped, and remembered."
if ! $SWING data --backfill; then
  echo "!! backfill reported an error. Status below shows what did land;"
  echo "   re-run this script to resume — the cache is incremental."
fi
$SWING data --status

# -- 4. the gate -------------------------------------------------------------
step "4/5  walk-forward backtest — THE gate  (20-40 min)"
$SWING backtest --walk-forward
wf_rc=$?

step "5/5  ETF-only backtest — the survivorship-free floor"
$SWING backtest --etf-only
etf_rc=$?

# -- done --------------------------------------------------------------------
elapsed=$(( ($(date +%s) - started) / 60 ))
step "done in ${elapsed} min"
echo "walk-forward exit=$wf_rc   etf-only exit=$etf_rc"
echo
echo "reports written to:"
ls -dt reports/*/ 2>/dev/null | head -4
echo
echo "gate verdict:"
$SWING scan --dry-run 2>&1 | sed -n '1,12p' || true
echo
echo "Full log: $LOG"
echo "Paste the walk-forward headline + per-window table back for review."
