from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.broker import ExecutionScenario, PaperBroker
from crypto_trader_v2.config import Config, StrategyConfig
from crypto_trader_v2.context import ContextState, MarketContext
from crypto_trader_v2.data import demo_dataset, validate_bars
from crypto_trader_v2.development import run_development
from crypto_trader_v2.diagnostics import build_diagnostics, export_diagnostics, read_diagnostics
from crypto_trader_v2.domain import Bar, Instrument, Mode, Quote, Signal, ZERO
from crypto_trader_v2.engine import Coordinator
from crypto_trader_v2.importer import import_kraken
from crypto_trader_v2.research import run_backtest
from crypto_trader_v2.storage import Store


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def config():
    return replace(Config(), instruments=(Instrument("BTC/USD", D("0.001"), D("0.001"), D(1)),),
                   strategy=StrategyConfig(trend_period=3, slope_bars=1, breakout_bars=3, exit_bars=2, atr_period=2))


def history(cfg, now):
    return {"BTC/USD": [Bar("BTC/USD", now - timedelta(seconds=(12 - i) * cfg.seconds),
                             cfg.seconds, D(100), D(101), D(99), D(100), D(1000)) for i in range(12)]}


class ContextTests(unittest.TestCase):
    def test_bad_market_data_is_rejected(self):
        cfg = config()
        bar = history(cfg, NOW)["BTC/USD"][0]
        for bad in (replace(bar, high=D("Infinity")), replace(bar, volume=D("NaN")),
                    replace(bar, start=bar.start + timedelta(seconds=1))):
            with self.assertRaises(ValueError):
                validate_bars([bad], cfg)
        with self.assertRaises(ValueError):
            Quote("BTC/USD", NOW, D(100), D("Infinity")).validate()

    def test_future_mutation_does_not_change_context(self):
        cfg = config()
        original = demo_dataset(cfg, 300).bars["BTC/USD"]
        changed = original[:100] + [replace(b, open=b.open * 3, high=b.high * 3, low=b.low * 3, close=b.close * 3, volume=b.volume * 4) for b in original[100:]]
        results = []
        for bars in (original, changed):
            state = ContextState(cfg)
            results.append([state.update(b) for b in bars])
        self.assertEqual(results[0][:100], results[1][:100])

    def test_flat_context_and_zero_volume_are_finite(self):
        cfg = config()
        state = ContextState(cfg)
        for i in range(30):
            bar = Bar("BTC/USD", NOW + timedelta(seconds=i * cfg.seconds), cfg.seconds, D(100), D(100), D(100), D(100), ZERO)
            signal, context = state.update(bar)
        self.assertEqual(context.regime, "range")
        self.assertEqual(context.atr_pct, ZERO)
        self.assertEqual(context.efficiency_20, ZERO)
        self.assertIsNone(context.volume_ratio_20)
        self.assertEqual(context.at, bar.end)

    def test_context_detects_trend_without_using_outcomes(self):
        cfg = config()
        state = ContextState(cfg)
        for i in range(60):
            price = D(100 + i)
            _, context = state.update(Bar("BTC/USD", NOW + timedelta(seconds=i * cfg.seconds), cfg.seconds,
                                          price, price + 1, price - 1, price, D(100)))
        self.assertEqual(context.regime, "trend_up")
        self.assertEqual(context.efficiency_20, D(1))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.cfg = config()
        self.path = Path(self.temp.name) / "run.sqlite"
        self.store = Store(self.path, self.cfg, Mode.BACKTEST, create=True, source="UNIT TEST ONLY")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def buy(self):
        self.store.intent("buy", "BTC/USD", "buy", D(1), NOW, "fixture", cash_reserved=D(102), risk_reserved=D(5), stop=D(95), atr_distance=D(5))
        PaperBroker(self.cfg).execute(self.store, "buy", Quote("BTC/USD", NOW, D(100), D(100)))

    def test_invalid_scenarios_fail_before_creating_replay(self):
        for scenario in (ExecutionScenario(entry_fill_fraction=D("NaN")), ExecutionScenario(exit_fill_fraction=D("1.1")),
                         ExecutionScenario(miss_every_entry=-1), ExecutionScenario(extra_slippage=D("-0.01"))):
            with self.assertRaises(ValueError):
                run_backtest(demo_dataset(self.cfg, 300), self.cfg, Path(self.temp.name) / "invalid.sqlite", execution=scenario)
            self.assertFalse((Path(self.temp.name) / "invalid.sqlite").exists())

    def test_partial_entry_is_rounded_and_accounted(self):
        self.store.intent("buy", "BTC/USD", "buy", D("0.999"), NOW, "fixture", cash_reserved=D(102), risk_reserved=D(5), stop=D(95), atr_distance=D(5))
        PaperBroker(self.cfg, ExecutionScenario(entry_fill_fraction=D("0.5"))).execute(self.store, "buy", Quote("BTC/USD", NOW, D(100), D(100)))
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("0.499"))
        self.assertEqual(self.store.order("buy")["status"], "CANCELED")
        self.assertEqual(self.store.reserved(), (ZERO, ZERO))
        self.store.assert_balanced()

    def test_recurring_fraction_partial_fill_does_not_false_reject(self):
        cfg = replace(self.cfg, instruments=(replace(self.cfg.instruments[0], quantity_step=D("1e-8")),))
        self.store.intent("fraction", "BTC/USD", "buy", D("0.00067219"), NOW, "regression",
                          cash_reserved=D("16.15525942343545209600"), risk_reserved=D("0.75"),
                          stop=D("23239.22246263114249685392597"), atr_distance=D(600))
        PaperBroker(cfg, ExecutionScenario(entry_fill_fraction=D("0.5"))).execute(
            self.store, "fraction", Quote("BTC/USD", NOW, D("23800"), D("23831.10960")))
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("0.00033609"))
        self.store.assert_balanced()

    def test_no_fill_does_not_report_buy(self):
        coordinator = Coordinator(self.store, self.cfg, execution=ExecutionScenario(miss_every_entry=1))
        signal = Signal("BTC/USD", NOW, True, False, D(2), "breakout")
        with patch("crypto_trader_v2.engine.evaluate", return_value=signal):
            coordinator.cycle(NOW, history(self.cfg, NOW), {"BTC/USD": Quote("BTC/USD", NOW, D(100), D(100))})
        self.assertFalse(self.store.positions())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decisions WHERE action='BUY'").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM decisions WHERE action='NO_FILL'").fetchone()[0], 1)
        report = build_diagnostics(self.store.db)
        self.assertEqual(report["execution"]["no_fill_entries"], 1)

    def test_partial_exit_remains_flat_target_after_price_rebounds(self):
        self.buy()
        coordinator = Coordinator(self.store, self.cfg, execution=ExecutionScenario(exit_fill_fraction=D("0.5")))
        coordinator.sell("BTC/USD", Quote("BTC/USD", NOW, D(90), D(90)), "protective_stop")
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D("0.5"))
        old_stop = self.store.positions()["BTC/USD"]["stop"]
        coordinator.historical_bar({"BTC/USD": Bar("BTC/USD", NOW, self.cfg.seconds, D(100), D(200), D(80), D(110), D(1000))})
        self.assertEqual(self.store.positions()["BTC/USD"]["stop"], old_stop)
        later = NOW + timedelta(seconds=self.cfg.seconds)
        coordinator.cycle(later, history(self.cfg, later), {"BTC/USD": Quote("BTC/USD", later, D(120), D(120))})
        self.assertFalse(self.store.positions())
        self.assertEqual(self.store.get_meta("exit_required:BTC/USD"), "")
        self.assertEqual(build_diagnostics(self.store.db)["summary"]["episodes"], 1)

    def test_failed_exit_requirement_survives_restart(self):
        self.buy()
        Coordinator(self.store, self.cfg, execution=ExecutionScenario(exit_fill_fraction=ZERO)).sell(
            "BTC/USD", Quote("BTC/USD", NOW, D(90), D(90)), "protective_stop")
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.BACKTEST)
        later = NOW + timedelta(seconds=self.cfg.seconds)
        Coordinator(self.store, self.cfg).cycle(later, history(self.cfg, later), {"BTC/USD": Quote("BTC/USD", later, D(120), D(120))})
        self.assertFalse(self.store.positions())
        self.store.assert_balanced()

    def test_stale_exit_requirement_clears_after_external_fill_reconciliation(self):
        self.buy()
        coordinator = Coordinator(self.store, self.cfg, execution=ExecutionScenario(exit_fill_fraction=ZERO))
        coordinator.sell("BTC/USD", Quote("BTC/USD", NOW, D(90), D(90)), "protective_stop")
        self.store.intent("reconciled", "BTC/USD", "sell", D(1), NOW, "fixture")
        self.store.acknowledge("reconciled")
        self.store.apply_fill("reconciled", "reconciled", D(1), D(100), ZERO, "USD", NOW)
        later = NOW + timedelta(seconds=self.cfg.seconds)
        coordinator.cycle(later, history(self.cfg, later), {"BTC/USD": Quote("BTC/USD", later, D(100), D(100))})
        self.assertEqual(self.store.get_meta("exit_required:BTC/USD"), "")

    def test_bid_stop_charges_spread_once(self):
        self.buy()
        bar = Bar("BTC/USD", NOW, self.cfg.seconds, D(100), D(102), D(90), D(99), D(1000))
        Coordinator(self.store, self.cfg).historical_bar({"BTC/USD": bar})
        row = self.store.db.execute("SELECT price FROM fills WHERE order_id!='buy'").fetchone()
        self.assertEqual(D(row[0]), D(95) * (1 - self.cfg.costs.slippage))

    def test_historical_gap_exit_uses_worse_open_bid(self):
        self.buy()
        bar = Bar("BTC/USD", NOW, self.cfg.seconds, D(80), D(100), D(70), D(85), D(1000))
        Coordinator(self.store, self.cfg).historical_bar({"BTC/USD": bar})
        row = self.store.db.execute("SELECT price FROM fills WHERE order_id!='buy'").fetchone()
        expected = D(80) * (1 - self.cfg.costs.simulated_spread / 2) * (1 - self.cfg.costs.slippage)
        self.assertEqual(D(row[0]), expected)
        self.assertEqual(build_diagnostics(self.store.db)["episodes"][0]["exit_reason"], "historical_gap_stop")

    def test_unknown_exit_is_not_retried_by_historical_bar(self):
        self.buy()
        self.store.intent("unknown", "BTC/USD", "sell", D(1), NOW, "fixture")
        self.store.mark_unknown("unknown")
        Coordinator(self.store, self.cfg).historical_bar({"BTC/USD": Bar("BTC/USD", NOW, self.cfg.seconds, D(80), D(100), D(70), D(85), D(1000))})
        self.assertEqual(self.store.positions()["BTC/USD"]["quantity"], D(1))
        self.assertEqual(self.store.order("unknown")["status"], "UNKNOWN")

    def test_context_is_idempotent_but_conflict_rejected(self):
        context = MarketContext("BTC/USD", NOW, "range", D("0.01"), ZERO, ZERO, ZERO, None, ZERO)
        signal = Signal("BTC/USD", NOW, True, False, D(1), "fixture")
        self.store.record_context(context, signal)
        self.store.record_context(context, signal)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.store.record_context(replace(context, regime="trend_up"), signal)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM signal_contexts").fetchone()[0], 1)

    def test_readonly_diagnostics_reconciles_fees_and_partial_exits(self):
        self.buy()
        coordinator = Coordinator(self.store, self.cfg, execution=ExecutionScenario(exit_fill_fraction=D("0.5")))
        coordinator.sell("BTC/USD", Quote("BTC/USD", NOW, D(102), D(102)), "fixture_exit")
        coordinator.sell("BTC/USD", Quote("BTC/USD", NOW + timedelta(seconds=1), D(101), D(101)), "fixture_exit")
        report = read_diagnostics(self.path)
        row = report["episodes"][0]
        self.assertTrue(row["execution_attribution_complete"])
        self.assertEqual(row["pnl_plus_fees"], row["net_pnl"] + row["fee_quote_equivalent"])
        self.assertEqual(report["summary"]["episodes"], 1)
        self.assertEqual(report["execution"]["partial_exits"], 1)
        self.assertEqual(self.store.cash, self.cfg.initial_cash + row["net_pnl"])
        export_diagnostics(report, Path(self.temp.name) / "report")
        with self.assertRaisesRegex(ValueError, "exists"):
            export_diagnostics(report, Path(self.temp.name) / "report")

    def test_open_episodes_are_not_counted_as_closed(self):
        self.buy()
        report = build_diagnostics(self.store.db)
        self.assertEqual(report["open_episode_count"], 1)
        self.assertEqual(report["summary"]["episodes"], 0)
        self.assertIsNone(report["episodes"][0]["pnl_plus_fees"])

    def test_schema_one_is_supported_without_migration_or_fabrication(self):
        self.buy()
        with self.store.transaction():
            self.store.db.execute("DROP TABLE execution_events")
            self.store.db.execute("DROP TABLE signal_contexts")
            self.store.set_meta("schema", "1")
        self.store.close()
        before = self.path.read_bytes()
        report = read_diagnostics(self.path)
        self.assertEqual(report["episodes"][0]["entry_regime"], "not_recorded")
        self.assertFalse(report["episodes"][0]["execution_attribution_complete"])
        self.assertEqual(self.path.read_bytes(), before)
        self.store = Store(self.path, self.cfg, Mode.BACKTEST)
        Coordinator(self.store, self.cfg).sell("BTC/USD", Quote("BTC/USD", NOW, D(100), D(100)), "fixture")
        self.assertFalse(self.store.positions())
        self.assertEqual(self.store.get_meta("schema"), "1")

    def test_base_fees_and_missing_audit_are_not_silently_reconstructed(self):
        self.store.intent("buy", "BTC/USD", "buy", D(1), NOW, "fixture", cash_reserved=D(102), risk_reserved=D(5), stop=D(95), atr_distance=D(5))
        self.store.acknowledge("buy")
        self.store.apply_fill("buy", "base_entry", D(1), D(100), D("0.01"), "BTC", NOW)
        self.store.intent("exit", "BTC/USD", "sell", D("0.99"), NOW, "fixture")
        self.store.acknowledge("exit")
        self.store.apply_fill("exit", "exit", D("0.99"), D(110), ZERO, "USD", NOW)
        row = build_diagnostics(self.store.db)["episodes"][0]
        self.assertEqual(row["net_pnl"], D("8.9"))
        self.assertEqual(row["fee_quote_equivalent"], D(1))
        self.assertIsNone(row["slippage_cost"])


