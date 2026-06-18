#!/usr/bin/env bash
# ============================================================================
# check-profit.sh — ดูผลกำไร/ขาดทุนของบอทบน GCP VM (รันบน Mac)
#   ใช้: ./check-profit.sh
# ============================================================================
set -euo pipefail

VM_IP="${VM_IP:-34.42.212.100}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
APP_DIR="${APP_DIR:-/home/ubuntu/crypto-trader}"

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "ubuntu@${VM_IP}" "
cd '$APP_DIR'
.venv/bin/python - <<'PY'
import json, os, csv
from datetime import datetime

INIT = 300.0  # ทุนจำลองเริ่มต้น

# --- อ่าน portfolio + positions จาก bot_state.json ---
st = {}
if os.path.exists('bot_state.json'):
    st = json.load(open('bot_state.json', encoding='utf-8'))
pf = st.get('__portfolio__', {})
cash = float(pf.get('cash', INIT))
realized = float(pf.get('realized_pnl', 0.0))

print('=' * 44)
print(' 📊 ผลบอท (paper / เงินจำลอง)')
print('=' * 44)
print(f' ทุนเริ่มต้น     : {INIT:,.2f} USDT')
print(f' เงินสดคงเหลือ   : {cash:,.2f} USDT')
print(f' กำไรสะสม (real) : {realized:+,.2f} USDT  ({realized/INIT*100:+.2f}%)')

# --- positions ที่ถืออยู่ ---
open_pos = [(k, v) for k, v in st.items()
            if k != '__portfolio__' and isinstance(v, dict) and v.get('in_position')]
print('-' * 44)
if open_pos:
    print(f' ไม้ที่ถืออยู่ตอนนี้ : {len(open_pos)}')
    for k, v in open_pos:
        print(f'   • {k}  เข้าที่ {v.get(\"entry_price\")}  จำนวน {v.get(\"amount\")}')
else:
    print(' ไม้ที่ถืออยู่ตอนนี้ : ไม่มี (ถือเงินสดทั้งหมด)')

# --- ประวัติเทรดจาก journal ---
print('-' * 44)
rows = []
if os.path.exists('trade_journal.csv'):
    with open('trade_journal.csv', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
sells = [r for r in rows if (r.get('side') or '').upper() == 'SELL']
wins = sum(1 for r in sells if float(r.get('pnl') or 0) > 0)
print(f' จำนวนไม้ทั้งหมด : BUY={sum(1 for r in rows if (r.get(\"side\") or \"\").upper()==\"BUY\")} '
      f'SELL={len(sells)}')
if sells:
    wr = wins / len(sells) * 100
    print(f' Win rate        : {wr:.0f}%  ({wins}/{len(sells)})')
    print(' 5 ไม้ปิดล่าสุด:')
    for r in rows[-5:]:
        if (r.get('side') or '').upper() == 'SELL':
            pnl = float(r.get('pnl') or 0)
            print(f'   {r.get(\"timestamp\",\"\")[:16]}  {r.get(\"symbol\")}  PnL={pnl:+.2f}  ({r.get(\"reason\")})')
else:
    print(' ยังไม่มีไม้ที่ปิด (ขายออก) — รอสัญญาณ')
print('=' * 44)
PY
"
