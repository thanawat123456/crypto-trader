"""กลยุทธ์ — แปลงราคา/อินดิเคเตอร์เป็นสัญญาณ

แต่ละกลยุทธ์คืน Series ของ "position" ที่ align กับ df:
    1 = ควรถือเหรียญ (long)
    0 = ควรถือเงินสด (flat)

จากนั้น backtest/bot จะดูจุดที่ position เปลี่ยน 0->1 เป็น BUY และ 1->0 เป็น SELL
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


def ema_cross(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """ซื้อเมื่อ EMA เร็วตัดขึ้นเหนือ EMA ช้า, ขายเมื่อตัดลง"""
    s = cfg["strategy"]
    fast = ind.ema(df["close"], s["fast"])
    slow = ind.ema(df["close"], s["slow"])
    return (fast > slow).astype(int)


def rsi_strategy(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """ซื้อเมื่อ RSI หลุดลงต่ำกว่า oversold แล้วเด้งกลับ, ขายเมื่อเกิน overbought"""
    s = cfg["strategy"]
    r = ind.rsi(df["close"], s["rsi_period"])
    pos = pd.Series(0, index=df.index)
    holding = 0
    for i in range(len(r)):
        val = r.iloc[i]
        if pd.isna(val):
            pos.iloc[i] = holding
            continue
        if holding == 0 and val < s["rsi_oversold"]:
            holding = 1
        elif holding == 1 and val > s["rsi_overbought"]:
            holding = 0
        pos.iloc[i] = holding
    return pos


def macd_strategy(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """ซื้อเมื่อ MACD ตัดขึ้นเหนือ signal line, ขายเมื่อตัดลง"""
    s = cfg["strategy"]
    macd_line, signal_line, _ = ind.macd(df["close"], s["fast"], s["slow"])
    return (macd_line > signal_line).astype(int)


STRATEGIES = {
    "ema_cross": ema_cross,
    "rsi": rsi_strategy,
    "macd": macd_strategy,
}


def get_position(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """เรียกใช้กลยุทธ์ตามชื่อใน config"""
    name = cfg["strategy"]["name"]
    fn = STRATEGIES.get(name)
    if fn is None:
        raise ValueError(f"ไม่รู้จักกลยุทธ์ '{name}' (มี: {list(STRATEGIES)})")
    return fn(df, cfg)


def latest_signal(df: pd.DataFrame, cfg: dict) -> str:
    """ดูสัญญาณ ณ แท่งล่าสุด: 'BUY' | 'SELL' | 'HOLD'"""
    pos = get_position(df, cfg)
    if len(pos) < 2:
        return "HOLD"
    prev, now = pos.iloc[-2], pos.iloc[-1]
    if prev == 0 and now == 1:
        return "BUY"
    if prev == 1 and now == 0:
        return "SELL"
    return "HOLD"
