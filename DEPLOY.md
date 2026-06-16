# รันบอทแจ้งเตือน 24/7 ฟรีด้วย GitHub Actions (ทาง A)

บอทจะรันบน server ของ GitHub ทุก 30 นาที โดยไม่ต้องเปิดคอม ฟรีสนิท ไม่ต้องผูกบัตร

- **Exchange:** Kraken (เข้าถึงได้จาก IP ของ GitHub — Binance โดนบล็อก 451)
- **โหมด:** DRY-RUN — บอทเช็คสัญญาณแล้ว **ส่งแจ้งเตือน BUY/SELL เข้า Discord** ให้คุณ
  (ไม่กดออเดอร์เอง คุณเปิดแอป exchange กดเทรดเองตามสัญญาณ)
- **ไม่ต้องใช้ API key** เพราะแค่ดึงข้อมูลสาธารณะ + แจ้งเตือน

> 💡 อยากให้บอทกดออเดอร์อัตโนมัติด้วย → ต้องรันบน VPS (ดู `DEPLOY_ORACLE.md`)

---

## ขั้นที่ 1 — เอาโปรเจกต์ขึ้น GitHub (ถ้ายังไม่ได้ทำ)

```bash
cd ~/Downloads/trade
git add .
git commit -m "crypto trading bot"
git push
```

---

## ขั้นที่ 2 — สร้าง Discord Webhook (ฟรี 2 นาที)

1. เปิด Discord → ไปที่เซิร์ฟเวอร์ของคุณ (ถ้าไม่มี กด `+` สร้างใหม่ฟรี)
2. คลิกขวาที่ช่อง (channel) ที่อยากให้แจ้งเตือนเข้า → **Edit Channel**
3. **Integrations → Webhooks → New Webhook**
4. ตั้งชื่อ (เช่น "Trade Bot") → กด **Copy Webhook URL**
   - URL หน้าตาประมาณ `https://discord.com/api/webhooks/123.../abc...`

---

## ขั้นที่ 3 — ใส่ Webhook เป็น Secret ใน GitHub

ในหน้า repo: **Settings → Secrets and variables → Actions → New repository secret**

| ชื่อ Secret | ค่า |
|------------|-----|
| `DISCORD_WEBHOOK_URL` | Webhook URL ที่ copy มา |

---

## ขั้นที่ 4 — เปิดใช้งานและทดสอบ

1. ไปแท็บ **Actions** ของ repo → ถ้ามีแถบให้ enable workflows ให้กดเปิด
2. เลือก workflow **crypto-trader-bot** → กด **Run workflow** (ทดสอบทันที)
3. ดู log ว่ารันผ่าน และเช็ก Discord ว่ามีข้อความเด้งเข้ามาไหม

หลังจากนี้บอทจะรันเอง **ทุก 30 นาที** อัตโนมัติ — พอมีสัญญาณ BUY/SELL จะเด้งเข้า Discord

---

## ปรับแต่ง

- **เปลี่ยนเหรียญ/timeframe:** แก้บรรทัด `run:` ใน `.github/workflows/bot.yml`
  เช่น `python -m crypto_trader bot ETH/USDT -t 4h --once`
- **เปลี่ยนความถี่:** แก้ `cron:` (เช่น `*/15 * * * *` = ทุก 15 นาที)
- **เปลี่ยน exchange:** แก้ `EXCHANGE_NAME:` (เช่น `coinbase`) — ต้องเป็นเจ้าที่ IP สหรัฐฯ เข้าได้

---

## หยุดบอท

แท็บ Actions → workflow → ปุ่ม `•••` → **Disable workflow**

---

## เปลี่ยนไปเทรดอัตโนมัติเต็มตัว (อนาคต)

เมื่อ Oracle Cloud มี capacity ว่าง → ทำตาม `DEPLOY_ORACLE.md`
โค้ดเดิมทั้งหมดใช้ต่อได้ รวมถึง Discord (แค่เปลี่ยน exchange กลับเป็น Binance + ใส่ API key)
