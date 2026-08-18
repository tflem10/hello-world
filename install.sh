#!/usr/bin/env bash
# Bootstrap swing on a clean machine: install uv if needed, then sync the venv.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  echo "    uv not found — installing from https://astral.sh/uv/install.sh"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # The installer drops uv in ~/.local/bin (or $CARGO_HOME/bin on older setups).
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "    uv still is not on PATH. Add ~/.local/bin to your PATH and re-run." >&2
  exit 1
fi

echo "    uv $(uv --version | awk '{print $2}')"

echo "==> Installing Python and dependencies (uv sync)"
uv sync

echo "==> Verifying the CLI"
uv run swing --version

if [ ! -f config.toml ]; then
  echo "==> Creating config.toml from config.example.toml"
  cp config.example.toml config.toml
  echo "    config.toml is gitignored. Put your real settings there."
else
  echo "==> config.toml already exists; leaving it alone"
fi

cat <<'NEXT'

Done. Next steps:

  1. Edit config.toml            — set [account] equity and risk, and an alert
                                    channel under [alerts] (ntfy is easiest).
  2. make test                   — the suite runs fully offline.
  3. uv run swing backtest       — nothing emits picks until this passes the gate.
  4. uv run swing report         — see how the backtest actually did.
  5. uv run swing scan --dry-run — build a scan report without notifying anyone.

Optional, later:
  uv run swing auth              — connect a Schwab account (read-only until you
                                   also set [execution] enabled = true).
  uv run swing schedule install  — run the scan at 17:30 and confirm at 09:00 ET.

NEXT
