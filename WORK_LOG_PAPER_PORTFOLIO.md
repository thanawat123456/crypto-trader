# Work Log: Paper Portfolio 300 USDT

วันที่: 2026-06-16

## สิ่งที่เพิ่ม

- เพิ่มพอร์ตจำลองสำหรับ `DRY-RUN`
- ตั้งทุนจำลองเริ่มต้น `300 USDT`
- เพิ่ม config หมวด `paper`
- เมื่อเกิด `BUY` ใน DRY-RUN บอทจะคำนวณจำนวนเหรียญจากเงินสดจำลองแทนการใช้ `trade_amount` ตายตัว
- เมื่อเกิด `SELL` บอทจะคืนเงินสดจำลองและคำนวณ realized PnL หลังหัก fee จำลอง
- บันทึกพอร์ตจำลองไว้ใน `bot_state.json` ใต้ key `__portfolio__`
- Summary ใน Discord แสดง `Paper cash` และ `Paper realized`
- ข้อความเริ่มบอทใน DRY-RUN จะแสดง `paper allocation` และเงินสดจำลองแทนขนาดเหรียญคงที่

## ค่า config ใหม่

```yaml
paper:
  enabled: true
  initial_cash: 300
  allocation_pct: 0.2
  fee: 0.001
```

## ความหมาย

- `initial_cash: 300` = ทุนจำลองเริ่มต้น 300 USDT
- `allocation_pct: 0.2` = ใช้เงินสดจำลองสูงสุด 20% ต่อไม้
- `fee: 0.001` = fee จำลอง 0.1% ต่อฝั่ง
- ถ้าเงินสดจำลองไม่พอ บอทจะข้าม BUY

## หมายเหตุ

- ระบบนี้ยังไม่ใช้เงินจริง
- เหมาะสำหรับพิสูจน์ก่อนว่า 300 USDT จำลองโตหรือขาดทุน
- ควรรอดูผลหลายวัน/หลายสิบไม้ก่อนเปิดเงินจริง
- PnL ยังเป็น simulation จากราคาที่บอทเห็น ไม่ใช่ fill จริงจาก exchange
