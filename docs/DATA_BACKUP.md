# Data Backup — สำรองข้อมูลบอท

สรุปวิธีสำรองข้อมูลบอทจาก VM มาเก็บที่ Mac + วิธีหยุด/กู้คืน

---

## ข้อมูลที่สำรอง (จาก VM `~/crypto-trader/`)
| ไฟล์ | เก็บอะไร |
|------|---------|
| `bot_state.json` | สถานะ position รายเหรียญ + พอร์ตจำลอง (cash/PnL) + marker |
| `trade_journal.csv` | ประวัติทุกไม้ที่เทรด (BUY/SELL/PnL) — ข้อมูลผลงานหลัก |
| `validation_log.csv` | ประวัติ validate รายสัปดาห์ |
| `bot.log` | log การทำงานของบอท |

## เก็บที่ไหน (local บน Mac เท่านั้น — ไม่ใช่ iCloud)
- `backups/latest/` — สำเนาล่าสุดเสมอ
- `backups/snapshots/<YYYYMMDD-HHMMSS>/` — ประวัติย้อนหลัง (เก็บ 30 ชุดล่าสุด)
- `backups/backup.log` — log การ backup อัตโนมัติ

> หมายเหตุ: `Downloads/` ไม่ซิงค์ iCloud → backup อยู่ในเครื่องล้วน ไม่กินพื้นที่ cloud
> `backups/` อยู่ใน .gitignore → ไม่ขึ้น GitHub

---

## การทำงานอัตโนมัติ (launchd)
- รันทุกวัน **21:00 น.** ผ่าน launchd agent: `~/Library/LaunchAgents/com.cryptobot.backup.plist`
- รันเฉพาะตอน **Mac เปิดอยู่** — ถ้าตอน 21:00 Mac หลับ จะรันครั้งเดียวตอนเปิดเครื่องครั้งถัดไป
- เก็บ snapshot ย้อนหลัง 30 ชุด แล้วลบเก่าทิ้งอัตโนมัติ

## คำสั่งที่ใช้บ่อย
```bash
./backup.sh                          # สำรองเดี๋ยวนี้เลย (manual)
ls backups/snapshots/                # ดูประวัติ backup ทั้งหมด
cat backups/backup.log               # ดู log การ backup อัตโนมัติ
ls -lh backups/latest/               # ดูไฟล์สำเนาล่าสุด
```

---

## ▶️ เปิด / ⏸️ หยุด auto-backup

**หยุดชั่วคราว (unload):**
```bash
launchctl unload ~/Library/LaunchAgents/com.cryptobot.backup.plist
```

**เปิดใหม่ (load):**
```bash
launchctl load ~/Library/LaunchAgents/com.cryptobot.backup.plist
```

**ปิดถาวร (ลบทิ้ง):**
```bash
launchctl unload ~/Library/LaunchAgents/com.cryptobot.backup.plist
rm ~/Library/LaunchAgents/com.cryptobot.backup.plist
```

**เช็คว่ายังเปิดอยู่ไหม:**
```bash
launchctl list | grep cryptobot      # ถ้าเจอ = ยังทำงาน
```

> ปรับเวลา/จำนวนที่เก็บ: แก้ `KEEP` ใน `backup.sh` หรือ `StartCalendarInterval` ใน plist

---

## 🔁 กู้คืนข้อมูลกลับขึ้น VM (ถ้า VM พัง/สร้างใหม่)
```bash
# คัดลอกไฟล์สำเนาล่าสุดกลับขึ้น VM
scp -i ~/.ssh/id_rsa backups/latest/bot_state.json \
    ubuntu@<VM_IP>:/home/ubuntu/crypto-trader/
scp -i ~/.ssh/id_rsa backups/latest/trade_journal.csv \
    ubuntu@<VM_IP>:/home/ubuntu/crypto-trader/

# แล้ว restart บอท
ssh -i ~/.ssh/id_rsa ubuntu@<VM_IP> "sudo systemctl restart crypto-trader"
```

---

## ⚠️ ข้อจำกัด (รู้ไว้)
- backup อยู่บน Mac เครื่องเดียว — **ถ้าลบโฟลเดอร์ trade หรือ Mac พัง → backup หายด้วย**
- รันเฉพาะตอน Mac เปิด (best-effort รายวัน ไม่ใช่เป๊ะทุกวัน)
- เพียงพอสำหรับ paper data — ถ้าวันหลังมีประวัติเทรดสำคัญ ค่อยพิจารณาเก็บ cloud เพิ่ม
