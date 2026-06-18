#!/bin/bash
# ============================================================================
# Cloud-init user-data — รันอัตโนมัติตอน VM บูตครั้งแรก (Ubuntu, รันในสิทธิ์ root)
# ทำขั้นที่ 4-5 ของคู่มือให้: ติดตั้ง deps, clone repo, venv, ตั้ง systemd
# *ไม่* สตาร์ทบอท และ *ไม่* ใส่ API key (คุณ SSH เข้าไปใส่ Testnet key เองครั้งเดียว
#  แล้วค่อย start — เพื่อความปลอดภัย ไม่เอา secret ไว้ใน user-data/metadata)
# ดู log การทำงานได้ที่:  /var/log/deploy-bot.log
# ============================================================================
set -uxo pipefail
exec > /var/log/deploy-bot.log 2>&1
echo "=== deploy start: $(date) ==="

export DEBIAN_FRONTEND=noninteractive
REPO="https://github.com/thanawat123456/crypto-trader.git"
APP_USER="ubuntu"
APP_DIR="/home/${APP_USER}/crypto-trader"

# รอ apt lock ของ cloud-init ให้ว่างก่อน (กันชนกัน)
for i in $(seq 1 30); do
  fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break
  echo "waiting for apt lock... ($i)"; sleep 10
done

apt-get update -y
apt-get install -y python3-venv python3-pip git

# กัน user ปลายทางยังไม่ถูกสร้าง (เช่นบน GCP ช่วงบูตแรก guest agent ยังสร้างไม่เสร็จ)
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "$APP_USER"
fi

# clone repo (ในสิทธิ์ผู้ใช้ ubuntu)
if [ ! -d "$APP_DIR" ]; then
  sudo -u "$APP_USER" git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR" || { echo "ไม่พบโฟลเดอร์ repo"; exit 1; }

# venv + dependencies
sudo -u "$APP_USER" python3 -m venv .venv
sudo -u "$APP_USER" .venv/bin/pip install --upgrade pip
sudo -u "$APP_USER" .venv/bin/pip install -r requirements.txt

# สร้าง config จากตัวอย่าง (เว้น API key ไว้ให้คุณกรอกเอง, sandbox=true)
if [ -f config.example.yaml ] && [ ! -f config.yaml ]; then
  sudo -u "$APP_USER" cp config.example.yaml config.yaml
fi

# ติดตั้ง systemd service: enable ให้สตาร์ทเองหลังรีบูต แต่ยัง "ไม่ start"
# เพราะต้องใส่ Testnet API key ใน config.yaml ก่อน
if [ -f deploy/crypto-trader.service ]; then
  cp deploy/crypto-trader.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable crypto-trader
fi

# ฝากโน้ตขั้นต่อไปไว้ใน home
cat > /home/${APP_USER}/NEXT_STEPS.txt <<'NOTE'
ดีพลอยอัตโนมัติเสร็จแล้ว ✅  ขั้นต่อไป (ทำครั้งเดียว):
1) nano ~/crypto-trader/config.yaml   -> ใส่ Testnet api_key/api_secret, ตั้ง sandbox: true
2) ทดสอบ: cd ~/crypto-trader && .venv/bin/python -m crypto_trader bot BTC/USDT -t 1h --once
3) สตาร์ทรันค้าง 24/7: sudo systemctl start crypto-trader
4)  ssh -i ~/.ssh/id_rsa ubuntu@34.42.212.100
5) ดู log สด: journalctl -u crypto-trader -f
NOTE
chown ${APP_USER}:${APP_USER} /home/${APP_USER}/NEXT_STEPS.txt

echo "=== deploy done: $(date) ==="
