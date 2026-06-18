"""Monte Carlo simulation — ประเมินความเสี่ยงที่ "แท้จริง" ของกลยุทธ์

แนวคิด (จาก Poudel & Paudel 2025): ผลลัพธ์ backtest ครั้งเดียวคือ "ลำดับเหตุการณ์เดียว"
ที่อาจโชคดี/โชคร้าย เราจึงสุ่มสลับลำดับผลตอบแทนรายแท่งหลายพันครั้ง (bootstrap)
เพื่อดูการกระจายของผลลัพธ์ที่เป็นไปได้ → รู้ "กรณีแย่" ไม่ใช่แค่ค่าเฉลี่ย
"""
from __future__ import annotations

import numpy as np

from .backtest import run_backtest


def _percentile(arr, p: float) -> float:
    return float(np.percentile(arr, p))


def monte_carlo(df, cfg: dict, timeframe: str, runs: int = 1000, seed: int = 42) -> dict:
    """bootstrap ผลตอบแทนรายแท่งจาก backtest จริง คืนสถิติการกระจาย"""
    res = run_backtest(df, cfg, timeframe)
    returns = res.equity.pct_change().dropna().to_numpy()
    returns = returns[np.isfinite(returns)]
    if len(returns) < 10:
        return {"error": "ข้อมูลน้อยเกินไปสำหรับ Monte Carlo"}

    rng = np.random.default_rng(seed)
    finals, drawdowns = [], []
    for _ in range(runs):
        sample = rng.choice(returns, size=len(returns), replace=True)
        eq = np.cumprod(1.0 + sample)
        finals.append((eq[-1] - 1.0) * 100.0)
        peak = np.maximum.accumulate(eq)
        drawdowns.append(float((eq / peak - 1.0).min()) * 100.0)

    finals = np.array(finals)
    drawdowns = np.array(drawdowns)
    return {
        "runs": runs,
        "actual_return": res.total_return,
        "actual_maxdd": res.max_drawdown,
        "ret_median": _percentile(finals, 50),
        "ret_p05": _percentile(finals, 5),     # กรณีแย่ (5%)
        "ret_p95": _percentile(finals, 95),    # กรณีดี (95%)
        "prob_loss": float((finals < 0).mean() * 100),
        "mdd_median": _percentile(drawdowns, 50),
        "mdd_p95worst": _percentile(drawdowns, 5),  # drawdown แย่สุด 5%
    }


def summarize(stats: dict) -> str:
    if "error" in stats:
        return stats["error"]
    return "\n".join([
        "=" * 50,
        f"  Monte Carlo ({stats['runs']:,} รอบ — สุ่มสลับลำดับผล)",
        "=" * 50,
        f"  ผลจริง (1 ลำดับ)   : {stats['actual_return']:+.2f}%  (maxDD {stats['actual_maxdd']:.1f}%)",
        "-" * 50,
        f"  ผลตอบแทนกลาง (P50) : {stats['ret_median']:+.2f}%",
        f"  กรณีดี   (P95)     : {stats['ret_p95']:+.2f}%",
        f"  กรณีแย่   (P05)     : {stats['ret_p05']:+.2f}%",
        f"  โอกาสขาดทุน        : {stats['prob_loss']:.1f}%",
        "-" * 50,
        f"  Max DD กลาง (P50)  : {stats['mdd_median']:.2f}%",
        f"  Max DD แย่สุด (P05): {stats['mdd_p95worst']:.2f}%  ← เตรียมใจรับให้ไหว",
        "=" * 50,
    ])
