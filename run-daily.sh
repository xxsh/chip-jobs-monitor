#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/reports"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]] && [[ -x "$SCRIPT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
fi
if [[ -z "$PYTHON_BIN" ]] && command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "python interpreter not found (expected $SCRIPT_DIR/.venv/bin/python)" >&2
  exit 1
fi

export MYSQL_USER="${MYSQL_USER:-root}"
export MYSQL_SOCKET_PATH="${MYSQL_SOCKET_PATH:-/tmp/mysql.sock}"
export MYSQL_DATABASE="${MYSQL_DATABASE:-nvidia_jobs_monitor}"

cd "$SCRIPT_DIR"
exec env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY "$PYTHON_BIN" "$SCRIPT_DIR/daily.py"
