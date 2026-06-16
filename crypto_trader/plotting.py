"""วาดกราฟราคา + อินดิเคเตอร์ และกราฟ equity จาก backtest"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind


def plot_chart(df: pd.DataFrame, cfg: dict, out_path: str = "chart.png") -> str:
    import matplotlib
    matplotlib.use("Agg")  # ไม่ต้องมีหน้าจอ
    import matplotlib.pyplot as plt

    s = cfg["strategy"]
    fast = ind.ema(df["close"], s["fast"])
    slow = ind.ema(df["close"], s["slow"])
    rsi = ind.rsi(df["close"], s["rsi_period"])

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )

    ax1.plot(df.index, df["close"], label="Close", linewidth=1.2)
    ax1.plot(df.index, fast, label=f"EMA{s['fast']}", linewidth=1, alpha=0.8)
    ax1.plot(df.index, slow, label=f"EMA{s['slow']}", linewidth=1, alpha=0.8)
    ax1.set_title("Price + EMA")
    ax1.legend(loc="upper left")
    ax1.grid(alpha=0.3)

    ax2.plot(df.index, rsi, color="purple", linewidth=1)
    ax2.axhline(s["rsi_overbought"], color="red", linestyle="--", alpha=0.5)
    ax2.axhline(s["rsi_oversold"], color="green", linestyle="--", alpha=0.5)
    ax2.set_ylim(0, 100)
    ax2.set_title(f"RSI({s['rsi_period']})")
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


def plot_equity(equity: pd.Series, out_path: str = "equity.png") -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(equity.index, equity.values, color="teal", linewidth=1.3)
    ax.set_title("Equity Curve")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path
