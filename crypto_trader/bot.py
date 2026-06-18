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
from .indicators import atr, ema, rsi
from .journal import loss_cooldown_reason, performance_summary, record_trade
from .strategy import get_position, latest_signal

MAX_BACKOFF = 600  # หน่วงสูงสุด 10 นาทีเมื่อ error ติดกัน


def _place_order(exchange, symbol: str, side: str, amount: float, dry_run: bool):
    if dry_run:
        return {"info": "dry-run (ไม่ส่งคำสั่งจริง)"}
    if side == "buy":
        return exchange.create_market_buy_order(symbol, amount)
    return exchange.create_market_sell_order(symbol, amount)


def _paper_buy_amount(cfg: dict, portfolio: dict, price: float, fallback_amount: float) -> float:
    paper = cfg.get("paper", {})
    if not paper.get("enabled", True):
        return fallback_amount
    cash = float(portfolio.get("cash", 0.0))
    allocation = min(max(float(paper.get("allocation_pct", 0.2)), 0.0), 1.0)
    budget = cash * allocation
    fee = float(paper.get("fee", 0.001))
    if budget <= 0:
        return 0.0
    return budget / (float(price) * (1 + fee))


def _market_allows_buy(exchange, cfg: dict) -> tuple[bool, str]:
    market = cfg.get("market_filter", {})
    if not market.get("enabled", True):
        return True, "market_filter_disabled"

    symbol = market.get("symbol", "BTC/USDT")
    timeframe = market.get("timeframe", "4h")
    period = int(market.get("ema_period", 200))
    try:
        df = fetch_ohlcv(exchange, symbol, timeframe, max(period + 5, 250))
    except Exception as e:  # noqa: BLE001
        return False, f"market_filter_error={e}"

    closed = df.iloc[:-1]
    if len(closed) < period:
        return False, f"market_filter_wait_data={len(closed)}/{period}"

    close = float(closed["close"].iloc[-1])
    trend = float(ema(closed["close"], period).iloc[-1])
    if close > trend:
        return True, f"market_bullish {symbol} {timeframe} close={close:,.2f} ema{period}={trend:,.2f}"
    return False, f"market_not_bullish {symbol} {timeframe} close={close:,.2f} ema{period}={trend:,.2f}"


def _symbol_allows_buy(exchange, cfg: dict, symbol: str) -> tuple[bool, str]:
    smart = cfg.get("smart_filter", {})
    if not smart.get("enabled", True):
        return True, "smart_filter_disabled"

    timeframe = smart.get("timeframe", "4h")
    ema_period = int(smart.get("ema_period", 200))
    rsi_period = int(smart.get("rsi_period", 14))
    atr_period = int(smart.get("atr_period", 14))
    limit = max(ema_period + 5, rsi_period + 20, atr_period + 20, 250)

    try:
        df = fetch_ohlcv(exchange, symbol, timeframe, limit)
    except Exception as e:  # noqa: BLE001
        return False, f"smart_filter_error={e}"

    closed = df.iloc[:-1]
    if len(closed) < ema_period:
        return False, f"smart_filter_wait_data={len(closed)}/{ema_period}"

    close = float(closed["close"].iloc[-1])
    trend = float(ema(closed["close"], ema_period).iloc[-1])
    rsi_value = float(rsi(closed["close"], rsi_period).iloc[-1])
    atr_pct = float(atr(closed, atr_period).iloc[-1] / close)
    rsi_min = float(smart.get("rsi_min", 45))
    rsi_max = float(smart.get("rsi_max", 70))
    max_atr_pct = float(smart.get("max_atr_pct", 0.06))

    if close <= trend:
        return False, f"symbol_not_bullish {symbol} {timeframe} close={close:,.4f} ema{ema_period}={trend:,.4f}"
    if rsi_value < rsi_min:
        return False, f"momentum_weak {symbol} {timeframe} rsi={rsi_value:.1f} min={rsi_min:.1f}"
    if rsi_value > rsi_max:
        return False, f"momentum_overheated {symbol} {timeframe} rsi={rsi_value:.1f} max={rsi_max:.1f}"
    if atr_pct > max_atr_pct:
        return False, f"volatility_high {symbol} {timeframe} atr_pct={atr_pct:.2%} max={max_atr_pct:.2%}"

    return True, (
        f"smart_ok {symbol} {timeframe} close={close:,.4f} "
        f"ema{ema_period}={trend:,.4f} rsi={rsi_value:.1f} atr_pct={atr_pct:.2%}"
    )


