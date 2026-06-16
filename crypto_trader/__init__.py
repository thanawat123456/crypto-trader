"""crypto_trader — ชุดเครื่องมือเทรดคริปโตด้วย Python

โมดูลหลัก:
  data       ดึงข้อมูลราคา (OHLCV) จาก exchange ผ่าน ccxt
  indicators อินดิเคเตอร์ทางเทคนิค (EMA, RSI, MACD, ...)
  strategy   สร้างสัญญาณซื้อ/ขายจากอินดิเคเตอร์
  backtest   ทดสอบกลยุทธ์ย้อนหลังบนข้อมูลในอดีต
  bot        ลูปเทรดอัตโนมัติ (paper / live)
  alerts     แจ้งเตือนสัญญาณ (console / Telegram)
"""

__version__ = "0.1.0"
