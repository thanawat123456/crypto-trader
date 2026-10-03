from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crypto_trader_v2.ml_batch import (TRIAL_ORDER, batch_status, launch_registered_batch,
                                      register_batch, run_registered_batch, summarize_reports)
from crypto_trader_v2.ml_labels import entry_features, feature_names
from crypto_trader_v2.domain import utc
from crypto_trader_v2.ml_model import HYPERPARAMETERS, fit_model, load_model
from crypto_trader_v2.ml_variants import INTERACTION_SCHEMA, VARIANTS, map_features, mapped_names
from tests.test_ml import START, context, trained_fixture


class VariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg, cls.spec, cls.fit, cls.calibration, cls.control = trained_fixture()

    def test_fixed_feature_maps_are_finite_causal_and_keep_raw_domain_inputs(self):
        raw = entry_features(context(START), ("BTC/USD",))
        for name in VARIANTS:
            mapped = map_features(raw, ("BTC/USD",), name)
            self.assertEqual(mapped[:len(raw)], raw)
            self.assertEqual(len(mapped), len(mapped_names(("BTC/USD",), name)))
            self.assertEqual(mapped, map_features(raw, ("BTC/USD",), name))
            for value in mapped[len(raw):]:
                self.assertLessEqual(abs(value), 1)
        self.assertEqual(map_features(raw, ("BTC/USD",), "linear-v1"), raw)
        for raw_bad, variant in ((raw[:-1], "interactions"), ((float("nan"), *raw[1:]), "interactions"), (raw, "optimized")):
            with self.assertRaises(ValueError):
                map_features(raw_bad, ("BTC/USD",), variant)

    def test_legacy_identity_payload_is_unchanged(self):
        self.assertNotIn("variant", self.control.summary)
        self.assertEqual(self.control.feature_names, feature_names(self.control.symbols))
        self.assertEqual(self.control.summary["hyperparameters"], HYPERPARAMETERS)
        archived = Path("v2_data/ml-20261002-001/fold-01-model.json")
        if archived.exists():
            raw = json.loads(archived.read_bytes())
            self.assertEqual(load_model(archived).digest, raw["sha256"])

    def test_variants_roundtrip_and_do_not_lower_decision_gates(self):
        with TemporaryDirectory() as folder:
            for name in VARIANTS:
                model = fit_model(self.fit, self.calibration, self.cfg, self.spec, self.control.inference_from, variant=name)
                again = fit_model(self.fit, self.calibration, self.cfg, self.spec, self.control.inference_from, variant=name)
                self.assertEqual(model.digest, again.digest)
                self.assertEqual(model.ready, self.control.ready)
                self.assertEqual(model.summary["hyperparameters"]["probability_threshold"], 0.60)
                self.assertEqual(model.summary["hyperparameters"]["minimum_stress_return_pct"], 0.25)
                self.assertEqual(model.summary["requirements"], self.control.summary["requirements"])
                if name != "linear-v1":
                    self.assertEqual(model.schema, INTERACTION_SCHEMA)
                path = Path(folder) / (name + ".json")
                model.save(path)
                self.assertEqual(load_model(path).digest, model.digest)
                prediction = model.predict(self.fit[0].features)
                self.assertAlmostEqual(sum(prediction["contributions"].values()) + prediction["return_intercept_pct"],
                                       prediction["stress_return_pct"])

    def test_calibration_never_changes_interaction_scaler_or_primary_fit(self):
        for name in ("interactions", "interactions-shrink"):
            model = fit_model(self.fit, self.calibration, self.cfg, self.spec, self.control.inference_from, variant=name)
            changed = [replace(s, features=tuple(v + 100 for v in s.features)) for s in self.calibration]
            other = fit_model(self.fit, changed, self.cfg, self.spec, self.control.inference_from, variant=name)
            for field in ("mean", "scale", "logistic", "ridge"):
                self.assertEqual(getattr(model, field), getattr(other, field))
            self.assertFalse(other.ready)
            self.assertTrue(other.predict(changed[0].features)["ood"])

    def test_new_variants_still_reject_future_outcomes_and_invalid_artifact_protocol(self):
        with self.assertRaisesRegex(ValueError, "inference"):
            fit_model(self.fit, [replace(self.calibration[0], label_end=utc(self.control.inference_from))],
                      self.cfg, self.spec, self.control.inference_from, variant="interactions")
        model = fit_model(self.fit, self.calibration, self.cfg, self.spec, self.control.inference_from, variant="interactions")
        for bad in (replace(model, schema="net-entry-linear-v1"),
                    replace(model, summary={**model.summary, "variant": "not-registered"}),
                    replace(model, summary={**model.summary, "hyperparameters": HYPERPARAMETERS | {"probability_threshold": 0.1}})):
            with self.assertRaises(ValueError):
                bad.validate()


