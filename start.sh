#!/usr/bin/env bash
# Linux / WSL launcher. On a Mac, double-click start.command instead.
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install it with your package manager first."
  exit 1
fi

[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import fastapi, anthropic" >/dev/null 2>&1; then
  echo "Installing components…"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
fi

PYTHONPATH=src exec python -m companies_research "$@"
