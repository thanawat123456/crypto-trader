"""สรุปผลงานการเทรดจาก trade_journal.csv → ข้อความสำหรับส่ง Discord/console

เมตริก (จาก quant paper Poudel & Paudel): win rate, profit factor,
avg win/loss, max loss — แยกรายเหรียญ + ภาพรวม
"""
from __future__ import annotations

from .journal import _read_rows


def _pnl(row: dict) -> float:
    try:
        return float(row.get("pnl") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_pf(wins_sum: float, losses_sum: float) -> str:
    if losses_sum < 0:
        return f"{wins_sum / abs(losses_sum):.2f}"
    return "∞" if wins_sum > 0 else "0.00"


def _stats_line(label: str, sells: list[dict]) -> str:
    pnls = [_pnl(r) for r in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = (len(wins) / len(pnls) * 100) if pnls else 0.0
    pf = _fmt_pf(sum(wins), sum(losses))
    avg_w = (sum(wins) / len(wins)) if wins else 0.0
    avg_l = (sum(losses) / len(losses)) if losses else 0.0
    max_l = min(pnls) if pnls else 0.0
    return (f"{label}: {len(pnls)}ไม้ | WR {wr:.0f}% | PF {pf} | "
            f"avgW {avg_w:+.1f}/avgL {avg_l:+.1f} | maxL {max_l:+.1f} | ΣPnL {sum(pnls):+.1f}")


def build_report(journal_path: str, portfolio: dict | None = None,
                 initial_cash: float = 300.0, period_label: str = "สะสมทั้งหมด") -> str:
    rows = _read_rows(journal_path)
    sells = [r for r in rows if (r.get("side") or "").upper() == "SELL"]

    lines = [f"📊 รายงานผลงานบอท ({period_label})", "─" * 32]
    if portfolio:
        cash = float(portfolio.get("cash", initial_cash))
        realized = float(portfolio.get("realized_pnl", 0.0))
        lines.append(f"💰 Paper cash {cash:,.2f} | return {realized / initial_cash * 100:+.2f}%")

    if not sells:
        lines.append("(ยังไม่มีไม้ที่ปิด — รอบอทเทรด)")
        return "\n".join(lines)

    lines.append(_stats_line("รวม", sells))
    lines.append("─ รายเหรียญ ─")
    for sym in sorted({r.get("symbol") for r in sells if r.get("symbol")}):
        lines.append("• " + _stats_line(sym, [r for r in sells if r.get("symbol") == sym]))
    return "\n".join(lines)
