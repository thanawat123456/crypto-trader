# รันบอท 24/7 บน Oracle Cloud (ฟรีตลอด)

Oracle Cloud ให้ VM ฟรีตลอด (Always Free) — เราจะตั้ง VM ที่ **region สิงคโปร์/ญี่ปุ่น**
(ไม่ใช่สหรัฐฯ) เพื่อให้ Binance ไม่บล็อก แล้วรันบอทเป็น service ที่รันค้างตลอด

> ⚠️ ต้องใช้บัตรเครดิต/เดบิตยืนยันตัวตน (Oracle **ไม่ตัดเงิน** สำหรับ Always Free)
> ⚠️ เริ่มที่ Testnet (เงินปลอม) เสมอ

---

## ขั้นที่ 1 — สมัคร Oracle Cloud + สร้าง VM

1. สมัครที่ https://www.oracle.com/cloud/free/
   - **Home Region เลือก Singapore หรือ Tokyo** (สำคัญ! เลือกแล้วเปลี่ยนไม่ได้ — ห้ามเลือก US)
2. เข้า Console → เมนู → **Compute → Instances → Create Instance**
   - **Image:** Canonical Ubuntu (24 หรือใหม่กว่า)
   - **Shape:** เลือกตัวที่มีป้าย **"Always Free-eligible"** (เช่น VM.Standard.E2.1.Micro หรือ Ampere A1)
   - **SSH keys:** เลือก "Generate a key pair for me" → **ดาวน์โหลด private key** เก็บไว้
3. กด Create แล้วรอจน status เป็น **Running** → จด **Public IP address**

---

## ขั้นที่ 2 — เปิดให้ SSH เข้าได้ (ปกติเปิดให้อยู่แล้ว)

ถ้าต่อไม่ติด: ไป **VCN → Security List → Add Ingress Rule** อนุญาต TCP port 22

---

## ขั้นที่ 3 — SSH เข้า VM

จากเครื่อง Mac ของคุณ (แก้ path private key และ IP ให้ตรง):

```bash
chmod 400 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<PUBLIC_IP>
```

---

## ขั้นที่ 4 — ติดตั้งบอทบน VM

รันทีละชุดบน VM:

```bash
# ติดตั้ง python + git
sudo apt update && sudo apt install -y python3-venv python3-pip git

# โคลน repo ของคุณ
git clone https://github.com/thanawat123456/crypto-trader.git
cd crypto-trader

# สร้าง venv + ติดตั้ง dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# สร้าง config + ใส่ API key (อยู่บน VM เท่านั้น ไม่ขึ้น GitHub)
cp config.example.yaml config.yaml
nano config.yaml
```

ใน `config.yaml` แก้ส่วน `exchange:` ให้เป็น:
```yaml
exchange:
  name: binance
  api_key: "<API Key จาก Testnet>"
  api_secret: "<Secret Key จาก Testnet>"
  sandbox: true        # true = Testnet เงินปลอม
```
(กด `Ctrl+O`, `Enter`, `Ctrl+X` เพื่อเซฟใน nano)

ทดสอบว่าทำงาน (Binance ไม่บล็อกเพราะ VM อยู่สิงคโปร์):
```bash
.venv/bin/python -m crypto_trader bot BTC/USDT -t 1h --once
```
ควรเห็น `🤖 เริ่มบอท [PAPER]` และ **ไม่มี error 451**

---

## ขั้นที่ 5 — ตั้งให้รันค้าง 24/7 (systemd)

```bash
# คัดลอก service file เข้าระบบ
sudo cp deploy/crypto-trader.service /etc/systemd/system/

# เปิดใช้งาน + สตาร์ท
sudo systemctl daemon-reload
sudo systemctl enable crypto-trader
sudo systemctl start crypto-trader
```

บอทจะรันค้างตลอด รีสตาร์ทเองถ้าดับ และสตาร์ทเองหลัง VM รีบูต ✅

---

## คำสั่งดูแลบอท (บน VM)

```bash
sudo systemctl status crypto-trader      # ดูว่าทำงานอยู่ไหม
journalctl -u crypto-trader -f           # ดู log สดๆ
tail -f ~/crypto-trader/bot.log          # ดู log ของบอท
sudo systemctl restart crypto-trader     # รีสตาร์ท (หลังแก้ config)
sudo systemctl stop crypto-trader        # หยุด
```

---

## เปลี่ยน symbol / timeframe / กลยุทธ์

แก้ที่ `/etc/systemd/system/crypto-trader.service` บรรทัด `ExecStart=`
เช่นเปลี่ยนเป็น `ETH/USDT -t 4h` แล้ว:
```bash
sudo systemctl daemon-reload && sudo systemctl restart crypto-trader
```
ส่วนค่ากลยุทธ์ (fast/slow ฯลฯ) แก้ใน `~/crypto-trader/config.yaml` แล้ว restart

---

## ⚠️ เปลี่ยนเป็นเงินจริง (เมื่อมั่นใจแล้วเท่านั้น)

1. สร้าง API key จาก Binance บัญชีจริง (**เปิดเฉพาะ Spot Trading ห้ามเปิด Withdraw**)
2. แก้ `config.yaml`: ใส่ key จริง + `sandbox: false`
3. `sudo systemctl restart crypto-trader`
4. เริ่มด้วยเงินก้อนเล็ก + `trade_amount` น้อยๆ

> การเทรดอัตโนมัติด้วยเงินจริงมีความเสี่ยงขาดทุน คุณรับผิดชอบผลที่เกิดขึ้นเองทั้งหมด
