"""Local simulated dust wallet: retained cost basis and auditable FIFO exits.

Parking is an internal inventory transfer, NOT a sale or episode closure.
No exchange dust-conversion endpoint, top-up purchase, or credentials exist.
Only fresh Binance TH schema-4 databases use this accounting path.
"""
from __future__ import annotations

from collections import defaultdict

from .domain import ZERO, money, utc


SCHEMA = """
CREATE TABLE dust_lots (
    episode_id TEXT PRIMARY KEY REFERENCES episodes(id), symbol TEXT NOT NULL,
    quantity TEXT NOT NULL, cost TEXT NOT NULL, parked_at TEXT NOT NULL
);
CREATE TABLE dust_transfers (
    episode_id TEXT PRIMARY KEY REFERENCES episodes(id), symbol TEXT NOT NULL,
    quantity TEXT NOT NULL, cost TEXT NOT NULL, at TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE fill_allocations (
    trade_id TEXT NOT NULL REFERENCES fills(trade_id), episode_id TEXT NOT NULL REFERENCES episodes(id),
    wallet TEXT NOT NULL CHECK(wallet IN ('position','dust')), quantity TEXT NOT NULL,
    cash_flow TEXT NOT NULL, cost TEXT NOT NULL, pnl TEXT NOT NULL, fee_quote TEXT NOT NULL,
    PRIMARY KEY(trade_id,episode_id)
);
CREATE TABLE valuation_snapshots (
    at TEXT PRIMARY KEY REFERENCES snapshots(at), marks TEXT NOT NULL, ledger_watermark INTEGER NOT NULL
);
"""


def lots(store, symbol=None):
    if store.get_meta("schema") != "4":
        return []
    sql = "SELECT d.*,e.opened_at FROM dust_lots d JOIN episodes e ON e.id=d.episode_id"
    rows = store.db.execute(sql + (" WHERE d.symbol=?" if symbol is not None else "") + " ORDER BY e.opened_at,d.episode_id",
                            (symbol,) if symbol is not None else ())
    return [{**dict(row), "quantity": money(row["quantity"]), "cost": money(row["cost"])} for row in rows]


def inventory(store):
    result = {s: p["quantity"] for s, p in store.positions().items()}
    for lot in lots(store):
        result[lot["symbol"]] = result.get(lot["symbol"], ZERO) + lot["quantity"]
    return result


def exit_lots(store, symbol):
    result = [{**lot, "wallet": "dust"} for lot in lots(store, symbol)]
    position = store.positions().get(symbol)
    if position:
        result.append({**position, "symbol": symbol, "wallet": "position"})
    return sorted(result, key=lambda lot: (utc(lot["opened_at"]), lot["episode_id"]))


def park(store, symbol, quote, instrument, price, reason):
    if store.get_meta("schema") != "4" or store.cfg.venue != "binance_th":
        raise ValueError("Dust wallet requires a new Binance TH schema-4 database")
    quote.validate()
    if quote.symbol != symbol or instrument.symbol != symbol or not reason.strip() or not price.is_finite() or price <= 0:
        raise ValueError("Invalid dust transfer context")
    with store.transaction():
        position = store.positions().get(symbol)
        if position is None:
            return False
        if utc(quote.time) < utc(position["opened_at"]) or not store.get_meta("exit_required:" + symbol):
            raise ValueError("Dust transfer requires an explicit exit and causal quote")
        if any(order["symbol"] == symbol for order in store.orders(active=True)):
            raise ValueError("Resolve active/UNKNOWN order before parking inventory")
        tradable = instrument.round_quantity(position["quantity"])
        if tradable >= instrument.min_quantity and tradable * price >= instrument.min_notional:
            raise ValueError("Tradable inventory must not be hidden in the dust wallet")
        at = utc(quote.time).isoformat()
        store.db.execute("INSERT INTO dust_lots VALUES (?,?,?,?,?)", (
            position["episode_id"], symbol, str(position["quantity"]), str(position["cost"]), at))
        store.db.execute("INSERT INTO dust_transfers VALUES (?,?,?,?,?,?)", (
            position["episode_id"], symbol, str(position["quantity"]), str(position["cost"]), at, reason))
        store._posting("dust:" + position["episode_id"], "dust_inventory", "inventory", symbol.split("/")[0], position["quantity"], at)
        store.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        store.set_meta("exit_required:" + symbol, "")
        store.set_meta("last_exit:" + symbol, at)
        store.set_meta("residual_block:" + symbol, "")
        store.assert_balanced()
    store.decision(symbol, quote.time, "PARK_DUST", "retained_inventory_and_cost_basis")
    return True


