#!/usr/bin/env bash
# One-command launcher for macOS/Linux: first run sets everything up, then starts the bot.
# Usage: bash start.sh      (control the bot from the Telegram chat: /go, /stop, /status)
set -e
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8

# A venv copied from another machine is broken: remove it, it is rebuilt below.
if [ -x .venv/bin/python ] && ! .venv/bin/python -c "import sys" >/dev/null 2>&1; then
    rm -rf .venv
fi

if [ ! -f .venv/.ready ]; then
    PY=$(command -v python3 || command -v python || true)
    if [ -z "$PY" ]; then
        echo "Python 3.10+ not found. Install it (https://www.python.org/downloads/ or your package manager) and run again."
        exit 1
    fi
    echo "First run on this machine: setting up. This takes a few minutes, once."
    [ -x .venv/bin/python ] || "$PY" -m venv .venv
    .venv/bin/python -m pip install -q -r requirements.txt
    # On a bare Linux server the browser may need system libraries: use `playwright install --with-deps chromium` (needs sudo).
    .venv/bin/python -m playwright install chromium
    touch .venv/.ready
fi

exec .venv/bin/python main.py
