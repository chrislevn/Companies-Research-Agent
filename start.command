#!/bin/bash
# Double-click this file on a Mac to start the agent.
# It installs what it needs the first time, then opens your browser.

cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "  Python is not installed yet."
  echo "  Download it from https://www.python.org/downloads/ , install it,"
  echo "  then double-click this file again."
  echo
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "  First run — setting things up. This takes a minute or two…"
  python3 -m venv .venv || exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import fastapi, anthropic" >/dev/null 2>&1; then
  echo "  Installing components…"
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt || {
    echo "  Installation failed. Please send the messages above to whoever set this up."
    read -r -p "Press Enter to close."
    exit 1
  }
fi

PYTHONPATH=src python -m companies_research

echo
read -r -p "The agent has stopped. Press Enter to close this window."