def fake_report(eligible=False, validation_return="-1"):
    return {"candidate_samples": 100, "fitted_models": 1, "ready_models": 1,
            "selected_cash_windows": 0 if eligible else 1,
            "folds": [{"fold": "fold-01", "selection": {"failure_reasons": [] if eligible else ["approval_failed"]},
                       "validation": {"ml": {"estimated_liquidation_return_pct": validation_return},
                                      "cash": {"estimated_liquidation_return_pct": "0"}}}]}


class BatchTests(unittest.TestCase):
    def test_selection_priority_uses_approval_only_not_higher_validation_profit(self):
        _, choices = summarize_reports([("linear-v1", fake_report(True, "-10")),
                                        ("interactions", fake_report(True, "100"))])
        self.assertEqual(choices[0]["choice"], "linear-v1")
        self.assertEqual(choices[0]["retrospective_selected_validation"]["estimated_liquidation_return_pct"], "-10")
        _, cash = summarize_reports([("linear-v1", fake_report(False, "100"))])
        self.assertEqual(cash[0]["choice"], "cash")

    def test_registration_before_fit_complete_trial_ledger_and_duplicate_refusal(self):
        cfg = trained_fixture()[0]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            registration = root / "source.json"
            registration.write_text("{}")
            output = root / "batch"
            source = {"development_end_exclusive": "2023-07-17T04:00:00+00:00"}
            with patch("crypto_trader_v2.ml_batch.load_prepared_ml_source", return_value=(SimpleNamespace(checksum="a" * 64), source)):
                plan = register_batch(root, cfg, registration, output)
                calls = []

                def research(directory, config, registration_arg, destination, **kwargs):
                    self.assertTrue((output / "registration.json").exists())
                    calls.append(kwargs["variant"])
                    destination.mkdir()
                    report = fake_report()
                    (destination / "report.json").write_text(json.dumps(report))
                    return report

                with patch("crypto_trader_v2.ml_batch.run_ml_study", side_effect=research):
                    result = run_registered_batch(output, cfg)
                self.assertEqual(calls, list(TRIAL_ORDER))
                self.assertEqual(result["completed_trials"], 4)
                self.assertEqual(result["status"], "complete")
                self.assertFalse(result["approved_for_live"])
                self.assertEqual(batch_status(output)["status"], "complete")
                self.assertEqual(len(list(output.glob("checkpoint-*.json"))), 4)
                self.assertEqual(plan["repeats_per_trial"], 1)
                with self.assertRaisesRegex(ValueError, "completed"):
                    run_registered_batch(output, cfg)
                with self.assertRaisesRegex(ValueError, "exists"):
                    register_batch(root, cfg, registration, output)

    def test_time_budget_and_failure_stop_without_retries_or_promotion(self):
        cfg = trained_fixture()[0]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            registration = root / "source.json"
            registration.write_text("{}")
            source = {"development_end_exclusive": "2023-07-17T04:00:00+00:00"}
            with patch("crypto_trader_v2.ml_batch.load_prepared_ml_source", return_value=(SimpleNamespace(checksum="a" * 64), source)):
                for exc, expected in ((TimeoutError("deadline"), "deadline_reached"), (ValueError("bad data"), "failed")):
                    output = root / expected
                    register_batch(root, cfg, registration, output)
                    with patch("crypto_trader_v2.ml_batch.run_ml_study", side_effect=exc) as run:
                        report = run_registered_batch(output, cfg)
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(report["status"], expected)
                    self.assertEqual(report["completed_trials"], 0)
                    self.assertFalse(report["default_policy_changed"])
                for hours in (True, 0, 13):
                    with self.assertRaisesRegex(ValueError, "budget"):
                        register_batch(root, cfg, registration, root / str(hours), hours=hours)

    def test_changed_code_blocks_worker_and_background_duplicate_is_refused(self):
        cfg = trained_fixture()[0]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            registration = root / "source.json"
            registration.write_text("{}")
            output = root / "batch"
            source = {"development_end_exclusive": "2023-07-17T04:00:00+00:00"}
            with patch("crypto_trader_v2.ml_batch.load_prepared_ml_source", return_value=(SimpleNamespace(checksum="a" * 64), source)):
                register_batch(root, cfg, registration, output)
                with patch("crypto_trader_v2.ml_batch._code_hash", return_value="changed"):
                    with self.assertRaisesRegex(ValueError, "changed"):
                        run_registered_batch(output, cfg)
                self.assertFalse((output / "worker.json").exists())
                with patch("crypto_trader_v2.ml_batch.subprocess.Popen", return_value=SimpleNamespace(pid=123)) as popen:
                    launched = launch_registered_batch(output, cfg, None)
                    self.assertEqual(launched["pid"], 123)
                    self.assertTrue(popen.call_args.kwargs["start_new_session"])
                with self.assertRaisesRegex(ValueError, "launched"):
                    launch_registered_batch(output, cfg, None)


if __name__ == "__main__":
    unittest.main()
