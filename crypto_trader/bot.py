"""ลูปเทรดอัตโนมัติ — ออกแบบให้ทนทานสำหรับการรัน 24/7

โหมดการทำงาน:
  - dry-run : ไม่มี API key → แค่คำนวณสัญญาณและแจ้งเตือน (ไม่ส่งคำสั่งจริง)
  - paper   : exchange.sandbox = true → ส่งคำสั่งบนเงินปลอม
  - live    : exchange.sandbox = false → เงินจริง (ต้องยืนยันก่อน)

ความทนทาน:
  - จำสถานะ (in_position) ลงไฟล์ → รีสตาร์ทแล้วไม่ลืมว่าถือเหรียญอยู่
  - ดักทุก error ในลูป + หน่วงเวลาแบบ backoff เมื่อพัง (เช่น net หลุด)
  - ใช้เฉพาะแท่งที่ปิดแล้ว เพื่อกันสัญญาณกระพริบ
"""
from __future__ import annotations

import time

from . import alerts, state
from .data import fetch_ohlcv, latest_price
from .strategy import latest_signal

MAX_BACKOFF = 600  # หน่วงสูงสุด 10 นาทีเมื่อ error ติดกัน


def _place_order(exchange, symbol: str, side: str, amount: float, dry_run: bool):
    if dry_run:
        return {"info": "dry-run (ไม่ส่งคำสั่งจริง)"}
    if side == "buy":
        return exchange.create_market_buy_order(symbol, amount)
    return exchange.create_market_sell_order(symbol, amount)


def _tick(exchange, cfg, symbol, timeframe, amount, limit, dry_run, in_position):
    """ทำงานหนึ่งรอบ: เช็คสัญญาณ + เทรดถ้าจำเป็น คืนสถานะใหม่"""
    df = fetch_ohlcv(exchange, symbol, timeframe, limit)
    closed = df.iloc[:-1]  # ตัดแท่งปัจจุบันที่ยังวิ่งอยู่ออก
    signal = latest_signal(closed, cfg)
    price = latest_price(exchange, symbol)

    if signal == "BUY" and not in_position:
        _place_order(exchange, symbol, "buy", amount, dry_run)
        in_position = True
        state.save_state(symbol, timeframe, in_position)
        alerts.notify(cfg, alerts.signal_message(symbol, timeframe, "BUY", price))
    elif signal == "SELL" and in_position:
        _place_order(exchange, symbol, "sell", amount, dry_run)
        in_position = False
        state.save_state(symbol, timeframe, in_position)
        alerts.notify(cfg, alerts.signal_message(symbol, timeframe, "SELL", price))
    else:
        where = "ถืออยู่" if in_position else "ถือเงินสด"
        alerts._console(f"… ไม่มีสัญญาณใหม่ ({where}) | {symbol} @ {price:,.2f}")

    return in_position


def run_bot(exchange, cfg: dict, symbol: str, timeframe: str, once: bool = False) -> None:
    bot_cfg = cfg["bot"]
    amount = float(bot_cfg["trade_amount"])
    poll = int(bot_cfg["poll_seconds"])
    limit = int(cfg["defaults"]["limit"])

    has_keys = bool(cfg["exchange"].get("api_key"))
    dry_run = not has_keys
    sandbox = cfg["exchange"].get("sandbox", True)
    mode = "DRY-RUN" if dry_run else ("PAPER" if sandbox else "LIVE 💰")

    # โหลดสถานะที่จำไว้ (กันลืม position ตอนรีสตาร์ท)
    saved = state.load_state(symbol, timeframe)
    in_position = bool(saved.get("in_position", False))

    alerts.notify(
        cfg,
        f"🤖 เริ่มบอท [{mode}] | {symbol} {timeframe} | กลยุทธ์={cfg['strategy']['name']} "
        f"(fast={cfg['strategy']['fast']}/slow={cfg['strategy']['slow']}) | "
        f"เทรดครั้งละ {amount} | สถานะเริ่ม: {'ถืออยู่' if in_position else 'ถือเงินสด'}",
    )

    fails = 0
    while True:
        try:
            in_position = _tick(
                exchange, cfg, symbol, timeframe, amount, limit, dry_run, in_position
            )
            fails = 0  # สำเร็จ → รีเซ็ตตัวนับ
        except Exception as e:  # noqa: BLE001
            fails += 1
            wait = min(poll * (2 ** fails), MAX_BACKOFF)
            alerts._console(f"⚠️  ผิดพลาด (ครั้งที่ {fails}): {e} — รอ {wait}s แล้วลองใหม่")
            if once:
                break
            time.sleep(wait)
            continue

        if once:
            break
        time.sleep(poll)
