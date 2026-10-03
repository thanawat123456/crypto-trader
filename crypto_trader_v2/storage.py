"""Transactional paper ledger. Decimal values are persisted as text."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
import fcntl
import json
from pathlib import Path
import sqlite3

from .config import Config
from .domain import Mode, ONE, ZERO, money, utc
from . import dust


def proportional_reserve(value: Decimal, numerator: Decimal, denominator: Decimal) -> Decimal:
    """Multiply before division, then conservatively round the remaining reserve."""
    with localcontext() as precision:
        precision.prec = max(64, len(value.as_tuple().digits) + len(numerator.as_tuple().digits) + 10)
        exact = value * numerator / denominator
    with localcontext() as rounding:
        rounding.rounding = ROUND_CEILING
        return +exact


SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE orders (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    quantity TEXT NOT NULL, filled TEXT NOT NULL, status TEXT NOT NULL,
    cash_reserved TEXT NOT NULL, risk_reserved TEXT NOT NULL,
    price_limit TEXT, stop TEXT, atr_distance TEXT, created_at TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE fills (
    trade_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
    quantity TEXT NOT NULL, price TEXT NOT NULL, fee TEXT NOT NULL,
    fee_asset TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE ledger (
    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, account TEXT NOT NULL,
    asset TEXT NOT NULL, delta TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE positions (
    symbol TEXT PRIMARY KEY, quantity TEXT NOT NULL, cost TEXT NOT NULL,
    stop TEXT NOT NULL, atr_distance TEXT NOT NULL, peak TEXT NOT NULL,
    episode_id TEXT NOT NULL, opened_at TEXT NOT NULL
);
CREATE TABLE episodes (
    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, opened_at TEXT NOT NULL,
    closed_at TEXT, pnl TEXT NOT NULL
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL,
    action TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE snapshots (
    at TEXT PRIMARY KEY, cash TEXT NOT NULL, nav TEXT, exposure TEXT,
    drawdown TEXT, data_complete INTEGER NOT NULL
);
CREATE INDEX ledger_asset_account ON ledger(asset, account);
CREATE TABLE signal_contexts (
    symbol TEXT NOT NULL, at TEXT NOT NULL, enter_signal INTEGER NOT NULL,
    exit_signal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol,at)
);
CREATE TABLE execution_events (
    order_id TEXT PRIMARY KEY REFERENCES orders(id), episode_id TEXT NOT NULL,
    at TEXT NOT NULL, bid TEXT NOT NULL, ask TEXT NOT NULL,
    requested TEXT NOT NULL, filled TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE entry_evaluations (
    symbol TEXT NOT NULL, signal_at TEXT NOT NULL, evaluated_at TEXT NOT NULL,
    accepted INTEGER NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(symbol,signal_at,evaluated_at)
);
"""


