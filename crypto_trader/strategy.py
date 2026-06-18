"""กลยุทธ์ — แปลงราคา/อินดิเคเตอร์เป็นสัญญาณ

แต่ละกลยุทธ์คืน Series ของ "position" ที่ align กับ df:
    1 = ควรถือเหรียญ (long)
    0 = ควรถือเงินสด (flat)

จากนั้น backtest/bot จะดูจุดที่ position เปลี่ยน 0->1 เป็น BUY และ 1->0 เป็น SELL
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


def _apply_trend_filter(df: pd.DataFrame, cfg: dict, pos: pd.Series) -> pd.Series:
    """กรอง long signal ออกเมื่อราคาอยู่ใต้ EMA ระยะยาว"""
    s = cfg["strategy"]
    if not s.get("trend_filter_enabled", False):
        return pos
    trend = ind.ema(df["close"], int(s.get("trend_ema_period", 200)))
    return pos.where(df["close"] > trend, 0).astype(int)


def ema_cross(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """ซื้อเมื่อ EMA เร็วตัดขึ้นเหนือ EMA ช้า, ขายเมื่อตัดลง"""
    s = cfg["strategy"]
    fast = ind.ema(df["close"], s["fast"])
    slow = ind.ema(df["close"], s["slow"])
    pos = (fast > slow).astype(int)
    return _apply_trend_filter(df, cfg, pos)


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
    return _apply_trend_filter(df, cfg, pos)


def macd_strategy(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """ซื้อเมื่อ MACD ตัดขึ้นเหนือ signal line, ขายเมื่อตัดลง"""
    s = cfg["strategy"]
    macd_line, signal_line, _ = ind.macd(df["close"], s["fast"], s["slow"])
    pos = (macd_line > signal_line).astype(int)
    return _apply_trend_filter(df, cfg, pos)


def bb_squeeze(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Bollinger squeeze — ความผันผวนต่ำ (แบนด์บีบ) แล้วราคาระเบิดทะลุเส้นกลางขึ้น

    (จากหนังสือ: low volatility → high volatility) ซื้อเมื่อเพิ่งมี squeeze + close เหนือ SMA
    """
    s = cfg["strategy"]
    period = int(s.get("bb_period", 20))
    nstd = float(s.get("bb_std", 2.0))
    lookback = int(s.get("bb_squeeze_lookback", 100))
    quantile = float(s.get("bb_squeeze_quantile", 0.25))
    arm = int(s.get("bb_squeeze_arm", 5))

    middle, upper, lower = ind.bollinger(df["close"], period, nstd)
    width = (upper - lower) / middle
    thresh = width.rolling(lookback, min_periods=period).quantile(quantile)
    squeezed = (width <= thresh).fillna(False)
    recent_squeeze = squeezed.rolling(arm, min_periods=1).max().astype(bool)

    pos = pd.Series(0, index=df.index)
    holding = 0
    for i in range(len(df)):
        c, m = df["close"].iloc[i], middle.iloc[i]
        if pd.isna(m):
            pos.iloc[i] = holding
            continue
        if holding == 0 and recent_squeeze.iloc[i] and c > m:
            holding = 1
        elif holding == 1 and c < m:
            holding = 0
        pos.iloc[i] = holding
    return _apply_trend_filter(df, cfg, pos)


