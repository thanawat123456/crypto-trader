from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.broker import ExecutionScenario, PaperBroker
from crypto_trader_v2.config import Config, StrategyConfig
from crypto_trader_v2.context import MarketContext
from crypto_trader_v2.data import Dataset, demo_dataset
from crypto_trader_v2.diagnostics import read_diagnostics
from crypto_trader_v2.domain import Instrument, Mode, Quote, Signal, ZERO
from crypto_trader_v2.engine import Coordinator, intent_id
from crypto_trader_v2.entry_policy import EntryPolicy, context_from_bars, policies
from crypto_trader_v2.importer import import_kraken
from crypto_trader_v2.policy_study import chronological_windows, run_policy_study, select_policy
from crypto_trader_v2.research import run_backtest
from crypto_trader_v2.storage import Store


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def config():
    return replace(Config(), instruments=(Instrument("BTC/USD", D("0.001"), D("0.001"), D(1)),),
                   strategy=StrategyConfig(trend_period=3, slope_bars=1, breakout_bars=3, exit_bars=2, atr_period=2))


def context(at=NOW, regime="trend_up"):
    return MarketContext("BTC/USD", at, regime, D("0.02"), D(1), D("0.1"), D("0.5"), D(1), D("0.1"))


def market(cfg, at=NOW):
    bars = demo_dataset(cfg, 300).bars["BTC/USD"][-12:]
    shifted = [replace(b, start=at - timedelta(seconds=(12 - i) * cfg.seconds)) for i, b in enumerate(bars)]
    return {"BTC/USD": shifted}, {"BTC/USD": Quote("BTC/USD", at, D(100), D(100))}


def good_score(**changes):
    return {"estimated_liquidation_return_pct": D(1), "net_return_pct": D(1), "max_drawdown_pct": D(1),
            "closed_episodes": 30, "profit_factor": D("1.3"), "net_expectancy_quote": D("0.1"),
            "fees_paid_quote_equivalent": D(1), "start": "fixture", "end": "fixture", "open_positions": [], **changes}


