"""Read-only episode attribution; never tunes or approves a strategy."""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import sqlite3

from .domain import ZERO, money, utc


def _aggregate(rows):
    net = sum((r["net_pnl"] for r in rows), ZERO)
    fees = sum((r["fee_quote_equivalent"] for r in rows), ZERO)
    wins = sum((r["net_pnl"] for r in rows if r["net_pnl"] > 0), ZERO)
    losses = -sum((r["net_pnl"] for r in rows if r["net_pnl"] < 0), ZERO)
    return {"episodes": len(rows), "net_pnl": net, "fee_quote_equivalent": fees,
            "pnl_plus_fees": net + fees,
            "mean_net_per_episode": net / len(rows) if rows else None,
            "profit_factor": wins / losses if losses else None,
            "fee_drag_episodes": sum(r["net_pnl"] <= 0 < r["pnl_plus_fees"] for r in rows),
            "execution_attribution_complete_episodes": sum(r["execution_attribution_complete"] for r in rows)}


def build_diagnostics(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in db.execute("SELECT * FROM metadata")}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    orders = {r["id"]: dict(r) for r in db.execute("SELECT * FROM orders")}
    episodes = {r["id"]: dict(r) for r in db.execute("SELECT * FROM episodes ORDER BY opened_at,id")}
    fills = db.execute("SELECT f.*,f.rowid AS sequence FROM fills f ORDER BY at,sequence").fetchall()
    audit = {r["order_id"]: dict(r) for r in db.execute("SELECT * FROM execution_events")} if "execution_events" in tables else {}
    contexts = { (r["symbol"], r["at"]): json.loads(r["payload"])
                 for r in db.execute("SELECT * FROM signal_contexts") } if "signal_contexts" in tables else {}
    active, grouped = {}, defaultdict(list)
    remaining = defaultdict(lambda: ZERO)
    native = meta.get("schema") == "4"
    for fill in fills:
        order = orders[fill["order_id"]]
        if native:
            allocations = db.execute("SELECT * FROM fill_allocations WHERE trade_id=?", (fill["trade_id"],)).fetchall()
            if not allocations:
                raise ValueError("Native fill lacks FIFO allocation")
            for allocation in allocations:
                episode = allocation["episode_id"]
                if episode not in episodes:
                    raise ValueError("FIFO allocation lacks episode")
                projected = dict(fill)
                projected["allocation_cash_flow"] = money(allocation["cash_flow"])
                projected["allocation_fee_quote"] = money(allocation["fee_quote"])
                if order["side"] == "sell":
                    projected["quantity"] = allocation["quantity"]
                    projected["fee"] = allocation["fee_quote"]
                grouped[episode].append((projected, order))
            continue
        symbol, base = order["symbol"], order["symbol"].split("/")[0]
        quantity = money(fill["quantity"])
        base_fee = money(fill["fee"]) if fill["fee_asset"] == base else ZERO
        if order["side"] == "buy":
            episode = active.setdefault(symbol, order["id"])
            remaining[symbol] += quantity - base_fee
        else:
            if symbol not in active:
                raise ValueError("Exit fill lacks an attributable entry episode")
            episode = active[symbol]
            remaining[symbol] -= quantity + base_fee
        if episode not in episodes:
            raise ValueError("Fill episode is absent from ledger projection")
        grouped[episode].append((fill, order))
        if remaining[symbol] < 0:
            raise ValueError("Diagnostic inventory reconciliation failed")
        if remaining[symbol] == 0:
            active.pop(symbol, None)
    rows = []
    for identifier, episode in episodes.items():
        associated = grouped[identifier]
        if not associated:
            raise ValueError("Episode lacks fills")
        entry_order = orders[identifier]
        context = contexts.get((episode["symbol"], entry_order["created_at"]))
        fee_total = cashflow = spread = slippage = ZERO
        complete = True
        exit_reasons = []
        entry_notional = entry_quantity = exit_quantity = ZERO
        for fill, order in associated:
            q, price, fee = (money(fill[k]) for k in ("quantity", "price", "fee"))
            base, quote = order["symbol"].split("/")
            if fill["fee_asset"] not in {base, quote}:
                raise ValueError("Unsupported fee asset in diagnostics")
            fee_total += fill["allocation_fee_quote"] if native else fee * price if fill["fee_asset"] == base else fee
            cashflow += fill["allocation_cash_flow"] if native else q * price * (1 if order["side"] == "sell" else -1) - (fee if fill["fee_asset"] == quote else ZERO)
            if order["side"] == "buy":
                entry_quantity += q
                entry_notional += q * price
            else:
                exit_quantity += q
                exit_reasons.append(order["reason"])
            event = audit.get(order["id"])
            if not event or event["at"] != fill["at"] or (not native and event["episode_id"] != identifier):
                complete = False
                continue
            bid, ask = money(event["bid"]), money(event["ask"])
            mid = (bid + ask) / 2
            spread += q * ((ask - mid) if order["side"] == "buy" else (mid - bid))
            slippage += q * ((price - ask) if order["side"] == "buy" else (bid - price))
        net = money(episode["pnl"])
        closed = episode["closed_at"] is not None
        if closed and abs(cashflow - net) > money("1e-18"):
            raise ValueError("Episode PnL does not reconcile to cash flows")
        hours = (utc(episode["closed_at"]) - utc(episode["opened_at"])).total_seconds() / 3600 if closed else None
        duration = "open" if hours is None else "<=24h" if hours <= 24 else "24-72h" if hours <= 72 else "72-168h" if hours <= 168 else ">168h"
        reasons = sorted(set(exit_reasons))
        intent_risk = money(meta.get("initial_risk:" + identifier, "0"))
        effective_risk = intent_risk * entry_quantity / money(entry_order["quantity"])
        rows.append({"episode_id": identifier, "symbol": episode["symbol"], "opened_at": episode["opened_at"],
                     "closed_at": episode["closed_at"], "closed": closed, "holding_hours": hours,
                     "duration_bucket": duration, "entry_regime": context["regime"] if context else "not_recorded",
                     "entry_context": context, "exit_reason": reasons[0] if len(reasons) == 1 else "mixed" if reasons else "open",
                     "exit_reasons": reasons, "entry_quantity": entry_quantity, "exit_quantity": exit_quantity,
                     "entry_notional": entry_notional, "net_pnl": net, "fee_quote_equivalent": fee_total,
                     "pnl_plus_fees": net + fee_total if closed else None,
                     "net_return_on_entry_notional_pct": net / entry_notional * 100 if closed and entry_notional else None,
                     "net_r_multiple": net / effective_risk if closed and effective_risk else None,
                     "execution_attribution_complete": complete,
                     "spread_cost": spread if complete else None, "slippage_cost": slippage if complete else None})
    closed_rows = [r for r in rows if r["closed"]]
    groups = {}
    for field in ("symbol", "entry_regime", "exit_reason", "duration_bucket"):
        buckets = defaultdict(list)
        for row in closed_rows:
            buckets[row[field]].append(row)
        groups[field] = {key: _aggregate(values) for key, values in sorted(buckets.items())}
    decisions = [dict(r) for r in db.execute("SELECT action,reason,COUNT(*) AS count FROM decisions GROUP BY action,reason ORDER BY action,reason")]
    outcomes = Counter(e["outcome"] for e in audit.values())
    entry_evaluations = [dict(r) for r in db.execute("SELECT * FROM entry_evaluations ORDER BY evaluated_at,symbol")] if "entry_evaluations" in tables else []
    buy_orders = [o for o in orders.values() if o["side"] == "buy"]
    sell_orders = [o for o in orders.values() if o["side"] == "sell"]
    candidates = db.execute("SELECT COUNT(*) FROM signal_contexts WHERE enter_signal=1").fetchone()[0] if "signal_contexts" in tables else None
    return {"schema": 1, "scope": meta.get("analysis_scope", "descriptive ledger diagnostics; not a validation gate"),
            "mode": meta["mode"], "source": meta["source"], "config_hash": meta["config_hash"],
            "dataset_hash": meta.get("dataset_hash"), "context_schema": meta.get("context_schema"),
            "execution_model_version": meta.get("execution_model_version", "legacy / not recorded"),
            "execution_scenario": json.loads(meta.get("execution_scenario", "{}")),
            "entry_policy": json.loads(meta.get("entry_policy", "{}")),
            "entry_evaluations": [{**row, "payload": json.loads(row["payload"])} for row in entry_evaluations],
            "entry_evaluation_reasons": dict(Counter(row["reason"] for row in entry_evaluations)),
            "evaluation_start": meta.get("evaluation_start"), "evaluation_end": meta.get("evaluation_end"),
            "summary": _aggregate(closed_rows), "open_episode_count": len(rows) - len(closed_rows),
            "groups": groups, "episodes": rows, "decisions": decisions,
            "execution": {"candidate_signals": candidates, "entry_orders": len(buy_orders),
                          "filled_entries": sum(money(o["filled"]) > 0 for o in buy_orders),
                          "partial_entries": sum(0 < money(o["filled"]) < money(o["quantity"]) for o in buy_orders),
                          "no_fill_entries": sum(money(o["filled"]) == 0 and o["status"] == "CANCELED" for o in buy_orders),
                          "exit_orders": len(sell_orders), "partial_exits": sum(0 < money(o["filled"]) < money(o["quantity"]) for o in sell_orders),
                          "incomplete_execution_audits": sum(e["status"] != orders[e["order_id"]]["status"] or e["filled"] != orders[e["order_id"]]["filled"] for e in audit.values()),
                          "outcomes": dict(outcomes)},
            "approved_for_live": False,
            "dust_accounting": "FIFO per-fill allocations; episodes remain open until their dust is actually sold" if native else None,
            "limitations": ["Descriptive buckets use fixed rules, not predictive or causal explanations of losses",
                            "Executed episodes are selection-biased; rejected signals have no counterfactual trade labels",
                            "PnL plus fee value is accounting attribution, not a fee-free strategy rerun; base fees change inventory",
                            "Open episodes are excluded from closed-episode groups; their partial realized PnL remains in the ledger",
                            "Native FIFO closure dates can include delayed dust disposal; they are not the strategy's desired exit date",
                            "Missing quote audits/context remain unknown, not reconstructed from future data",
                            "Execution quote audits are auxiliary; fills/ledger are authoritative if an audit is incomplete after a crash",
                            "Historical OHLC stop timestamps are bar-end conventions, so holding duration is approximate",
                            "OHLC cannot establish intrabar MFE/MAE, queue position or measured market impact"]}


