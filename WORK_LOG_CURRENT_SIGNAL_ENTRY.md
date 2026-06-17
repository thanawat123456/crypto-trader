# Work Log: Current Signal Entry

วันที่: 2026-06-17

## ปัญหา

- บอทรันหลายรอบแล้วค่าดูนิ่ง เพราะยังไม่มี `BUY/SELL`
- ตรวจล่าสุดพบว่า `BTC/USDT`, `ETH/USDT`, `SOL/USDT`, `XRP/USDT` มี position ของกลยุทธ์เป็น `1` อยู่แล้ว
- แต่ `latest_signal` คืน `HOLD` เพราะ logic เดิมซื้อเฉพาะตอนเกิด cross ใหม่จาก `0 -> 1`
- ถ้าบอทเริ่มหลังจากตลาดเข้าสถานะ long ไปแล้ว จะไม่เข้าไม้จนกว่าจะเกิด cross รอบใหม่

## สิ่งที่เพิ่ม

- เพิ่ม config `bot.enter_on_current_signal`
- ใน `DRY-RUN` ถ้า state ยังไม่ถือ แต่กลยุทธ์ปัจจุบันบอกว่าควรถือ (`desired_position == 1`) บอทจะจำลอง `BUY`
- Journal จะบันทึก reason เป็น `current_signal`
- จำกัด behavior นี้ไว้กับ `DRY-RUN` เพื่อไม่ให้เปิดเงินจริงแล้วซื้อทันทีโดยไม่ตั้งใจ

## ค่า config ใหม่

```yaml
bot:
  enter_on_current_signal: true
```

## ผลที่คาดหวัง

- Paper portfolio จะเริ่มมี trade เร็วขึ้นเมื่อกลยุทธ์ปัจจุบันอยู่ฝั่ง long แล้ว
- Discord จะเห็น `BUY | reason=current_signal` หากเงื่อนไขเข้าไม้ตรง
- ทำให้สามารถเริ่มเก็บ `trade_journal.csv` และวัดผล paper portfolio ได้จริง
