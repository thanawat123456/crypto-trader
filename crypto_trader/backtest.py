"""เครื่องมือ backtest — ทดสอบกลยุทธ์ย้อนหลังบนข้อมูลในอดีต

หลีกเลี่ยง lookahead bias: สัญญาณที่เกิดบนแท่ง t จะถูกนำไปเทรดที่แท่ง t+1
(เลื่อน position ด้วย .shift(1))
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .strategy import get_position


@dataclass
class BacktestResult:
    equity: pd.Series          # มูลค่าพอร์ตตามเวลา
    trades: int
    total_return: float        # ผลตอบแทนกลยุทธ์ (%)
    buy_hold_return: float     # ผลตอบแทนถ้าซื้อแล้วถือเฉยๆ (%)
    max_drawdown: float        # ดรอว์ดาวน์สูงสุด (%)
    sharpe: float
    final_value: float
    initial_cash: float
    params: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            "=" * 44,
            "  ผลการ Backtest",
            "=" * 44,
            f"  เงินเริ่มต้น      : {self.initial_cash:,.2f}",
            f"  มูลค่าสุดท้าย     : {self.final_value:,.2f}",
            f"  ผลตอบแทนกลยุทธ์  : {self.total_return:+.2f}%",
            f"  ซื้อแล้วถือเฉยๆ   : {self.buy_hold_return:+.2f}%",
            f"  จำนวนการเทรด     : {self.trades}",
            f"  Max Drawdown     : {self.max_drawdown:.2f}%",
            f"  Sharpe (annual)  : {self.sharpe:.2f}",
            "=" * 44,
        ]
        return "\n".join(lines)


# จำนวนแท่งต่อปี โดยประมาณ (ใช้คำนวณ Sharpe)
_BARS_PER_YEAR = {
    "1m": 525600, "5m": 105120, "15m": 35040, "30m": 17520,
    "1h": 8760, "2h": 4380, "4h": 2190, "1d": 365,
}


def run_backtest(df: pd.DataFrame, cfg: dict, timeframe: str = "1h") -> BacktestResult:
    bt = cfg["backtest"]
    fee = float(bt["fee"])
    initial = float(bt["initial_cash"])

    position = get_position(df, cfg)
    # เทรดที่แท่งถัดไปจากที่เกิดสัญญาณ
    held = position.shift(1).fillna(0)

    # ผลตอบแทนรายแท่งของสินทรัพย์
    asset_ret = df["close"].pct_change().fillna(0)
    # ผลตอบแทนของกลยุทธ์ = ถือเมื่อ held==1
    strat_ret = asset_ret * held

    # หักค่าธรรมเนียมตอนเปลี่ยนสถานะ (เข้า/ออก)
    turnover = held.diff().abs().fillna(held.abs())
    strat_ret = strat_ret - turnover * fee

    equity = (1 + strat_ret).cumprod() * initial

    # เมตริก
    trades = int((held.diff().abs() > 0).sum())
    total_return = (equity.iloc[-1] / initial - 1) * 100
    buy_hold_return = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100

    running_max = equity.cummax()
    drawdown = (equity / running_max - 1) * 100
    max_drawdown = drawdown.min()

    bars = _BARS_PER_YEAR.get(timeframe, 8760)
    std = strat_ret.std()
    sharpe = (strat_ret.mean() / std * np.sqrt(bars)) if std > 0 else 0.0

    return BacktestResult(
        equity=equity,
        trades=trades,
        total_return=total_return,
        buy_hold_return=buy_hold_return,
        max_drawdown=max_drawdown,
        sharpe=sharpe,
        final_value=float(equity.iloc[-1]),
        initial_cash=initial,
        params=dict(cfg["strategy"]),
    )
