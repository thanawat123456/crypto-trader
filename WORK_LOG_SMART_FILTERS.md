# Work Log: Smart Entry Filters

วันที่: 2026-06-18

## เหตุผล

- Paper portfolio ขาดทุนจากการเข้า long ในช่วงตลาดอ่อน
- ต้องเพิ่มข้อมูลประกอบก่อนเปิด BUY ใหม่ แทนการใช้ EMA cross 1h อย่างเดียว

## สิ่งที่เพิ่ม

- เพิ่ม `ATR` indicator เพื่อวัด volatility
- เพิ่ม smart filter ก่อน BUY ใหม่
- ดึงข้อมูล 4h ของเหรียญนั้นเองจาก exchange
- เช็กว่าเหรียญอยู่เหนือ EMA200 ของตัวเอง
- เช็ก RSI ว่าไม่อ่อนเกินไปและไม่ร้อนเกินไป
- เช็ก ATR/ราคา ว่า volatility ไม่สูงเกินไป
- เพิ่ม cooldown หลัง SELL ขาดทุน เพื่อไม่กลับเข้า symbol เดิมเร็วเกินไป

## ค่า config ใหม่

```yaml
smart_filter:
  enabled: true
  timeframe: 4h
  ema_period: 200
  rsi_period: 14
  rsi_min: 45
  rsi_max: 70
  atr_period: 14
  max_atr_pct: 0.06
  loss_cooldown_hours: 24
```

## ลำดับก่อนเปิด BUY

1. BTC 4h ต้อง bullish ตาม `market_filter`
2. Symbol นั้นต้องอยู่เหนือ EMA200 บน 4h
3. RSI ต้องอยู่ระหว่าง `45` ถึง `70`
4. ATR/ราคาต้องไม่เกิน `6%`
5. Symbol นั้นต้องไม่เพิ่ง SELL ขาดทุนใน 24 ชั่วโมงล่าสุด

## หมายเหตุ

- ไม่มี logic ใดรับประกันว่าจะไม่ขาดทุน
- เป้าหมายคือให้บอทเลือกจังหวะน้อยลงและลดไม้คุณภาพต่ำ
- ถ้า filter เข้มเกินไป บอทอาจไม่เข้าเทรดเป็นเวลานาน ซึ่งดีกว่าฝืนเข้าในตลาดแย่
