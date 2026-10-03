from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.broker import PaperBroker
from crypto_trader_v2.config import Config, CostConfig, StrategyConfig, load_config
from crypto_trader_v2.data import Dataset, KrakenPublicFeed, demo_dataset, read_csv, validate_bars
from crypto_trader_v2.domain import Bar, Instrument, Mode, Quote, Signal, ZERO
from crypto_trader_v2.engine import Coordinator, intent_id
from crypto_trader_v2.report import build_report, read_report
from crypto_trader_v2.research import run_backtest
from crypto_trader_v2.risk import size_entry
from crypto_trader_v2.storage import Store
from crypto_trader_v2.strategy import evaluate


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
INSTRUMENT = Instrument("BTC/USD", D("0.001"), D("0.001"), D("1"))


def config():
    return replace(Config(), instruments=(INSTRUMENT,),
                   strategy=StrategyConfig(trend_period=3, slope_bars=1, breakout_bars=3, exit_bars=2, atr_period=2))


def histories(cfg, now=NOW):
    bars = []
    for index in range(12):
        start = now - timedelta(seconds=(12 - index) * cfg.seconds)
        bars.append(Bar("BTC/USD", start, cfg.seconds, D("100"), D("101"), D("99"), D("100"), D("1000")))
    return {"BTC/USD": bars}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="v2-test-")
        self.path = Path(self.temp.name) / "run.sqlite"
        self.cfg = config()
        self.store = Store(self.path, self.cfg, Mode.PAPER, create=True, source="unit fixture")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def buy(self, quantity=D("1"), price=D("100"), fee=D("1")):
        self.store.intent("buy", "BTC/USD", "buy", quantity, NOW, "fixture",
                          cash_reserved=quantity * price + fee, risk_reserved=D("5"),
                          price_limit=price, stop=D("95"), atr_distance=D("5"))
        self.store.acknowledge("buy")
        self.store.apply_fill("buy", "entry", quantity, price, fee, "USD", NOW)

    def test_cash_inventory_fee_transaction_and_restart(self):
        self.buy()
        self.assertEqual(self.store.cash, D("199"))
        self.assertEqual(self.store.positions()["BTC/USD"]["cost"], D("101"))
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.PAPER)
        self.assertEqual(self.store.cash, D("199"))
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("1"))
        self.store.assert_balanced()

    def test_duplicate_fill_is_idempotent_and_conflict_rejected(self):
        self.buy()
        self.assertFalse(self.store.apply_fill("buy", "entry", D("1"), D("100"), D("1"), "USD", NOW))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.store.apply_fill("buy", "entry", D("1"), D("101"), D("1"), "USD", NOW)
        self.assertEqual(self.store.cash, D("199"))

    def test_partial_exits_group_as_one_episode(self):
        self.buy()
        for identifier, qty, price, fee in (("exit1", D("0.4"), D("110"), D("0.44")), ("exit2", D("0.6"), D("90"), D("0.54"))):
            self.store.intent(identifier, "BTC/USD", "sell", qty, NOW, "fixture")
            self.store.acknowledge(identifier)
            self.store.apply_fill(identifier, identifier, qty, price, fee, "USD", NOW)
            if identifier == "exit1":
                self.assertEqual(build_report(self.store.db)["closed_episodes"], 0)
                self.assertEqual(self.store.positions()["BTC/USD"]["cost"], D("60.6"))
        report = build_report(self.store.db)
        self.assertEqual(report["closed_episodes"], 1)
        self.assertEqual(report["realized_net_pnl"], D("-3.98"))
        self.assertEqual(self.store.cash, D("296.02"))

    def test_base_fee_reduces_inventory_and_is_cost_basis(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("101"), risk_reserved=D("5"), stop=D("95"), atr_distance=D("5"))
        self.store.acknowledge("buy")
        self.store.apply_fill("buy", "basefee", D("1"), D("100"), D("0.01"), "BTC", NOW)
        self.assertEqual(self.store.cash, D("200"))
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("0.99"))
        self.assertEqual(self.store.positions()["BTC/USD"]["cost"], D("100"))
        self.store.assert_balanced()

    def test_failed_fill_rolls_back_everything(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("100"), risk_reserved=D("5"), stop=D("95"), atr_distance=D("5"))
        self.store.acknowledge("buy")
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.store.apply_fill("buy", "bad", D("1"), D("100"), D("5"), "USD", NOW)
        self.assertEqual(self.store.cash, D("300"))
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_reservations_prevent_overspending_and_cancel_releases(self):
        self.store.intent("first", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("250"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        with self.assertRaisesRegex(ValueError, "unreserved"):
            self.store.intent("second", "ETH/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("100"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        self.store.cancel("first")
        self.assertEqual(self.store.reserved(), (ZERO, ZERO))

    def test_no_price_means_unavailable_nav_not_cash(self):
        self.buy()
        self.assertIsNone(self.store.mark_nav(NOW, {})["nav"])
        self.assertIsNone(build_report(self.store.db)["marked_nav"])

    def test_trailing_stop_never_moves_down(self):
        self.buy()
        self.store.update_stop("BTC/USD", D("120"))
        self.store.update_stop("BTC/USD", D("90"))
        self.assertEqual(self.store.positions()["BTC/USD"]["stop"], D("115"))

    def test_risk_pause_persists_across_restart(self):
        self.buy()
        self.store.mark_nav(NOW, {"BTC/USD": D("80")})
        self.assertEqual(self.store.entry_block(), "drawdown_pause")
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.PAPER)
        self.assertEqual(self.store.entry_block(), "drawdown_pause")

    def test_mode_config_mismatch_missing_corrupt_db_fail_closed(self):
        self.store.close()
        with self.assertRaisesRegex(ValueError, "mismatch"):
            Store(self.path, self.cfg, Mode.BACKTEST)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            Store(self.path, replace(self.cfg, initial_cash=D("400")), Mode.PAPER)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            Store(Path(self.temp.name) / "missing.sqlite", self.cfg, Mode.PAPER)
        bad = Path(self.temp.name) / "bad.sqlite"
        bad.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            Store(bad, self.cfg, Mode.PAPER)
        self.assertEqual(bad.read_bytes(), b"not a sqlite database")

    def test_single_writer_and_readonly_status(self):
        with self.assertRaises(BlockingIOError):
            Store(self.path, self.cfg, Mode.PAPER)
        self.assertEqual(read_report(self.path)["cash"], D("300"))

    def test_backup_can_restore_accounting_and_refuses_overwrite(self):
        self.buy()
        target = Path(self.temp.name) / "backup.sqlite"
        self.store.backup(target)
        with Store(target, self.cfg, Mode.PAPER) as restored:
            self.assertEqual(restored.positions(), self.store.positions())
            self.assertEqual(restored.cash, self.store.cash)
        with self.assertRaisesRegex(ValueError, "exists"):
            self.store.backup(target)

    def test_partial_ioc_keeps_position_and_releases_unfilled_budget(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        broker = PaperBroker(self.cfg)
        broker.execute(self.store, "buy", Quote("BTC/USD", NOW, D("99"), D("100")), max_quantity=D("0.4"))
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("0.4"))
        self.assertEqual(self.store.order("buy")["status"], "CANCELED")
        self.assertEqual(self.store.reserved(), (ZERO, ZERO))

    def test_unknown_order_never_retried(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        self.store.mark_unknown("buy")
        with self.assertRaisesRegex(ValueError, "Reconcile"):
            PaperBroker(self.cfg).execute(self.store, "buy", Quote("BTC/USD", NOW, D("99"), D("100")))
        self.assertEqual(self.store.cash, D("300"))
        self.assertEqual(self.store.entry_block(), "unknown_order")

    def test_price_ceiling_nonfill(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), price_limit=D("100"), stop=D("90"), atr_distance=D("10"))
        PaperBroker(self.cfg).execute(self.store, "buy", Quote("BTC/USD", NOW, D("101"), D("102")))
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.order("buy")["status"], "CANCELED")

    def test_recovery_after_ack_before_fill_does_not_duplicate(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        self.store.acknowledge("buy")
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.PAPER)
        coordinator = Coordinator(self.store, self.cfg)
        q = {"BTC/USD": Quote("BTC/USD", NOW, D("99.9"), D("100"))}
        coordinator.cycle(NOW, histories(self.cfg), q)
        coordinator.cycle(NOW, histories(self.cfg), q)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("1"))

    def test_missed_sell_transition_still_exits_desired_flat(self):
        self.buy()
        signal = Signal("BTC/USD", NOW, False, True, D("2"), "desired_flat")
        with patch("crypto_trader_v2.engine.evaluate", return_value=signal):
            Coordinator(self.store, self.cfg).cycle(NOW, histories(self.cfg), {"BTC/USD": Quote("BTC/USD", NOW, D("100"), D("100.1"))})
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(build_report(self.store.db)["closed_episodes"], 1)

    def test_expired_fee_blocks_entry_but_stops_work(self):
        expired = replace(self.cfg, costs=replace(self.cfg.costs, valid_until="2026-09-29"))
        # Same store uses the selected configuration in its own namespace.
        self.store.close()
        self.path = Path(self.temp.name) / "expired.sqlite"
        self.cfg = expired
        self.store = Store(self.path, expired, Mode.PAPER, create=True)
        signal = Signal("BTC/USD", NOW, True, False, D("2"), "breakout")
        with patch("crypto_trader_v2.engine.evaluate", return_value=signal):
            Coordinator(self.store, expired).cycle(NOW, histories(expired), {"BTC/USD": Quote("BTC/USD", NOW, D("100"), D("100.1"))})
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.db.execute("SELECT reason FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0], "fee_assumption_expired")
        self.buy()
        Coordinator(self.store, expired).cycle(NOW, {}, {"BTC/USD": Quote("BTC/USD", NOW, D("94"), D("94.1"))})
        self.assertEqual(self.store.positions(), {})

    def test_future_candle_cannot_create_entry(self):
        future = histories(self.cfg, NOW + timedelta(seconds=self.cfg.seconds))
        with patch("crypto_trader_v2.engine.evaluate") as evaluate_mock:
            Coordinator(self.store, self.cfg).cycle(NOW, future, {"BTC/USD": Quote("BTC/USD", NOW, D("100"), D("100.1"))})
        evaluate_mock.assert_not_called()
        self.assertEqual(self.store.positions(), {})

    def test_stale_quote_blocks_entry_and_nav(self):
        self.buy()
        stale = Quote("BTC/USD", NOW - timedelta(seconds=31), D("100"), D("100.1"))
        Coordinator(self.store, self.cfg).cycle(NOW, histories(self.cfg), {"BTC/USD": stale})
        self.assertIsNone(build_report(self.store.db)["marked_nav"])

    def test_original_risk_record_atomic_with_intent(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        self.assertEqual(self.store.get_meta("initial_risk:buy"), "5")

    def test_successful_entry_is_not_repeated_on_same_signal(self):
        signal = Signal("BTC/USD", NOW, True, False, D("2"), "breakout")
        q = {"BTC/USD": Quote("BTC/USD", NOW, D("100"), D("100.1"))}
        with patch("crypto_trader_v2.engine.evaluate", return_value=signal):
            coordinator = Coordinator(self.store, self.cfg)
            coordinator.cycle(NOW, histories(self.cfg), q)
            coordinator.cycle(NOW, histories(self.cfg), q)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM orders WHERE side='buy'").fetchone()[0], 1)
        self.assertGreater(self.store.positions()["BTC/USD"]["quantity"], ZERO)
        self.assertLessEqual(D(self.store.get_meta("initial_risk:" + self.store.orders()[0]["id"])), self.cfg.initial_cash * self.cfg.risk.per_trade)

    def test_incomplete_other_symbol_snapshot_blocks_all_new_entries(self):
        cfg = replace(self.cfg, instruments=(INSTRUMENT, replace(INSTRUMENT, symbol="ETH/USD")))
        self.store.close()
        self.path = Path(self.temp.name) / "multi.sqlite"
        self.store = Store(self.path, cfg, Mode.PAPER, create=True)
        data = histories(cfg)
        data["ETH/USD"] = [replace(b, symbol="ETH/USD") for b in data["BTC/USD"]]
        signal = Signal("BTC/USD", NOW, True, False, D("2"), "breakout")
        with patch("crypto_trader_v2.engine.evaluate", return_value=signal):
            Coordinator(self.store, cfg).cycle(NOW, data, {"BTC/USD": Quote("BTC/USD", NOW, D("100"), D("100.1"))})
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.orders(), [])

    def test_missing_mark_cancels_recovered_entry_without_fill(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        Coordinator(self.store, self.cfg).cycle(NOW, histories(self.cfg), {})
        self.assertEqual(self.store.order("buy")["status"], "CANCELED")
        self.assertEqual(self.store.positions(), {})

    def test_candle_dictionary_cannot_mix_instruments(self):
        wrong = {"BTC/USD": [replace(b, symbol="ETH/USD") for b in histories(self.cfg)["BTC/USD"]]}
        with patch("crypto_trader_v2.engine.evaluate") as evaluate_mock:
            Coordinator(self.store, self.cfg).cycle(NOW, wrong, {"BTC/USD": Quote("BTC/USD", NOW, D("100"), D("100.1"))})
        evaluate_mock.assert_not_called()
        self.assertEqual(self.store.positions(), {})

    def test_unprotected_inventory_does_not_get_lost_on_unknown_exit(self):
        self.buy()
        self.store.intent("sell", "BTC/USD", "sell", D("1"), NOW, "fixture")
        self.store.mark_unknown("sell")
        Coordinator(self.store, self.cfg).cycle(NOW, histories(self.cfg), {"BTC/USD": Quote("BTC/USD", NOW, D("90"), D("90.1"))})
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("1"))
        self.assertEqual(self.store.order("sell")["status"], "UNKNOWN")

    def test_sell_cannot_exceed_inventory(self):
        self.buy()
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.store.intent("oversell", "BTC/USD", "sell", D("1.1"), NOW, "fixture")
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("1"))

    def test_fill_before_order_is_rejected(self):
        self.store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("102"), risk_reserved=D("5"), stop=D("90"), atr_distance=D("10"))
        self.store.acknowledge("buy")
        with self.assertRaisesRegex(ValueError, "precedes"):
            self.store.apply_fill("buy", "early", D("1"), D("100"), D("0.8"), "USD", NOW - timedelta(seconds=1))
        self.assertEqual(self.store.cash, D("300"))


