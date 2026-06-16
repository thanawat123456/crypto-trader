# crypto_trader — ชุดเครื่องมือเทรดคริปโตด้วย Python

เครื่องมือครบชุดสำหรับเทรดคริปโต: ดูข้อมูล/กราฟ, backtest กลยุทธ์, บอทเทรดอัตโนมัติ และแจ้งเตือนสัญญาณ
ใช้ [ccxt](https://github.com/ccxt/ccxt) ต่อกับ exchange (Binance, OKX, Bybit ฯลฯ)

> ⚠️ **คำเตือน:** เครื่องมือนี้เพื่อการศึกษา ผลตอบแทนในอดีต (backtest) ไม่รับประกันอนาคต
> การเทรดมีความเสี่ยงสูง **ทดสอบด้วย paper trading ให้ดีก่อนใช้เงินจริงเสมอ**

## ติดตั้ง

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # แก้ค่าตามต้องการ
```

## ใช้งาน (4 ฟังก์ชันหลัก)

รันด้วย `python -m crypto_trader <คำสั่ง> [SYMBOL] [options]`
(ถ้าไม่ใส่ใน venv ใช้ `.venv/bin/python -m crypto_trader ...`)

### 1. ดูข้อมูล / กราฟ
```bash
python -m crypto_trader fetch BTC/USDT -t 1h -l 500   # ดึงราคา + บันทึก CSV
python -m crypto_trader chart BTC/USDT -t 4h          # วาดกราฟ Price+EMA+RSI เป็น PNG
```

### 2. Backtest กลยุทธ์
```bash
python -m crypto_trader backtest BTC/USDT -t 1h --strategy ema_cross
python -m crypto_trader backtest ETH/USDT -t 4h --strategy rsi
```
แสดงผลตอบแทน, จำนวนเทรด, Max Drawdown, Sharpe และบันทึกกราฟ equity

### 3. แจ้งเตือนสัญญาณ
```bash
python -m crypto_trader signal BTC/USDT -t 1h         # เช็คสัญญาณล่าสุด BUY/SELL/HOLD
```
เปิด Telegram ใน `config.yaml` เพื่อรับแจ้งเตือนเข้ามือถือ

### 4. บอทเทรดอัตโนมัติ
```bash
python -m crypto_trader bot BTC/USDT -t 1h --once     # รันรอบเดียวทดสอบ
python -m crypto_trader bot BTC/USDT -t 1h            # รันวนลูปตาม poll_seconds
```
โหมดทำงานขึ้นกับ `config.yaml`:
- **dry-run** — ไม่ใส่ API key → คำนวณสัญญาณ + แจ้งเตือนเฉยๆ (ปลอดภัยสุด)
- **paper** — ใส่ key + `sandbox: true` → เทรดด้วยเงินปลอม
- **live** — `sandbox: false` → **เงินจริง** ต้องพิมพ์ยืนยันก่อน

## ตัวเลือก (options)
| flag | ความหมาย | ค่าเริ่มต้น |
|------|----------|------------|
| `SYMBOL` | คู่เหรียญ เช่น `BTC/USDT` | จาก config |
| `-t, --timeframe` | `1m 5m 15m 1h 4h 1d` | `1h` |
| `-l, --limit` | จำนวนแท่งเทียน | `500` |
| `--strategy` | `ema_cross` \| `rsi` \| `macd` | จาก config |
| `-c, --config` | path ไฟล์ config | `config.yaml` |

## กลยุทธ์ที่มีให้ (`crypto_trader/strategy.py`)
- **ema_cross** — EMA เร็วตัด EMA ช้า
- **rsi** — ซื้อตอน oversold / ขายตอน overbought
- **macd** — MACD ตัด signal line

เพิ่มกลยุทธ์ใหม่ได้โดยเขียนฟังก์ชันที่คืน `pd.Series` (1=ถือ, 0=ว่าง) แล้วลงทะเบียนใน `STRATEGIES`

## โครงสร้างโปรเจกต์
```
crypto_trader/
  config.py       โหลด/รวมค่า config
  data.py         ดึง OHLCV ผ่าน ccxt
  indicators.py   EMA, SMA, RSI, MACD, Bollinger
  strategy.py     สร้างสัญญาณ
  backtest.py     เครื่องมือ backtest + เมตริก
  bot.py          ลูปเทรดอัตโนมัติ
  alerts.py       แจ้งเตือน console / Telegram
  plotting.py     วาดกราฟ
  __main__.py     CLI
```
