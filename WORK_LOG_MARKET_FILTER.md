# Work Log: BTC 4h Market Filter

วันที่: 2026-06-17

## เหตุผล

- Paper portfolio รอบแรกขาดทุน เพราะกลยุทธ์ `ema_cross` เข้า long หลายตัวในช่วงตลาดอ่อนตัว
- ต้องเพิ่มข้อมูลภาพรวมตลาดก่อนเปิด BUY ใหม่ ไม่ใช่ดูสัญญาณรายเหรียญอย่างเดียว

## สิ่งที่เพิ่ม

- เพิ่ม config `market_filter`
- ก่อนเปิด BUY ใหม่ บอทจะดึง `BTC/USDT` timeframe `4h`
- คำนวณ EMA200 จากแท่งที่ปิดแล้ว
- อนุญาต BUY เฉพาะเมื่อ BTC 4h close ล่าสุดอยู่เหนือ EMA200
- ถ้า BTC 4h ต่ำกว่า EMA200 หรือดึงข้อมูลไม่ได้ บอทจะข้าม BUY และส่ง Discord ว่า `ข้าม BUY`

## ค่า config ใหม่

```yaml
market_filter:
  enabled: true
  symbol: BTC/USDT
  timeframe: 4h
  ema_period: 200
```

## ผลที่คาดหวัง

- ลดการเปิด long ตอนตลาดหลักไม่สนับสนุน
- ลดจำนวนไม้ในช่วง downtrend/sideway หนัก
- ยังปล่อยให้ position ที่ถืออยู่ใช้ stop loss / take profit / signal exit ตามเดิม

## หมายเหตุ

- Filter นี้ไม่ได้รับประกันกำไร
- เป็นชั้นป้องกันก่อนเข้าไม้ ไม่ใช่สัญญาณทำกำไรด้วยตัวเอง
- ควรดูผลจาก paper portfolio ต่อเนื่องหลังเปิด filter