class PolicyTests(unittest.TestCase):
    def test_round_trip_cost_and_ratio_are_not_probability(self):
        cfg = config()
        quote = Quote("BTC/USD", NOW, D("99.95"), D("100.05"))
        signal = Signal("BTC/USD", NOW, True, False, D(2), "fixture")
        result = policies()["cost"].evaluate(signal, context(), quote, cfg)
        fee, slip = cfg.costs.taker_fee, cfg.costs.slippage
        expected = quote.ask * (1 + slip) * (1 + fee) / (quote.bid * (1 - slip) * (1 - fee)) - 1
        self.assertEqual(result.round_trip_break_even, expected)
        self.assertEqual(result.atr_room_proxy, D("0.04"))
        self.assertEqual(result.room_cost_ratio, D("0.04") / expected)
        self.assertNotIn("probability", result.payload())
        self.assertTrue(result.allowed)

    def test_low_room_and_adverse_slippage_reject_entry(self):
        cfg = config()
        quote = Quote("BTC/USD", NOW, D(100), D(100))
        signal = Signal("BTC/USD", NOW, True, False, D("0.1"), "fixture")
        self.assertFalse(policies()["cost"].evaluate(signal, context(), quote, cfg).allowed)
        signal = replace(signal, atr=D(2))
        self.assertTrue(policies()["cost"].evaluate(signal, context(), quote, cfg).allowed)
        self.assertFalse(policies()["cost"].evaluate(signal, context(), quote, cfg,
                                                    ExecutionScenario(extra_slippage=D("0.02"))).allowed)

    def test_regime_and_context_fail_closed(self):
        cfg = config()
        quote = Quote("BTC/USD", NOW, D(100), D(100))
        signal = Signal("BTC/USD", NOW, True, False, D(2), "fixture")
        self.assertTrue(policies()["trend"].evaluate(signal, context(), quote, cfg).allowed)
        for invalid in (None, context(regime="range"), context(regime="high_volatility"),
                        context(at=NOW + timedelta(hours=4)), replace(context(), efficiency_20=D("NaN"))):
            self.assertFalse(policies()["trend"].evaluate(signal, invalid, quote, cfg).allowed)
        self.assertFalse(policies()["trend"].evaluate(signal, context(), replace(quote, symbol="ETH/USD"), cfg).allowed)
        self.assertFalse(policies()["trend"].evaluate(signal, context(), replace(quote, time=NOW - timedelta(seconds=1)), cfg).allowed)

    def test_zero_cost_has_no_infinite_serialized_score(self):
        cfg = replace(config(), costs=replace(config().costs, taker_fee=ZERO, slippage=ZERO, simulated_spread=ZERO))
        result = policies()["cost"].evaluate(Signal("BTC/USD", NOW, True, False, D(2), "fixture"),
                                            context(), Quote("BTC/USD", NOW, D(100), D(100)), cfg)
        self.assertTrue(result.allowed)
        self.assertEqual(result.round_trip_break_even, ZERO)
        self.assertIsNone(result.room_cost_ratio)

    def test_cost_gate_rejects_unwarmed_context(self):
        result = policies()["cost"].evaluate(Signal("BTC/USD", NOW, True, False, D(2), "fixture"),
                                            context(regime="warmup"), Quote("BTC/USD", NOW, D(100), D(100)), config())
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "entry_policy_context_unavailable")

    def test_cash_and_baseline_behaviors(self):
        signal = Signal("BTC/USD", NOW, True, False, D(2), "fixture")
        quote = Quote("BTC/USD", NOW, D(100), D(100))
        self.assertTrue(policies()["baseline"].evaluate(signal, None, quote, config()).allowed)
        self.assertFalse(policies()["cash"].evaluate(signal, None, quote, config()).allowed)

    def test_invalid_thresholds_reject_before_database(self):
        with TemporaryDirectory() as folder:
            cfg = config()
            path = Path(folder) / "bad.sqlite"
            for policy in (EntryPolicy(min_room_cost_ratio=D("NaN")), EntryPolicy(room_atr_multiple=ZERO), EntryPolicy(require_uptrend=1)):
                with self.assertRaises(ValueError):
                    run_backtest(demo_dataset(cfg, 300), cfg, path, entry_policy=policy)
                self.assertFalse(path.exists())


class EnginePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.cfg = config()
        self.path = Path(self.temp.name) / "run.sqlite"
        self.store = Store(self.path, self.cfg, Mode.PAPER, create=True, source="UNIT TEST ONLY")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def buy(self):
        self.store.intent("buy", "BTC/USD", "buy", D(1), NOW, "fixture", cash_reserved=D(102),
                          risk_reserved=D(5), stop=D(95), atr_distance=D(5))
        PaperBroker(self.cfg).execute(self.store, "buy", Quote("BTC/USD", NOW, D(100), D(100)))

    def test_gate_rejects_before_intent_and_audits_reason(self):
        coordinator = Coordinator(self.store, self.cfg, entry_policy=policies()["trend"])
        bars, quotes = market(self.cfg)
        signal = Signal("BTC/USD", NOW, True, False, D(2), "fixture")
        coordinator.cycle(NOW, bars, quotes, prepared_signals={"BTC/USD": signal}, contexts={"BTC/USD": context(regime="range")})
        self.assertFalse(self.store.orders())
        row = self.store.db.execute("SELECT accepted,reason,payload FROM entry_evaluations").fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "entry_policy_not_uptrend")
        self.assertEqual(json.loads(row[2])["entry_regime"], "range")

    def test_gate_pass_keeps_risk_budget_and_entry_accounting(self):
        coordinator = Coordinator(self.store, self.cfg, entry_policy=policies()["trend_cost"])
        bars, quotes = market(self.cfg)
        coordinator.cycle(NOW, bars, quotes, prepared_signals={"BTC/USD": Signal("BTC/USD", NOW, True, False, D(2), "fixture")},
                          contexts={"BTC/USD": context()})
        self.assertIn("BTC/USD", self.store.positions())
        self.assertLessEqual(D(self.store.get_meta("initial_risk:" + self.store.orders()[0]["id"])), self.cfg.initial_cash * self.cfg.risk.per_trade)
        self.store.assert_balanced()

    def test_missing_context_does_not_allow_prepared_entry(self):
        coordinator = Coordinator(self.store, self.cfg, entry_policy=policies()["trend"])
        bars, quotes = market(self.cfg)
        coordinator.cycle(NOW, bars, quotes, prepared_signals={"BTC/USD": Signal("BTC/USD", NOW, True, False, D(2), "fixture")})
        self.assertFalse(self.store.positions())

    def test_entry_policy_cannot_block_protective_exit(self):
        self.buy()
        coordinator = Coordinator(self.store, self.cfg, entry_policy=policies()["cash"])
        bars, _ = market(self.cfg)
        coordinator.cycle(NOW, bars, {"BTC/USD": Quote("BTC/USD", NOW, D(90), D(90))},
                          prepared_signals={"BTC/USD": Signal("BTC/USD", NOW, False, False, D(2), "fixture")},
                          contexts={"BTC/USD": context(regime="range")})
        self.assertFalse(self.store.positions())

    def test_policy_identity_is_bound_across_restart(self):
        Coordinator(self.store, self.cfg, entry_policy=policies()["trend"])
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.PAPER)
        Coordinator(self.store, self.cfg, entry_policy=policies()["trend"])
        with self.assertRaisesRegex(ValueError, "policy mismatch"):
            Coordinator(self.store, self.cfg, entry_policy=policies()["cost"])

    def test_old_run_without_policy_cannot_silently_enable_filter(self):
        with self.store.transaction():
            self.store.db.execute("DROP TABLE entry_evaluations")
            self.store.set_meta("schema", "2")
        self.store.close()
        self.store = Store(self.path, self.cfg, Mode.PAPER)
        with self.assertRaisesRegex(ValueError, "no bound entry policy"):
            Coordinator(self.store, self.cfg, entry_policy=policies()["trend"])
        self.assertEqual(self.store.get_meta("schema"), "2")
        Coordinator(self.store, self.cfg)

    def test_recovered_filtered_entry_is_rechecked(self):
        coordinator = Coordinator(self.store, self.cfg, entry_policy=policies()["trend"])
        self.store.intent("unvalidated", "BTC/USD", "buy", D(1), NOW, "fixture", cash_reserved=D(102),
                          risk_reserved=D(5), stop=D(95), atr_distance=D(5))
        self.store.acknowledge("unvalidated")
        bars, quotes = market(self.cfg)
        coordinator.cycle(NOW, bars, quotes)
        self.assertEqual(self.store.order("unvalidated")["status"], "CANCELED")
        self.assertFalse(self.store.positions())

    def test_recovered_filtered_entry_requires_current_breakout(self):
        coordinator = Coordinator(self.store, self.cfg, entry_policy=policies()["cost"])
        bars, quotes = market(self.cfg)
        signal = Signal("BTC/USD", NOW, False, False, D(2), "fixture")
        identifier = intent_id("BTC/USD", "buy", signal.at.isoformat())
        self.store.intent(identifier, "BTC/USD", "buy", D(1), NOW, "fixture", cash_reserved=D(102),
                          risk_reserved=D(5), stop=D(95), atr_distance=D(5))
        self.store.acknowledge(identifier)
        with patch("crypto_trader_v2.engine.context_from_bars", return_value=(signal, context())):
            coordinator.cycle(NOW, bars, quotes)
        self.assertEqual(self.store.order(identifier)["status"], "CANCELED")
        self.assertFalse(self.store.positions())

    def test_closed_history_path_matches_prepared_context(self):
        bars = demo_dataset(self.cfg, 300).bars["BTC/USD"][:21]
        signal, ctx = context_from_bars(bars, self.cfg)
        self.assertTrue(signal.enter)
        now = bars[-1].end
        quotes = {"BTC/USD": Quote("BTC/USD", now, bars[-1].close, bars[-1].close)}
        Coordinator(self.store, self.cfg, entry_policy=policies()["trend_cost"]).cycle(now, {"BTC/USD": bars}, quotes)
        other = Path(self.temp.name) / "prepared.sqlite"
        with Store(other, self.cfg, Mode.PAPER, create=True, source="UNIT TEST ONLY") as store:
            Coordinator(store, self.cfg, entry_policy=policies()["trend_cost"]).cycle(
                now, {"BTC/USD": bars}, quotes, prepared_signals={"BTC/USD": signal}, contexts={"BTC/USD": ctx})
            self.assertEqual(self.store.positions(), store.positions())
            self.assertEqual(self.store.cash, store.cash)
            records = "SELECT * FROM entry_evaluations ORDER BY evaluated_at,symbol"
            self.assertEqual([tuple(r) for r in self.store.db.execute(records)], [tuple(r) for r in store.db.execute(records)])
            self.assertGreater(len(self.store.orders()), 0)


