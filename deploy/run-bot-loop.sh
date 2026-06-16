#!/usr/bin/env bash
# รันบอทหลาย symbol วนทุก 30 นาที สำหรับ systemd/Oracle VPS
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
TIMEFRAME="${TIMEFRAME:-1h}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-1800}"
RUN_ONCE="${RUN_ONCE:-false}"

SYMBOLS=("BTC/USDT" "ETH/USDT" "SOL/USDT" "XRP/USDT" "ADA/USDT")

cd "$APP_DIR"

while true; do
  echo "[$(date -u '+%F %T UTC')] starting multi-symbol cycle"
  for symbol in "${SYMBOLS[@]}"; do
    echo "[$(date -u '+%F %T UTC')] running $symbol $TIMEFRAME"
    "$PYTHON" -m crypto_trader bot "$symbol" -t "$TIMEFRAME" --once
  done
  echo "[$(date -u '+%F %T UTC')] cycle complete"

  if [ "$RUN_ONCE" = "true" ]; then
    break
  fi
  sleep "$INTERVAL_SECONDS"
done
