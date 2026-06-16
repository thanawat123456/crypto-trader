# รันบอท 24/7 ฟรีด้วย GitHub Actions

บอทจะรันบน server ของ GitHub อัตโนมัติทุก 30 นาที โดยไม่ต้องเปิดคอมของคุณ
ฟรี ไม่ต้องผูกบัตรเครดิต

> ⚠️ เริ่มจาก **Testnet (เงินปลอม)** เสมอ อย่าเพิ่งใช้เงินจริงจนกว่าจะมั่นใจ

---

## ขั้นที่ 1 — เอาโปรเจกต์ขึ้น GitHub

```bash
cd ~/Downloads/trade
git init
git add .
git commit -m "crypto trading bot"
```

1. ไปสร้าง repo ใหม่ที่ https://github.com/new
   - **เลือก Private** (กันคนอื่นเห็นการตั้งค่าของคุณ)
2. ทำตามคำสั่งที่ GitHub ให้ เพื่อ push เช่น:
```bash
git remote add origin https://github.com/<ชื่อคุณ>/<ชื่อ-repo>.git
git branch -M main
git push -u origin main
```

> ✅ ปลอดภัย: `config.yaml`, `bot_state.json`, `bot.log` ถูกใส่ใน `.gitignore` แล้ว
> API key จะ **ไม่ถูก push** ขึ้น GitHub — เราจะใส่เป็น "Secret" แทน (ขั้นที่ 3)

---

## ขั้นที่ 2 — สมัคร Binance Testnet (เงินปลอม)

1. เข้า https://testnet.binance.vision → ล็อกอินด้วย GitHub
2. กด **Generate HMAC_SHA256 Key**
3. เก็บ **API Key** และ **Secret Key** ไว้ (Secret จะเห็นครั้งเดียว)

---

## ขั้นที่ 3 — ใส่ Secrets ใน GitHub

ในหน้า repo: **Settings → Secrets and variables → Actions → New repository secret**
เพิ่มทีละตัว:

| ชื่อ Secret | ค่า |
|------------|-----|
| `EXCHANGE_API_KEY` | API Key จาก Testnet |
| `EXCHANGE_API_SECRET` | Secret Key จาก Testnet |
| `EXCHANGE_SANDBOX` | `true` |

(ถ้าอยากได้แจ้งเตือนเข้า Telegram เพิ่ม `TELEGRAM_BOT_TOKEN` และ `TELEGRAM_CHAT_ID` ด้วย)

---

## ขั้นที่ 4 — เปิดใช้งานและทดสอบ

1. ไปแท็บ **Actions** ของ repo → ถ้ามีแถบให้กด "I understand... enable workflows" ให้กดเปิด
2. เลือก workflow **crypto-trader-bot** → กด **Run workflow** (รันเองทันทีเพื่อทดสอบ)
3. กดเข้าไปดู log ว่าบอทรันผ่าน เห็นข้อความ `🤖 เริ่มบอท [PAPER]`

หลังจากนี้บอทจะรันเอง **ทุก 30 นาที** อัตโนมัติ

---

## ปรับแต่ง

- **เปลี่ยนเหรียญ/timeframe**: แก้บรรทัด `run:` ใน `.github/workflows/bot.yml`
  เช่น `python -m crypto_trader bot ETH/USDT -t 4h --once`
- **เปลี่ยนความถี่**: แก้ `cron:` (เช่น `*/15 * * * *` = ทุก 15 นาที)
- **ดูสถานะปัจจุบัน**: เปิดไฟล์ `bot_state.json` ใน repo (บอท commit กลับมาเอง)
- **ดูประวัติการเทรด**: ดู log ในแต่ละ run ที่แท็บ Actions

---

## หยุดบอท

ไปแท็บ Actions → workflow → ปุ่ม `•••` → **Disable workflow**

---

## ⚠️ เปลี่ยนเป็นเงินจริง (เมื่อมั่นใจแล้วเท่านั้น)

1. สมัคร Binance บัญชีจริง + สร้าง API key (**เปิดเฉพาะ Spot Trading ห้ามเปิด Withdraw**)
2. แก้ Secret: `EXCHANGE_API_KEY`/`EXCHANGE_API_SECRET` เป็นของบัญชีจริง, `EXCHANGE_SANDBOX` = `false`
3. เริ่มด้วยเงินก้อนเล็กที่รับการขาดทุนได้ และตั้ง `trade_amount` ใน `config.yaml` ให้น้อย

> การเทรดอัตโนมัติด้วยเงินจริงมีความเสี่ยงขาดทุน คุณรับผิดชอบผลที่เกิดขึ้นเองทั้งหมด
