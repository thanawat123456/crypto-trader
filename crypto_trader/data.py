"""ดึงข้อมูลราคา OHLCV จาก exchange ผ่าน ccxt"""
from __future__ import annotations

import pandas as pd

try:
    import ccxt
except ImportError:  # pragma: no cover
    ccxt = None


def make_exchange(cfg: dict):
    """สร้าง object exchange จาก config (ใช้ทั้งดึงข้อมูลและส่งคำสั่ง)"""
    if ccxt is None:
        raise ImportError("ยังไม่ได้ติดตั้ง ccxt — รัน: pip install -r requirements.txt")

    ex_cfg = cfg["exchange"]
    klass = getattr(ccxt, ex_cfg["name"], None)
    if klass is None:
        raise ValueError(f"ccxt ไม่รู้จัก exchange ชื่อ '{ex_cfg['name']}'")

    exchange = klass(
        {
            "apiKey": ex_cfg.get("api_key") or None,
            "secret": ex_cfg.get("api_secret") or None,
            "enableRateLimit": True,
        }
    )
    if ex_cfg.get("sandbox"):
        # โหมด paper trading — เงินปลอม
        try:
            exchange.set_sandbox_mode(True)
        except Exception:
            pass
    return exchange


def fetch_ohlcv(exchange, symbol: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame:
    """ดึงแท่งเทียนและคืนเป็น DataFrame (index = เวลา UTC)

    คอลัมน์: open, high, low, close, volume
    """
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    return df.astype(float)


def latest_price(exchange, symbol: str) -> float:
    """ราคาล่าสุด (last trade)"""
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker["last"])
