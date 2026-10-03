from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
import json
from pathlib import Path
import random
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.broker import PaperBroker
from crypto_trader_v2.config import CostConfig, StrategyConfig, load_config
from crypto_trader_v2.domain import Bar, Mode, Quote, Signal, ZERO
from crypto_trader_v2.dust_audit import verify_dust_ledger
from crypto_trader_v2.engine import Coordinator
from crypto_trader_v2.report import build_report, read_report
from crypto_trader_v2.diagnostics import build_diagnostics
from crypto_trader_v2.risk import size_entry, Sizing
from crypto_trader_v2.storage import Store


NOW = datetime(2026, 10, 3, 8, tzinfo=timezone.utc)


def config():
    cfg = load_config("config.binance-th.paper.yaml")
    return replace(cfg, costs=CostConfig(D("0.001"), ZERO, ZERO, D("0.003"), "software fixture", "2026-10-10"),
                   strategy=StrategyConfig(3, 1, 3, 2, 2))


class DustTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.cfg = config()
        self.store = Store(self.path, self.cfg, Mode.PAPER, create=True, source="synthetic accounting test; never profit evidence")
        self.broker = PaperBroker(self.cfg)
        self.coordinator = Coordinator(self.store, self.cfg)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def buy(self, identifier="a", quantity=D("0.05001"), price=D("100"), at=NOW, symbol="BTC/USDT"):
        self.store.intent(identifier, symbol, "buy", quantity, at, "fixture", cash_reserved=quantity * price,
                          risk_reserved=D("0.1"), price_limit=price, stop=price - D("5"), atr_distance=D("5"))
        self.broker.execute(self.store, identifier, Quote(symbol, at, price, price))

    def sell(self, price=D("110"), at=NOW, symbol="BTC/USDT"):
        self.coordinator.sell(symbol, Quote(symbol, at, price, price), "desired_flat")

    def audit(self):
        result = verify_dust_ledger(self.path, self.cfg)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["approved_for_live"])
        return result

    def test_park_is_internal_transfer_not_sale_or_episode_closure(self):
        self.buy()
        held = self.store.positions()["BTC/USDT"]["quantity"]
        self.store.intent("exit", "BTC/USDT", "sell", D("0.04995"), NOW, "fixture")
        self.broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        remaining = self.store.positions()["BTC/USDT"].copy()
        before = self.store.mark_nav(NOW, {"BTC/USDT": D("110")})
        cash, fills, realized = self.store.cash, self.store.db.execute("SELECT count(*) FROM fills").fetchone()[0], build_report(self.store.db)["realized_net_pnl"]
        with self.store.transaction():
            self.store.set_meta("exit_required:BTC/USDT", "desired_flat")
        self.assertTrue(self.store.park_dust("BTC/USDT", Quote("BTC/USDT", NOW, D("110"), D("110")), self.cfg.instruments[0], D("110"), "desired_flat"))
        after = self.store.mark_nav(NOW, {"BTC/USDT": D("110")})
        self.assertEqual(before, after)
        self.assertEqual(self.store.cash, cash)
        self.assertEqual(self.store.positions(), {})
        lot = self.store.dust_lots()[0]
        self.assertEqual((lot["quantity"], lot["cost"]), (remaining["quantity"], remaining["cost"]))
        self.assertEqual(build_report(self.store.db)["realized_net_pnl"], realized)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fills").fetchone()[0], fills)
        self.assertEqual(build_report(self.store.db)["closed_episodes"], 0)
        self.assertIsNone(self.store.db.execute("SELECT closed_at FROM episodes WHERE id='a'").fetchone()[0])
        self.assertFalse(self.store.park_dust("BTC/USDT", Quote("BTC/USDT", NOW, D("110"), D("110")), self.cfg.instruments[0], D("110"), "desired_flat"))
        self.store.assert_balanced()
        self.assertEqual(self.audit()["dust_transfers_replayed"], 1)

    def test_new_trade_can_open_and_next_exit_closes_old_fifo_lot(self):
        self.buy()
        self.sell()
        first_dust = self.store.dust_lots()[0].copy()
        later = NOW + timedelta(hours=4)
        self.buy("b", quantity=D("0.05002"), at=later)
        self.assertEqual(self.store.dust_lots()[0]["quantity"], first_dust["quantity"])
        self.sell(price=D("120"), at=later + timedelta(hours=4))
        self.assertEqual(self.store.positions(), {})
        self.assertEqual([lot["episode_id"] for lot in self.store.dust_lots()], ["b"])
        closed = self.store.db.execute("SELECT closed_at FROM episodes WHERE id='a'").fetchone()[0]
        self.assertEqual(closed, (later + timedelta(hours=4)).isoformat())
        rows = self.store.db.execute("SELECT a.*,o.side FROM fill_allocations a JOIN fills f ON f.trade_id=a.trade_id JOIN orders o ON o.id=f.order_id WHERE a.episode_id='a' AND a.wallet='dust'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(D(rows[0]["quantity"]), first_dust["quantity"])
        self.assertEqual(D(rows[0]["cost"]), first_dust["cost"])
        self.store.mark_nav(later + timedelta(hours=4), {"BTC/USDT": D("120")})
        report = build_report(self.store.db)
        self.assertEqual(report["closed_episodes"], 1)
        self.assertEqual(report["marked_nav"], report["cash"] + report["dust_marked_value"])
        self.assertAlmostEqual(report["cash"] + report["dust_cost_basis"] - self.cfg.initial_cash, report["realized_net_pnl"], places=20)
        diagnostic = build_diagnostics(self.store.db)
        self.assertEqual(diagnostic["summary"]["episodes"], 1)
        self.assertEqual(diagnostic["open_episode_count"], 1)
        self.assertEqual(diagnostic["summary"]["execution_attribution_complete_episodes"], 1)
        self.assertEqual(self.audit()["closed_episodes"], 1)

    def test_partial_exit_consumes_old_dust_first_and_resumes_after_restart(self):
        self.buy()
        self.sell()
        old = self.store.dust_lots()[0]["quantity"]
        later = NOW + timedelta(hours=4)
        self.buy("b", quantity=D("1"), at=later)
        self.coordinator.broker.observed_capacity = {("BTC/USDT", "sell"): (later, D("0.01"))}
        self.sell(at=later)
        self.assertEqual(self.store.dust_lots(), [])
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], D("0.999") - (D("0.01") - old))
        self.assertEqual(build_report(self.store.db)["closed_episodes"], 1)
        self.assertEqual(self.store.reserved(), (ZERO, ZERO))
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.PAPER)
        self.coordinator = Coordinator(self.store, self.cfg)
        self.broker = self.coordinator.broker
        self.sell(at=later + timedelta(minutes=1))
        self.assertEqual(self.store.positions(), {})
        self.store.assert_balanced()
        self.audit()

    def test_valid_ioc_partial_entry_below_order_minimum_is_still_real_inventory(self):
        self.broker.observed_capacity = {("BTC/USDT", "buy"): (NOW, D("0.00001"))}
        self.buy(quantity=D("1"))
        self.assertEqual(D(self.store.order("a")["filled"]), D("0.00001"))
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], D("0.00000999"))
        self.assertEqual(self.store.cash, D("299.999"))
        self.sell()
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.dust_lots()[0]["quantity"], D("0.00000999"))
        self.audit()

    def test_dust_only_pool_can_sell_when_price_makes_it_tradable(self):
        self.buy(quantity=D("0.10001"))
        # A real partial IOC leaves inventory < min notional, but above step.
        self.coordinator.broker.observed_capacity = {("BTC/USDT", "sell"): (NOW, D("0.06"))}
        self.sell(price=D("100"))
        self.assertEqual(self.store.positions(), {})
        lot = self.store.dust_lots()[0]
        self.assertEqual(lot["quantity"], D("0.03990999"))
        later = NOW + timedelta(hours=4)
        self.coordinator.broker.observed_capacity = None
        self.sell(price=D("200"), at=later)
        self.assertLess(self.store.dust_lots()[0]["quantity"], self.cfg.instruments[0].quantity_step)
        self.assertGreater(self.store.cash, D("297"))
        self.audit()

    def test_dust_no_quote_is_incomplete_nav_not_cash_and_zero_risk(self):
        self.buy()
        self.sell()
        self.assertIsNone(self.store.mark_nav(NOW, {})["nav"])
        self.assertFalse(self.store.mark_nav(NOW, {})["complete"])
        self.audit()

    def test_two_parked_lots_pool_and_partial_sale_closes_only_oldest(self):
        self.buy(quantity=D("0.10001"))
        self.coordinator.broker.observed_capacity = {("BTC/USDT", "sell"): (NOW, D("0.06"))}
        self.sell(price=D("100"))
        later = NOW + timedelta(hours=4)
        self.broker.observed_capacity = {("BTC/USDT", "buy"): (later, D("0.04"))}
        self.buy("b", quantity=D("0.1"), at=later)
        self.coordinator.broker.observed_capacity = {("BTC/USDT", "sell"): (later, ZERO)}
        self.sell(price=D("100"), at=later)
        self.assertEqual([lot["episode_id"] for lot in self.store.dust_lots()], ["a", "b"])
        self.assertEqual(self.store.positions(), {})
        old_qty = self.store.dust_lots()[0]["quantity"]
        sale_time = later + timedelta(minutes=1)
        self.coordinator.broker.observed_capacity = {("BTC/USDT", "sell"): (sale_time, D("0.05"))}
        self.sell(price=D("100"), at=sale_time)
        self.assertEqual([lot["episode_id"] for lot in self.store.dust_lots()], ["b"])
        self.assertEqual(self.store.dust_lots()[0]["quantity"], D("0.03996") - (D("0.05") - old_qty))
        self.assertEqual(build_report(self.store.db)["closed_episodes"], 1)
        self.assertIsNone(self.store.db.execute("SELECT closed_at FROM episodes WHERE id='b'").fetchone()[0])
        self.assertEqual(self.coordinator.broker.observed_capacity[("BTC/USDT", "sell")][1], ZERO)
        # A second intent cannot reuse the same observed book volume.
        self.store.intent("same-quote", "BTC/USDT", "sell", D("0.02"), sale_time, "fixture")
        fills = self.store.db.execute("SELECT count(*) FROM fills").fetchone()[0]
        self.coordinator.broker.execute(self.store, "same-quote", Quote("BTC/USDT", sale_time, D("300"), D("300")))
        self.assertEqual(self.store.order("same-quote")["status"], "CANCELED")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fills").fetchone()[0], fills)
        self.store.mark_nav(sale_time, {"BTC/USDT": D("100")})
        self.audit()

    def test_failed_fill_rolls_back_fifo_closure_and_duplicate_is_noop(self):
        self.buy()
        self.sell()
        at = NOW + timedelta(hours=4)
        self.buy("b", quantity=D("0.1"), at=at)
        self.store.intent("sell", "BTC/USDT", "sell", D("0.05"), at, "fixture")
        self.store.acknowledge("sell")
        before = list(self.store.db.iterdump())
        with patch.object(self.store, "assert_balanced", side_effect=ValueError("injected reconciliation failure")):
            with self.assertRaisesRegex(ValueError, "injected"):
                self.store.apply_fill("sell", "sale", D("0.05"), D("100"), D("0.005"), "USDT", at)
        self.assertEqual(list(self.store.db.iterdump()), before)
        self.assertTrue(self.store.apply_fill("sell", "sale", D("0.05"), D("100"), D("0.005"), "USDT", at))
        self.assertEqual(build_report(self.store.db)["closed_episodes"], 1)
        after = list(self.store.db.iterdump())
        self.assertFalse(self.store.apply_fill("sell", "sale", D("0.05"), D("100"), D("0.005"), "USDT", at))
        self.assertEqual(list(self.store.db.iterdump()), after)
        self.store.cancel("sell")
        self.audit()

    def test_park_tradable_active_unknown_and_causal_guards(self):
        self.buy(quantity=D("1"))
        with self.store.transaction():
            self.store.set_meta("exit_required:BTC/USDT", "desired_flat")
        with self.assertRaisesRegex(ValueError, "Tradable"):
            self.store.park_dust("BTC/USDT", Quote("BTC/USDT", NOW, D("100"), D("100")), self.cfg.instruments[0], D("100"), "fixture")
        with self.assertRaisesRegex(ValueError, "causal"):
            self.store.park_dust("BTC/USDT", Quote("BTC/USDT", NOW - timedelta(seconds=1), D("100"), D("100")), self.cfg.instruments[0], D("100"), "fixture")
        self.store.intent("pending", "BTC/USDT", "sell", D("0.999"), NOW, "fixture")
        self.store.mark_unknown("pending")
        with self.assertRaisesRegex(ValueError, "UNKNOWN"):
            self.store.park_dust("BTC/USDT", Quote("BTC/USDT", NOW, D("100"), D("100")), self.cfg.instruments[0], D("100"), "fixture")
        self.assertEqual(self.store.entry_block(), "unknown_order")
        self.assertEqual(self.store.dust_lots(), [])
        self.store.assert_balanced()

    def test_oversell_and_wrong_fee_roll_back_wallets_cash_and_allocations(self):
        self.buy()
        self.sell()
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.store.intent("bad", "BTC/USDT", "sell", D("1"), NOW, "fixture")
        self.buy("b", quantity=D("0.1"), at=NOW + timedelta(hours=4))
        self.store.intent("wrong", "BTC/USDT", "sell", D("0.05"), NOW + timedelta(hours=4), "fixture")
        self.store.acknowledge("wrong")
        cash, lots = self.store.cash, self.store.dust_lots()
        with self.assertRaisesRegex(ValueError, "received-asset"):
            self.store.apply_fill("wrong", "wrong", D("0.05"), D("100"), D("0.001"), "BTC", NOW + timedelta(hours=4))
        self.assertEqual(self.store.cash, cash)
        self.assertEqual(self.store.dust_lots(), lots)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fill_allocations WHERE trade_id='wrong'").fetchone()[0], 0)
        self.store.assert_balanced()

    def test_dust_asset_cap_is_subtracted_not_given_another_full_budget(self):
        cfg = self.cfg
        sized = size_entry(cfg, cfg.instruments[0], nav=D("300"), cash=D("300"), entry=D("100"), stop=D("99"),
                           exposure=D("74"), open_risk=ZERO, open_count=0, asset_exposure=D("74"))
        self.assertLessEqual(sized.cash_reserved, D("1"))
        self.assertEqual(sized.quantity, ZERO)  # remaining budget below min notional

    def test_cycle_counts_retained_dust_as_full_loss_risk(self):
        self.buy()
        self.sell()
        now = NOW + timedelta(hours=4)
        histories = {i.symbol: [Bar(i.symbol, now - timedelta(seconds=(8-j)*self.cfg.seconds), self.cfg.seconds,
                                     D("100"), D("100"), D("100"), D("100"), D("1")) for j in range(8)] for i in self.cfg.instruments}
        quotes = {i.symbol: Quote(i.symbol, now, D("100"), D("100")) for i in self.cfg.instruments}
        signals = {i.symbol: Signal(i.symbol, now, True, False, D("1"), "fixture") for i in self.cfg.instruments}
        with patch("crypto_trader_v2.engine.size_entry", return_value=Sizing(ZERO,ZERO,ZERO,"fixture")) as size:
            self.coordinator.cycle(now, histories, quotes, prepared_signals=signals)
        self.assertEqual(size.call_count, 2)
        dust_risk = self.store.dust_lots()[0]["quantity"] * D("100")
        for call in size.call_args_list:
            self.assertEqual(call.kwargs["open_risk"], dust_risk)
            self.assertEqual(call.kwargs["open_count"], 0)
        self.assertEqual(size.call_args_list[0].kwargs["asset_exposure"], dust_risk)
        self.audit()

    def test_same_closed_bar_cannot_reenter_immediately_after_parking(self):
        self.buy()
        self.sell()
        histories = {i.symbol: [Bar(i.symbol, NOW - timedelta(seconds=(8-j)*self.cfg.seconds), self.cfg.seconds,
                                     D("100"), D("100"), D("100"), D("100"), D("1")) for j in range(8)] for i in self.cfg.instruments}
        quotes = {i.symbol: Quote(i.symbol, NOW, D("100"), D("100")) for i in self.cfg.instruments}
        signals = {i.symbol: Signal(i.symbol, NOW, i.symbol=="BTC/USDT", False, D("1"), "fixture") for i in self.cfg.instruments}
        self.coordinator.cycle(NOW, histories, quotes, prepared_signals=signals)
        self.assertEqual(len([o for o in self.store.orders() if o["side"]=="buy"]), 1)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM decisions WHERE reason='wait_for_new_closed_bar'").fetchone())

    def test_seeded_many_round_trips_reconcile_each_restart(self):
        rng = random.Random(20261003)
        for index in range(20):
            at = NOW + timedelta(hours=4*index)
            q = D(rng.randint(7000,15000)) * D("0.00001")
            self.buy(str(index), quantity=q, at=at)
            self.sell(price=D(rng.randint(80,130)), at=at + timedelta(minutes=1))
            self.store.mark_nav(at + timedelta(minutes=1), {"BTC/USDT": D("100")})
            self.store.assert_balanced()
            self.audit()
        self.assertEqual(self.store.positions(), {})
        self.assertGreater(build_report(self.store.db)["closed_episodes"], 15)

    def test_read_only_audit_does_not_change_rows_and_detects_tampering(self):
        self.buy()
        self.sell()
        self.store.mark_nav(NOW, {"BTC/USDT": D("110")})
        before = list(self.store.db.iterdump())
        self.audit()
        self.assertEqual(before, list(self.store.db.iterdump()))
        self.store.db.execute("UPDATE dust_lots SET cost='0'")
        self.store.db.commit()
        with self.assertRaisesRegex(ValueError, "cost basis"):
            verify_dust_ledger(self.path, self.cfg)

    def test_independent_audit_rejects_wrong_fifo_allocation_and_nav(self):
        self.buy()
        self.sell()
        self.buy("b", at=NOW + timedelta(hours=4))
        self.sell(at=NOW + timedelta(hours=8))
        self.store.mark_nav(NOW + timedelta(hours=8), {"BTC/USDT": D("110")})
        with self.store.transaction():
            self.store.db.execute("UPDATE fill_allocations SET fee_quote='99' WHERE wallet='dust'")
        with self.assertRaisesRegex(ValueError, "Per-fill FIFO"):
            verify_dust_ledger(self.path, self.cfg)

    def test_snapshot_audit_rejects_nav_omitting_dust(self):
        self.buy()
        self.sell()
        self.store.mark_nav(NOW, {"BTC/USDT": D("110")})
        with self.store.transaction():
            self.store.db.execute("UPDATE snapshots SET nav=cash")
        with self.assertRaisesRegex(ValueError, "NAV"):
            verify_dust_ledger(self.path, self.cfg)

    def test_snapshot_watermark_cannot_cut_a_double_entry_event_in_half(self):
        self.buy()
        self.sell()
        self.store.mark_nav(NOW, {"BTC/USDT": D("110")})
        with self.store.transaction():
            maximum = self.store.db.execute("SELECT max(id) FROM ledger").fetchone()[0]
            self.store.db.execute("UPDATE valuation_snapshots SET ledger_watermark=?", (maximum-1,))
        with self.assertRaisesRegex(ValueError, "incomplete-event"):
            verify_dust_ledger(self.path, self.cfg)

    def test_legacy_native_artifact_is_readable_not_implicitly_migrated(self):
        self.store.set_meta("schema", "3")
        self.store.db.commit()
        self.store.close()
        report = read_report(self.path)
        self.assertEqual(report["cash"], D("300"))
        with self.assertRaisesRegex(ValueError, "new schema-4"):
            Store(self.path, self.cfg, Mode.PAPER)

    def test_cli_audit_exports_new_only_and_never_opens_exchange(self):
        self.buy()
        self.sell()
        output = Path(self.temp.name) / "audit"
        with patch("crypto_trader_v2.__main__.load_config", return_value=self.cfg), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            args = ["verify-dust-ledger", "--db", str(self.path), "--output", str(output)]
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args), 1)
        report = json.loads((output/"report.json").read_text())
        self.assertTrue(report["read_only"])
        self.assertFalse(report["approved_for_live"])


if __name__ == "__main__":
    unittest.main()
