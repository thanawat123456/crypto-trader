"""เครื่องมือ backtest — ทดสอบกลยุทธ์ย้อนหลังบนข้อมูลในอดีต

หลีกเลี่ยง lookahead bias: สัญญาณที่เกิดบนแท่ง t จะถูกนำไปเทรดที่แท่ง t+1
(เลื่อน position ด้วย .shift(1))
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import atr
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
    risk = cfg.get("risk", {})
    fee = float(bt["fee"])
    initial = float(bt["initial_cash"])
    position_size = min(
        float(bt.get("position_size", 1.0)),
        float(risk.get("max_position_pct", 1.0)),
    )
    stop_loss = float(risk.get("stop_loss_pct", 0.0) or 0.0)
    take_profit = float(risk.get("take_profit_pct", 0.0) or 0.0)
    trailing = float(risk.get("trailing_stop_pct", 0.0) or 0.0)
    breakeven = float(risk.get("breakeven_trigger_pct", 0.0) or 0.0)

    # ATR-based stops: เตรียมระยะ SL/TP รายแท่งตามความผันผวนจริง
    atr_enabled = bool(risk.get("atr_stops_enabled", False))
    atr_pct = None
    if atr_enabled:
        atr_pct = (atr(df, int(risk.get("atr_period", 14))) / df["close"]).fillna(0.0)
        atr_sl_mult = float(risk.get("atr_sl_mult", 2.0))
        atr_tp_mult = float(risk.get("atr_tp_mult", 3.0))

    signal_position = get_position(df, cfg).shift(1).fillna(0)
    closes = df["close"]

    equity_values = []
    strat_returns = []
    current_equity = initial
    held = 0.0
    entry_price = None
    peak_price = None
    trades = 0

    for i, (_, price) in enumerate(closes.items()):
        asset_ret = 0.0 if i == 0 else float(price / closes.iloc[i - 1] - 1)
        strat_ret = asset_ret * held

        target = position_size if signal_position.iloc[i] == 1 else 0.0
        if held > 0 and entry_price:
            move = float(price / entry_price - 1)
            peak_price = max(peak_price or entry_price, float(price))
            # ระยะ SL/TP — ปรับตาม ATR ถ้าเปิดใช้ ไม่งั้น % ตายตัว
            if atr_enabled and float(atr_pct.iloc[i]) > 0:
                sl_dist = atr_sl_mult * float(atr_pct.iloc[i])
                tp_dist = atr_tp_mult * float(atr_pct.iloc[i])
            else:
                sl_dist, tp_dist = stop_loss, take_profit
            hit_sl = sl_dist and move <= -sl_dist
            hit_tp = tp_dist and move >= tp_dist
            hit_trail = trailing and price <= peak_price * (1 - trailing)
            hit_be = (
                breakeven
                and peak_price >= entry_price * (1 + breakeven)
                and price <= entry_price
            )
            if hit_sl or hit_tp or hit_trail or hit_be:
                target = 0.0

        turnover = abs(target - held)
        if turnover > 0:
            trades += 1
            strat_ret -= turnover * fee
            entry_price = float(price) if target > 0 else None
            peak_price = float(price) if target > 0 else None

        current_equity *= 1 + strat_ret
        equity_values.append(current_equity)
        strat_returns.append(strat_ret)
        held = target

    equity = pd.Series(equity_values, index=df.index)
    strat_ret = pd.Series(strat_returns, index=df.index)

    # เมตริก
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
