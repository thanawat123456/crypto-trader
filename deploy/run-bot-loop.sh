#!/usr/bin/env bash
# รันบอทหลาย symbol วนทุก 30 นาที สำหรับ systemd/Oracle VPS
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"
TIMEFRAME="${TIMEFRAME:-1h}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-1800}"
RUN_ONCE="${RUN_ONCE:-false}"
REPORT_EVERY_HOURS="${REPORT_EVERY_HOURS:-24}"   # ส่งสรุปผลเข้า Discord ทุกกี่ชม. (0 = ปิด)
VALIDATE_EVERY_HOURS="${VALIDATE_EVERY_HOURS:-168}"  # ตรวจสุขภาพกลยุทธ์ทุกกี่ชม. (168=สัปดาห์, 0 = ปิด)
SCAN_EVERY_HOURS="${SCAN_EVERY_HOURS:-72}"           # สแกนเหรียญ momentum เข้า Discord ทุกกี่ชม. (72=3วัน, 0 = ปิด)
SCAN_QUOTE="${SCAN_QUOTE:-USD}"                      # quote สำหรับสแกน (Kraken=USD)
STATUS_EVERY_HOURS="${STATUS_EVERY_HOURS:-24}"       # รายงานสถานะ (ทำไมเทรด/ไม่เทรด) ทุกกี่ชม. (0 = ปิด)

SYMBOLS=("BTC/USD" "ETH/USD" "SOL/USD" "XRP/USD" "ADA/USD")

cd "$APP_DIR"

while true; do
  echo "[$(date -u '+%F %T UTC')] starting multi-symbol cycle"
  for symbol in "${SYMBOLS[@]}"; do
    echo "[$(date -u '+%F %T UTC')] running $symbol $TIMEFRAME"
    "$PYTHON" -m crypto_trader bot "$symbol" -t "$TIMEFRAME" --once
  done
  echo "[$(date -u '+%F %T UTC')] cycle complete"

  # ส่งสรุปผลเข้า Discord วันละครั้ง (self-throttle ด้วย marker ใน state)
  if [ "$REPORT_EVERY_HOURS" != "0" ]; then
    "$PYTHON" -m crypto_trader report --every-hours "$REPORT_EVERY_HOURS" || true
  fi

  # ตรวจสุขภาพกลยุทธ์รายสัปดาห์ (เตือนถ้าเสื่อม — ไม่ปรับ risk เอง)
  if [ "$VALIDATE_EVERY_HOURS" != "0" ]; then
    "$PYTHON" -m crypto_trader validate --every-hours "$VALIDATE_EVERY_HOURS" || true
  fi

  # สแกนเหรียญ momentum แรง (สภาพคล่องดี) เข้า Discord รายสัปดาห์ — read-only ไม่เทรด
  if [ "$SCAN_EVERY_HOURS" != "0" ]; then
    "$PYTHON" -m crypto_trader scan -t 4h --quote "$SCAN_QUOTE" --notify \
      --every-hours "$SCAN_EVERY_HOURS" || true
  fi

  # รายงานสถานะ watchlist (ทำไมเทรด/ไม่เทรด + momentum) — read-only
  if [ "$STATUS_EVERY_HOURS" != "0" ]; then
    "$PYTHON" -m crypto_trader status --notify --every-hours "$STATUS_EVERY_HOURS" || true
  fi

  if [ "$RUN_ONCE" = "true" ]; then
    break
  fi
  sleep "$INTERVAL_SECONDS"
done