def _tick(exchange, cfg, symbol, timeframe, amount, limit, dry_run, position_state, portfolio):
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
    desired_position = int(get_position(closed, cfg).iloc[-1]) if len(closed) else 0
    price = latest_price(exchange, symbol)

    exit_reason = None
    if in_position and entry_price:
        move = price / float(entry_price) - 1
        if stop_loss and move <= -stop_loss:
            exit_reason = "stop_loss"
        elif take_profit and move >= take_profit:
            exit_reason = "take_profit"

    buy_reason = None
    if signal == "BUY" and not in_position:
        buy_reason = "signal"
    elif (
        dry_run
        and cfg.get("bot", {}).get("enter_on_current_signal", True)
        and desired_position == 1
        and not in_position
    ):
        buy_reason = "current_signal"

    if buy_reason:
        allowed, market_reason = _market_allows_buy(exchange, cfg)
        if not allowed:
            alerts.notify(cfg, f"⏸️ ข้าม BUY | {symbol} | reason={buy_reason} | {market_reason}")
            position_state["last_price"] = price
            return position_state, portfolio
        cooldown = loss_cooldown_reason(
            journal_path,
            symbol,
            timeframe,
            float(cfg.get("smart_filter", {}).get("loss_cooldown_hours", 24)),
        )
        if cooldown:
            alerts.notify(cfg, f"⏸️ ข้าม BUY | {symbol} | reason={buy_reason} | {cooldown}")
            position_state["last_price"] = price
            return position_state, portfolio
        allowed, smart_reason = _symbol_allows_buy(exchange, cfg, symbol)
        if not allowed:
            alerts.notify(cfg, f"⏸️ ข้าม BUY | {symbol} | reason={buy_reason} | {smart_reason}")
            position_state["last_price"] = price
            return position_state, portfolio
        buy_amount = _paper_buy_amount(cfg, portfolio, price, amount) if dry_run else amount
        if buy_amount <= 0:
            alerts._console(f"… ข้าม BUY เพราะเงินสดจำลองไม่พอ | {symbol} @ {price:,.2f}")
            position_state["last_price"] = price
            return position_state, portfolio
        _place_order(exchange, symbol, "buy", buy_amount, dry_run)
        if dry_run and cfg.get("paper", {}).get("enabled", True):
            fee = float(cfg.get("paper", {}).get("fee", 0.001))
            portfolio["cash"] = float(portfolio.get("cash", 0.0)) - (buy_amount * price * (1 + fee))
            state.save_portfolio(portfolio)
        position_state = {"in_position": True, "entry_price": price, "amount": buy_amount}
        state.save_state(symbol, timeframe, True, entry_price=price, amount=buy_amount)
        record_trade(journal_path, symbol, timeframe, "BUY", price, buy_amount, buy_reason)
        msg = alerts.signal_message(symbol, timeframe, "BUY", price)
        alerts.notify(cfg, f"{msg} | reason={buy_reason}")
    elif (signal == "SELL" or exit_reason) and in_position:
        reason = exit_reason or "signal"
        sell_amount = float(saved_amount)
        _place_order(exchange, symbol, "sell", sell_amount, dry_run)
        gross_pnl = (price - float(entry_price or price)) * sell_amount
        pnl = gross_pnl
        if dry_run and cfg.get("paper", {}).get("enabled", True):
            fee = float(cfg.get("paper", {}).get("fee", 0.001))
            entry_cost = float(entry_price or price) * sell_amount
            exit_value = price * sell_amount
            pnl = exit_value * (1 - fee) - entry_cost * (1 + fee)
            portfolio["cash"] = float(portfolio.get("cash", 0.0)) + exit_value * (1 - fee)
            portfolio["realized_pnl"] = float(portfolio.get("realized_pnl", 0.0)) + pnl
            state.save_portfolio(portfolio)
        position_state = {"in_position": False, "entry_price": None, "amount": None}
        state.save_state(symbol, timeframe, False, entry_price=None, amount=None)
        record_trade(journal_path, symbol, timeframe, "SELL", price, sell_amount, reason, pnl)
        msg = alerts.signal_message(symbol, timeframe, "SELL", price)
        alerts.notify(cfg, f"{msg} | reason={reason} | PnL≈{pnl:,.2f}")
    else:
        where = "ถืออยู่" if in_position else "ถือเงินสด"
        alerts._console(f"… ไม่มีสัญญาณใหม่ ({where}) | {symbol} @ {price:,.2f}")

    position_state["last_price"] = price
    return position_state, portfolio


def run_bot(exchange, cfg: dict, symbol: str, timeframe: str, once: bool = False) -> None:
    bot_cfg = cfg["bot"]
    amount = float(bot_cfg["trade_amount"])
    poll = int(bot_cfg["poll_seconds"])
    limit = int(cfg["defaults"]["limit"])

    has_keys = bool(cfg["exchange"].get("api_key"))
    dry_run = not has_keys
    sandbox = cfg["exchange"].get("sandbox", True)
    mode = "DRY-RUN" if dry_run else ("PAPER" if sandbox else "LIVE 💰")
    paper_cfg = cfg.get("paper", {})
    portfolio = state.load_portfolio(float(paper_cfg.get("initial_cash", 300)))
    if dry_run and paper_cfg.get("enabled", True):
        state.save_portfolio(portfolio)
    size_text = f"เทรดครั้งละ {amount}"
    if dry_run and paper_cfg.get("enabled", True):
        allocation = float(paper_cfg.get("allocation_pct", 0.2)) * 100
        size_text = f"paper allocation={allocation:.0f}% | cash≈{portfolio['cash']:,.2f}"

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
        f"{size_text} | สถานะเริ่ม: {'ถืออยู่' if position_state['in_position'] else 'ถือเงินสด'}",
    )

    fails = 0
    while True:
        try:
            position_state, portfolio = _tick(
                exchange, cfg, symbol, timeframe, amount, limit, dry_run, position_state, portfolio
            )
            if bot_cfg.get("summary_enabled", True):
                journal_path = cfg.get("risk", {}).get("journal_path", "trade_journal.csv")
                summary = performance_summary(
                    journal_path,
                    symbol,
                    timeframe,
                    position_state,
                    position_state.get("last_price"),
                    portfolio,
                )
                alerts.notify(cfg, summary)
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
