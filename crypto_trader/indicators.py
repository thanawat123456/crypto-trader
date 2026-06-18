"""อินดิเคเตอร์ทางเทคนิค — ใช้ pandas/numpy ล้วน ไม่ต้องลง TA-Lib"""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average"""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """คืน (macd_line, signal_line, histogram)"""
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger(series: pd.Series, period: int = 20, num_std: float = 2.0):
    """คืน (middle, upper, lower)"""
    middle = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return middle, upper, lower


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range ใช้วัดความผันผวนจาก OHLC"""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def stochastic(df: pd.DataFrame, k_period: int = 14, k_smooth: int = 3, d_period: int = 3):
    """Stochastic Oscillator — คืน (%K, %D) วัด momentum 0-100"""
    low_min = df["low"].rolling(k_period, min_periods=k_period).min()
    high_max = df["high"].rolling(k_period, min_periods=k_period).max()
    rng = (high_max - low_min).replace(0, pd.NA)
    raw_k = 100 * (df["close"] - low_min) / rng
    k = raw_k.rolling(k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(d_period, min_periods=d_period).mean()
    return k, d


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """แปลง OHLC เป็นแท่ง Heikin-Ashi (กรอง noise เห็นเทรนด์ชัดขึ้น)"""
    ha = pd.DataFrame(index=df.index)
    ha["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = [float(df["open"].iloc[0] + df["close"].iloc[0]) / 2]
    closes = ha["close"].tolist()
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + closes[i - 1]) / 2)
    ha["open"] = ha_open
    ha["high"] = pd.concat([df["high"], ha["open"], ha["close"]], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha["open"], ha["close"]], axis=1).min(axis=1)
    return ha