def allocate_sell(store, symbol, quantity, cash_flow, fee_quote, at):
    """Inside the enclosing fill transaction; returns per-lot allocations.

    Use the final allocation's remainder so cash/fees are never lost to rounding.
    Episodes close only when their actual held units have all been sold.
    """
    held = exit_lots(store, symbol)
    if quantity > sum((lot["quantity"] for lot in held), ZERO):
        raise ValueError("Fill exceeds actual active plus dust inventory")
    remaining, remaining_cash, remaining_fee = quantity, cash_flow, fee_quote
    allocations, consumed = [], {"position": ZERO, "dust": ZERO}
    for lot in held:
        if remaining == 0:
            break
        if utc(at) < utc(lot["opened_at"]) or (lot["wallet"] == "dust" and utc(at) < utc(lot["parked_at"])):
            raise ValueError("FIFO sale precedes its inventory lot")
        taken = min(remaining, lot["quantity"])
        cost = lot["cost"] if taken == lot["quantity"] else lot["cost"] * taken / lot["quantity"]
        cash = remaining_cash if taken == remaining else cash_flow * taken / quantity
        fee = remaining_fee if taken == remaining else fee_quote * taken / quantity
        pnl, left = cash - cost, lot["quantity"] - taken
        previous = money(store.db.execute("SELECT pnl FROM episodes WHERE id=?", (lot["episode_id"],)).fetchone()[0])
        store.db.execute("UPDATE episodes SET pnl=?,closed_at=? WHERE id=?", (
            str(previous + pnl), at if left == 0 else None, lot["episode_id"]))
        table, key, identifier = ("positions", "symbol", symbol) if lot["wallet"] == "position" else ("dust_lots", "episode_id", lot["episode_id"])
        if left == 0:
            store.db.execute(f"DELETE FROM {table} WHERE {key}=?", (identifier,))
            if lot["wallet"] == "position":
                store.set_meta("last_exit:" + symbol, at)
        else:
            store.db.execute(f"UPDATE {table} SET quantity=?,cost=? WHERE {key}=?", (str(left), str(lot["cost"] - cost), identifier))
        allocations.append((lot["episode_id"], lot["wallet"], taken, cash, cost, pnl, fee))
        consumed[lot["wallet"]] += taken
        remaining -= taken
        remaining_cash -= cash
        remaining_fee -= fee
    if remaining or remaining_cash or remaining_fee:
        raise ValueError("FIFO allocation failed to conserve quantity/cash/fee")
    return allocations, consumed


def assert_allocations(store):
    """Reconcile fills, per-episode FIFO cost/PnL and both wallet projections."""
    tolerance = money("1e-20")
    episodes = {row["id"]: dict(row) for row in store.db.execute("SELECT * FROM episodes")}
    outstanding = defaultdict(lambda: {"position_qty": ZERO, "position_cost": ZERO, "dust_qty": ZERO, "dust_cost": ZERO, "pnl": ZERO})
    by_fill = defaultdict(list)
    for row in store.db.execute("SELECT a.*,o.side FROM fill_allocations a JOIN fills f ON f.trade_id=a.trade_id JOIN orders o ON o.id=f.order_id"):
        by_fill[row["trade_id"]].append(row)
        amount, cost, pnl = (money(row[k]) for k in ("quantity", "cost", "pnl"))
        if amount <= 0 or cost < 0 or money(row["fee_quote"]) < 0:
            raise ValueError("Invalid fill allocation")
        side = 1 if row["side"] == "buy" else -1
        values = outstanding[row["episode_id"]]
        values[row["wallet"] + "_qty"] += side * amount
        values[row["wallet"] + "_cost"] += side * cost
        values["pnl"] += pnl
        expected_pnl = ZERO if side == 1 else money(row["cash_flow"]) - cost
        if abs(expected_pnl - pnl) > tolerance:
            raise ValueError("Allocation realized PnL mismatch")
    for fill in store.db.execute("SELECT f.*,o.side,o.symbol FROM fills f JOIN orders o ON o.id=f.order_id"):
        rows = by_fill[fill["trade_id"]]
        qty, price, fee = (money(fill[k]) for k in ("quantity", "price", "fee"))
        buy = fill["side"] == "buy"
        expected_qty = qty - fee if buy else qty
        expected_cash = -qty * price if buy else qty * price - fee
        expected_fee = fee * price if buy else fee
        if fill["fee_asset"] != (fill["symbol"].split("/")[0] if buy else store.cfg.quote_currency):
            raise ValueError("Received-asset fee policy mismatch")
        for key, expected in (("quantity", expected_qty), ("cash_flow", expected_cash), ("fee_quote", expected_fee)):
            if abs(sum((money(row[key]) for row in rows), ZERO) - expected) > tolerance:
                raise ValueError("Fill allocation conservation mismatch")
    for transfer in store.db.execute("SELECT * FROM dust_transfers"):
        values = outstanding[transfer["episode_id"]]
        qty, cost = money(transfer["quantity"]), money(transfer["cost"])
        if qty <= 0 or cost < 0:
            raise ValueError("Invalid dust transfer")
        values["position_qty"] -= qty
        values["position_cost"] -= cost
        values["dust_qty"] += qty
        values["dust_cost"] += cost
    positions = {p["episode_id"]: p for p in store.positions().values()}
    parked = {lot["episode_id"]: lot for lot in lots(store)}
    for identifier, episode in episodes.items():
        values = outstanding[identifier]
        for wallet, projection in (("position", positions.get(identifier)), ("dust", parked.get(identifier))):
            for field, suffix in (("quantity", "qty"), ("cost", "cost")):
                expected = projection[field] if projection else ZERO
                if abs(values[wallet + "_" + suffix] - expected) > tolerance:
                    raise ValueError("FIFO wallet quantity/cost basis mismatch")
        if abs(values["pnl"] - money(episode["pnl"])) > tolerance:
            raise ValueError("Episode realized PnL allocation mismatch")
        if (episode["closed_at"] is not None) != (identifier not in positions and identifier not in parked):
            raise ValueError("Episode closure must reflect actual inventory, including dust")
