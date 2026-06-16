# Work Log: Performance Summary

วันที่: 2026-06-16

## สิ่งที่เพิ่ม

- เพิ่ม `summary_enabled` ใน config หมวด `bot`
- เพิ่มฟังก์ชันอ่าน `trade_journal.csv` เพื่อคำนวณ performance summary
- ให้บอทส่งสรุปหลังรันแต่ละรอบผ่าน `alerts.notify`
- Summary แสดงสถานะปัจจุบัน, จำนวน BUY/SELL, win rate, realized PnL, trade ล่าสุด, entry และ unrealized PnL ถ้ามี position เปิดอยู่

## ค่า config ใหม่

```yaml
bot:
  summary_enabled: true
```

## ตัวอย่างข้อความ summary

```text
📊 Summary | BTC/USDT 1h
สถานะ: ถือเงินสด
Trades: BUY=0 SELL=0 | Win rate=0.0%
Realized PnL≈0.00
ล่าสุด: ยังไม่มี trade
```

## หมายเหตุ

- ถ้ายังไม่มี BUY/SELL จะยังไม่มี `trade_journal.csv` แต่ summary ยังส่งได้
- `Realized PnL` มาจาก SELL ใน journal เท่านั้น
- `Unrealized PnL` จะแสดงเฉพาะตอนบอทถือ position และมี `entry_price`
- ตัวเลข PnL ยังเป็นค่าประมาณ ยังไม่รวม fee/slippage จริงจาก exchange
