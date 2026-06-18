"""Walk-forward validation — ทดสอบกลยุทธ์แบบกัน overfitting

แนวคิด: แบ่งข้อมูลเป็นหน้าต่างเลื่อน ๆ แต่ละหน้าต่าง
  1) optimize หาพารามิเตอร์ที่ดีสุดบนช่วง "train" (in-sample)
  2) เอาพารามิเตอร์นั้นไปทดสอบบนช่วง "test" ที่ "ไม่เคยเห็น" (out-of-sample)

ถ้าผล out-of-sample ใกล้เคียง in-sample → กลยุทธ์ทนทานจริง
ถ้า out-of-sample แย่กว่ามาก → overfit (ดีแต่ในอดีต ใช้จริงไม่รอด)
"""
from __future__ import annotations

from copy import deepcopy

import pandas as pd

from .backtest import run_backtest
from .optimize import optimize_ema


def walk_forward(
    df: pd.DataFrame,
    cfg: dict,
    timeframe: str,
    train: int = 300,
    test: int = 100,
    step: int = 100,
    metric: str = "total_return",
    fast_range=range(5, 25, 2),
    slow_range=range(20, 60, 5),
) -> pd.DataFrame:
    """เลื่อนหน้าต่าง train→test ไปเรื่อย ๆ คืนตารางผลแต่ละหน้าต่าง"""
    rows = []
    n = len(df)
    start = 0
    while start + train + test <= n:
        train_df = df.iloc[start:start + train]
        test_df = df.iloc[start + train:start + train + test]

        table = optimize_ema(train_df, cfg, timeframe, fast_range, slow_range, metric)
        if table.empty:
            start += step
            continue
        best = table.iloc[0]

        trial = deepcopy(cfg)
        trial["strategy"]["name"] = "ema_cross"
        trial["strategy"]["fast"] = int(best["fast"])
        trial["strategy"]["slow"] = int(best["slow"])
        oos = run_backtest(test_df, trial, timeframe)

        rows.append({
            "test_end": str(test_df.index[-1])[:10],
            "fast": int(best["fast"]),
            "slow": int(best["slow"]),
            "in_sample_%": round(float(best["return_%"]), 2),
            "out_sample_%": round(oos.total_return, 2),
            "oos_buyhold_%": round(oos.buy_hold_return, 2),
            "oos_trades": oos.trades,
            "oos_maxDD_%": round(oos.max_drawdown, 2),
        })
        start += step

    return pd.DataFrame(rows)


def summarize(table: pd.DataFrame) -> str:
    """สรุปผล walk-forward เป็นข้อความอ่านง่าย + เตือน overfit"""
    if table.empty:
        return "ข้อมูลไม่พอสำหรับ walk-forward (ลองเพิ่ม -l หรือลด --train/--test)"
    avg_is = table["in_sample_%"].mean()
    avg_oos = table["out_sample_%"].mean()
    sum_oos = table["out_sample_%"].sum()
    win_windows = int((table["out_sample_%"] > 0).sum())
    n = len(table)
    gap = avg_is - avg_oos

    verdict = "✅ ทนทานดี (gap น้อย)"
    if gap > abs(avg_is) * 0.7 and avg_is > 0:
        verdict = "⚠️ น่าจะ overfit (in-sample ดีกว่า out-of-sample เยอะ)"
    if avg_oos <= 0:
        verdict = "❌ out-of-sample ขาดทุนเฉลี่ย — กลยุทธ์นี้ยังไม่น่าใช้จริง"

    return "\n".join([
        "=" * 52,
        "  Walk-forward summary",
        "=" * 52,
        f"  จำนวนหน้าต่าง        : {n}",
        f"  in-sample เฉลี่ย     : {avg_is:+.2f}%  (ผลตอน optimize — มองโลกสวย)",
        f"  out-of-sample เฉลี่ย : {avg_oos:+.2f}%  (ผลของจริงที่คาดหวังได้)",
        f"  out-of-sample รวม    : {sum_oos:+.2f}%",
        f"  หน้าต่างที่กำไร       : {win_windows}/{n}",
        f"  overfit gap          : {gap:.2f}%",
        "-" * 52,
        f"  สรุป: {verdict}",
        "=" * 52,
    ])
