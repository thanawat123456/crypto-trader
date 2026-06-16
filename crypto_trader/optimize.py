"""ค้นหาพารามิเตอร์ที่ดีที่สุดด้วยการกวาด (grid search) บน backtest

หมายเหตุ: เป็นการ optimize บนข้อมูลในอดีต ระวัง overfitting —
ค่าที่ดีที่สุดในอดีตไม่รับประกันอนาคต ควรทดสอบกับช่วงเวลาอื่นด้วย
"""
from __future__ import annotations

from copy import deepcopy

import pandas as pd

from .backtest import run_backtest


def optimize_ema(df: pd.DataFrame, cfg: dict, timeframe: str,
                 fast_range=range(5, 25, 2), slow_range=range(20, 60, 5),
                 metric: str = "total_return") -> pd.DataFrame:
    """กวาดคู่ (fast, slow) ของ ema_cross แล้วจัดอันดับตาม metric

    metric: 'total_return' | 'sharpe'
    """
    rows = []
    for fast in fast_range:
        for slow in slow_range:
            if fast >= slow:
                continue
            trial = deepcopy(cfg)
            trial["strategy"]["name"] = "ema_cross"
            trial["strategy"]["fast"] = fast
            trial["strategy"]["slow"] = slow
            res = run_backtest(df, trial, timeframe)
            rows.append(
                {
                    "fast": fast,
                    "slow": slow,
                    "return_%": round(res.total_return, 2),
                    "trades": res.trades,
                    "maxDD_%": round(res.max_drawdown, 2),
                    "sharpe": round(res.sharpe, 2),
                }
            )

    table = pd.DataFrame(rows)
    sort_key = "sharpe" if metric == "sharpe" else "return_%"
    return table.sort_values(sort_key, ascending=False).reset_index(drop=True)
