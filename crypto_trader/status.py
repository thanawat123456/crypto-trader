"""รายงานสถานะ read-only — ทำไมแต่ละเหรียญถึง (ไม่) ถูกเทรด + อันดับ momentum ของ watchlist

ช่วยให้เข้าใจว่าตอนบอทนิ่ง มันนิ่งเพราะ gate ไหนบล็อก (ตลาด/เทรนด์เหรียญ/momentum/cooldown)
"""
from __future__ import annotations

from .bot import _market_allows_buy, _momentum_allows_buy, _symbol_allows_buy
from .data import fetch_ohlcv
from .journal import loss_cooldown_reason
from .state import load_state
from .strategy import latest_signal


def build_status(exchange, cfg: dict, timeframe: str) -> str:
    syms = cfg.get("bot", {}).get("symbols") or [cfg["defaults"]["symbol"]]
    limit = int(cfg["defaults"]["limit"])
    journal_path = cfg.get("risk", {}).get("journal_path", "trade_journal.csv")
    mf = cfg.get("momentum_filter", {})
    lb, mtf = int(mf.get("lookback", 30)), mf.get("timeframe", "4h")
    cooldown_h = float(cfg.get("smart_filter", {}).get("loss_cooldown_hours", 24))

    mkt_ok, mkt_reason = _market_allows_buy(exchange, cfg)
    lines = [
        "🩺 สถานะบอท — ทำไมเทรด/ไม่เทรด",
        "─" * 34,
        "🟢 ตลาดรวม: เปิดให้ซื้อ" if mkt_ok else "🔴 ตลาดรวม: ปิด (บล็อกซื้อทุกเหรียญ)",
        f"   └ {mkt_reason}",
        "─ watchlist (เรียงตาม momentum) ─",
    ]

    # วัด momentum รายเหรียญ + จัดอันดับ
    moms = {}
    for s in syms:
        try:
            c = fetch_ohlcv(exchange, s, mtf, lb + 5)["close"]
            if len(c) > lb:
                moms[s] = float(c.iloc[-1] / c.iloc[-1 - lb] - 1)
        except Exception:  # noqa: BLE001
            pass
    ordered = sorted(syms, key=lambda s: moms.get(s, -999), reverse=True)

    for s in ordered:
        st = load_state(s, timeframe)
        if st.get("in_position"):
            why = "💼 ถืออยู่"
        elif not mkt_ok:
            why = "⏸️ ตลาดปิด"
        else:
            ok, reason = _symbol_allows_buy(exchange, cfg, s)
            if not ok:
                why = f"⏸️ {reason.split()[0]}"          # เช่น symbol_not_bullish / momentum_weak
            elif loss_cooldown_reason(journal_path, s, timeframe, cooldown_h):
                why = "⏸️ cooldown (เพิ่งขาดทุน)"
            elif not _momentum_allows_buy(exchange, cfg, s)[0]:
                why = "⏸️ momentum ไม่ติด top"
            else:
                try:
                    sig = latest_signal(fetch_ohlcv(exchange, s, timeframe, limit).iloc[:-1], cfg)
                except Exception:  # noqa: BLE001
                    sig = "?"
                why = "🟢 เข้าเงื่อนไขซื้อ!" if sig == "BUY" else "✅ พร้อม (รอสัญญาณ)"
        mtxt = f"{moms[s]:+.1%}" if s in moms else "n/a"
        lines.append(f"• {s:<9} mom {mtxt:>7} | {why}")
    return "\n".join(lines)
