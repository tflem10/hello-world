#!/usr/bin/env bash
# One-shot installer for the swing trading pick system.
# Safe to re-run; creates .venv, installs deps, seeds config.toml.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> creating virtualenv (.venv, python 3.11+)"
uv venv --python 3.11 .venv

echo "==> installing swing + dependencies"
uv pip install --python .venv/bin/python -e '.[dev]'

if [ ! -f config.toml ]; then
  echo "==> seeding config.toml from config.example.toml"
  cp config.example.toml config.toml
  chmod 600 config.toml
  echo "    edit config.toml before running live anything"
fi

mkdir -p "$HOME/.swing"
chmod 700 "$HOME/.swing"

echo
echo "done. next:"
echo "  source .venv/bin/activate"
echo "  swing --help"
echo "  make test"
