"""สแกนตลาดหาเหรียญ momentum แรง "ที่สภาพคล่องดี" (read-only — ไม่เทรด)

ขั้นตอน (สำคัญ: กรองสภาพคล่องก่อนเสมอ กัน memecoin/ปั่น):
  1) ดึงคู่ <quote> ทั้งหมด → เรียงตาม 24h volume → เก็บ top_by_volume ตัวแรก
  2) ในกลุ่มนั้น วัด momentum (ผลตอบแทนย้อนหลัง lookback) → คืน top_by_momentum
"""
from __future__ import annotations

from .data import fetch_ohlcv

# เหรียญ pegged/stable/ทอง — ไม่ใช่เป้าหมาย momentum (กรองทิ้ง)
_PEGGED = {
    "USDT", "USDC", "DAI", "USDG", "USDS", "TUSD", "PYUSD", "USDD", "GUSD",
    "FRAX", "LUSD", "USDR", "EUR", "EURT", "EURR", "USD", "AUD", "GBP",
    "CAD", "CHF", "JPY", "XAUT", "PAXG", "WBTC", "WETH",
}


def scan_market(exchange, quote: str = "USDT", top_by_volume: int = 30,
                top_by_momentum: int = 5, timeframe: str = "4h",
                lookback: int = 30, min_volume: float = 0.0) -> tuple[list[dict], int]:
    """คืน (รายการเหรียญ momentum แรงสุดที่สภาพคล่องดี, จำนวนเหรียญที่ผ่านกรอง volume)"""
    exchange.load_markets()
    tickers = exchange.fetch_tickers()

    candidates = []
    for sym, t in tickers.items():
        if not sym.endswith("/" + quote):
            continue
        base = sym.split("/")[0]
        if base in _PEGGED:           # ข้าม stablecoin/ทอง/wrapped
            continue
        market = exchange.markets.get(sym, {})
        if market.get("spot") is False or market.get("active") is False:
            continue
        qv = t.get("quoteVolume") or 0
        if qv and float(qv) >= min_volume:
            candidates.append((sym, float(qv)))

    candidates.sort(key=lambda x: x[1], reverse=True)
    top_liquid = candidates[:top_by_volume]

    rows = []
    for sym, qv in top_liquid:
        try:
            df = fetch_ohlcv(exchange, sym, timeframe, lookback + 5)
            close = df["close"]
            if len(close) > lookback:
                rows.append({
                    "symbol": sym,
                    "vol": qv,
                    "mom": float(close.iloc[-1] / close.iloc[-1 - lookback] - 1),
                    "price": float(close.iloc[-1]),
                })
        except Exception:  # noqa: BLE001
            continue

    rows.sort(key=lambda r: r["mom"], reverse=True)
    return rows[:top_by_momentum], len(top_liquid)


def format_scan(rows: list[dict], n_liquid: int, timeframe: str, lookback: int) -> str:
    lines = [
        "🔭 Market Scan — momentum แรงสุด (สภาพคล่องดี)",
        f"จาก {n_liquid} เหรียญ volume สูงสุด | momentum {lookback}×{timeframe}",
        "─" * 32,
    ]
    if not rows:
        lines.append("(ไม่มีเหรียญผ่านเกณฑ์ — ตลาดอาจหมีทั้งกระดาน)")
        return "\n".join(lines)
    for i, r in enumerate(rows, 1):
        arrow = "🟢" if r["mom"] > 0 else "🔴"
        if r["mom"] > 0.80:        # พุ่งพาราโบลา = เสี่ยงปั่น/ไล่ดอยมากกว่าเทรนด์จริง
            warn = " 🚨พุ่งแรงผิดปกติ (เสี่ยงปั่น/ไล่ดอย)"
        elif r["vol"] < 1e6:
            warn = " ⚠️สภาพคล่องต่ำ"
        else:
            warn = ""
        lines.append(
            f"{i}. {arrow} {r['symbol']}: {r['mom']:+.1%} | "
            f"vol ${r['vol'] / 1e6:.1f}M | ${r['price']:,.4g}{warn}"
        )
    return "\n".join(lines)
