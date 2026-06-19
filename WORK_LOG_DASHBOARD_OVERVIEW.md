# Work Log: Dashboard Overview

วันที่: 2026-06-19

## สิ่งที่ปรับ

- เขียน `dashboard.py` ใหม่ให้ดูภาพรวม 11 เหรียญพร้อมกัน
- อ่าน watchlist จาก `crypto_trader.config` แทน hardcode 5 เหรียญเดิม
- รองรับข้อมูลเก่า `/USDT` และข้อมูลใหม่ `/USD`
- เพิ่มตารางรวมทุกเหรียญ แสดงสถานะ, ราคา, 1h/24h/7d movement, entry, amount, unrealized, realized, BUY/SELL, win rate, trade ล่าสุด
- เพิ่ม metric รวม: paper cash, equity ประมาณ, realized PnL, unrealized PnL, จำนวน position, win rate
- เพิ่มสรุปรายเหรียญเรียงตาม total PnL
- ยังมีกราฟราคา + จุด BUY/SELL และ trade history ล่าสุด
- ปุ่ม sync จาก GCP ดึง `bot_state.json`, `trade_journal.csv`, `validation_log.csv`

## วิธีเปิด

```bash
.venv/bin/streamlit run dashboard.py
```

หรือใช้ script เดิมถ้ามี:

```bash
./run-dashboard.sh
```

## หมายเหตุ

- ถ้าเปิด `ดึงราคาตลาดสด` dashboard จะเรียก Kraken เพื่อคำนวณราคา/ความเคลื่อนไหว
- ถ้า network หรือ Kraken มีปัญหา ตารางยังแสดง state/journal ได้ แต่ราคา live อาจเป็น `-`
- Dashboard เป็น read-only ไม่ส่งคำสั่งซื้อขาย
