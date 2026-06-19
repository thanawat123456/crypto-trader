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


# ฟิลด์เริ่มต้นของสถานะหนึ่ง position (เพิ่มได้เรื่อย ๆ โดยไม่พังของเก่า)
_STATE_DEFAULTS = {
    "in_position": False,
    "updated_at": None,
    "entry_price": None,
    "amount": None,
    "peak_price": None,
    "entry_time": None,    # เวลาเข้าไม้ (ISO) — สำหรับ time stop
    "entry_r": None,       # ระยะ stop ตอนเข้า (R) — สำหรับ partial take-profit
    "init_amount": None,   # ปริมาณตอนเข้าเต็ม — ฐานคำนวณ scale-out
    "scale_level": 0,      # ทำ partial TP ไปแล้วกี่ขั้น
}


def load_state(symbol: str, timeframe: str, path: str = DEFAULT_PATH) -> dict:
    """อ่านสถานะของคู่ symbol+timeframe (เติม default ให้ครบ ถ้ายังไม่มี)"""
    saved = _load_all(path).get(_key(symbol, timeframe), {})
    state = {**_STATE_DEFAULTS, **saved}
    state["in_position"] = bool(state.get("in_position", False))
    return state


def save_state(
    symbol: str,
    timeframe: str,
    in_position: bool,
    path: str = DEFAULT_PATH,
    **fields,
) -> None:
    """บันทึกสถานะแบบ atomic + merge (เก็บฟิลด์เดิมที่ไม่ได้ส่งมาไว้ → กันลืม state ข้ามรอบ)"""
    all_state = _load_all(path)
    record = all_state.get(_key(symbol, timeframe), {})
    record.update(fields)
    record["in_position"] = in_position
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    all_state[_key(symbol, timeframe)] = record

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


def count_open_positions(path: str = DEFAULT_PATH, exclude_key: str | None = None) -> int:
    """นับจำนวนคู่ symbol+timeframe ที่กำลังถือเหรียญอยู่ (ข้าม __portfolio__)

    ใช้คุมความเสี่ยงพอร์ตรวม: จำกัดจำนวนไม้ที่ถือพร้อมกัน
    exclude_key: ข้ามคีย์ของ symbol ปัจจุบัน (กันนับตัวเอง)
    """
    count = 0
    for key, val in _load_all(path).items():
        if key.startswith("__") or key == exclude_key:
            continue
        if isinstance(val, dict) and val.get("in_position"):
            count += 1
    return count


def get_marker(name: str, path: str = DEFAULT_PATH) -> str | None:
    """อ่าน timestamp ของ marker (เช่น 'report') — ใช้ throttle งานที่ทำเป็นรอบ ๆ"""
    return _load_all(path).get(f"__marker_{name}__", {}).get("at")


def set_marker(name: str, path: str = DEFAULT_PATH) -> None:
    """ปั๊ก timestamp ปัจจุบันให้ marker"""
    all_state = _load_all(path)
    all_state[f"__marker_{name}__"] = {"at": datetime.now(timezone.utc).isoformat()}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(all_state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_note(name: str, path: str = DEFAULT_PATH) -> str | None:
    """อ่านค่าข้อความที่จำไว้ (เช่น เหตุผล reject ล่าสุด — ใช้ dedupe แจ้งเตือน)"""
    return _load_all(path).get(f"__note_{name}__", {}).get("v")


def set_note(name: str, value: str | None, path: str = DEFAULT_PATH) -> None:
    """บันทึก/ล้างค่าข้อความ (value=None = ล้าง)"""
    all_state = _load_all(path)
    key = f"__note_{name}__"
    if value is None:
        all_state.pop(key, None)
    else:
        all_state[key] = {"v": value, "at": datetime.now(timezone.utc).isoformat()}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(all_state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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
