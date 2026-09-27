#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
else
    echo "Python was not found on PATH. Install Python 3.10+ from https://www.python.org/downloads/"
    exit 1
fi

"$PYTHON_BIN" main.py "$@"
