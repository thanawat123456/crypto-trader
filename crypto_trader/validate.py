"""ตรวจสุขภาพกลยุทธ์เป็นรอบ ๆ (routine validation) — รู้ตัวถ้ากลยุทธ์เริ่มเสื่อม

ทำ 3 อย่าง (เบา รันบน e2-micro ได้):
  1) backtest กลยุทธ์ปัจจุบันบนแต่ละเหรียญ → เมตริก (return, PF, Sharpe, maxDD)
  2) เทียบช่วง "ล่าสุด" vs "ก่อนหน้า" → ตรวจว่ากลยุทธ์เสื่อมไหม
  3) Monte Carlo บนเหรียญตัวแทน → ดูความเสี่ยง (กรณีแย่/โอกาสขาดทุน)

*ไม่* ปรับ risk อัตโนมัติ — แค่ "เตือน" ให้คนตัดสินใจ (กัน overfit จาก noise ระยะสั้น)
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone

from .backtest import run_backtest
from .data import fetch_ohlcv
from .montecarlo import monte_carlo

LOG_PATH = "validation_log.csv"
_LOG_FIELDS = ["timestamp", "symbol", "strategy", "return_%", "profit_factor",
               "sharpe", "maxDD_%", "recent_%", "earlier_%"]


def _log_rows(rows: list[dict], path: str = LOG_PATH) -> None:
    exists = os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_LOG_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def run_validation(exchange, cfg: dict, symbols: list[str], timeframe: str,
                   limit: int = 1000, mc_runs: int = 500) -> tuple[str, bool]:
    """คืน (ข้อความสรุป, degraded?) + เขียน log"""
    strat = cfg["strategy"]["name"]
    ts = datetime.now(timezone.utc).isoformat()
    log_rows, report_rows = [], []
    sum_recent = sum_earlier = sum_pf = 0.0
    n = 0

    for sym in symbols:
        try:
            df = fetch_ohlcv(exchange, sym, timeframe, limit)
        except Exception as e:  # noqa: BLE001
            report_rows.append(f"• {sym}: ดึงข้อมูลไม่ได้ ({str(e)[:30]})")
            continue
        full = run_backtest(df, cfg, timeframe)
        cut = len(df) * 2 // 3            # 2/3 แรก = earlier, 1/3 ท้าย = recent
        earlier = run_backtest(df.iloc[:cut], cfg, timeframe)
        recent = run_backtest(df.iloc[cut:], cfg, timeframe)
        n += 1
        sum_recent += recent.total_return
        sum_earlier += earlier.total_return
        sum_pf += full.profit_factor

        log_rows.append({
            "timestamp": ts, "symbol": sym, "strategy": strat,
            "return_%": round(full.total_return, 2),
            "profit_factor": round(full.profit_factor, 2),
            "sharpe": round(full.sharpe, 2),
            "maxDD_%": round(full.max_drawdown, 2),
            "recent_%": round(recent.total_return, 2),
            "earlier_%": round(earlier.total_return, 2),
        })
        report_rows.append(
            f"• {sym}: ret {full.total_return:+.1f}% | PF {full.profit_factor:.2f} | "
            f"recent {recent.total_return:+.1f}% vs ก่อน {earlier.total_return:+.1f}%"
        )

    if n == 0:
        return "⚠️ validate: ดึงข้อมูลไม่ได้เลย", False

    _log_rows(log_rows)
    avg_pf = sum_pf / n

    # เกณฑ์ "เสื่อม" (อนุรักษ์นิยม): ช่วงล่าสุดติดลบทั้งที่ก่อนหน้าบวก หรือ PF เฉลี่ย < 1
    reasons = []
    if sum_recent < 0 and sum_earlier > 0:
        reasons.append("ช่วงล่าสุดติดลบ")
    if avg_pf < 1.0:
        reasons.append(f"PF เฉลี่ย {avg_pf:.3f}<1")
    degraded = bool(reasons)

    # Monte Carlo บนเหรียญตัวแทน (ตัวแรก) — ดูความเสี่ยง
    mc_line = ""
    try:
        mc = monte_carlo(fetch_ohlcv(exchange, symbols[0], timeframe, limit),
                         cfg, timeframe, runs=mc_runs)
        if "error" not in mc:
            mc_line = (f"🎲 MC {symbols[0]}: median {mc['ret_median']:+.1f}% | "
                       f"แย่สุด(P05) {mc['ret_p05']:+.1f}% | โอกาสขาดทุน {mc['prob_loss']:.0f}%")
    except Exception:  # noqa: BLE001
        pass

    head = ("⚠️ กลยุทธ์อาจเสื่อม (" + ", ".join(reasons) + ") — พิจารณาลด risk/หยุด"
            if degraded else "✅ กลยุทธ์ยังปกติ")
    lines = [f"🔬 Validation รายสัปดาห์ ({strat})", "─" * 32, head,
             f"recent รวม {sum_recent:+.1f}% vs ก่อนหน้า {sum_earlier:+.1f}% | PF เฉลี่ย {avg_pf:.2f}",
             "─ รายเหรียญ ─", *report_rows]
    if mc_line:
        lines += ["─" * 32, mc_line]
    return "\n".join(lines), degraded
