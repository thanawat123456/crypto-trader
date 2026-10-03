from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.config import StrategyConfig, load_config
from crypto_trader_v2.data import Dataset, demo_dataset
from crypto_trader_v2.dust_checks import CHECKS, run_dust_checks


class DustCheckProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "checks"
        self.cfg = replace(load_config("config.binance-th.paper.yaml"), strategy=StrategyConfig(3, 1, 3, 2, 2))
        self.dataset = demo_dataset(self.cfg, 100)
        # Software orchestration fixtures, not a substitute for raw capture QA.
        self.capture = patch("crypto_trader_v2.dust_checks.verify_binance_capture", return_value={"status": "fixture"}).start()
        self.loader = patch("crypto_trader_v2.dust_checks.load_dataset", return_value=self.dataset).start()
        self.backtest = patch("crypto_trader_v2.dust_checks.run_backtest", side_effect=self.replay).start()
        self.audit = patch("crypto_trader_v2.dust_checks.verify_dust_ledger", return_value={
            "status": "verified", "fills_replayed": 0, "dust_transfers_replayed": 0}).start()
        self.addCleanup(patch.stopall)

    def replay(self, dataset, cfg, path, **kwargs):
        self.assertTrue((self.output / "registration.json").exists())
        self.assertFalse((self.output / "report.json").exists())
        plan = json.loads((self.output / "registration.json").read_text())
        self.assertEqual(plan["dataset_sha256"], dataset.checksum)
        self.assertEqual(plan["start_index"], cfg.warmup + 1)
        self.assertFalse(plan["approved_for_live"])
        return {"cash": D("300"), "marked_nav": D("300"), "net_return_pct": D("0"),
                "estimated_liquidation_return_pct": D("0"), "realized_net_pnl": D("0"),
                "fees_paid_quote_equivalent": D("0"), "max_drawdown_pct": D("0"),
                "closed_episodes": 0, "dust_marked_value": D("0")}

    def run_checks(self):
        return run_dust_checks(Path("native-fixture"), self.cfg, self.output)

    def test_four_cases_registered_before_replay_and_never_select_or_train(self):
        result = self.run_checks()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["models_trained"], 0)
        self.assertFalse(result["selection_performed"])
        self.assertFalse(result["default_policy_changed"])
        self.assertFalse(result["approved_for_live"])
        self.assertIn("NOT OOS", result["scope"])
        self.assertEqual(self.backtest.call_count, 4)
        self.assertEqual(self.audit.call_count, 4)
        for call, (name, policy, scenario) in zip(self.backtest.call_args_list, CHECKS):
            self.assertEqual(call.args[2].name, name + ".sqlite")
            self.assertEqual(call.kwargs["entry_policy"].name, policy)
            self.assertEqual(call.kwargs["execution"], scenario)
            self.assertEqual(call.kwargs["start_index"], self.cfg.warmup + 1)
            self.assertTrue((self.output / (name + "-audit.json")).exists())
        self.assertEqual(CHECKS[2][2].extra_slippage, D("0.001"))
        self.assertEqual(CHECKS[3][2].entry_fill_fraction, D("0.5"))
        self.assertEqual(CHECKS[3][2].exit_fill_fraction, D("0.4"))

    def test_existing_output_is_refused_before_capture_and_preserved(self):
        self.run_checks()
        original = (self.output / "report.json").read_bytes()
        self.capture.reset_mock()
        with self.assertRaisesRegex(ValueError, "output exists"):
            self.run_checks()
        self.capture.assert_not_called()
        self.assertEqual((self.output / "report.json").read_bytes(), original)

    def test_bad_capture_or_insufficient_history_cannot_register(self):
        self.capture.side_effect = ValueError("raw mismatch")
        with self.assertRaisesRegex(ValueError, "raw mismatch"):
            self.run_checks()
        self.assertFalse(self.output.exists())
        self.capture.side_effect = None
        self.loader.return_value = Dataset({s: bars[:self.cfg.warmup+1] for s, bars in self.dataset.bars.items()},
                                           "short fixture", "short", True)
        with self.assertRaisesRegex(ValueError, "post-warmup"):
            self.run_checks()
        self.assertFalse(self.output.exists())
        self.backtest.assert_not_called()

    def test_replay_or_audit_failure_leaves_registration_not_complete_report(self):
        self.audit.side_effect = ValueError("FIFO mismatch")
        with self.assertRaisesRegex(ValueError, "FIFO mismatch"):
            self.run_checks()
        self.assertTrue((self.output / "registration.json").exists())
        self.assertFalse((self.output / "report.json").exists())
        self.assertEqual(self.backtest.call_count, 1)

    def test_changed_package_cannot_emit_complete_report(self):
        with patch("crypto_trader_v2.dust_checks.code_hash", side_effect=["before", "after"]):
            with self.assertRaisesRegex(ValueError, "Package changed"):
                self.run_checks()
        self.assertTrue((self.output / "registration.json").exists())
        self.assertFalse((self.output / "report.json").exists())

    def test_other_venue_cannot_enter_native_engineering_protocol(self):
        with self.assertRaisesRegex(ValueError, "Binance TH"):
            run_dust_checks(Path("native-fixture"), load_config("config.v2.example.yaml"), self.output)
        self.capture.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
