from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3
import json

from .domain import ONE, ZERO, money


def build_report(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in db.execute("SELECT * FROM metadata")}
    snapshot = db.execute("SELECT * FROM snapshots ORDER BY at DESC LIMIT 1").fetchone()
    closed = [money(r["pnl"]) for r in db.execute("SELECT pnl FROM episodes WHERE closed_at IS NOT NULL")]
    realized = sum((money(r["pnl"]) for r in db.execute("SELECT pnl FROM episodes")), ZERO)
    wins, losses = sum((p for p in closed if p > 0), ZERO), -sum((p for p in closed if p < 0), ZERO)
    fees = ZERO
    for fill in db.execute("SELECT f.*,o.symbol FROM fills f JOIN orders o ON f.order_id=o.id"):
        fees += money(fill["fee"]) * (money(fill["price"]) if fill["fee_asset"] == fill["symbol"].split("/")[0] else ONE)
    nav = money(snapshot["nav"]) if snapshot and snapshot["nav"] is not None else None
    cash = money(meta["cash"])
    initial = sum((money(r["delta"]) for r in db.execute("SELECT delta FROM ledger WHERE event_id='initial_deposit' AND account='cash'")), ZERO)
    position_cost = sum((money(r["cost"]) for r in db.execute("SELECT cost FROM positions")), ZERO)
    dust = [dict(row) for row in db.execute("SELECT * FROM dust_lots ORDER BY symbol,episode_id")] if meta.get("schema") == "4" else []
    dust_cost = sum((money(row["cost"]) for row in dust), ZERO)
    position_cost += dust_cost
    inventory = {row["symbol"]: money(row["quantity"]) for row in db.execute("SELECT * FROM positions")}
    for row in dust:
        inventory[row["symbol"]] = inventory.get(row["symbol"], ZERO) + money(row["quantity"])
    marks = json.loads(meta.get("last_marks", "{}"))
    dust_value = sum((money(row["quantity"]) * money(marks[row["symbol"]]) for row in dust), ZERO) if all(row["symbol"] in marks for row in dust) else None
    maxdd = max((money(r["drawdown"]) for r in db.execute("SELECT drawdown FROM snapshots WHERE drawdown IS NOT NULL")), default=ZERO)
    return {
        "mode": meta["mode"], "venue": meta["venue"], "source": meta["source"],
        "quote_currency": json.loads(meta["config"])["quote_currency"],
        "cash": cash, "marked_nav": nav,
        "net_return_pct": (nav / initial - ONE) * 100 if nav is not None else None,
        "realized_net_pnl": realized,
        "unrealized_net_of_entry_fees": nav - cash - position_cost if nav is not None else None,
        "fees_paid_quote_equivalent": fees,
        "max_drawdown_pct": maxdd * 100,
        "closed_episodes": len(closed),
        "win_rate_pct": sum(p > 0 for p in closed) / len(closed) * 100 if closed else None,
        "profit_factor": wins / losses if losses > 0 else None,
        "net_expectancy_quote": sum(closed, ZERO) / len(closed) if closed else None,
        "open_positions": [dict(r) for r in db.execute("SELECT * FROM positions ORDER BY symbol")],
        "dust_inventory": dust, "dust_cost_basis": dust_cost, "dust_marked_value": dust_value,
        "inventory_by_symbol": inventory,
        "execution_model_version": meta.get("execution_model_version"),
        "execution_profile": meta.get("execution_profile") or None,
        "active_orders": [dict(r) for r in db.execute("SELECT * FROM orders WHERE status IN ('INTENT','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')")],
        "risk_pause": meta.get("risk_paused") or None,
        "last_snapshot": snapshot["at"] if snapshot else None,
        "last_decisions": [dict(r) for r in db.execute("SELECT symbol,at,action,reason FROM decisions ORDER BY id DESC LIMIT 12")],
        "effective_market_rules": meta.get("effective_market_rules"),
        "fee_asset_policy": meta.get("fee_asset_policy"),
        "quote_freshness_basis": meta.get("quote_freshness_basis"),
        "simulation_limitations": ("Registered native LIMIT-IOC shadow: adverse tick/fee rounding assumption, not verified account rounding; "
                                   "unknown filter reference basis blocks fills; no exchange queue, latency or profit proof"
                                   if meta.get("execution_profile") else "Binance TH engineering simulation only: received-asset fees are proportional assumptions; "
                                   "dust retains cost basis and remains exposed; FIFO sale only when tradable, no exchange dust conversion; "
                                   "THB conversion, tax, exchange fee rounding, queue or latency proof"
                                   if meta.get("schema") == "4" else "Legacy Binance TH simulation: unsold residuals block re-entry; read-only historical artifact"
                                   if meta["venue"] == "binance_th" else None),
        "entry_policy": meta.get("entry_policy"),
        "research_gate": "INCONCLUSIVE: no untouched holdout, bootstrap or forward evidence approved",
    }


def read_report(path: Path) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        return build_report(db)
