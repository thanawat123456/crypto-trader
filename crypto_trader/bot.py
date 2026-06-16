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
from .journal import record_trade
from .strategy import latest_signal

MAX_BACKOFF = 600  # หน่วงสูงสุด 10 นาทีเมื่อ error ติดกัน


def _place_order(exchange, symbol: str, side: str, amount: float, dry_run: bool):
    if dry_run:
        return {"info": "dry-run (ไม่ส่งคำสั่งจริง)"}
    if side == "buy":
        return exchange.create_market_buy_order(symbol, amount)
    return exchange.create_market_sell_order(symbol, amount)


def _tick(exchange, cfg, symbol, timeframe, amount, limit, dry_run, position_state):
    """ทำงานหนึ่งรอบ: เช็คสัญญาณ + เทรดถ้าจำเป็น คืนสถานะใหม่"""
    in_position = bool(position_state.get("in_position", False))
    entry_price = position_state.get("entry_price")
    saved_amount = position_state.get("amount") or amount
    risk = cfg.get("risk", {})
    stop_loss = float(risk.get("stop_loss_pct", 0.0) or 0.0)
    take_profit = float(risk.get("take_profit_pct", 0.0) or 0.0)
    journal_path = risk.get("journal_path", "trade_journal.csv")

    df = fetch_ohlcv(exchange, symbol, timeframe, limit)
    closed = df.iloc[:-1]  # ตัดแท่งปัจจุบันที่ยังวิ่งอยู่ออก
    signal = latest_signal(closed, cfg)
    price = latest_price(exchange, symbol)

    exit_reason = None
    if in_position and entry_price:
        move = price / float(entry_price) - 1
        if stop_loss and move <= -stop_loss:
            exit_reason = "stop_loss"
        elif take_profit and move >= take_profit:
            exit_reason = "take_profit"

    if signal == "BUY" and not in_position:
        _place_order(exchange, symbol, "buy", amount, dry_run)
        position_state = {"in_position": True, "entry_price": price, "amount": amount}
        state.save_state(symbol, timeframe, True, entry_price=price, amount=amount)
        record_trade(journal_path, symbol, timeframe, "BUY", price, amount, "signal")
        alerts.notify(cfg, alerts.signal_message(symbol, timeframe, "BUY", price))
    elif (signal == "SELL" or exit_reason) and in_position:
        reason = exit_reason or "signal"
        sell_amount = float(saved_amount)
        _place_order(exchange, symbol, "sell", sell_amount, dry_run)
        pnl = (price - float(entry_price or price)) * sell_amount
        position_state = {"in_position": False, "entry_price": None, "amount": None}
        state.save_state(symbol, timeframe, False, entry_price=None, amount=None)
        record_trade(journal_path, symbol, timeframe, "SELL", price, sell_amount, reason, pnl)
        msg = alerts.signal_message(symbol, timeframe, "SELL", price)
        alerts.notify(cfg, f"{msg} | reason={reason} | PnL≈{pnl:,.2f}")
    else:
        where = "ถืออยู่" if in_position else "ถือเงินสด"
        alerts._console(f"… ไม่มีสัญญาณใหม่ ({where}) | {symbol} @ {price:,.2f}")

    return position_state


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
    position_state = {
        "in_position": bool(saved.get("in_position", False)),
        "entry_price": saved.get("entry_price"),
        "amount": saved.get("amount"),
    }
    # เขียนไฟล์ state ทันที เพื่อให้มีไฟล์เสมอ (สำคัญสำหรับ GitHub Actions ที่ต้อง commit)
    state.save_state(
        symbol,
        timeframe,
        position_state["in_position"],
        entry_price=position_state["entry_price"],
        amount=position_state["amount"],
    )

    alerts.notify(
        cfg,
        f"🤖 เริ่มบอท [{mode}] | {symbol} {timeframe} | กลยุทธ์={cfg['strategy']['name']} "
        f"(fast={cfg['strategy']['fast']}/slow={cfg['strategy']['slow']}) | "
        f"เทรดครั้งละ {amount} | สถานะเริ่ม: {'ถืออยู่' if position_state['in_position'] else 'ถือเงินสด'}",
    )

    fails = 0
    while True:
        try:
            position_state = _tick(
                exchange, cfg, symbol, timeframe, amount, limit, dry_run, position_state
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
