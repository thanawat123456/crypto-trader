"""โหลดไฟล์ config.yaml และเติมค่า default ให้ครบ"""
from __future__ import annotations

import os
from copy import deepcopy

import yaml

DEFAULTS = {
    "exchange": {"name": "binance", "api_key": "", "api_secret": "", "sandbox": True},
    "defaults": {"symbol": "BTC/USD", "timeframe": "1h", "limit": 500},
    "strategy": {
        "name": "rsi2",       # จาก backtest 5 เหรียญ: rsi2 ชนะ ema_cross ทุกตัว + drawdown ต่ำกว่ามาก
        "fast": 19,           # (ema_cross) จาก optimize BTC/USDT 1h (ดีกว่า 12/26)
        "slow": 55,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "trend_filter_enabled": False,
        "trend_ema_period": 200,
        # bb_squeeze (จากหนังสือ HumbleTraders)
        "bb_period": 20,
        "bb_std": 2.0,
        "bb_squeeze_lookback": 100,
        "bb_squeeze_quantile": 0.25,
        "bb_squeeze_arm": 5,
        # rsi2 (Connors 2-period RSI)
        "rsi2_period": 2,
        "rsi2_buy": 10,
        "rsi2_exit_ma": 5,
        "rsi2_trend_ma": 200,
        # heikin_stoch (Heikin-Ashi + Stochastic)
        "stoch_k": 14,
        "stoch_smooth": 3,
        "stoch_d": 3,
        "stoch_overbought": 80,
        # tsmom (Time-Series Momentum — รวมหลาย lookback)
        "tsmom_lookbacks": [20, 60, 120],
    },
    "backtest": {
        "initial_cash": 10000,
        "fee": 0.001,           # ค่าธรรมเนียม exchange ต่อด้าน
        "slippage_pct": 0.0005, # slippage จริง (ข้าม spread + market impact) ต่อด้าน — Almgren/LOB
        "position_size": 1.0,
    },
    "bot": {
        "poll_seconds": 60,
        "trade_amount": 0.001,
        "summary_enabled": True,
        "enter_on_current_signal": True,
        # ตะกร้าเหรียญที่บอทเทรด (Kraken /USD liquid — 10 established + HYPE ตัวลอง)
        "symbols": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
                    "DOGE/USD", "XLM/USD", "SUI/USD", "NEAR/USD", "XMR/USD", "HYPE/USD"],
    },
    "momentum_filter": {
        # ซื้อเฉพาะเหรียญที่ momentum แรงสุด top_k ในตะกร้า (จาก SSRN: เน้นตัวชนะไม่กี่ตัว)
        "enabled": False,     # default OFF — เปิดแล้ว backtest/paper เทียบก่อน
        "timeframe": "4h",
        "lookback": 30,       # จำนวนแท่งย้อนหลังที่ใช้วัดผลตอบแทน
        "top_k": 3,           # อนุญาตซื้อเฉพาะอันดับ 1..top_k
    },
    "paper": {
        "enabled": True,
        "initial_cash": 300,
        "allocation_pct": 0.2,
        "fee": 0.001,
        "slippage_pct": 0.0005,      # slippage จริงต่อด้าน (ให้ paper sim สมจริงเท่า backtest)
        "sizing_mode": "risk",       # "allocation" | "risk" (เสี่ยงคงที่/ไม้) | "volatility" (vol targeting)
        "risk_per_trade_pct": 0.01,  # โหมด risk: ถ้าโดน SL จะเสีย ~1% ของพอร์ต
        "target_vol": 0.40,          # โหมด volatility: เป้าความผันผวนต่อปี (จาก TSMOM literature 40%)
    },
    "market_filter": {
        "enabled": True,
        "symbol": "BTC/USD",
        "timeframe": "4h",
        "ema_period": 200,
    },
    "smart_filter": {
        "enabled": True,
        "timeframe": "4h",
        "ema_period": 200,
        "rsi_period": 14,
        "rsi_min": 45,
        "rsi_max": 70,
        "atr_period": 14,
        "max_atr_pct": 0.06,
        "loss_cooldown_hours": 24,
    },
    "risk": {
        "stop_loss_pct": 0.02,
        "take_profit_pct": 0.04,
        "max_position_pct": 1.0,
        # exit ขั้นสูง — ปิดไว้ก่อน (backtest ขาลงยังไม่ชนะ SL/TP เรียบง่าย)
        # เปิดเพื่อทดลองในตลาดขาขึ้น แล้ว backtest เทียบก่อนใช้จริง
        "trailing_stop_pct": 0.0,         # 0 = ปิด; เช่น 0.08 = ขายเมื่อราคาย่อ 8% จากจุดสูงสุด (ปล่อยกำไรวิ่ง)
        "atr_stops_enabled": False,       # True = ใช้ ATR กำหนด SL/TP ตามความผันผวนจริง (แทน % ตายตัว)
        "atr_period": 14,
        "atr_sl_mult": 3.0,               # SL = ราคาเข้า − 3×ATR
        "atr_tp_mult": 5.0,               # TP = ราคาเข้า + 5×ATR
        "max_concurrent_positions": 3,    # 0 = ไม่จำกัด; เช่น 3 = ถือพร้อมกันสูงสุด 3 เหรียญ
        "breakeven_trigger_pct": 0.02,    # 0 = ปิด; พอกำไรถึง +2% เลื่อน SL มาที่ราคาเข้า (ไม้ไม่ขาดทุน)
        "circuit_breaker_pct": 0.15,      # 0 = ปิด; ถ้าพอร์ตขาดทุนสะสมเกิน 15% หยุดเปิดไม้ใหม่ + แจ้งเตือน
        # partial take-profit (Waverly scale-out): ขายบางส่วนที่ R-multiple แล้วถือที่เหลือวิ่งต่อ
        "partial_tp_enabled": False,      # default OFF — เปิดแล้ว backtest เทียบก่อน
        "partial_tp_levels": [1.0, 2.0],  # ขายที่กำไร = 1R, 2R (R = ระยะ stop ตอนเข้า)
        "partial_tp_fraction": 0.33,      # ขายกี่ % ของปริมาณเดิมในแต่ละขั้น
        "max_hold_hours": 0,              # time stop: 0 = ปิด; เช่น 168 = ออกถ้าถือเกิน 7 วัน
        "journal_path": "trade_journal.csv",
    },
    "alerts": {
        "discord": {"enabled": False, "webhook_url": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "heartbeat_url": "",  # ping ทุกรอบที่สำเร็จ (เช่น healthchecks.io) → ถ้าเงียบ = บอทตาย ให้บริการนอกแจ้งเตือน
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: str = "config.yaml") -> dict:
    """อ่าน config.yaml ถ้าไม่มีก็ใช้ค่า default ทั้งหมด"""
    cfg = deepcopy(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user)

    cfg = _apply_env(cfg)
    return cfg


def _apply_env(cfg: dict) -> dict:
    """ทับค่าลับด้วย environment variables (สำหรับ GitHub Actions/server)

    ห้าม commit API key ลง repo — ให้ตั้งเป็น Secret แล้วส่งผ่าน env แทน
    """
    env = os.environ
    if env.get("EXCHANGE_NAME"):
        cfg["exchange"]["name"] = env["EXCHANGE_NAME"]
    if env.get("EXCHANGE_API_KEY"):
        cfg["exchange"]["api_key"] = env["EXCHANGE_API_KEY"]
    if env.get("EXCHANGE_API_SECRET"):
        cfg["exchange"]["api_secret"] = env["EXCHANGE_API_SECRET"]
    if env.get("EXCHANGE_SANDBOX"):
        cfg["exchange"]["sandbox"] = env["EXCHANGE_SANDBOX"].lower() in ("1", "true", "yes")

    if env.get("DISCORD_WEBHOOK_URL"):
        cfg["alerts"]["discord"]["webhook_url"] = env["DISCORD_WEBHOOK_URL"]
        cfg["alerts"]["discord"]["enabled"] = True

    if env.get("TELEGRAM_BOT_TOKEN"):
        cfg["alerts"]["telegram"]["bot_token"] = env["TELEGRAM_BOT_TOKEN"]
        cfg["alerts"]["telegram"]["enabled"] = True
    if env.get("TELEGRAM_CHAT_ID"):
        cfg["alerts"]["telegram"]["chat_id"] = env["TELEGRAM_CHAT_ID"]

    if env.get("HEARTBEAT_URL"):
        cfg["alerts"]["heartbeat_url"] = env["HEARTBEAT_URL"]
    return cfg
