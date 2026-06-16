# Work Log: Risk Management Upgrade

วันที่: 2026-06-16

## สิ่งที่เพิ่ม

- เพิ่มหมวด `risk` ใน config default และ `config.example.yaml`
- เพิ่ม `stop_loss_pct` สำหรับออกจากไม้เมื่อราคาลงจากจุดเข้า
- เพิ่ม `take_profit_pct` สำหรับออกจากไม้เมื่อราคาขึ้นจากจุดเข้า
- เพิ่ม `max_position_pct` เพื่อจำกัดขนาด position ใน backtest
- เพิ่ม `journal_path` สำหรับกำหนดไฟล์บันทึกประวัติเทรด
- เพิ่มไฟล์ `crypto_trader/journal.py` สำหรับบันทึก trade journal เป็น CSV
- ปรับ `bot_state.json` ให้รองรับ `entry_price` และ `amount` นอกจาก `in_position`
- ปรับ `bot` ให้ขายออกเมื่อถึง stop loss หรือ take profit แม้ยังไม่มีสัญญาณ SELL
- ปรับ `backtest` ให้ใช้ `position_size`, `max_position_pct`, `stop_loss_pct`, และ `take_profit_pct`
- ปรับ GitHub Actions cache ให้เก็บ `trade_journal.csv` ข้ามรอบพร้อม `bot_state.json`

## ค่า config ใหม่

```yaml
risk:
  stop_loss_pct: 0.02
  take_profit_pct: 0.04
  max_position_pct: 1.0
  journal_path: trade_journal.csv
```

## ความหมาย

- `stop_loss_pct: 0.02` = ถ้าราคาลงจากราคาเข้า 2% ให้ขายออก
- `take_profit_pct: 0.04` = ถ้าราคาขึ้นจากราคาเข้า 4% ให้ขายออก
- `max_position_pct: 1.0` = backtest ลงได้สูงสุด 100% ของพอร์ต
- `journal_path` = ไฟล์ CSV ที่เก็บประวัติ BUY/SELL, ราคา, จำนวน, เหตุผล, PnL โดยประมาณ

## หมายเหตุสำคัญ

- ฟีเจอร์นี้ช่วยควบคุมความเสี่ยง แต่ไม่รับประกันกำไร
- ควรทดสอบใน `DRY-RUN` หรือ testnet ก่อนใช้เงินจริง
- PnL ใน journal เป็นค่าประมาณจากราคาเข้า/ออก ยังไม่ได้รวม fee และ slippage จริงจาก exchange
- ถ้าจะใช้เงินจริง ควรเริ่มด้วยขนาดเล็กมากและติดตาม log/Discord ทุกครั้ง