class ReplayTests(unittest.TestCase):
    def test_filtered_backtest_and_paper_replay_parity(self):
        cfg, data = config(), demo_dataset(config(), 300)
        for name in ("trend", "cost", "trend_cost"):
            baseline = run_backtest(data, cfg, entry_policy=policies()[name])
            paper = run_backtest(data, cfg, mode=Mode.PAPER, entry_policy=policies()[name])
            for field in ("marked_nav", "estimated_liquidation_nav", "closed_episodes", "realized_net_pnl"):
                self.assertEqual(baseline[field], paper[field])

    def test_cash_control_does_not_mean_profitable_model(self):
        result = run_backtest(demo_dataset(config(), 300), config(), entry_policy=policies()["cash"])
        self.assertEqual(result["estimated_liquidation_return_pct"], ZERO)
        self.assertEqual(result["closed_episodes"], 0)
        self.assertIsNone(result["profit_factor"])

    def test_filtered_partial_and_missed_execution_replay(self):
        cfg, data = config(), demo_dataset(config(), 600)
        scenario = ExecutionScenario(D("0.5"), D("0.5"), 3, D("0.001"))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "filtered.sqlite"
            result = run_backtest(data, cfg, path, execution=scenario, entry_policy=policies()["trend_cost"])
            diagnostic = read_diagnostics(path)
            self.assertGreater(diagnostic["execution"]["partial_entries"], 0)
            self.assertGreater(diagnostic["execution"]["partial_exits"], 0)
            self.assertGreater(diagnostic["execution"]["no_fill_entries"], 0)
            self.assertGreater(result["closed_episodes"], 0)
            self.assertEqual(diagnostic["open_episode_count"], len(result["open_positions"]))
            self.assertLessEqual(result["estimated_liquidation_nav"], result["marked_nav"])
            self.assertEqual(diagnostic["entry_policy"]["name"], "trend_cost")

    def test_liquidation_estimate_includes_open_exit_costs(self):
        cfg = config()
        result = run_backtest(demo_dataset(cfg, 300), cfg, end_index=60)
        self.assertGreater(len(result["open_positions"]), 0)
        self.assertLess(result["estimated_liquidation_nav"], result["marked_nav"])

    def test_future_mutation_cannot_change_policy_evaluations(self):
        cfg = config()
        data = demo_dataset(cfg, 300)
        bars = data.bars["BTC/USD"]
        mutated = Dataset({"BTC/USD": bars[:150] + [replace(b, open=b.open * 5, high=b.high * 5, low=b.low * 5, close=b.close * 5) for b in bars[150:]]}, "UNIT TEST MUTATION", "changed", True)
        with TemporaryDirectory() as folder:
            paths = [Path(folder) / "a.sqlite", Path(folder) / "b.sqlite"]
            for source, path in zip((data, mutated), paths):
                run_backtest(source, cfg, path, entry_policy=policies()["trend_cost"])
            import sqlite3
            records = []
            cutoff = bars[149].end.isoformat()
            for path in paths:
                with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                    records.append(db.execute("SELECT * FROM entry_evaluations WHERE evaluated_at<? ORDER BY evaluated_at,symbol", (cutoff,)).fetchall())
            self.assertEqual(records[0], records[1])
            self.assertGreater(len(records[0]), 0)

    def test_cli_policy_flag(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["demo", "--bars", "300", "--entry-policy", "cash"]), 0)


