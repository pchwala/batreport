#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -d "$SCRIPT_DIR/.venv" ]]; then
    echo "Error: virtual environment not found." >&2
    echo "Run ./install.sh first." >&2
    exit 1
fi

exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/batreport.py"
