#!/usr/bin/env bash
# ============================================================================
# backup.sh — สำรองข้อมูลบอทจาก VM มาเก็บที่ Mac (กันข้อมูลหายถ้า VM มีปัญหา)
#   ใช้: ./backup.sh   (หรือให้ launchd รันอัตโนมัติรายวัน — ดู install-backup-cron)
#
#   เก็บที่:  backups/latest/         = สำเนาล่าสุด
#            backups/snapshots/<วันเวลา>/ = ประวัติย้อนหลัง (เก็บ KEEP ชุดล่าสุด)
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

VM_IP="${VM_IP:-34.42.212.100}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
APP_DIR=/home/ubuntu/crypto-trader
KEEP="${KEEP:-30}"            # เก็บ snapshot ย้อนหลังกี่ชุด
FILES=(bot_state.json trade_journal.csv validation_log.csv bot.log)

stamp="$(date '+%Y%m%d-%H%M%S')"
snap="backups/snapshots/$stamp"
mkdir -p backups/latest "$snap"

echo ">> สำรองข้อมูลจาก $VM_IP ($stamp)"
got=0
for f in "${FILES[@]}"; do
  if scp -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
       "ubuntu@${VM_IP}:${APP_DIR}/$f" "$snap/" 2>/dev/null; then
    cp -f "$snap/$f" backups/latest/ 2>/dev/null || true
    echo "  ✅ $f"
    got=1
  else
    echo "  ⚠ ข้าม $f (ยังไม่มีบน VM)"
  fi
done

if [ "$got" -eq 0 ]; then
  rmdir "$snap" 2>/dev/null || true
  echo "❌ ดึงข้อมูลไม่ได้เลย (VM ล่ม/เน็ตหลุด?)"
  exit 1
fi

# เก็บแค่ KEEP ชุดล่าสุด ลบของเก่าทิ้ง
ls -1dt backups/snapshots/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
echo "💾 เสร็จ → $snap (เก็บย้อนหลังสูงสุด $KEEP ชุด)"
