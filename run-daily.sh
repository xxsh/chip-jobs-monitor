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

if [[ -z "${NVIDIA_CHROMIUM_PATH:-}" ]]; then
  for browser in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"; do
    if [[ -x "$browser" ]]; then
      export NVIDIA_CHROMIUM_PATH="$browser"
      break
    fi
  done
fi

cd "$SCRIPT_DIR"

# Hard cap on the pipeline. A hung Playwright fetch or codex call must not pin
# the whole run (and the cron job that exec's this) until the gateway's ~60min
# global timeout. Override with DAILY_MAX_SECONDS.
DAILY_MAX_SECONDS="${DAILY_MAX_SECONDS:-1200}"

env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  "$PYTHON_BIN" "$SCRIPT_DIR/daily.py" &
daily_pid=$!

(
  sleep "$DAILY_MAX_SECONDS"
  if kill -0 "$daily_pid" 2>/dev/null; then
    echo "run-daily: daily.py exceeded ${DAILY_MAX_SECONDS}s; terminating" >&2
    kill -TERM "$daily_pid" 2>/dev/null
    sleep 10
    kill -KILL "$daily_pid" 2>/dev/null
  fi
) &
watchdog_pid=$!

rc=0
wait "$daily_pid" || rc=$?

# daily.py finished on its own — stop the idle watchdog.
kill "$watchdog_pid" 2>/dev/null || true
wait "$watchdog_pid" 2>/dev/null || true

exit "$rc"
