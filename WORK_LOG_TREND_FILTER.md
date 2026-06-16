# Work Log: Trend Filter

วันที่: 2026-06-16

## สิ่งที่เพิ่ม

- เพิ่ม trend filter ใน `crypto_trader/strategy.py`
- เพิ่ม config `trend_filter_enabled` และ `trend_ema_period` โดยปิดไว้เป็นค่าเริ่มต้น
- ใช้ filter เดียวกันกับ `ema_cross`, `rsi`, และ `macd`
- ถ้าเปิด filter บอทจะถือ long เฉพาะตอนราคาปิดอยู่เหนือ EMA ระยะยาว

## ค่า config ใหม่

```yaml
strategy:
  trend_filter_enabled: false
  trend_ema_period: 200
```

## เหตุผล

- ลดโอกาสซื้อในตลาดขาลงชัดเจน
- ช่วยให้กลยุทธ์ long-only ไม่ฝืนเทรดตอนโครงสร้างตลาดไม่เอื้อ
- ใช้ logic เดียวกันทั้ง backtest, signal, และ bot

## หมายเหตุ

- Filter นี้อาจทำให้จำนวนสัญญาณ BUY ลดลง
- ค่าเริ่มต้นปิดไว้ก่อน เพราะต้องเทียบ backtest หลายช่วงเวลาก่อนเปิดใช้งานจริง
- ไม่ได้การันตีกำไร แต่ช่วยเพิ่มชั้นป้องกันความเสี่ยง
- ควรดูผล backtest และ summary ใน Discord ต่อเนื่องก่อนใช้เงินจริง
