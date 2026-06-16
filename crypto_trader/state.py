"""จำสถานะบอทลงไฟล์ JSON — กันบอท "ลืม" ว่าถือเหรียญอยู่ไหมตอนรีสตาร์ท

สำคัญมากสำหรับการรัน 24/7: ถ้าบอทดับแล้วเปิดใหม่โดยไม่จำสถานะ
มันอาจซื้อซ้ำหรือขายทั้งที่ไม่ได้ถือ
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

DEFAULT_PATH = "bot_state.json"
PORTFOLIO_KEY = "__portfolio__"


def _key(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


def load_state(symbol: str, timeframe: str, path: str = DEFAULT_PATH) -> dict:
    """อ่านสถานะของคู่ symbol+timeframe (คืน default ถ้ายังไม่มี)"""
    if not os.path.exists(path):
        return {"in_position": False, "updated_at": None, "entry_price": None, "amount": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            all_state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"in_position": False, "updated_at": None, "entry_price": None, "amount": None}
    saved = all_state.get(_key(symbol, timeframe), {})
    return {
        "in_position": bool(saved.get("in_position", False)),
        "updated_at": saved.get("updated_at"),
        "entry_price": saved.get("entry_price"),
        "amount": saved.get("amount"),
    }


def save_state(
    symbol: str,
    timeframe: str,
    in_position: bool,
    path: str = DEFAULT_PATH,
    entry_price: float | None = None,
    amount: float | None = None,
) -> None:
    """บันทึกสถานะแบบ atomic (เขียนไฟล์ temp แล้ว replace กันไฟล์พังถ้าดับกลางคัน)"""
    all_state = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                all_state = json.load(f)
        except (json.JSONDecodeError, OSError):
            all_state = {}

    all_state[_key(symbol, timeframe)] = {
        "in_position": in_position,
        "entry_price": entry_price,
        "amount": amount,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(all_state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_all(path: str = DEFAULT_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_portfolio(initial_cash: float, path: str = DEFAULT_PATH) -> dict:
    """อ่านพอร์ตจำลอง ถ้ายังไม่มีให้สร้างจาก initial_cash"""
    all_state = _load_all(path)
    saved = all_state.get(PORTFOLIO_KEY, {})
    return {
        "cash": float(saved.get("cash", initial_cash)),
        "realized_pnl": float(saved.get("realized_pnl", 0.0)),
        "updated_at": saved.get("updated_at"),
    }


def save_portfolio(portfolio: dict, path: str = DEFAULT_PATH) -> None:
    """บันทึกพอร์ตจำลองไว้ในไฟล์ state เดียวกับสถานะราย symbol"""
    all_state = _load_all(path)
    all_state[PORTFOLIO_KEY] = {
        "cash": float(portfolio.get("cash", 0.0)),
        "realized_pnl": float(portfolio.get("realized_pnl", 0.0)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(all_state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
