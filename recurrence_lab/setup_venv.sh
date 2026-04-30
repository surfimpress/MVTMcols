#!/usr/bin/env bash
# One-shot venv setup for the recurrence lab. Idempotent — re-running
# is fine (will reuse the existing venv and re-pip if requirements
# changed).
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d venv ]; then
    python3 -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done. To use:"
echo "  source recurrence_lab/venv/bin/activate"
echo "  python embed.py --years 1947"
