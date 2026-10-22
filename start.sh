#!/usr/bin/env bash
# Start the 小A顾问 (Xiao A Advisor) dashboard backend (analysis only) — self-healing and idempotent.
#
#   ./start.sh              ensure it is running: no-op if already healthy, otherwise
#                           (re)build the Python env if broken, then start it
#   ./start.sh --restart    force a restart (env is still checked/repaired first)
#   ./start.sh --check      only report: env OK? backend healthy? (exit 0 = healthy)
#   ./start.sh --stop       stop the instance on $XIAO_A_PORT
#
# The env check runs `python -c "import xiao_a.cli, pytdx, baostock"` inside .venv. If that fails
# (venv missing, its python symlink dangling after a machine move/upgrade, packages lost), a fresh
# venv is built next to it from requirements-lock.txt (uv if available, else python -m venv + pip),
# verified, and swapped in. If no python >= 3.11 exists on the machine, uv installs one into
# ./.runtime/python so it lives with the project files.
#
# Env: XIAO_A_PORT (8768) XIAO_A_VENV (./.venv) XIAO_A_PYTHON (interpreter to build the venv from)
#      XIAO_A_LOG (./service.log) XIAO_A_DATA_DIR (./userdata) XIAO_A_PORTFOLIO_FILE XIAO_A_AUDIT_FILE
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${XIAO_A_PORT:-8768}"
VENV="${XIAO_A_VENV:-$APP_DIR/.venv}"
LOG="${XIAO_A_LOG:-$APP_DIR/service.log}"
LOCK_FILE="$APP_DIR/requirements-lock.txt"
MODE="${1:-ensure}"
HEALTH_URL="http://127.0.0.1:${PORT}/api/runtime"
PROC_PATTERN="xiao_a.cli dashboard --port ${PORT}( |$)"

say() { echo "[xiao-a $(date '+%F %T')] $*"; }

healthy() { curl -fsS -m 5 -o /dev/null "$HEALTH_URL" 2>/dev/null; }

venv_ok() {
  local py="$1/bin/python"
  [ -x "$py" ] || return 1
  "$py" - "$APP_DIR" <<'PY' >/dev/null 2>&1
import os, sys
assert sys.version_info >= (3, 11)
import xiao_a, xiao_a.cli, pytdx, baostock  # noqa: F401
src = os.path.realpath(os.path.join(sys.argv[1], "src"))
assert os.path.realpath(xiao_a.__file__).startswith(src + os.sep), xiao_a.__file__
PY
}

pick_python() {
  local c v
  for c in "${XIAO_A_PYTHON:-}" python3.13 python3.12 python3.11 python3 \
           "$APP_DIR"/.runtime/python/*/bin/python3; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    v=$("$c" -c 'import sys; print(int(sys.version_info >= (3, 11)))' 2>/dev/null) || continue
    [ "$v" = 1 ] && { command -v "$c" 2>/dev/null || echo "$c"; return 0; }
  done
  if command -v uv >/dev/null 2>&1; then
    say "no python >= 3.11 found; installing one with uv into .runtime/python" >&2
    UV_PYTHON_INSTALL_DIR="$APP_DIR/.runtime/python" uv python install 3.13 >&2 || return 1
    UV_PYTHON_INSTALL_DIR="$APP_DIR/.runtime/python" uv python find 3.13 && return 0
  fi
  return 1
}

install_into() {  # $1 = venv dir, $2 = python used to create it
  local new="$1" base="$2"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q --relocatable --python "$base" "$new" || return 1
    if [ -f "$LOCK_FILE" ] && uv pip install -q --python "$new/bin/python" -r "$LOCK_FILE" -e "$APP_DIR"; then
      return 0
    fi
    say "locked install failed; retrying unpinned"
    uv pip install -q --python "$new/bin/python" -e "$APP_DIR[fallback-data]"
  else
    "$base" -m venv "$new" || return 1
    "$new/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
    if [ -f "$LOCK_FILE" ] && "$new/bin/python" -m pip install -q -r "$LOCK_FILE" -e "$APP_DIR"; then
      return 0
    fi
    say "locked install failed; retrying unpinned"
    "$new/bin/python" -m pip install -q -e "$APP_DIR[fallback-data]"
  fi
}

ensure_venv() {
  venv_ok "$VENV" && return 0
  say "python env at $VENV is missing or broken; rebuilding"
  local base new="$VENV.new"
  base="$(pick_python)" || { say "ERROR: no usable python >= 3.11 and no uv to install one"; return 1; }
  say "building from $base ($("$base" -V 2>&1))"
  rm -rf "$new"
  if ! install_into "$new" "$base" || ! venv_ok "$new"; then
    say "ERROR: rebuilt env failed verification; left at $new for inspection"
    return 1
  fi
  if [ -e "$VENV" ] || [ -L "$VENV" ]; then
    rm -rf "$VENV".broken-*
    mv "$VENV" "$VENV.broken-$(date +%Y%m%d%H%M%S)"
  fi
  mv "$new" "$VENV"
  venv_ok "$VENV" || { say "ERROR: env failed verification after swap"; return 1; }
  say "python env rebuilt OK"
}

stop_backend() {
  pkill -f "$PROC_PATTERN" 2>/dev/null || true
  for _ in $(seq 1 20); do pgrep -f "$PROC_PATTERN" >/dev/null || return 0; sleep 0.5; done
  pkill -9 -f "$PROC_PATTERN" 2>/dev/null || true; sleep 0.5
}

start_backend() {
  export XIAO_A_DATA_DIR="${XIAO_A_DATA_DIR:-$APP_DIR/userdata}"
  export XIAO_A_PORTFOLIO_FILE="${XIAO_A_PORTFOLIO_FILE:-$XIAO_A_DATA_DIR/portfolio.json}"
  export XIAO_A_AUDIT_FILE="${XIAO_A_AUDIT_FILE:-$XIAO_A_DATA_DIR/prediction_audit.jsonl}"
  export PYTHONUNBUFFERED=1 TZ="${TZ:-Asia/Shanghai}"
  mkdir -p "$XIAO_A_DATA_DIR"
  cd "$APP_DIR" || return 1
  nohup setsid "$VENV/bin/python" -m xiao_a.cli dashboard --port "$PORT" --no-browser >> "$LOG" 2>&1 < /dev/null 9>&- &
  local pid=$!
  for _ in $(seq 1 60); do
    healthy && { say "started pid $pid, healthy on 127.0.0.1:$PORT"; return 0; }
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  say "ERROR: backend did not become healthy on port $PORT; last log lines:"
  tail -n 20 "$LOG"
  return 1
}

case "$MODE" in
  --check)
    if venv_ok "$VENV"; then say "env OK"; else say "env BROKEN"; fi
    if healthy; then say "backend healthy on $PORT"; exit 0; else say "backend NOT responding on $PORT"; exit 1; fi ;;
  --stop) stop_backend; say "stopped"; exit 0 ;;
  ensure|--restart) ;;
  *) echo "usage: $0 [--restart|--check|--stop]" >&2; exit 2 ;;
esac

exec 9>"$APP_DIR/.start-${PORT}.lock"
flock -w 600 9 || { say "ERROR: another start.sh is still running"; exit 1; }

ensure_venv || exit 1
if [ "$MODE" = ensure ] && healthy; then
  say "already running and healthy on 127.0.0.1:$PORT"; exit 0
fi
stop_backend
start_backend