def read_diagnostics(path: Path) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        return build_diagnostics(db)


def export_diagnostics(report: dict, output: Path):
    if output.exists():
        raise ValueError("Diagnostic output exists; use a new directory")
    output.mkdir(parents=True)
    with (output / "report.json").open("x") as stream:
        json.dump(report, stream, indent=2, default=str)
    fields = ("episode_id", "symbol", "opened_at", "closed_at", "closed", "holding_hours", "entry_regime",
              "exit_reason", "entry_quantity", "exit_quantity", "entry_notional", "net_pnl", "fee_quote_equivalent",
              "pnl_plus_fees", "net_r_multiple", "execution_attribution_complete", "spread_cost", "slippage_cost")
    with (output / "episodes.csv").open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["episodes"])
    summary = report["summary"]
    lines = ["# Descriptive V2 trade diagnostics", "", f"Scope: {report['scope']}. Approved for live: False.", "",
             f"Closed episodes: {summary['episodes']}; open episodes: {report['open_episode_count']}",
             f"Closed net PnL: {summary['net_pnl']}; fee quote equivalent: {summary['fee_quote_equivalent']}", ""]
    for field, buckets in report["groups"].items():
        lines += [f"## By {field}", "", "| Bucket | Episodes | Net PnL | Fees | PnL + fees |", "| --- | ---: | ---: | ---: | ---: |"]
        lines += [f"| {key} | {b['episodes']} | {b['net_pnl']:.4f} | {b['fee_quote_equivalent']:.4f} | {b['pnl_plus_fees']:.4f} |" for key, b in buckets.items()]
        lines.append("")
    lines += ["## Limitations", ""] + [f"- {item}" for item in report["limitations"]]
    with (output / "report.md").open("x") as stream:
        stream.write("\n".join(lines) + "\n")
