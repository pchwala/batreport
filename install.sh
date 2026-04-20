#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Creating virtual environment..."
python3 -m venv .venv

echo "Upgrading pip..."
.venv/bin/pip install --quiet --upgrade pip

echo "Installing dependencies..."
.venv/bin/pip install --quiet -r requirements.txt

echo ""
echo "Done. Run the app with:  ./run.sh"