class ResearchTests(unittest.TestCase):
    def test_backtest_and_paper_event_replay_parity(self):
        cfg = config()
        data = demo_dataset(cfg, 220)
        historical = run_backtest(data, cfg)
        paper = run_backtest(data, cfg, mode=Mode.PAPER)
        for key in ("cash", "marked_nav", "fees_paid_quote_equivalent", "closed_episodes", "realized_net_pnl", "max_drawdown_pct"):
            self.assertEqual(historical[key], paper[key])
        self.assertGreater(historical["closed_episodes"], 0)
        self.assertGreater(historical["fees_paid_quote_equivalent"], 0)
        self.assertTrue(historical["research_gate"].startswith("INCONCLUSIVE"))

    def test_future_mutation_cannot_change_past_decisions(self):
        cfg = config()
        data = demo_dataset(cfg, 90)
        bars = data.bars["BTC/USD"]
        before = evaluate(bars[:50], cfg)
        modified = bars[:50] + [replace(b, open=b.open * 10, high=b.high * 10, low=b.low * 10, close=b.close * 10) for b in bars[50:]]
        self.assertEqual(evaluate(modified[:50], cfg), before)
        with TemporaryDirectory(prefix="v2-causal-") as folder:
            paths = [Path(folder) / name for name in ("original.sqlite", "modified.sqlite")]
            run_backtest(data, cfg, paths[0])
            run_backtest(Dataset({"BTC/USD": modified}, "mutation", "changed", True), cfg, paths[1])
            records = []
            cutoff = bars[49].end.isoformat()
            for path in paths:
                with sqlite3.connect(path) as db:
                    records.append(db.execute("SELECT symbol,at,action,reason FROM decisions WHERE at<=? ORDER BY id", (cutoff,)).fetchall())
            self.assertEqual(records[0], records[1])

    def test_gap_stop_fills_at_worse_open(self):
        cfg = config()
        with TemporaryDirectory(prefix="v2-gap-") as folder:
            with Store(Path(folder) / "gap.sqlite", cfg, Mode.BACKTEST, create=True) as store:
                store.intent("buy", "BTC/USD", "buy", D("1"), NOW, "fixture", cash_reserved=D("101"), risk_reserved=D("5"), stop=D("95"), atr_distance=D("5"))
                store.acknowledge("buy")
                store.apply_fill("buy", "buy", D("1"), D("100"), D("1"), "USD", NOW)
                Coordinator(store, cfg).historical_bar({"BTC/USD": Bar("BTC/USD", NOW, cfg.seconds, D("80"), D("90"), D("70"), D("85"), D("1000"))})
                fill = store.db.execute("SELECT price FROM fills WHERE order_id!='buy'").fetchone()
                self.assertLess(D(fill[0]), D("80"))
                self.assertEqual(store.positions(), {})

    def test_quantity_includes_fees_and_rounds_down(self):
        cfg = config()
        result = size_entry(cfg, INSTRUMENT, nav=D("300"), cash=D("300"), entry=D("100"), stop=D("96"), exposure=ZERO, open_risk=ZERO, open_count=0)
        self.assertGreater(result.quantity, ZERO)
        self.assertLessEqual(result.risk_reserved, D("0.75"))
        self.assertEqual(result.quantity % INSTRUMENT.quantity_step, ZERO)
        self.assertLess(result.quantity, D("0.75") / D("4"))
        tiny = replace(INSTRUMENT, min_notional=D("100"))
        result = size_entry(cfg, tiny, nav=D("300"), cash=D("300"), entry=D("100"), stop=D("96"), exposure=ZERO, open_risk=ZERO, open_count=0)
        self.assertEqual(result.reason, "below_exchange_minimum")

    def test_no_trade_statistics_are_undefined(self):
        cfg = config()
        bars = histories(cfg)["BTC/USD"]
        report = run_backtest(Dataset({"BTC/USD": bars}, "flat", "fixture", True), cfg)
        self.assertIsNone(report["profit_factor"])
        self.assertIsNone(report["net_expectancy_quote"])

    def test_csv_requires_timezone_and_detects_gap(self):
        cfg = config()
        bars = histories(cfg)["BTC/USD"]
        with self.assertRaisesRegex(ValueError, "missing candle"):
            validate_bars(bars[:2] + bars[3:], cfg)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_bars(bars[:2] + bars[1:], cfg)
        with TemporaryDirectory(prefix="v2-data-") as folder:
            path = Path(folder) / "bad.csv"
            path.write_text("timestamp,symbol,open,high,low,close,volume\n2026-09-30T00:00:00,BTC/USD,100,101,99,100,1\n")
            with self.assertRaisesRegex(ValueError, "timezone"):
                read_csv(path, cfg, "fixture")

    def test_config_rejects_credentials_typos_and_bad_limits(self):
        with TemporaryDirectory(prefix="v2-config-") as folder:
            path = Path(folder) / "bad.yaml"
            path.write_text("api_key: secret\n")
            with self.assertRaisesRegex(ValueError, "Unknown config"):
                load_config(str(path))
        with self.assertRaisesRegex(ValueError, "Inconsistent"):
            replace(Config(), risk=replace(Config().risk, pause_drawdown=D("0.10"))).validate()

    def test_cli_error_is_nonzero_and_live_command_absent(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["-c", "/nonexistent-v2.yaml", "demo"]), 1)
            with self.assertRaises(SystemExit) as error:
                main(["live"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_paper_fetch_failure_returns_nonzero(self):
        with TemporaryDirectory(prefix="v2-cli-") as folder:
            path = Path(folder) / "paper.sqlite"
            with patch("crypto_trader_v2.__main__.KrakenPublicFeed.instruments", side_effect=OSError("offline")), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                result = main(["paper", "--db", str(path), "--initialize", "--once"])
            self.assertEqual(result, 1)
            self.assertEqual(read_report(path)["cash"], D("300"))

    def test_live_mode_cannot_initialize_database(self):
        with TemporaryDirectory(prefix="v2-mode-") as folder:
            path = Path(folder) / "live.sqlite"
            with self.assertRaises(ValueError):
                Store(path, config(), "LIVE", create=True)
            self.assertFalse(path.exists())

    def test_paper_public_feed_contract_and_closed_candles(self):
        cfg = replace(config(), timeframe_minutes=1)
        timestamp = int(NOW.timestamp())
        payloads = {
            "AssetPairs": {"XXBTZUSD": {"wsname": "XBT/USD", "lot_decimals": 8, "ordermin": "0.0001", "costmin": "5", "status": "online"}},
            "OHLC": {"last": timestamp, "XXBTZUSD": [[timestamp - 120, "100", "101", "99", "100", "100", "10", 3], [timestamp - 60, "100", "101", "99", "100", "100", "10", 3], [timestamp, "100", "101", "99", "100", "100", "10", 3]]},
            "Ticker": {"XXBTZUSD": {"b": ["99.9"], "a": ["100.1"]}},
        }
        feed = KrakenPublicFeed()
        with patch.object(feed, "request", side_effect=lambda endpoint, params: payloads[endpoint]) as calls, patch("crypto_trader_v2.data.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            instruments = feed.instruments(cfg)
            now, data, quotes = feed.snapshot(cfg)
        self.assertEqual(len(data["BTC/USD"]), 2)
        self.assertEqual(quotes["BTC/USD"].ask, D("100.1"))
        self.assertEqual(instruments["BTC/USD"].min_notional, D("5"))
        self.assertEqual({c.args[0] for c in calls.call_args_list}, {"AssetPairs", "OHLC", "Ticker"})
        with self.assertRaisesRegex(ValueError, "allowlist"):
            feed.request("AddOrder", {})


if __name__ == "__main__":
    unittest.main()