class PolicyStudyTests(unittest.TestCase):
    def fixture(self, root, count=900):
        cfg = replace(config(), timeframe_minutes=1440)
        source = root / "source.csv"
        start = datetime(2022, 1, 1, tzinfo=timezone.utc)
        source.write_text("".join(f"{int(start.timestamp()) + i * cfg.seconds},100,102,99,101,1,2\n" for i in range(count)))
        directory = root / "dataset"
        manifest = import_kraken(cfg, directory, files={"BTC/USD": source})
        split = count * 3 // 4
        registration = root / "original.json"
        registration.write_text(json.dumps({"schema": 1, "dataset_sha256": manifest["csv_sha256"],
                                            "holdout_start_index": split, "holdout_start": (start + timedelta(days=split)).isoformat()}))
        return cfg, directory, registration

    def test_selection_requires_eligibility_and_abstains(self):
        self.assertEqual(select_policy({"baseline": good_score(), "trend": good_score(estimated_liquidation_return_pct=D(2))}), "trend")
        for score in (good_score(estimated_liquidation_return_pct=ZERO), good_score(closed_episodes=19),
                      good_score(profit_factor=None), good_score(net_expectancy_quote=D("-0.1")), good_score(max_drawdown_pct=D(7))):
            self.assertEqual(select_policy({"baseline": score}), "cash")
        self.assertEqual(select_policy({"baseline": good_score(), "trend": good_score()}), "baseline")

    def test_registration_selection_and_temporal_boundaries(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, directory, registration = self.fixture(root)
            output = root / "study"
            boundary = datetime.fromisoformat(json.loads(registration.read_text())["holdout_start"])

            def replay(data, cfg_arg, path, **kwargs):
                plan = json.loads((output / "registration.json").read_text())
                self.assertFalse(plan["holdout_evaluated"])
                self.assertLessEqual(data.bars["BTC/USD"][-1].end, boundary)
                fold = path.name.split("-")[1]
                if "validation" in path.name:
                    selection = json.loads((output / f"fold-{fold}-selection.json").read_text())
                    self.assertEqual(selection["selected_policy"], "cash")
                    return good_score(estimated_liquidation_return_pct=D(999))
                self.assertEqual(cfg_arg.digest(), cfg.digest())
                return good_score(estimated_liquidation_return_pct=D(-1), net_return_pct=D(-1))

            with patch("crypto_trader_v2.policy_study.run_backtest", side_effect=replay), \
                 patch("crypto_trader_v2.policy_study.read_diagnostics", return_value={"entry_evaluation_reasons": {}}), \
                 patch("crypto_trader_v2.policy_study.export_diagnostics"):
                report = run_policy_study(directory, cfg, registration, output)
            self.assertFalse(report["approved_for_live"])
            self.assertFalse(report["holdout_evaluated"])
            self.assertTrue(report["retrospective_validation"])
            self.assertEqual(report["selected_cash_windows"], len(report["folds"]))
            self.assertFalse(report["default_policy_changed"])
            with self.assertRaisesRegex(ValueError, "exists"):
                run_policy_study(directory, cfg, registration, output)

    def test_short_prefix_refuses_before_experiment_creation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, directory, registration = self.fixture(root, count=400)
            with self.assertRaisesRegex(ValueError, "15 calendar months"):
                run_policy_study(directory, cfg, registration, root / "study")
            self.assertFalse((root / "study").exists())


if __name__ == "__main__":
    unittest.main()
