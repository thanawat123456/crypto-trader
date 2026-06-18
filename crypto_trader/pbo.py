"""PBO / CSCV — Probability of Backtest Overfitting (López de Prado, Bailey et al.)

Combinatorially Symmetric Cross-Validation: หั่นข้อมูลเป็น S บล็อก สลับ IS/OOS ทุกชุด
แล้วถามว่า "config ที่ดีสุดในช่วง IS ไปอยู่อันดับไหนในช่วง OOS"
ถ้ามันมักหลุดต่ำกว่าค่ากลาง OOS = กระบวนการเลือกของเรากำลัง overfit

PBO = สัดส่วนที่ config ดีสุด IS ได้อันดับ OOS ต่ำกว่าค่ากลาง (logit ≤ 0)
  PBO ต่ำ = เลือกกลยุทธ์แล้วทนทานจริง · PBO สูง = ที่เห็นว่าดีน่าจะฟลุค (read-only ไม่เทรด)
"""
from __future__ import annotations

from copy import deepcopy
from itertools import combinations

import numpy as np

from .backtest import run_backtest


def _config_returns(df, cfg, timeframe) -> np.ndarray:
    """ผลตอบแทนรายแท่งของ config หนึ่ง (จาก equity ของ backtest)"""
    eq = run_backtest(df, cfg, timeframe).equity
    return eq.pct_change().fillna(0.0).to_numpy()


def build_matrix(df, cfg, timeframe, mode: str = "strategies"):
    """สร้างเมทริกซ์ผลตอบแทน T×N (N = จำนวน config ที่จะเทียบ)"""
    cols, labels = [], []
    if mode == "ema":
        # param grid ของ ema_cross (เทสต์ overfit แบบจูนพารามิเตอร์ — ตัวอย่างคลาสสิกในเปเปอร์)
        for fast in range(5, 25, 3):
            for slow in range(20, 60, 8):
                if fast >= slow:
                    continue
                c = deepcopy(cfg)
                c["strategy"].update(name="ema_cross", fast=fast, slow=slow)
                cols.append(_config_returns(df, c, timeframe))
                labels.append(f"ema{fast}/{slow}")
    else:
        # เทียบทุกกลยุทธ์ที่บอทมี (เทสต์ว่า "การเลือกกลยุทธ์" overfit ไหม)
        from .strategy import STRATEGIES
        for name in STRATEGIES:
            c = deepcopy(cfg)
            c["strategy"]["name"] = name
            cols.append(_config_returns(df, c, timeframe))
            labels.append(name)
    return np.column_stack(cols), labels


def _sharpe(block: np.ndarray) -> np.ndarray:
    """Sharpe รายคอลัมน์ (mean/std ต่อแท่ง) — ใช้จัดอันดับ"""
    mu = block.mean(axis=0)
    sd = block.std(axis=0)
    return np.where(sd > 0, mu / sd, -np.inf)


def cscv_pbo(matrix: np.ndarray, blocks: int = 12) -> dict:
    T, N = matrix.shape
    S = blocks if blocks % 2 == 0 else blocks - 1
    bsize = T // S
    if bsize < 2 or N < 2:
        return {"error": "ข้อมูล/จำนวน config น้อยเกินไปสำหรับ CSCV"}
    M = matrix[: bsize * S]
    chunks = [M[i * bsize:(i + 1) * bsize] for i in range(S)]

    logits, n_neg, total = [], 0, 0
    for is_idx in combinations(range(S), S // 2):
        oos_idx = [b for b in range(S) if b not in is_idx]
        is_perf = _sharpe(np.vstack([chunks[i] for i in is_idx]))
        oos_perf = _sharpe(np.vstack([chunks[i] for i in oos_idx]))
        n_star = int(np.nanargmax(is_perf))                      # ดีสุดในช่วง IS
        rank = int(np.argsort(np.argsort(oos_perf))[n_star]) + 1  # อันดับ OOS (1..N, สูง=ดี)
        omega = rank / (N + 1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        lam = float(np.log(omega / (1 - omega)))
        logits.append(lam)
        n_neg += lam <= 0
        total += 1

    return {
        "pbo": n_neg / total,
        "trials_combos": total,
        "n_configs": N,
        "blocks": S,
        "median_logit": float(np.median(logits)),
    }


def summarize(stats: dict, labels: list[str], timeframe: str) -> str:
    if "error" in stats:
        return stats["error"]
    pbo = stats["pbo"]
    if pbo < 0.10:
        verdict = "✅ ทนทานดี — การเลือกไม่ค่อย overfit"
    elif pbo < 0.50:
        verdict = "⚠️ ระวัง — มีโอกาส overfit พอควร"
    else:
        verdict = "❌ overfit สูง — ที่เห็นว่าดีน่าจะฟลุค อย่าเชื่อ backtest"
    return "\n".join([
        "=" * 50,
        "  PBO / CSCV — Probability of Backtest Overfitting",
        "=" * 50,
        f"  config ที่เทียบ ({stats['n_configs']}): {', '.join(labels)}",
        f"  บล็อก S={stats['blocks']} | ชุด IS/OOS ที่ทดสอบ: {stats['trials_combos']:,}",
        "-" * 50,
        f"  PBO = {pbo:.1%}   (median logit {stats['median_logit']:+.2f})",
        f"  → {verdict}",
        "=" * 50,
    ])
