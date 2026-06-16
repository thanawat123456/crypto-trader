"""บันทึกประวัติการเข้า/ออกไม้ เพื่อวัดผลจริงของบอท"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone


FIELDS = [
    "timestamp",
    "symbol",
    "timeframe",
    "side",
    "price",
    "amount",
    "reason",
    "pnl",
]


def record_trade(
    path: str,
    symbol: str,
    timeframe: str,
    side: str,
    price: float,
    amount: float,
    reason: str,
    pnl: float | None = None,
) -> None:
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "side": side,
                "price": round(float(price), 8),
                "amount": round(float(amount), 8),
                "reason": reason,
                "pnl": "" if pnl is None else round(float(pnl), 8),
            }
        )


def _read_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def performance_summary(
    path: str,
    symbol: str,
    timeframe: str,
    position_state: dict,
    current_price: float | None = None,
    portfolio: dict | None = None,
) -> str:
    rows = [
        row for row in _read_rows(path)
        if row.get("symbol") == symbol and row.get("timeframe") == timeframe
    ]
    buys = [row for row in rows if row.get("side") == "BUY"]
    sells = [row for row in rows if row.get("side") == "SELL"]

    pnl_values = []
    for row in sells:
        try:
            pnl_values.append(float(row.get("pnl") or 0))
        except ValueError:
            continue

    total_pnl = sum(pnl_values)
    wins = sum(1 for pnl in pnl_values if pnl > 0)
    win_rate = (wins / len(pnl_values) * 100) if pnl_values else 0.0
    status = "ถืออยู่" if position_state.get("in_position") else "ถือเงินสด"
    entry_price = position_state.get("entry_price")
    amount = position_state.get("amount")

    unrealized = None
    if position_state.get("in_position") and entry_price and amount and current_price:
        unrealized = (float(current_price) - float(entry_price)) * float(amount)

    last = rows[-1] if rows else None
    last_trade = "ยังไม่มี trade"
    if last:
        last_trade = (
            f"{last.get('side')} @ {float(last.get('price') or 0):,.2f} "
            f"({last.get('reason') or '-'})"
        )

    lines = [
        f"📊 Summary | {symbol} {timeframe}",
        f"สถานะ: {status}",
        f"Trades: BUY={len(buys)} SELL={len(sells)} | Win rate={win_rate:.1f}%",
        f"Realized PnL≈{total_pnl:,.2f}",
        f"ล่าสุด: {last_trade}",
    ]
    if entry_price:
        lines.append(f"Entry≈{float(entry_price):,.2f} | Amount={float(amount or 0):.8f}")
    if unrealized is not None:
        lines.append(f"Unrealized PnL≈{unrealized:,.2f}")
    if portfolio:
        lines.append(
            f"Paper cash≈{float(portfolio.get('cash', 0.0)):,.2f} | "
            f"Paper realized≈{float(portfolio.get('realized_pnl', 0.0)):,.2f}"
        )
    return "\n".join(lines)
