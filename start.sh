#!/usr/bin/env bash
# ── BerTech UAT Runner — Start ───────────────────────────────────────
# Usage: ./start.sh [port]
#
# First time setup:
#   pip install -r requirements.txt
#   playwright install chromium

set -e
PORT=${1:-8080}
echo "Starting BerTech UAT Runner on http://localhost:${PORT}"
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --reload
