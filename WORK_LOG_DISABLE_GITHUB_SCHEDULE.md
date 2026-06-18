# Work Log: Disable GitHub Schedule

วันที่: 2026-06-18

## เหตุผล

- บอทถูกย้ายไปรันอัตโนมัติบน Google Cloud ด้วย `systemd` แล้ว
- ถ้ายังเปิด GitHub Actions schedule ไว้พร้อมกัน บอทจะรันซ้ำ 2 ที่
- การรันซ้ำทำให้ Discord ซ้ำ, state แยกกัน, journal แยกกัน และวิเคราะห์ paper portfolio ยาก

## สิ่งที่ปรับ

- ลบ `schedule` ออกจาก `.github/workflows/bot.yml`
- เก็บ `workflow_dispatch` ไว้ เพื่อให้ยังกด `Run workflow` ทดสอบเองได้

## ผลลัพธ์

- Google Cloud/systemd เป็นตัวรันอัตโนมัติหลัก
- GitHub Actions จะไม่รันเองตามเวลาแล้ว
- ถ้าต้องการทดสอบบน GitHub ยังเข้า `Actions` แล้วกด `Run workflow` ได้