def rsi2_strategy(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Connors 2-period RSI — ซื้อตอนย่อแรง (RSI2 ต่ำ) ในเทรนด์ขึ้น, ออกเมื่อเด้งเหนือ MA สั้น

    หมายเหตุ: หนังสือเขียนกลับด้าน (ซื้อ RSI2>90) ซึ่งเป็นเวอร์ชันไล่ราคา —
    ที่นี่ใช้เวอร์ชัน Connors ดั้งเดิม (mean-reversion) ที่มีงานวิจัยรองรับมากกว่า
    """
    s = cfg["strategy"]
    rp = int(s.get("rsi2_period", 2))
    buy_th = float(s.get("rsi2_buy", 10))
    exit_ma = int(s.get("rsi2_exit_ma", 5))
    trend_ma = int(s.get("rsi2_trend_ma", 200))

    r = ind.rsi(df["close"], rp)
    ma_exit = ind.sma(df["close"], exit_ma)
    trend = ind.sma(df["close"], trend_ma)

    pos = pd.Series(0, index=df.index)
    holding = 0
    for i in range(len(df)):
        c = df["close"].iloc[i]
        if pd.isna(trend.iloc[i]) or pd.isna(r.iloc[i]) or pd.isna(ma_exit.iloc[i]):
            pos.iloc[i] = holding
            continue
        if holding == 0 and c > trend.iloc[i] and r.iloc[i] < buy_th:
            holding = 1
        elif holding == 1 and c > ma_exit.iloc[i]:
            holding = 0
        pos.iloc[i] = holding
    return pos  # เทรนด์ถูกบังคับตอนเข้าแล้ว ไม่ต้องใส่ trend filter ซ้ำ


def heikin_stoch(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Heikin-Ashi + Stochastic — ถือ long ขณะแท่ง HA เขียว (เทรนด์ขึ้น) และไม่ overbought

    (จากหนังสือ: HA กรอง noise + Stochastic ยืนยัน momentum)
    """
    s = cfg["strategy"]
    ha = ind.heikin_ashi(df)
    k, _ = ind.stochastic(
        df, int(s.get("stoch_k", 14)), int(s.get("stoch_smooth", 3)), int(s.get("stoch_d", 3))
    )
    overbought = float(s.get("stoch_overbought", 80))
    green = ha["close"] > ha["open"]

    pos = pd.Series(0, index=df.index)
    holding = 0
    for i in range(len(df)):
        if pd.isna(k.iloc[i]):
            pos.iloc[i] = holding
            continue
        is_green = bool(green.iloc[i])
        if holding == 0 and is_green and k.iloc[i] < overbought:
            holding = 1
        elif holding == 1 and not is_green:
            holding = 0
        pos.iloc[i] = holding
    return _apply_trend_filter(df, cfg, pos)


def tsmom(df: pd.DataFrame, cfg: dict) -> pd.Series:
    """Time-Series (Absolute) Momentum — long เมื่อผลตอบแทนย้อนหลังของเหรียญเองเป็นบวก

    จาก Moskowitz/Ooi/Pedersen (2012), Levy & Lopes (2021), Martin (2023):
    รวมหลาย lookback (เร็ว+ช้า) แล้วโหวต → ถือ long ถ้าเสียงข้างมากเป็นเทรนด์ขึ้น
    (long-only สำหรับคริปโต — เวอร์ชันเต็มจะ short ตอนเทรนด์ลง)
    """
    s = cfg["strategy"]
    lookbacks = s.get("tsmom_lookbacks", [20, 60, 120])
    close = df["close"]
    votes = pd.Series(0.0, index=df.index)
    for lb in lookbacks:
        votes += (close / close.shift(int(lb)) - 1 > 0).astype(float)
    # ต้องมีเสียงข้างมากของ lookback เห็นพ้องว่าเป็นขาขึ้น
    pos = (votes >= (len(lookbacks) / 2.0)).astype(int)
    longest = max(lookbacks)
    if longest < len(pos):
        pos.iloc[:longest] = 0  # ช่วงข้อมูลยังไม่ครบ lookback ยาวสุด = ถือเงินสด
    return _apply_trend_filter(df, cfg, pos)


STRATEGIES = {
    "ema_cross": ema_cross,
    "rsi": rsi_strategy,
    "macd": macd_strategy,
    "bb_squeeze": bb_squeeze,
    "rsi2": rsi2_strategy,
    "heikin_stoch": heikin_stoch,
    "tsmom": tsmom,
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
