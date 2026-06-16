# Work Log: Schedule Diagnostics

วันที่: 2026-06-16

## ปัญหา

- Workflow แจ้งเตือนตอนกด `Run workflow` เอง แต่ผู้ใช้ยังไม่เห็น run จาก schedule ทุก 30 นาที

## สิ่งที่ปรับ

- เปลี่ยน cron จาก `*/30 * * * *` เป็น `7,37 * * * *`
- เหตุผล: เลี่ยงนาที `00` และ `30` ที่ GitHub Actions มักมี load สูงและอาจ delay/drop ได้ง่ายกว่า
- เพิ่ม step `แสดง trigger ของ workflow` เพื่อ print:
  - `event_name`
  - `ref`
  - `sha`

## วิธีตรวจหลัง push

1. เข้า GitHub repo > `Actions`
2. เปิด workflow `crypto-trader-bot`
3. รอดู run ที่มี event เป็น `schedule`
4. รอบอัตโนมัติควรเกิดแถว ๆ นาที `07` และ `37` ของแต่ละชั่วโมง UTC
5. เปิด log step `แสดง trigger ของ workflow` แล้วดูว่า `event_name=schedule`

## ถ้ายังไม่รันเอง

- ตรวจว่า workflow ไม่ถูก disable ในหน้า Actions
- ตรวจว่า repository default branch คือ `main`
- ตรวจ `Settings > Actions > General` ว่า Actions เปิดใช้งานอยู่
- Schedule ของ GitHub Actions จะทำงานบน default branch เท่านั้น
