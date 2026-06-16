# Work Log: GitHub Actions Artifacts

วันที่: 2026-06-16

## สิ่งที่เพิ่ม

- เพิ่มขั้นตอน `อัปโหลดสถานะและ journal` ใน `.github/workflows/bot.yml`
- ใช้ `actions/upload-artifact@v4`
- อัปโหลดไฟล์ `bot_state.json` และ `trade_journal.csv` เป็น artifact ชื่อ `trading-bot-state`
- ตั้ง `if-no-files-found: warn` เพื่อไม่ให้ workflow fail ถ้ายังไม่มี trade journal

## วิธีดูไฟล์บน GitHub

1. เข้า GitHub repo
2. ไปที่แท็บ `Actions`
3. เปิด run ล่าสุดของ workflow `crypto-trader-bot`
4. เลื่อนลงไปส่วน `Artifacts`
5. ดาวน์โหลด `trading-bot-state`

## หมายเหตุ

- `trade_journal.csv` จะมีเฉพาะเมื่อบอทเกิด `BUY` หรือ `SELL`
- ถ้ารอบนั้นมีแค่ `ไม่มีสัญญาณใหม่` อาจมีเฉพาะ `bot_state.json` หรือไม่มี journal
- Artifact เหมาะสำหรับเปิดดูผลจากแต่ละ workflow run ส่วน cache ใช้สำหรับให้ workflow กู้สถานะข้ามรอบ
