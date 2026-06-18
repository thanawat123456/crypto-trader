#!/usr/bin/env bash
# ============================================================================
# run-dashboard.sh — เปิด dashboard บน Mac (ดึงข้อมูลจาก VM มาแสดง)
#   ใช้: ./run-dashboard.sh   แล้วเปิดเบราว์เซอร์ตามลิงก์ที่ขึ้น (มักเป็น localhost:8501)
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
VM_IP="${VM_IP:-34.42.212.100}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
APP_DIR=/home/ubuntu/crypto-trader

[ -x "$PY" ] || { echo "ไม่พบ .venv — สร้างด้วย: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

# ติดตั้ง deps ของ dashboard ถ้ายังไม่มี (ครั้งแรกครั้งเดียว)
if ! "$PY" -c "import streamlit, plotly" 2>/dev/null; then
  echo ">> ติดตั้ง streamlit + plotly (ครั้งแรก)..."
  .venv/bin/pip install -q -r requirements-dashboard.txt
fi

# ดึงข้อมูลล่าสุดจาก VM (ครั้งแรก — ในแอปมีปุ่ม refresh ให้กดเพิ่มได้)
echo ">> ดึงข้อมูลจาก VM ($VM_IP)..."
mkdir -p dashboard_data
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  "ubuntu@${VM_IP}:${APP_DIR}/bot_state.json" dashboard_data/ 2>/dev/null \
  && echo "   ✅ bot_state.json" || echo "   ⚠ ดึง bot_state.json ไม่ได้"
scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  "ubuntu@${VM_IP}:${APP_DIR}/trade_journal.csv" dashboard_data/ 2>/dev/null \
  && echo "   ✅ trade_journal.csv" || echo "   ⚠ ยังไม่มี trade_journal.csv (ปกติถ้ายังไม่มีเทรด)"

echo ">> เปิด dashboard..."
VM_IP="$VM_IP" .venv/bin/streamlit run dashboard.py
