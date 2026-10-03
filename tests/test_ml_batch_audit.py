from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from crypto_trader_v2.data import demo_dataset
from crypto_trader_v2.entry_policy import policies
from crypto_trader_v2.policy_study import _metrics, _prefix_dataset
from crypto_trader_v2.research import run_backtest
from scripts.verify_ml_batch import distribution, equivalent, ledger_check
from tests.test_ml import config


class BatchAuditTests(unittest.TestCase):
    def test_numeric_tolerance_never_relaxes_identity_or_money_evidence(self):
        equivalent({"score": .2 + 1e-14, "money": "10.00", "id": "a"}, {"score": .2, "money": D(10), "id": "a"})
        for actual, expected in ((.201, .2), ("10.1", D(10)), ("a", "b"), ({"a": 1}, {"b": 1})):
            with self.assertRaises(ValueError):
                equivalent(actual, expected)

    def test_read_only_ledger_reconciles_and_modified_financial_report_is_rejected(self):
        cfg = config()
        dataset = demo_dataset(cfg, 90)
        prefix = _prefix_dataset(dataset, 70)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "cash.sqlite"
            report = run_backtest(prefix, cfg, path, start_index=10, entry_policy=policies()["cash"])
            metrics = _metrics(report)
            checked = ledger_check(path, cfg, dataset, 70, metrics, prefix=prefix, policy_name="cash")
            self.assertEqual(checked["orders"], 0)
            self.assertEqual(checked["integrity"], "ok")
            with self.assertRaisesRegex(ValueError, "Accounting"):
                ledger_check(path, cfg, dataset, 70, metrics | {"net_return_pct": "1"}, prefix=prefix)
            with self.assertRaises(ValueError):
                ledger_check(path, cfg, dataset, 70, metrics, prefix=prefix, policy_name="baseline")

    def test_gate_quantiles_include_rejected_predictions_not_only_passes(self):
        scored = [{"gate_reason": "low_net_profit_probability", "gate_pass_ignoring_portfolio_constraints": False,
                   "prediction": {"probability": .2, "stress_return_pct": -1}},
                  {"gate_reason": "pass", "gate_pass_ignoring_portfolio_constraints": True,
                   "prediction": {"probability": .7, "stress_return_pct": 1}}]
        row = distribution(scored)
        self.assertEqual(row["passing_candidates"], 1)
        self.assertEqual(row["probability_quantiles"]["min"], .2)
        self.assertEqual(row["probability_quantiles"]["max"], .7)


if __name__ == "__main__":
    unittest.main()
