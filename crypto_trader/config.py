"""โหลดไฟล์ config.yaml และเติมค่า default ให้ครบ"""
from __future__ import annotations

import os
from copy import deepcopy

import yaml

DEFAULTS = {
    "exchange": {"name": "binance", "api_key": "", "api_secret": "", "sandbox": True},
    "defaults": {"symbol": "BTC/USDT", "timeframe": "1h", "limit": 500},
    "strategy": {
        "name": "ema_cross",
        "fast": 19,           # จาก optimize BTC/USDT 1h (ดีกว่า 12/26)
        "slow": 55,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
    },
    "backtest": {"initial_cash": 10000, "fee": 0.001, "position_size": 1.0},
    "bot": {"poll_seconds": 60, "trade_amount": 0.001},
    "alerts": {
        "discord": {"enabled": False, "webhook_url": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
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
    return cfg
