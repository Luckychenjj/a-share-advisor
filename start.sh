#!/usr/bin/env bash
# Start/restart the 小A顾问 (Xiao A Advisor) dashboard backend (analysis only).
# Configure with environment variables; defaults keep all data inside ./userdata (git-ignored).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${XIAO_A_PORT:-8768}"
PYTHON="${XIAO_A_PYTHON:-.venv/bin/python}"
pkill -f "xiao_a.cli dashboard --port ${PORT}" 2>/dev/null || true; sleep 1
export XIAO_A_DATA_DIR="${XIAO_A_DATA_DIR:-$PWD/userdata}"
export XIAO_A_PORTFOLIO_FILE="${XIAO_A_PORTFOLIO_FILE:-$XIAO_A_DATA_DIR/portfolio.json}"
export XIAO_A_AUDIT_FILE="${XIAO_A_AUDIT_FILE:-$XIAO_A_DATA_DIR/prediction_audit.jsonl}"
export PYTHONUNBUFFERED=1 TZ="${TZ:-Asia/Shanghai}"
mkdir -p "$XIAO_A_DATA_DIR"
nohup "$PYTHON" -m xiao_a.cli dashboard --port "$PORT" --no-browser >> service.log 2>&1 &
echo "started pid $! on 127.0.0.1:${PORT}"
