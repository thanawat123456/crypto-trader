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

import numpy as np

from crypto_trader_v2.__main__ import main, parser
from crypto_trader_v2.broker import ExecutionScenario, PaperBroker
from crypto_trader_v2.config import Config, StrategyConfig
from crypto_trader_v2.context import MarketContext
from crypto_trader_v2.data import Dataset, demo_dataset
from crypto_trader_v2.domain import Bar, Instrument, Mode, Quote, Signal, ZERO, utc
from crypto_trader_v2.engine import Coordinator
from crypto_trader_v2.entry_policy import policies
from crypto_trader_v2.importer import import_kraken
from crypto_trader_v2.ml_labels import (LabelSpec, Sample, TradeLabel, build_samples, entry_features,
                                      feature_names, purged_partition, simulate_label, uniqueness_weights)
from crypto_trader_v2.ml_model import fit_model, load_model, prediction_metrics
from crypto_trader_v2.ml_policy import MLEntryPolicy
from crypto_trader_v2.ml_study import approval_reasons, run_ml_study, score_candidates
from crypto_trader_v2.research import run_backtest
from crypto_trader_v2.storage import Store


START = datetime(2022, 1, 1, tzinfo=timezone.utc)


def config():
    return replace(Config(), instruments=(Instrument("BTC/USD", D("0.001"), D("0.001"), D(1)),),
                   strategy=StrategyConfig(trend_period=3, slope_bars=1, breakout_bars=3, exit_bars=2, atr_period=2))


def context(at, distance=D("3.3")):
    return MarketContext("BTC/USD", at, "trend_up", D("0.02"), distance,
                         distance / 20, D("0.5"), D(1), D("0.1"))


