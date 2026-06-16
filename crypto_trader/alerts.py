"""แจ้งเตือนสัญญาณ — console + ไฟล์ log เสมอ, Telegram ถ้าเปิดใช้ใน config"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

LOG_FILE = "bot.log"


def _console(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {message}"
    print(line, flush=True)
    # เก็บ log ลงไฟล์ด้วย เพื่อดูย้อนหลังตอนรัน 24/7
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _telegram(cfg: dict, message: str) -> None:
    tg = cfg["alerts"]["telegram"]
    if not tg.get("enabled"):
        return
    token, chat_id = tg.get("bot_token"), tg.get("chat_id")
    if not token or not chat_id:
        _console("⚠️  เปิด Telegram แต่ยังไม่ได้ตั้ง bot_token/chat_id")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:  # noqa: BLE001
        _console(f"⚠️  ส่ง Telegram ไม่สำเร็จ: {e}")


def notify(cfg: dict, message: str) -> None:
    """ส่งแจ้งเตือนทุกช่องทางที่เปิดใช้"""
    _console(message)
    _telegram(cfg, message)


def signal_message(symbol: str, timeframe: str, signal: str, price: float) -> str:
    icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(signal, "•")
    return f"{icon} {signal} | {symbol} ({timeframe}) @ {price:,.2f}"
