# Work Log: Multi-Symbol Watchlist

วันที่: 2026-06-16

## สิ่งที่เพิ่ม

- ปรับ GitHub Actions ให้รันบอทหลายคู่ใน workflow เดียว
- เพิ่ม environment variable `SYMBOLS`
- เริ่มจาก 5 คู่สภาพคล่องสูงบน Kraken/USDT:
  - `BTC/USDT`
  - `ETH/USDT`
  - `SOL/USDT`
  - `XRP/USDT`
  - `ADA/USDT`

## เหตุผลที่เลือก 5 คู่นี้

- เป็นเหรียญหลักที่มีสภาพคล่องสูง
- มี volume และความนิยมสูงกว่าเหรียญเล็กส่วนใหญ่
- เหมาะกับการเริ่มเก็บสถิติแบบ dry-run ก่อนเพิ่มจำนวนคู่

## ผลกับระบบเดิม

- `bot_state.json` รองรับอยู่แล้ว เพราะแยก state ด้วย key `symbol|timeframe`
- `trade_journal.csv` รองรับอยู่แล้ว เพราะมีคอลัมน์ `symbol` และ `timeframe`
- Discord summary จะส่งแยกแต่ละ symbol
- Artifact `trading-bot-state` ยังเก็บ `bot_state.json` และ `trade_journal.csv` เหมือนเดิม

## หมายเหตุ

- การเพิ่มหลายคู่ไม่ได้การันตีกำไร แต่เพิ่มโอกาสเจอสัญญาณและเก็บสถิติเร็วขึ้น
- ควรดูว่า Discord แจ้งเตือนถี่เกินไปไหม
- ถ้าคู่ใดไม่มีบน Kraken หรือ API error ให้ดู log ใน GitHub Actions แล้วปรับ `SYMBOLS`