def fake_samples(cfg, spec, start, count, seed):
    """Known linear relationship for SOFTWARE tests; no market profitability claim."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(count):
        at = start + timedelta(days=i)
        x = float(rng.uniform(-1.5, 1.5))
        features = entry_features(context(at, D(str(2 + x))), ("BTC/USD",))
        end = at + timedelta(seconds=cfg.seconds * spec.max_holding_bars)
        target = D(str(x * 3))
        index = int((at - START).total_seconds() // cfg.seconds) + 1
        rows.append(Sample(str(seed) + ":" + str(i), "BTC/USD", at, index, features, end,
                           TradeLabel(target + D("0.2"), end, "test_only"), TradeLabel(target, end, "test_only")))
    return rows


def trained_fixture(spec=LabelSpec()):
    cfg = config()
    fit = fake_samples(cfg, spec, START, 120, 1)
    calibration = fake_samples(cfg, spec, fit[-1].label_end + timedelta(days=1), 60, 2)
    boundary = calibration[-1].label_end + timedelta(days=1)
    return cfg, spec, fit, calibration, fit_model(fit, calibration, cfg, spec, boundary)


def bars(cfg, count=8, prices=None):
    return [Bar("BTC/USD", START + timedelta(seconds=i * cfg.seconds), cfg.seconds,
                *(prices[i] if prices else (D(100), D(100), D(100), D(100))), D(1)) for i in range(count)]


def signals(rows, atr=D(2)):
    return [Signal(bar.symbol, bar.end, i == 0, False, atr, "test_only") for i, bar in enumerate(rows)]


class LabelTests(unittest.TestCase):
    def test_horizon_exit_uses_next_open_and_both_side_costs(self):
        cfg, spec = config(), LabelSpec(max_holding_bars=2)
        rows = bars(cfg)
        result = simulate_label(rows, signals(rows), 1, cfg, spec)
        buy = D(100) * (1 + cfg.costs.simulated_spread / 2) * (1 + cfg.costs.slippage) * (1 + cfg.costs.taker_fee)
        sell = D(100) * (1 - cfg.costs.simulated_spread / 2) * (1 - cfg.costs.slippage) * (1 - cfg.costs.taker_fee)
        self.assertEqual(result.net_return_pct, (sell / buy - 1) * 100)
        self.assertEqual(result.exited_at, rows[3].start)
        self.assertEqual(result.exit_reason, "ml_holding_limit")

    def test_stop_before_same_bar_high_and_gap_execution(self):
        cfg = replace(config(), costs=replace(config().costs, taker_fee=ZERO, slippage=ZERO, simulated_spread=ZERO))
        rows = bars(cfg, prices=[(D(100), D(100), D(100), D(100)),
                                (D(100), D(150), D(95), D(110)),
                                *[(D(100), D(100), D(100), D(100))] * 6])
        result = simulate_label(rows, signals(rows), 1, cfg, LabelSpec(max_holding_bars=2))
        self.assertEqual(result.net_return_pct, D(-4))
        self.assertEqual(result.exited_at, rows[1].end)
        rows[1] = replace(rows[1], low=D(99))
        result = simulate_label(rows, signals(rows), 1, cfg, LabelSpec(max_holding_bars=2))
        self.assertEqual(result.net_return_pct, ZERO)  # not a fictitious same-bar fill at 146
        self.assertEqual(result.exited_at, rows[2].start)
        self.assertEqual(result.exit_reason, "protective_stop")

    def test_desired_flat_is_next_open(self):
        cfg, rows = config(), bars(config())
        prepared = signals(rows)
        prepared[1] = replace(prepared[1], exit=True)
        result = simulate_label(rows, prepared, 1, cfg, LabelSpec(max_holding_bars=3))
        self.assertEqual(result.exited_at, rows[2].start)
        self.assertEqual(result.exit_reason, "desired_flat")

    def test_incomplete_horizon_does_not_make_early_stop_label(self):
        cfg, rows = config(), bars(config(), 3)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            simulate_label(rows, signals(rows), 1, cfg, LabelSpec(max_holding_bars=2))

    def test_all_candidates_exist_even_when_portfolio_never_trades(self):
        cfg, data = config(), demo_dataset(config(), 600)
        samples = build_samples(data, cfg)
        cash = run_backtest(data, cfg, entry_policy=policies()["cash"])
        self.assertGreater(len(samples), 0)
        self.assertEqual(cash["closed_episodes"], 0)
        self.assertEqual(cash["open_positions"], [])
        self.assertTrue(any(s.labelled for s in samples))
        for sample in samples:
            sample.validate(cfg, LabelSpec())
            self.assertEqual(len(sample.features), len(feature_names(("BTC/USD",))))

    def test_future_prices_cannot_change_past_features_or_complete_labels(self):
        cfg, data = config(), demo_dataset(config(), 600)
        source = data.bars["BTC/USD"]
        changed = Dataset({"BTC/USD": source[:150] + [replace(b, open=b.open * 5, high=b.high * 5, low=b.low * 5, close=b.close * 5) for b in source[150:]]}, "UNIT TEST FUTURE MUTATION", "changed", True)
        before, after = build_samples(data, cfg), build_samples(changed, cfg)
        cutoff = source[150].start
        earlier = lambda rows: [(s.id, s.features) for s in rows if s.feature_at <= cutoff]
        complete = lambda rows: [s for s in rows if s.label_end < cutoff]
        self.assertEqual(earlier(before), earlier(after))
        self.assertEqual(complete(before), complete(after))
        self.assertGreater(len(complete(before)), 0)

    def test_missing_volume_is_explicit_not_future_imputed(self):
        values = entry_features(replace(context(START), volume_ratio_20=None), ("BTC/USD",))
        self.assertEqual(values[4:6], (0.0, 1.0))
        for bad in (replace(context(START), regime="warmup"), replace(context(START), atr_pct=D("NaN")),
                    replace(context(START), volume_ratio_20=D(-1))):
            with self.assertRaises(ValueError):
                entry_features(bad, ("BTC/USD",))

    def test_purging_and_embargo_are_global_across_symbols(self):
        cfg, spec = config(), LabelSpec(max_holding_bars=2)
        row = fake_samples(cfg, spec, START, 1, 1)[0]
        rows = [row, replace(row, id="eth", symbol="ETH/USD")]
        boundary = row.label_end
        kept, audit = purged_partition(rows, START, boundary, cfg, spec)
        self.assertEqual(kept, [])
        self.assertEqual(audit["purged_or_censored"], 2)
        kept, _ = purged_partition(rows, START, boundary + timedelta(seconds=cfg.seconds), cfg, spec)
        self.assertEqual(len(kept), 2)
        self.assertEqual(uniqueness_weights(rows, 2), [0.5, 0.5])

    def test_invalid_label_specs_fail(self):
        for spec in (LabelSpec(max_holding_bars=True), LabelSpec(embargo_bars=0), LabelSpec(adverse_slippage=D("NaN"))):
            with self.assertRaises(ValueError):
                spec.validate()


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.spec, cls.fit, cls.calibration, cls.model = trained_fixture()

    def test_model_learns_known_relationship_and_is_deterministic(self):
        self.assertTrue(self.model.fitted)
        self.assertTrue(self.model.ready)
        self.assertEqual(self.model, fit_model(self.fit, self.calibration, self.cfg, self.spec, self.model.inference_from))
        positive = self.model.predict(entry_features(context(START), ("BTC/USD",)))
        negative = self.model.predict(entry_features(context(START, D("0.7")), ("BTC/USD",)))
        self.assertGreater(positive["probability"], negative["probability"])
        self.assertGreater(positive["stress_return_pct"], 0)
        self.assertLess(negative["stress_return_pct"], 0)

    def test_calibration_does_not_refit_scaler_or_primary_coefficients(self):
        changed = [replace(s, features=tuple(v + 1 for v in s.features)) for s in self.calibration]
        model = fit_model(self.fit, changed, self.cfg, self.spec, self.model.inference_from)
        for field in ("mean", "scale", "ridge", "logistic"):
            self.assertEqual(getattr(model, field), getattr(self.model, field))
        self.assertNotEqual(model.digest, self.model.digest)
        self.assertFalse(model.ready)
        self.assertEqual(model.summary["calibration_in_domain_count"], 0)
        self.assertIn("calibration_domain_shift", model.readiness_reasons)

    def test_insufficient_support_and_single_class_abstain(self):
        tiny = fit_model(self.fit[:8], [], self.cfg, self.spec, self.model.inference_from)
        self.assertFalse(tiny.fitted)
        self.assertFalse(tiny.ready)
        one_class = [replace(s, adverse=replace(s.adverse, net_return_pct=D(1))) for s in self.fit]
        model = fit_model(one_class, self.calibration, self.cfg, self.spec, self.model.inference_from)
        self.assertFalse(model.fitted)
        self.assertIn("fit_class_support", model.readiness_reasons)

    def test_numerical_fit_without_reliability_stays_disabled(self):
        model = fit_model(self.fit[:30], self.calibration, self.cfg, self.spec, self.model.inference_from)
        self.assertTrue(model.fitted)
        self.assertFalse(model.ready)
        self.assertIn("fit_sample_count", model.readiness_reasons)

    def test_overlapping_outcomes_and_future_labels_cannot_fit(self):
        for fit, calibration in ((self.fit, [self.fit[-1]]),
                                 (self.fit, [replace(self.calibration[0], label_end=utc(self.model.inference_from))]),
                                 (self.fit + [self.fit[0]], self.calibration)):
            with self.assertRaises(ValueError):
                fit_model(fit, calibration, self.cfg, self.spec, self.model.inference_from)

    def test_outcome_cannot_hide_future_inside_earlier_interval(self):
        bad = replace(self.fit[0], adverse=replace(self.fit[0].adverse, exited_at=utc(self.model.inference_from)))
        with self.assertRaisesRegex(ValueError, "outside"):
            fit_model([bad, *self.fit[1:]], self.calibration, self.cfg, self.spec, self.model.inference_from)

    def test_json_artifact_roundtrip_and_tamper_detection(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "model.json"
            self.model.save(path)
            self.assertEqual(load_model(path).digest, self.model.digest)
            with self.assertRaises(FileExistsError):
                self.model.save(path)
            raw = json.loads(path.read_text())
            raw["model"]["ridge"][0] += 1
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_model(path)

    def test_invalid_model_readiness_and_coefficients_fail(self):
        with self.assertRaises(ValueError):
            replace(self.model, scale=(0.0,) * len(self.model.scale)).validate()
        with self.assertRaises(ValueError):
            replace(self.model, logistic=(float("nan"), *self.model.logistic[1:])).validate()
        with self.assertRaises(ValueError):
            replace(self.model, ready=True, calibration=None).validate()
        small = fit_model(self.fit[:30], self.calibration, self.cfg, self.spec, self.model.inference_from)
        with self.assertRaises(ValueError):
            replace(small, ready=True, readiness_reasons=()).validate()

    def test_future_validation_changes_do_not_refit_model(self):
        boundary = utc(self.model.inference_from)
        later = fake_samples(self.cfg, self.spec, boundary + timedelta(days=5), 30, 3)
        source = [*self.fit, *self.calibration, *later]
        changed = [*self.fit, *self.calibration, *[replace(s, adverse=replace(s.adverse, net_return_pct=D(-99))) for s in later]]
        build = lambda rows: fit_model([s for s in rows if s.id in {x.id for x in self.fit}],
                                     [s for s in rows if s.id in {x.id for x in self.calibration}], self.cfg, self.spec, boundary)
        self.assertEqual(build(source).digest, build(changed).digest)

    def test_probability_metrics_and_bins_on_separate_future_rows(self):
        later = fake_samples(self.cfg, self.spec, utc(self.model.inference_from) + timedelta(days=5), 40, 4)
        metrics = prediction_metrics(later, self.model)
        self.assertEqual(metrics["scored_samples"], 40)
        self.assertLess(metrics["brier"], metrics["constant_brier"])
        self.assertLess(metrics["log_loss"], metrics["constant_log_loss"])
        self.assertEqual(sum(row["count"] for row in metrics["calibration_bins"]), 40)

    def test_unseen_constant_features_do_not_hide_behind_scale_one(self):
        normal = self.model.predict(entry_features(context(START), ("BTC/USD",)))
        self.assertFalse(normal["ood"])
        shifted = replace(context(START), atr_pct=D("0.03"))
        self.assertTrue(self.model.predict(entry_features(shifted, ("BTC/USD",)))["ood"])
        unseen = replace(context(START), regime="high_volatility")
        self.assertTrue(self.model.predict(entry_features(unseen, ("BTC/USD",)))["ood"])


class MLPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.spec, cls.fit, cls.calibration, cls.model = trained_fixture()
        cls.now = utc(cls.model.inference_from)

    def check(self, policy=None, ctx=None, quote=None, at=None, execution=None):
        now = at or self.now
        return (policy or MLEntryPolicy(self.model)).evaluate(
            Signal("BTC/USD", now, True, False, D(2), "test_only"), ctx or context(now),
            quote or Quote("BTC/USD", now, D(100), D(100)), self.cfg, execution)

    def test_decision_includes_real_learned_scores_and_explanation(self):
        result = self.check()
        self.assertTrue(result.allowed)
        self.assertGreaterEqual(result.probability_net_positive, 0.6)
        self.assertGreater(result.estimated_stress_return_pct, 0.25)
        self.assertTrue(result.probability_calibrated)
        self.assertEqual(result.model_sha256, self.model.digest)
        self.assertIn("trend_distance_atr", result.return_contributions)

    def test_low_expectancy_probability_ood_and_time_reject(self):
        self.assertFalse(self.check(ctx=context(self.now, D("0.7"))).allowed)
        self.assertEqual(self.check(ctx=context(self.now, D(50))).reason, "ml_out_of_distribution")
        self.assertFalse(self.check(at=self.now - timedelta(seconds=1)).allowed)
        self.assertFalse(self.check(at=utc(self.model.expires_at)).allowed)
        self.assertFalse(self.check(ctx=context(self.now + timedelta(hours=4))).allowed)

    def test_cost_scope_and_training_scope_must_match(self):
        result = self.check(execution=ExecutionScenario(extra_slippage=D("0.02")))
        self.assertEqual(result.reason, "ml_execution_cost_out_of_scope")
        with self.assertRaisesRegex(ValueError, "differs from training"):
            MLEntryPolicy(self.model, adverse_slippage=D("0.002")).validate()

    def test_not_ready_model_can_score_but_cannot_buy(self):
        weak = fit_model(self.fit[:30], self.calibration, self.cfg, self.spec, self.now)
        result = self.check(policy=MLEntryPolicy(weak))
        self.assertIsNotNone(result.probability_net_positive)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "ml_evidence_insufficient")

    def test_config_mismatch_rejected_before_database_creation(self):
        other = replace(self.cfg, initial_cash=D(301))
        with TemporaryDirectory() as folder:
            path = Path(folder) / "bad.sqlite"
            with self.assertRaisesRegex(ValueError, "model/config"):
                run_backtest(demo_dataset(other, 300), other, path, entry_policy=MLEntryPolicy(self.model))
            self.assertFalse(path.exists())

    def test_entry_audit_risk_budget_and_restart_binding(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "run.sqlite"
            history = demo_dataset(self.cfg, 300).bars["BTC/USD"][-12:]
            history = [replace(b, start=self.now - timedelta(seconds=(12 - i) * self.cfg.seconds)) for i, b in enumerate(history)]
            with Store(path, self.cfg, Mode.PAPER, create=True, source="UNIT TEST ONLY") as store:
                policy = MLEntryPolicy(self.model)
                with patch("crypto_trader_v2.ml_model.fit_model", side_effect=AssertionError("Inference must not fit")):
                    Coordinator(store, self.cfg, entry_policy=policy).cycle(
                        self.now, {"BTC/USD": history}, {"BTC/USD": Quote("BTC/USD", self.now, D(100), D(100))},
                        prepared_signals={"BTC/USD": Signal("BTC/USD", self.now, True, False, D(2), "test_only")},
                        contexts={"BTC/USD": context(self.now)})
                self.assertIn("BTC/USD", store.positions())
                identifier = store.orders()[0]["id"]
                self.assertLessEqual(D(store.get_meta("initial_risk:" + identifier)), self.cfg.initial_cash * self.cfg.risk.per_trade)
                payload = json.loads(store.db.execute("SELECT payload FROM entry_evaluations").fetchone()[0])
                self.assertEqual(payload["model_sha256"], self.model.digest)
                store.assert_balanced()
            with Store(path, self.cfg, Mode.PAPER) as store:
                Coordinator(store, self.cfg, entry_policy=policy)
                with self.assertRaisesRegex(ValueError, "policy mismatch"):
                    Coordinator(store, self.cfg)

    def test_ml_holding_limit_cannot_be_blocked_by_entry_rejection(self):
        with TemporaryDirectory() as folder:
            with Store(Path(folder) / "run.sqlite", self.cfg, Mode.PAPER, create=True, source="UNIT TEST ONLY") as store:
                store.intent("buy", "BTC/USD", "buy", D(1), self.now, "test_only", cash_reserved=D(102),
                             risk_reserved=D(5), stop=D(95), atr_distance=D(5))
                PaperBroker(self.cfg).execute(store, "buy", Quote("BTC/USD", self.now, D(100), D(100)))
                later = self.now + timedelta(seconds=self.spec.max_holding_bars * self.cfg.seconds)
                history = demo_dataset(self.cfg, 300).bars["BTC/USD"][-12:]
                history = [replace(b, start=later - timedelta(seconds=(12 - i) * self.cfg.seconds)) for i, b in enumerate(history)]
                weak = fit_model(self.fit[:30], self.calibration, self.cfg, self.spec, self.now)
                Coordinator(store, self.cfg, entry_policy=MLEntryPolicy(weak)).cycle(
                    later, {"BTC/USD": history}, {"BTC/USD": Quote("BTC/USD", later, D(100), D(100))},
                    prepared_signals={"BTC/USD": Signal("BTC/USD", later, False, False, D(2), "test_only")})
                self.assertFalse(store.positions())
                self.assertEqual(store.orders()[-1]["reason"], "ml_holding_limit")

    def test_counterfactual_holding_label_matches_actual_replay_accounting(self):
        cfg, spec, _, _, model = trained_fixture(LabelSpec(max_holding_bars=2))
        now = utc(model.inference_from)
        rows = [replace(bar, start=now + timedelta(seconds=(i - 1) * cfg.seconds)) for i, bar in enumerate(bars(cfg))]
        prepared = signals(rows)
        label = simulate_label(rows, prepared, 1, cfg, spec)
        with TemporaryDirectory() as folder:
            with Store(Path(folder) / "replay.sqlite", cfg, Mode.BACKTEST, create=True, source="UNIT TEST ONLY") as store:
                coordinator = Coordinator(store, cfg, entry_policy=MLEntryPolicy(model))
                entry_cost = None
                for i in range(1, 4):
                    quote = Quote("BTC/USD", rows[i].start, rows[i].open * (1 - cfg.costs.simulated_spread / 2),
                                  rows[i].open * (1 + cfg.costs.simulated_spread / 2))
                    coordinator.cycle(rows[i].start, {"BTC/USD": rows[:i]}, {"BTC/USD": quote}, trail_quotes=False,
                                      prepared_signals={"BTC/USD": prepared[i - 1]}, contexts={"BTC/USD": context(rows[i].start)})
                    if i == 1:
                        entry_cost = store.positions()["BTC/USD"]["cost"]
                    coordinator.historical_bar({"BTC/USD": rows[i]})
                self.assertFalse(store.positions())
                episode = store.db.execute("SELECT pnl,closed_at FROM episodes").fetchone()
                actual_return = D(episode["pnl"]) / entry_cost * 100
                self.assertLess(abs(actual_return - label.net_return_pct), D("1e-20"))
                self.assertEqual(utc(episode["closed_at"]), label.exited_at)
                store.assert_balanced()


class MLStudyTests(unittest.TestCase):
    def test_approval_does_not_force_model_when_training_evidence_fails(self):
        cfg, spec, fit, calibration, model = trained_fixture()
        metric = {"closed_episodes": 15, "profit_factor": D("1.3"), "net_expectancy_quote": D("0.1"),
                  "estimated_liquidation_return_pct": D(1), "max_drawdown_pct": D(1)}
        forecast = {"scored_samples": 25, "brier": 0.1, "constant_brier": 0.2, "log_loss": 0.2, "constant_log_loss": 0.5}
        scored = [{"gate_pass_ignoring_portfolio_constraints": True, "adverse_net_return_pct": D(1)}] * 12
        self.assertEqual(approval_reasons(model, forecast, scored, {"base": metric, "adverse": metric}), [])
        forecast["brier"] = 0.3
        self.assertIn("approval_brier_not_better_than_prior", approval_reasons(model, forecast, scored, {"base": metric}))
        self.assertIn("approval_gate_pass_sample_count", approval_reasons(model, forecast, [], {"base": metric}))

    def test_registration_temporal_bounds_and_lock_before_validation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = replace(config(), timeframe_minutes=1440)
            source = root / "source.csv"
            source.write_text("".join(f"{int(START.timestamp()) + i * cfg.seconds},100,102,99,101,1,2\n" for i in range(900)))
            directory = root / "dataset"
            manifest = import_kraken(cfg, directory, files={"BTC/USD": source})
            boundary = START + timedelta(days=675)
            registration = root / "parent.json"
            registration.write_text(json.dumps({"schema": 1, "dataset_sha256": manifest["csv_sha256"],
                                                "holdout_start_index": 675, "holdout_start": boundary.isoformat()}))
            output = root / "ml"

            def build(data, cfg_arg, spec):
                self.assertTrue((output / "registration.json").exists())
                self.assertEqual(len(data.bars["BTC/USD"]), 675)
                return []

            def replay(data, cfg_arg, path, **kwargs):
                self.assertLessEqual(data.bars["BTC/USD"][-1].end, boundary)
                fold = path.name.split("-")[1]
                if "validation" in path.name:
                    selected = json.loads((output / f"fold-{fold}-selection.json").read_text())
                    self.assertEqual(selected["selected_policy"], "cash")
                return {"net_return_pct": ZERO, "estimated_liquidation_return_pct": ZERO, "max_drawdown_pct": ZERO,
                        "closed_episodes": 0, "profit_factor": None, "net_expectancy_quote": None,
                        "fees_paid_quote_equivalent": ZERO, "start": "test_only", "end": "test_only", "open_positions": []}

            with patch("crypto_trader_v2.ml_study.build_samples", side_effect=build), \
                 patch("crypto_trader_v2.ml_study.run_backtest", side_effect=replay), \
                 patch("crypto_trader_v2.ml_study.read_diagnostics", return_value={"entry_evaluation_reasons": {}}), \
                 patch("crypto_trader_v2.ml_study.export_diagnostics"):
                report = run_ml_study(directory, cfg, registration, output)
            self.assertFalse(report["approved_for_live"])
            self.assertFalse(report["holdout_evaluated"])
            self.assertFalse(report["default_policy_changed"])
            self.assertEqual(report["selected_cash_windows"], len(report["folds"]))
            self.assertEqual(report["fitted_models"], 0)
            self.assertNotIn("adverse", json.loads((output / "features.jsonl").read_text() or "{}"))
            with self.assertRaisesRegex(ValueError, "exists"):
                run_ml_study(directory, cfg, registration, output)

    def test_cli_has_offline_ml_research_not_live_ml_option(self):
        args = parser().parse_args(["ml-research", "data", "--registration", "parent.json", "--output", "new"])
        self.assertEqual(args.command, "ml-research")
        with self.assertRaises(SystemExit) as result, redirect_stdout(io.StringIO()):
            main(["ml-research", "--help"])
        self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