class Store:
    def __init__(self, path: str | Path, cfg: Config, mode: Mode, *, create: bool = False, source: str = "", execution_profile: str = ""):
        self.path, self.cfg, self.mode = Path(path), cfg.validate(), Mode(mode)
        self.created = create
        if execution_profile and (cfg.venue != "binance_th" or execution_profile != "native-limit-ioc-ceil-fee-v1"):
            raise ValueError("Unsupported simulation execution profile")
        self.db = None
        self._lock = None
        if not self.path.exists() and not create:
            raise ValueError(f"Database does not exist: {self.path}; initialize explicitly")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            exists = self.path.exists()
            if create and exists:
                raise ValueError("Refusing to overwrite an existing database")
            # URI mode prevents silent creation when opening a missing/corrupted run.
            self.db = sqlite3.connect(self.path if create else self.path.resolve().as_uri() + "?mode=rw", uri=not create)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if create:
                self.db.executescript(SCHEMA)
                if cfg.venue == "binance_th":
                    self.db.executescript(dust.SCHEMA)
                with self.transaction():
                    self.set_meta("schema", "4" if cfg.venue == "binance_th" else "3")
                    if cfg.venue == "binance_th":
                        self.set_meta("execution_model_version", "received-fee-dust-fifo-v1")
                        if execution_profile:
                            self.set_meta("execution_profile", execution_profile)
                    self.set_meta("config_hash", cfg.digest())
                    self.set_meta("config", json.dumps(asdict(cfg), default=str, sort_keys=True))
                    self.set_meta("mode", str(mode))
                    self.set_meta("venue", cfg.venue)
                    self.set_meta("source", source)
                    self.set_meta("cash", str(cfg.initial_cash))
                    self.set_meta("high_water", str(cfg.initial_cash))
                    self.set_meta("risk_paused", "")
                    now = datetime.now(timezone.utc).isoformat()
                    self._posting("initial_deposit", "cash", "external", cfg.quote_currency, cfg.initial_cash, now)
            if self.get_meta("schema") not in {"1", "2", "3", "4"} or self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Unsupported or corrupt database")
            if cfg.venue == "binance_th" and self.get_meta("schema") != "4":
                raise ValueError("Binance TH dust accounting requires a new schema-4 database; old artifacts are read-only, not implicitly migrated")
            if self.get_meta("schema") == "4":
                tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"dust_lots", "dust_transfers", "fill_allocations", "valuation_snapshots"} <= tables:
                    raise ValueError("Incomplete dust accounting schema; initialize a new database, never implicitly migrate artifacts")
            if self.get_meta("config_hash") != cfg.digest() or self.get_meta("mode") != str(mode):
                raise ValueError("Database config/mode mismatch; use its original config or a new database")
            if self.get_meta("execution_profile", "") != execution_profile:
                raise ValueError("Database execution profile mismatch; preserve its registered simulator")
            self.assert_balanced()
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        self.db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @property
    def cash(self) -> Decimal:
        return money(self.get_meta("cash"))

    def positions(self) -> dict[str, dict]:
        result = {}
        for row in self.db.execute("SELECT * FROM positions ORDER BY symbol"):
            position = dict(row)
            for key in ("quantity", "cost", "stop", "atr_distance", "peak"):
                position[key] = money(position[key])
            result[row["symbol"]] = position
        return result

    def orders(self, *, active: bool = False) -> list[dict]:
        sql = "SELECT * FROM orders"
        if active:
            sql += " WHERE status IN ('INTENT','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')"
        return [dict(r) for r in self.db.execute(sql + " ORDER BY created_at,id")]

    def dust_lots(self, symbol=None):
        return dust.lots(self, symbol)

    def inventory(self):
        return dust.inventory(self)

    def exit_lots(self, symbol):
        return dust.exit_lots(self, symbol)

    def park_dust(self, symbol, quote, instrument, price, reason):
        return dust.park(self, symbol, quote, instrument, price, reason)

    def order(self, identifier: str) -> dict:
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Unknown order")
        return dict(row)

    def reserved(self) -> tuple[Decimal, Decimal]:
        active = self.orders(active=True)
        return (sum((money(o["cash_reserved"]) for o in active), ZERO),
                sum((money(o["risk_reserved"]) for o in active), ZERO))

    def decision(self, symbol: str, at: datetime, action: str, reason: str):
        with self.transaction():
            self.db.execute("INSERT INTO decisions(symbol,at,action,reason) VALUES (?,?,?,?)", (symbol, utc(at).isoformat(), action, reason))

    def record_context(self, context, signal):
        # No implicit migration of historical artifacts or existing paper DBs.
        if self.get_meta("schema") == "1":
            return
        if context.symbol != signal.symbol or context.at != signal.at:
            raise ValueError("Context and signal timestamps/symbols must match")
        payload = json.dumps(context.payload(), default=str, sort_keys=True)
        values = (context.symbol, utc(context.at).isoformat(), int(signal.enter), int(signal.exit), payload)
        with self.transaction():
            old = self.db.execute("SELECT * FROM signal_contexts WHERE symbol=? AND at=?", values[:2]).fetchone()
            if old is not None and tuple(old) != values:
                raise ValueError("Conflicting closed-bar context")
            self.db.execute("INSERT OR IGNORE INTO signal_contexts VALUES (?,?,?,?,?)", values)

    def record_execution(self, order_id: str, episode_id: str, quote, outcome: str):
        if self.get_meta("schema") == "1":
            return
        order = self.order(order_id)
        with self.transaction():
            self.db.execute("INSERT INTO execution_events VALUES (?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(order_id) DO UPDATE SET filled=excluded.filled,status=excluded.status,outcome=excluded.outcome", (
                order_id, episode_id, utc(quote.time).isoformat(), str(quote.bid), str(quote.ask),
                order["quantity"], order["filled"], order["status"], outcome))

    def record_entry_evaluation(self, signal, quote, result):
        if self.get_meta("schema") not in {"3", "4"}:
            return
        payload = json.dumps(result.payload(), default=str, sort_keys=True)
        values = (signal.symbol, utc(signal.at).isoformat(), utc(quote.time).isoformat(),
                  int(result.allowed), result.reason, payload)
        with self.transaction():
            old = self.db.execute("SELECT * FROM entry_evaluations WHERE symbol=? AND signal_at=? AND evaluated_at=?", values[:3]).fetchone()
            if old is not None and tuple(old) != values:
                raise ValueError("Conflicting entry evaluation")
            self.db.execute("INSERT OR IGNORE INTO entry_evaluations VALUES (?,?,?,?,?,?)", values)

    def intent(self, identifier: str, symbol: str, side: str, quantity: Decimal, at: datetime,
               reason: str, *, cash_reserved=ZERO, risk_reserved=ZERO,
               price_limit: Decimal | None = None, stop=ZERO, atr_distance=ZERO) -> bool:
        if quantity <= 0 or side not in ("buy", "sell") or min(cash_reserved, risk_reserved) < 0:
            raise ValueError("Invalid order intent")
        with self.transaction():
            if self.db.execute("SELECT 1 FROM orders WHERE id=?", (identifier,)).fetchone():
                return False
            if self.get_meta("schema") == "4" and symbol not in {i.symbol for i in self.cfg.instruments}:
                raise ValueError("Unconfigured Binance TH inventory")
            if any(o["symbol"] == symbol for o in self.orders(active=True)):
                raise ValueError("Active order already exists for instrument")
            positions = self.positions()
            if side == "buy":
                if symbol in positions or stop <= 0 or atr_distance <= 0 or cash_reserved <= 0:
                    raise ValueError("Invalid entry or pyramiding")
                if cash_reserved > self.cash - self.reserved()[0]:
                    raise ValueError("Insufficient unreserved cash")
            elif quantity > self.inventory().get(symbol, ZERO):
                raise ValueError("Sell exceeds inventory")
            self.db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, symbol, side, str(quantity), "0", "INTENT", str(cash_reserved),
                str(risk_reserved), str(price_limit) if price_limit is not None else None,
                str(stop), str(atr_distance), utc(at).isoformat(), reason))
            if side == "buy":
                self.set_meta("initial_risk:" + identifier, str(risk_reserved))
        return True

    def acknowledge(self, identifier: str):
        with self.transaction():
            self.db.execute("UPDATE orders SET status='ACKNOWLEDGED' WHERE id=? AND status='INTENT'", (identifier,))

    def cancel(self, identifier: str):
        with self.transaction():
            row = self.order(identifier)
            if row["status"] == "UNKNOWN":
                raise ValueError("UNKNOWN order must be reconciled before cancellation")
            if row["status"] != "FILLED":
                self.db.execute("UPDATE orders SET status='CANCELED',cash_reserved='0',risk_reserved='0' WHERE id=?", (identifier,))

    def mark_unknown(self, identifier: str):
        with self.transaction():
            self.db.execute("UPDATE orders SET status='UNKNOWN' WHERE id=? AND status IN ('INTENT','ACKNOWLEDGED','PARTIALLY_FILLED')", (identifier,))

    def _posting(self, event: str, debit: str, credit: str, asset: str, delta: Decimal, at: str):
        self.db.executemany("INSERT INTO ledger(event_id,account,asset,delta,at) VALUES (?,?,?,?,?)", (
            (event, debit, asset, str(delta), at), (event, credit, asset, str(-delta), at)))

    def apply_fill(self, order_id: str, trade_id: str, quantity: Decimal, price: Decimal,
                   fee: Decimal, fee_asset: str, at: datetime) -> bool:
        if quantity <= 0 or price <= 0 or fee < 0:
            raise ValueError("Invalid fill")
        at_text = utc(at).isoformat()
        with self.transaction():
            duplicate = self.db.execute("SELECT * FROM fills WHERE trade_id=?", (trade_id,)).fetchone()
            if duplicate:
                if (duplicate["order_id"], money(duplicate["quantity"]), money(duplicate["price"]), money(duplicate["fee"]), duplicate["fee_asset"]) != (order_id, quantity, price, fee, fee_asset):
                    raise ValueError("Conflicting duplicate fill")
                return False
            order = self.order(order_id)
            if utc(at) < utc(order["created_at"]):
                raise ValueError("Fill precedes order intent")
            if order["status"] not in ("ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"):
                raise ValueError("Order cannot accept fills in its current state")
            total, previous = money(order["quantity"]), money(order["filled"])
            if quantity + previous > total:
                raise ValueError("Overfill")
            base, quote = order["symbol"].split("/")
            if fee_asset not in (base, quote):
                raise ValueError("Third-asset fees are not supported in this release")
            native = self.get_meta("schema") == "4"
            if native and fee_asset != (base if order["side"] == "buy" else quote):
                raise ValueError("Binance TH requires received-asset fees")
            if order["price_limit"] is not None:
                limit = money(order["price_limit"])
                if (order["side"] == "buy" and price > limit) or (order["side"] == "sell" and price < limit):
                    raise ValueError("Fill breaches order price limit")
            notional = quantity * price
            position = self.positions().get(order["symbol"])
            quote_fee = fee if fee_asset == quote else ZERO
            base_fee = fee if fee_asset == base else ZERO
            allocations, consumed_wallet = [], None
            if order["side"] == "buy":
                cash_delta, inventory_delta = -notional - quote_fee, quantity - base_fee
                if inventory_delta <= 0:
                    raise ValueError("Fee exceeds purchased quantity")
                # Cross-products avoid a recurring fraction rounding 1 ulp
                # below an otherwise exactly reserved partial fill. No epsilon
                # or weakened budget check is used.
                reserve = money(order["cash_reserved"])
                with localcontext() as precision:
                    precision.prec = max(64, len(reserve.as_tuple().digits) + len(quantity.as_tuple().digits) + 10)
                    if -cash_delta * (total - previous) > reserve * quantity:
                        raise ValueError("Fill exceeds reserved entry budget")
                if position is None:
                    episode = order_id
                    self.db.execute("INSERT INTO episodes VALUES (?,?,?,?,?)", (episode, order["symbol"], at_text, None, "0"))
                    self.db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)", (
                        order["symbol"], str(inventory_delta), str(-cash_delta), order["stop"],
                        order["atr_distance"], str(price), episode, at_text))
                else:
                    self.db.execute("UPDATE positions SET quantity=?,cost=? WHERE symbol=?", (
                        str(position["quantity"] + inventory_delta), str(position["cost"] - cash_delta), order["symbol"]))
                if native:
                    allocations = [(order_id, "position", inventory_delta, cash_delta, -cash_delta, ZERO, base_fee * price)]
            elif native:
                cash_delta = notional - quote_fee
                allocations, consumed_wallet = dust.allocate_sell(self, order["symbol"], quantity, cash_delta, quote_fee, at_text)
            else:
                consumed = quantity + base_fee
                if position is None or consumed > position["quantity"]:
                    raise ValueError("Fill exceeds actual inventory including fees")
                cash_delta, inventory_delta = notional - quote_fee, -consumed
                allocated = position["cost"] * consumed / position["quantity"]
                pnl = cash_delta - allocated
                remaining = position["quantity"] - consumed
                old_pnl = money(self.db.execute("SELECT pnl FROM episodes WHERE id=?", (position["episode_id"],)).fetchone()[0])
                self.db.execute("UPDATE episodes SET pnl=?,closed_at=? WHERE id=?", (
                    str(old_pnl + pnl), at_text if remaining == 0 else None, position["episode_id"]))
                if remaining == 0:
                    self.db.execute("DELETE FROM positions WHERE symbol=?", (order["symbol"],))
                    self.set_meta("last_exit:" + order["symbol"], at_text)
                else:
                    self.db.execute("UPDATE positions SET quantity=?,cost=? WHERE symbol=?", (
                        str(remaining), str(position["cost"] - allocated), order["symbol"]))
            if self.cash + cash_delta < 0:
                raise ValueError("Negative cash")
            self.set_meta("cash", str(self.cash + cash_delta))
            # Cash/inventory postings balance independently in each currency.
            self._posting(trade_id, "cash", "venue", quote, notional if order["side"] == "sell" else -notional, at_text)
            if consumed_wallet is not None:
                for wallet, consumed in consumed_wallet.items():
                    if consumed:
                        self._posting(trade_id, "inventory" if wallet == "position" else "dust_inventory", "venue", base, -consumed, at_text)
            else:
                self._posting(trade_id, "inventory", "venue", base, quantity if order["side"] == "buy" else -quantity, at_text)
            if fee:
                self._posting(trade_id, "fee_expense", "cash" if fee_asset == quote else "inventory", fee_asset, fee, at_text)
            new_filled = previous + quantity
            self.db.execute("UPDATE orders SET filled=?,status=?,cash_reserved=?,risk_reserved=? WHERE id=?", (
                str(new_filled), "FILLED" if new_filled == total else "PARTIALLY_FILLED",
                str(proportional_reserve(money(order["cash_reserved"]), total - new_filled, total - previous)),
                str(proportional_reserve(money(order["risk_reserved"]), total - new_filled, total - previous)), order_id))
            self.db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)", (trade_id, order_id, str(quantity), str(price), str(fee), fee_asset, at_text))
            for allocation in allocations:
                self.db.execute("INSERT INTO fill_allocations VALUES (?,?,?,?,?,?,?,?)", (trade_id, *(str(value) for value in allocation)))
            self.assert_balanced()
        return True

    def update_stop(self, symbol: str, observed_high: Decimal):
        with self.transaction():
            position = self.positions().get(symbol)
            if position:
                peak = max(observed_high, position["peak"])
                stop = max(position["stop"], peak - position["atr_distance"])
                self.db.execute("UPDATE positions SET peak=?,stop=? WHERE symbol=?", (str(peak), str(stop), symbol))

    def mark_nav(self, at: datetime, marks: dict[str, Decimal]) -> dict:
        if any(value <= 0 or not value.is_finite() for value in marks.values()):
            raise ValueError("Invalid inventory mark")
        inventory = self.inventory()
        complete = all(s in marks and marks[s] > 0 for s in inventory)
        exposure = sum((quantity * marks[s] for s, quantity in inventory.items()), ZERO) if complete else None
        nav = self.cash + exposure if complete else None
        with self.transaction():
            high = money(self.get_meta("high_water"))
            if self.get_meta("schema") == "4":
                self.set_meta("last_marks", json.dumps(marks, default=str, sort_keys=True))
            drawdown = None
            if nav is not None:
                high = max(high, nav)
                self.set_meta("high_water", str(high))
                drawdown = ONE - nav / high
                day = utc(at).date().isoformat()
                if self.get_meta("day") != day:
                    previous = self.db.execute("SELECT nav FROM snapshots WHERE nav IS NOT NULL ORDER BY at DESC LIMIT 1").fetchone()
                    self.set_meta("day", day)
                    self.set_meta("day_start", previous[0] if previous else str(nav))
                daily_loss = ONE - nav / money(self.get_meta("day_start"))
                if drawdown >= self.cfg.risk.emergency_drawdown:
                    self.set_meta("risk_paused", "emergency_drawdown")
                elif drawdown >= self.cfg.risk.pause_drawdown and self.get_meta("risk_paused") != "emergency_drawdown":
                    self.set_meta("risk_paused", "drawdown_pause")
                self.set_meta("daily_loss", str(daily_loss))
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?) ON CONFLICT(at) DO UPDATE SET cash=excluded.cash,nav=excluded.nav,exposure=excluded.exposure,drawdown=excluded.drawdown,data_complete=excluded.data_complete", (
                utc(at).isoformat(), str(self.cash), str(nav) if nav is not None else None,
                str(exposure) if exposure is not None else None, str(drawdown) if drawdown is not None else None, int(complete)))
            if self.get_meta("schema") == "4":
                watermark = self.db.execute("SELECT COALESCE(MAX(id),0) FROM ledger").fetchone()[0]
                self.db.execute("INSERT INTO valuation_snapshots VALUES (?,?,?) ON CONFLICT(at) DO UPDATE SET marks=excluded.marks,ledger_watermark=excluded.ledger_watermark", (
                    utc(at).isoformat(), json.dumps(marks, default=str, sort_keys=True), watermark))
        return {"nav": nav, "exposure": exposure, "drawdown": drawdown, "complete": complete}

    def entry_block(self) -> str:
        if self.get_meta("risk_paused"):
            return self.get_meta("risk_paused")
        if money(self.get_meta("daily_loss", "0")) >= self.cfg.risk.daily_loss:
            return "daily_loss"
        if any(o["status"] == "UNKNOWN" for o in self.orders(active=True)):
            return "unknown_order"
        return ""

    def assert_balanced(self):
        balances: dict[str, Decimal] = {}
        cash, inventory, parked, events = ZERO, {}, {}, {}
        for row in self.db.execute("SELECT * FROM ledger"):
            delta = money(row["delta"])
            balances[row["asset"]] = balances.get(row["asset"], ZERO) + delta
            key = (row["event_id"], row["asset"])
            events[key] = events.get(key, ZERO) + delta
            if row["account"] == "cash" and row["asset"] == self.cfg.quote_currency:
                cash += delta
            if row["account"] == "inventory":
                inventory[row["asset"]] = inventory.get(row["asset"], ZERO) + delta
            if row["account"] == "dust_inventory":
                parked[row["asset"]] = parked.get(row["asset"], ZERO) + delta
        expected = {s.split("/")[0]: p["quantity"] for s, p in self.positions().items()}
        if any(abs(v) > Decimal("1e-20") for v in balances.values()) or abs(cash - self.cash) > Decimal("1e-20"):
            raise ValueError("Ledger/cash mismatch")
        if any(abs(inventory.get(a, ZERO) - expected.get(a, ZERO)) > Decimal("1e-20") for a in inventory.keys() | expected.keys()):
            raise ValueError("Ledger/inventory mismatch")
        expected_dust = {}
        for lot in self.dust_lots():
            base = lot["symbol"].split("/")[0]
            expected_dust[base] = expected_dust.get(base, ZERO) + lot["quantity"]
        if any(abs(parked.get(a, ZERO) - expected_dust.get(a, ZERO)) > Decimal("1e-20") for a in parked.keys() | expected_dust.keys()):
            raise ValueError("Ledger/dust inventory mismatch")
        if any(abs(value) > Decimal("1e-20") for value in events.values()):
            raise ValueError("Per-event double-entry mismatch")
        if self.get_meta("schema") == "4":
            dust.assert_allocations(self)

    def backup(self, destination: Path):
        if destination.exists():
            raise ValueError("Backup destination already exists")
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
