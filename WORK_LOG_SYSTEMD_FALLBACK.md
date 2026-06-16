# Work Log: Systemd Fallback Runner

วันที่: 2026-06-17

## ปัญหา

- GitHub Actions `schedule` ไม่ trigger อัตโนมัติ แม้ manual `Run workflow` ทำงาน
- ต้องมีวิธีรันบอทอัตโนมัติที่เสถียรกว่า GitHub schedule

## สิ่งที่เพิ่ม

- เพิ่ม `deploy/run-bot-loop.sh`
- ปรับ `deploy/crypto-trader.service` ให้เรียก script ใหม่แทนการรันแค่ `BTC/USDT`
- Script รันครบ 5 คู่:
  - `BTC/USDT`
  - `ETH/USDT`
  - `SOL/USDT`
  - `XRP/USDT`
  - `ADA/USDT`
- วนรอบทุก `1800` วินาที หรือ 30 นาที
- ใช้ `EXCHANGE_NAME=kraken` เป็นค่า environment ใน service

## วิธีใช้บน Oracle VPS

```bash
cd ~/crypto-trader
git pull
sudo cp deploy/crypto-trader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart crypto-trader
journalctl -u crypto-trader -f
```

## ทดสอบก่อนรันค้าง

```bash
cd ~/crypto-trader
RUN_ONCE=true EXCHANGE_NAME=kraken bash deploy/run-bot-loop.sh
```

## หมายเหตุ

- ถ้ายังไม่มี API key บอทยังเป็น `DRY-RUN` และใช้ paper portfolio 300 USDT
- วิธีนี้ไม่ต้องพึ่ง GitHub Actions schedule
- GitHub Actions manual run ยังใช้ได้เหมือนเดิม
