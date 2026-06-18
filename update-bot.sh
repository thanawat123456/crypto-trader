#!/usr/bin/env bash
# ============================================================================
# update-bot.sh — อัปเดตโค้ดบอทบน GCP VM หลังแก้โค้ด (รันบน Mac)
#   ใช้: ./update-bot.sh
#   (ต้อง push โค้ดขึ้น GitHub ก่อน — ดูขั้นตอนใน README)
#
#   ปลอดภัย: config.yaml / bot_state.json / trade_journal.csv เป็น gitignore
#   → git pull ไม่แตะ → ค่า Discord/Kraken และพอร์ตจำลองไม่หาย
# ============================================================================
set -euo pipefail

VM_IP="${VM_IP:-34.42.212.100}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
APP_DIR="${APP_DIR:-/home/ubuntu/crypto-trader}"

echo ">> อัปเดตบอทบน $VM_IP ..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "ubuntu@${VM_IP}" "
set -e
cd '$APP_DIR'
echo '--- ดึงโค้ดใหม่จาก GitHub ---'
git pull --ff-only
echo '--- อัปเดต dependencies (ถ้ามีเปลี่ยน) ---'
.venv/bin/pip install -q -r requirements.txt
echo '--- restart service ---'
sudo systemctl restart crypto-trader
sleep 5
echo '--- สถานะ ---'
systemctl is-active crypto-trader
journalctl -u crypto-trader -n 4 --no-pager
"
echo "✅ อัปเดตเสร็จแล้ว"
