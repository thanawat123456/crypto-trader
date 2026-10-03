"""Independent, read-only replay of schema-4 fills, dust transfers and NAV.

Does not call the writer's FIFO allocator, Store, or mutate/migrate a database.
This verifies accounting, not price authenticity, profit or live readiness.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from .domain import ZERO, money, utc


TOLERANCE = Decimal("1e-20")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def equal(left, right, message):
    require(abs(money(left) - money(right)) <= TOLERANCE, message)


def verify_dust_ledger(path: Path, cfg):
    cfg.validate()
    require(cfg.venue == "binance_th", "Dust auditor requires Binance TH config")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failure")
        require(not db.execute("PRAGMA foreign_key_check").fetchall(), "Foreign-key failure")
        meta = {row["key"]: row["value"] for row in db.execute("SELECT * FROM metadata")}
        require(meta["schema"] == "4" and meta["config_hash"] == cfg.digest(), "Dust schema/config binding mismatch")
        require(meta["venue"] == cfg.venue and meta["mode"] in {"PAPER", "BACKTEST"}, "Dust venue/mode mismatch")
        require(meta["execution_model_version"] == "received-fee-dust-fifo-v1", "Dust execution model mismatch")
        ledger = [dict(row) for row in db.execute("SELECT * FROM ledger ORDER BY id")]
        fills = {row["trade_id"]: dict(row) for row in db.execute("SELECT * FROM fills")}
        transfers = {"dust:" + row["episode_id"]: dict(row) for row in db.execute("SELECT * FROM dust_transfers")}
        orders = {row["id"]: dict(row) for row in db.execute("SELECT * FROM orders")}
        episodes = {row["id"]: dict(row) for row in db.execute("SELECT * FROM episodes")}
        allocations = defaultdict(list)
        for row in db.execute("SELECT * FROM fill_allocations"):
            allocations[row["trade_id"]].append(dict(row))
        events = defaultdict(list)
        for row in ledger:
            events[row["event_id"]].append(row)
        require(set(events) == {"initial_deposit"} | set(fills) | set(transfers), "Missing/unrecognized accounting event")
        for rows in events.values():
            totals = defaultdict(lambda: ZERO)
            for row in rows:
                totals[row["asset"]] += money(row["delta"])
            require(all(abs(value) <= TOLERANCE for value in totals.values()), "Per-event/currency double-entry failure")
        balances = defaultdict(lambda: ZERO)
        positions, parked, pnl, fill_sums = {}, {}, defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        cash = cfg.initial_cash
        symbols = {instrument.symbol for instrument in cfg.instruments}
        for event, rows in sorted(events.items(), key=lambda pair: min(row["id"] for row in pair[1])):
            actual = defaultdict(lambda: ZERO)
            for row in rows:
                actual[(row["account"], row["asset"])] += money(row["delta"])
                balances[(row["account"], row["asset"])] += money(row["delta"])
            expected = defaultdict(lambda: ZERO)

            def post(debit, credit, asset, delta):
                expected[(debit, asset)] += delta
                expected[(credit, asset)] -= delta

            if event == "initial_deposit":
                post("cash", "external", cfg.quote_currency, cfg.initial_cash)
            elif event in transfers:
                transfer = transfers[event]
                identifier, symbol = transfer["episode_id"], transfer["symbol"]
                require(symbol in positions and identifier not in parked, "Dust transfer has no active lot")
                lot = positions.pop(symbol)
                require(lot["episode_id"] == identifier and transfer["reason"].strip(), "Dust transfer episode/reason mismatch")
                require(utc(transfer["at"]) >= utc(lot["opened_at"]), "Dust transfer precedes entry")
                equal(lot["quantity"], transfer["quantity"], "Dust transfer quantity mismatch")
                equal(lot["cost"], transfer["cost"], "Dust transfer cost mismatch")
                parked[identifier] = {**lot, "wallet": "dust", "parked_at": transfer["at"]}
                post("dust_inventory", "inventory", symbol.split("/")[0], lot["quantity"])
            else:
                fill = fills[event]
                order = orders[fill["order_id"]]
                symbol, identifier = order["symbol"], order["id"]
                require(symbol in symbols and utc(fill["at"]) >= utc(order["created_at"]), "Fill symbol/time mismatch")
                base, quote = symbol.split("/")
                qty, price, fee = (money(fill[key]) for key in ("quantity", "price", "fee"))
                require(qty > 0 and price > 0 and fee >= 0, "Invalid fill amounts")
                fill_sums[identifier] += qty
                require(fill_sums[identifier] <= money(order["quantity"]), "Order overfill")
                projected = []
                if order["side"] == "buy":
                    require(fill["fee_asset"] == base and fee < qty, "Invalid received-base entry fee")
                    held, cost = qty - fee, qty * price
                    require(symbol not in positions or positions[symbol]["episode_id"] == identifier, "Entry pyramiding across episodes")
                    if symbol not in positions:
                        positions[symbol] = {"episode_id": identifier, "symbol": symbol, "quantity": ZERO, "cost": ZERO,
                                             "opened_at": fill["at"], "wallet": "position"}
                    positions[symbol]["quantity"] += held
                    positions[symbol]["cost"] += cost
                    cash -= cost
                    projected.append((identifier, "position", held, -cost, cost, ZERO, fee * price))
                    post("cash", "venue", quote, -cost)
                    post("inventory", "venue", base, qty)
                    if fee:
                        post("fee_expense", "inventory", base, fee)
                else:
                    require(order["side"] == "sell" and fill["fee_asset"] == quote, "Invalid received-quote exit fee")
                    available = [lot for lot in parked.values() if lot["symbol"] == symbol]
                    if symbol in positions:
                        available.append(positions[symbol])
                    available.sort(key=lambda lot: (utc(lot["opened_at"]), lot["episode_id"]))
                    require(qty <= sum((lot["quantity"] for lot in available), ZERO), "Sale exceeds aggregate inventory")
                    left, cash_left, fee_left = qty, qty * price - fee, fee
                    cash += cash_left
                    post("cash", "venue", quote, qty * price)
                    if fee:
                        post("fee_expense", "cash", quote, fee)
                    for lot in available:
                        if not left:
                            break
                        require(utc(fill["at"]) >= utc(lot["opened_at"]) and
                                (lot["wallet"] != "dust" or utc(fill["at"]) >= utc(lot["parked_at"])), "FIFO sale precedes lot")
                        taken = min(left, lot["quantity"])
                        cost = lot["cost"] if taken == lot["quantity"] else lot["cost"] * taken / lot["quantity"]
                        proceeds = cash_left if taken == left else (qty * price - fee) * taken / qty
                        paid_fee = fee_left if taken == left else fee * taken / qty
                        realized = proceeds - cost
                        projected.append((lot["episode_id"], lot["wallet"], taken, proceeds, cost, realized, paid_fee))
                        pnl[lot["episode_id"]] += realized
                        lot["quantity"] -= taken
                        lot["cost"] -= cost
                        post("inventory" if lot["wallet"] == "position" else "dust_inventory", "venue", base, -taken)
                        if not lot["quantity"]:
                            require(episodes[lot["episode_id"]]["closed_at"] == fill["at"], "Incorrect actual closure time")
                            if lot["wallet"] == "position":
                                positions.pop(symbol)
                            else:
                                parked.pop(lot["episode_id"])
                        left -= taken
                        cash_left -= proceeds
                        fee_left -= paid_fee
                    require(not left and not cash_left and not fee_left, "FIFO sale conservation failure")
                rows_allocated = {row["episode_id"]: row for row in allocations[event]}
                require(set(rows_allocated) == {row[0] for row in projected}, "Per-fill FIFO episode mapping mismatch")
                for row in projected:
                    saved = rows_allocated[row[0]]
                    require(saved["wallet"] == row[1], "Per-fill FIFO wallet mismatch")
                    for field, value in zip(("quantity", "cash_flow", "cost", "pnl", "fee_quote"), row[2:]):
                        equal(saved[field], value, "Per-fill FIFO cost/cash/fee/PnL mismatch")
            for key in actual.keys() | expected.keys():
                equal(actual[key], expected[key], "Accounting event postings differ from raw fill/transfer")
        equal(cash, meta["cash"], "Cash projection mismatch")
        require(cash >= 0, "Negative simulated cash")
        saved_positions = {row["symbol"]: dict(row) for row in db.execute("SELECT * FROM positions")}
        saved_parked = {row["episode_id"]: dict(row) for row in db.execute("SELECT * FROM dust_lots")}
        require(set(positions) == set(saved_positions) and set(parked) == set(saved_parked), "Wallet lot identity mismatch")
        for reconstructed, saved in ((positions, saved_positions), (parked, saved_parked)):
            for key, lot in reconstructed.items():
                require(lot["episode_id"] == saved[key]["episode_id"] and lot["symbol"] == saved[key]["symbol"], "Wallet episode/symbol mismatch")
                equal(lot["quantity"], saved[key]["quantity"], "Wallet quantity mismatch")
                equal(lot["cost"], saved[key]["cost"], "Wallet cost basis mismatch")
        live_ids = {lot["episode_id"] for lot in positions.values()} | set(parked)
        require(set(episodes) == {orders[f["order_id"]]["id"] for f in fills.values() if orders[f["order_id"]]["side"] == "buy"}, "Unexpected episode identity")
        for identifier, episode in episodes.items():
            equal(episode["pnl"], pnl[identifier], "Episode realized PnL mismatch")
            require((episode["closed_at"] is None) == (identifier in live_ids), "Episode closure omits retained dust")
        for identifier, order in orders.items():
            equal(order["filled"], fill_sums[identifier], "Order filled projection mismatch")
        valuations = {row["at"]: dict(row) for row in db.execute("SELECT * FROM valuation_snapshots")}
        snapshots = [dict(row) for row in db.execute("SELECT * FROM snapshots ORDER BY at")]
        require(set(valuations) == {row["at"] for row in snapshots}, "Missing snapshot valuation basis")
        boundaries = {max(row["id"] for row in rows) for rows in events.values()}
        for snapshot in snapshots:
            basis = valuations[snapshot["at"]]
            require(basis["ledger_watermark"] in boundaries, "Invalid/incomplete-event snapshot ledger watermark")
            account = defaultdict(lambda: ZERO)
            for row in ledger:
                if row["id"] <= basis["ledger_watermark"]:
                    account[(row["account"], row["asset"])] += money(row["delta"])
            marked = json.loads(basis["marks"])
            require(all(money(value) > 0 for value in marked.values()), "Invalid snapshot mark")
            held = {symbol: account[("inventory", symbol.split("/")[0])] + account[("dust_inventory", symbol.split("/")[0])] for symbol in symbols}
            complete = all(symbol in marked for symbol, qty in held.items() if qty)
            require(bool(snapshot["data_complete"]) == complete, "Dust valuation completeness mismatch")
            equal(snapshot["cash"], account[("cash", cfg.quote_currency)], "Snapshot cash mismatch")
            if complete:
                exposure = sum((qty * money(marked[symbol]) for symbol, qty in held.items() if qty), ZERO)
                equal(snapshot["exposure"], exposure, "Snapshot omits active/dust exposure")
                equal(snapshot["nav"], account[("cash", cfg.quote_currency)] + exposure, "Snapshot NAV mismatch")
            else:
                require(snapshot["nav"] is None and snapshot["exposure"] is None, "Incomplete inventory priced as cash-only NAV")
        # Hash a consistent read transaction's content, not only a WAL-less file.
        tables = ["metadata", "orders", "fills", "ledger", "positions", "episodes", "dust_lots", "dust_transfers", "fill_allocations", "snapshots", "valuation_snapshots"]
        digest = hashlib.sha256()
        for table in tables:
            rows = [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            digest.update(json.dumps([table, rows], sort_keys=True, separators=(",", ":")).encode())
        return {"status": "verified", "read_only": True, "database": str(path), "schema": 4,
                "execution_profile": meta.get("execution_profile") or None,
                "config_hash": cfg.digest(), "content_sha256": digest.hexdigest(), "fills_replayed": len(fills),
                "dust_transfers_replayed": len(transfers), "fifo_allocations_verified": sum(map(len, allocations.values())),
                "valuation_snapshots_verified": len(snapshots), "cash": cash, "active_positions": len(positions),
                "retained_dust_lots": len(parked), "closed_episodes": len(episodes) - len(live_ids),
                "models_trained": 0, "approved_for_live": False,
                "limitations": "Accounting replay only; no market-price authenticity, queue/latency, fee rounding, strategy edge or live approval"}