class DevelopmentTests(unittest.TestCase):
    def fixture(self, root):
        cfg = config()
        source = root / "source.csv"
        start = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp())
        source.write_text("".join(f"{start + i * cfg.seconds},100,102,99,101,1,2\n" for i in range(80)))
        dataset = root / "dataset"
        manifest = import_kraken(cfg, dataset, files={"BTC/USD": source})
        registration = root / "registration.json"
        registration.write_text(json.dumps({"schema": 1, "dataset_sha256": manifest["csv_sha256"],
                                            "holdout_start_index": 60, "holdout_start": datetime.fromtimestamp(start + 60 * cfg.seconds, timezone.utc).isoformat()}))
        return cfg, dataset, registration

    def test_training_runner_never_replays_registered_holdout(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, dataset, registration = self.fixture(root)
            output = root / "development"
            original = hashlib.sha256((dataset / "candles.csv").read_bytes()).hexdigest()
            calls = []

            def replay(prefix, config, path, **kwargs):
                self.assertTrue((output / "registration.json").exists())
                boundary = datetime.fromisoformat(json.loads(registration.read_text())["holdout_start"])
                self.assertEqual(len(prefix.bars["BTC/USD"]), 60)
                self.assertLessEqual(prefix.bars["BTC/USD"][-1].end, boundary)
                calls.append(path)
                return run_backtest(prefix, config, path, **kwargs)

            with patch("crypto_trader_v2.development.run_backtest", side_effect=replay):
                report = run_development(dataset, cfg, registration, output)
            self.assertEqual(len(calls), 3)
            self.assertFalse(report["holdout_evaluated"])
            self.assertFalse(report["approved_for_live"])
            self.assertEqual(hashlib.sha256((dataset / "candles.csv").read_bytes()).hexdigest(), original)
            with self.assertRaisesRegex(ValueError, "exists"):
                run_development(dataset, cfg, registration, output)

    def test_bad_registration_is_rejected_before_outputs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, dataset, registration = self.fixture(root)
            payload = json.loads(registration.read_text())
            payload["dataset_sha256"] = "wrong"
            registration.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "does not match"):
                run_development(dataset, cfg, registration, root / "out")
            self.assertFalse((root / "out").exists())

    def test_cli_diagnostic_and_invalid_execution(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "simulation.sqlite"
            cfg = config()
            run_backtest(demo_dataset(cfg, 300), cfg, path)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["diagnostics", "--db", str(path)]), 0)
                self.assertEqual(main(["demo", "--entry-fill-fraction", "NaN", "--db", str(Path(folder) / "invalid.sqlite")]), 1)
            self.assertFalse((Path(folder) / "invalid.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
