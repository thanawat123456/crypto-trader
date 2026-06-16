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
