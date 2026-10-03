from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.__main__ import main, parser
from crypto_trader_v2.config import Config, StrategyConfig
from crypto_trader_v2.data import demo_dataset
from crypto_trader_v2.domain import ZERO, utc
from crypto_trader_v2.importer import import_kraken, load_dataset, read_imported_source
from crypto_trader_v2.ml_model import HYPERPARAMETERS, canonical
from crypto_trader_v2.ml_preparation import (EXTENDED_PROTOCOL, LEGACY_PROTOCOL, SUPPORT_PROTOCOL, audit_dataset,
                                             load_prepared_ml_source, ml_partition_boundaries, ml_windows, prepare_ml_source)
from crypto_trader_v2.ml_study import run_ml_study
from crypto_trader_v2.policy_study import chronological_windows
from crypto_trader_v2.study import add_months


START = datetime(2016, 1, 1, tzinfo=timezone.utc)


def config():
    return replace(Config(), timeframe_minutes=1440,
                   strategy=StrategyConfig(trend_period=3, slope_bars=1, breakout_bars=3, exit_bars=2, atr_period=2))


def source(root, name, cfg, *, start=0, count=1800, missing=None, changed_after=None):
    files = {}
    missing = missing or {}
    for index, instrument in enumerate(cfg.instruments):
        path = root / (name + str(index) + ".csv")
        with path.open("x") as stream:
            for i in range(start, start + count):
                if i in missing.get(instrument.symbol, ()):
                    continue
                factor = 2 if changed_after is not None and i >= changed_after else 1
                price = (100 + index * 100) * factor
                timestamp = int((START + timedelta(days=i)).timestamp())
                stream.write(f"{timestamp},{price},{price + 2},{price - 1},{price + 1},1,2\n")
        files[instrument.symbol] = path
    directory = root / name
    manifest = import_kraken(cfg, directory, files=files)
    return directory, manifest


def protected(root, cfg):
    directory, manifest = source(root, "protected", cfg, start=1000, count=600)
    registration = root / "original-registration.json"
    registration.write_text(json.dumps({"schema": 1, "dataset_sha256": manifest["csv_sha256"],
                                        "config_hash": cfg.digest(), "holdout_start_index": 450,
                                        "holdout_start": (START + timedelta(days=1450)).isoformat()}))
    return directory, registration


def rehash(directory):
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["csv_sha256"] = hashlib.sha256((directory / "candles.csv").read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))


class SourceAuditTests(unittest.TestCase):
    def test_gappy_source_is_audited_but_not_replayable(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "gappy", cfg, count=80,
                                  missing={"ETH/USD": (25,), "BTC/USD": (60,)})
            result = audit_dataset(directory, cfg, root / "audit")
            self.assertFalse(result["replay_ready"])
            self.assertFalse(result["synchronized"])
            self.assertEqual([r["bars_per_symbol"] for r in result["common_contiguous_runs"]], [25, 34, 19])
            self.assertEqual(result["quality"]["ETH/USD"]["missing_candles"], 1)
            self.assertFalse(result["labels_computed"])
            self.assertFalse(result["approved_for_live"])
            self.assertTrue((root / "audit/report.json").exists())
            with self.assertRaisesRegex(ValueError, "quality gate"):
                load_dataset(directory, cfg)
            with self.assertRaisesRegex(ValueError, "exists"):
                audit_dataset(directory, cfg, root / "audit")

    def test_csv_checksum_and_manifest_flags_fail_closed(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg, count=80)
            path = directory / "manifest.json"
            original = path.read_text()
            for field, value in (("replay_ready", False), ("synchronized", False), ("quality", {})):
                manifest = json.loads(original)
                manifest[field] = value
                path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "disagree"):
                    audit_dataset(directory, cfg)
            path.write_text(original)
            with (directory / "candles.csv").open("a") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                audit_dataset(directory, cfg)

    def test_rows_are_validated_even_when_operator_updates_hash(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg, count=80)
            csv_path = directory / "candles.csv"
            original = csv_path.read_text().splitlines(keepends=True)
            mutations = []
            duplicate = original.copy()
            duplicate.insert(2, duplicate[1])
            mutations.append(duplicate)
            unsorted = original.copy()
            unsorted[1], unsorted[2] = unsorted[2], unsorted[1]
            mutations.append(unsorted)
            invalid = original.copy()
            invalid[1] = invalid[1].replace(",100,102,99,101,", ",100,98,99,101,")
            mutations.append(invalid)
            for rows in mutations:
                csv_path.write_text("".join(rows))
                rehash(directory)
                with self.assertRaises(ValueError):
                    read_imported_source(directory, cfg)

    def test_wrong_coverage_and_config_are_rejected(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg, count=80)
            with self.assertRaisesRegex(ValueError, "metadata/config"):
                audit_dataset(directory, replace(cfg, timeframe_minutes=240))
            path = directory / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["end"] = (START + timedelta(days=81)).isoformat()
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "coverage"):
                audit_dataset(directory, cfg)


class PreparationTests(unittest.TestCase):
    def test_support_protocol_is_registered_without_labels_or_new_holdout_access(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "extended", cfg)
            original, registration = protected(root, cfg)
            output = root / "support"
            with patch("crypto_trader_v2.ml_labels.build_samples", side_effect=AssertionError("No labels")):
                plan = prepare_ml_source(directory, cfg, original, registration, output, protocol_name="support")
            data, loaded = load_prepared_ml_source(output / "development", cfg, output / "registration.json")
            self.assertEqual(loaded["protocol_name"], "support")
            self.assertEqual(plan["protocol"], SUPPORT_PROTOCOL)
            self.assertEqual(plan["fold_indices"], ml_windows(data.bars["BTC/USD"], cfg.warmup, SUPPORT_PROTOCOL))
            self.assertEqual(data.bars["BTC/USD"][-1].end, START + timedelta(days=1450))
            self.assertEqual(plan["reserved_bars_per_symbol"], {"BTC/USD": 350, "ETH/USD": 350})
            self.assertFalse(plan["models_trained"])
            self.assertFalse(plan["approved_for_live"])

    def test_unknown_support_name_and_rehashed_named_protocol_tampering_fail_closed(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "extended", cfg)
            original, registration = protected(root, cfg)
            for name in ("search", {"fit_months": 1}, None):
                with self.assertRaisesRegex(ValueError, "registered.*protocol"):
                    prepare_ml_source(directory, cfg, original, registration, root / "bad", protocol_name=name)
                self.assertFalse((root / "bad").exists())
            output = root / "support"
            prepare_ml_source(directory, cfg, original, registration, output, protocol_name="support")
            path = output / "registration.json"
            raw = path.read_text()
            for name in ("extended", "search", None):
                envelope = json.loads(raw)
                envelope["plan"]["protocol_name"] = name
                envelope["sha256"] = hashlib.sha256(canonical(envelope["plan"])).hexdigest()
                path.write_text(json.dumps(envelope))
                with self.assertRaisesRegex(ValueError, "protocol/config"):
                    load_prepared_ml_source(output / "development", cfg, path)

    def test_support_needs_full_41_calendar_months_and_keeps_nonoverlapping_validation(self):
        cfg = config()
        timeline = demo_dataset(cfg, 1600).bars["BTC/USD"]
        windows = ml_windows(timeline, cfg.warmup, SUPPORT_PROTOCOL)
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(a[2] == b[1] for a, b in zip(windows, windows[1:])))
        with self.assertRaisesRegex(ValueError, "41 continuous"):
            ml_windows(timeline[:1200], cfg.warmup, SUPPORT_PROTOCOL)

    def test_support_approval_plus_validation_stays_within_unchanged_model_lifetime(self):
        for year in (2020, 2021, 2022):
            for month in range(1, 13):
                beginning = datetime(year, month, 28, tzinfo=timezone.utc)
                _, calibration_end = ml_partition_boundaries(beginning, SUPPORT_PROTOCOL)
                end = add_months(beginning, sum(SUPPORT_PROTOCOL[k] for k in
                                  ("fit_months", "calibration_months", "approval_months", "validation_months")))
                self.assertLess((end - calibration_end).days, HYPERPARAMETERS["expires_days_after_calibration"])

    def test_extension_is_registered_and_reserved_rows_are_physically_excluded(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "extended", cfg, missing={"ETH/USD": (50,)})
            original, registration = protected(root, cfg)
            output = root / "prepared"
            with patch("crypto_trader_v2.ml_labels.build_samples", side_effect=AssertionError("No labels during preparation")), \
                 patch("crypto_trader_v2.ml_model.fit_model", side_effect=AssertionError("No fitting during preparation")):
                plan = prepare_ml_source(directory, cfg, original, registration, output)
            self.assertEqual(plan["development_start"], (START + timedelta(days=51)).isoformat())
            self.assertEqual(plan["development_bars_per_symbol"], 1399)
            self.assertEqual(plan["protocol"], EXTENDED_PROTOCOL)
            self.assertEqual(plan["reserved_bars_per_symbol"], {"BTC/USD": 350, "ETH/USD": 350})
            data, loaded = load_prepared_ml_source(output / "development", cfg, output / "registration.json")
            self.assertEqual(data.checksum, loaded["dataset_sha256"])
            self.assertEqual(data.bars["BTC/USD"][-1].end, START + timedelta(days=1450))
            self.assertTrue(all(b.end <= utc(plan["development_end_exclusive"]) for rows in data.bars.values() for b in rows))
            self.assertFalse(plan["labels_computed"])
            self.assertFalse(plan["holdout_evaluated"])
            self.assertIn("NOT certified untouched", plan["reserved_data_status"])
            with self.assertRaisesRegex(ValueError, "exists"):
                prepare_ml_source(directory, cfg, original, registration, output)

    def test_changes_to_unconsumed_reserved_tail_do_not_change_development(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            first, _ = source(root, "first", cfg)
            changed, _ = source(root, "changed", cfg, changed_after=1600)
            original, registration = protected(root, cfg)
            plans = [prepare_ml_source(directory, cfg, original, registration, root / name)
                     for directory, name in ((first, "one"), (changed, "two"))]
            self.assertEqual(plans[0]["dataset_sha256"], plans[1]["dataset_sha256"])
            self.assertEqual(plans[0]["fold_indices"], plans[1]["fold_indices"])
            self.assertNotEqual(plans[0]["extended_source_audit"]["dataset_sha256"], plans[1]["extended_source_audit"]["dataset_sha256"])

    def test_original_price_rewrite_or_gap_is_rejected_before_output(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            original, registration = protected(root, cfg)
            sources = [source(root, "rewritten", cfg, changed_after=1200)[0],
                       source(root, "missing", cfg, missing={"BTC/USD": (1200,)})[0]]
            for number, directory in enumerate(sources):
                output = root / f"bad-{number}"
                with self.assertRaisesRegex(ValueError, "changes or omits"):
                    prepare_ml_source(directory, cfg, original, registration, output)
                self.assertFalse(output.exists())

    def test_synthetic_source_and_wrong_parent_config_are_rejected(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg)
            original, registration = protected(root, cfg)
            path = directory / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["synthetic"] = True
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "non-synthetic"):
                prepare_ml_source(directory, cfg, original, registration, root / "bad")
            manifest["synthetic"] = False
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "config mismatch"):
                prepare_ml_source(directory, replace(cfg, initial_cash=D(301)), original, registration, root / "bad")
            self.assertFalse((root / "bad").exists())

    def test_short_continuous_source_reports_insufficient_evidence(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg, missing={"ETH/USD": (950,)})
            original, registration = protected(root, cfg)
            with self.assertRaisesRegex(ValueError, "protected boundary|35 continuous"):
                prepare_ml_source(directory, cfg, original, registration, root / "bad")
            self.assertFalse((root / "bad").exists())

    def test_prepared_identity_tampering_and_protocol_changes_fail(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg)
            original, registration = protected(root, cfg)
            output = root / "prepared"
            prepare_ml_source(directory, cfg, original, registration, output)
            path = output / "registration.json"
            raw = path.read_text()
            envelope = json.loads(raw)
            envelope["plan"]["protocol"]["fit_months"] = 8
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_prepared_ml_source(output / "development", cfg, path)
            envelope["sha256"] = hashlib.sha256(canonical(envelope["plan"])).hexdigest()
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "protocol/config"):
                load_prepared_ml_source(output / "development", cfg, path)
            path.write_text(raw)
            with self.assertRaisesRegex(ValueError, "protocol/config"):
                load_prepared_ml_source(output / "development", replace(cfg, initial_cash=D(301)), path)
            with self.assertRaisesRegex(ValueError, "bound to"):
                load_prepared_ml_source(directory, cfg, path)

    def test_parent_or_prepared_csv_changes_block_research_before_output(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg)
            original, registration = protected(root, cfg)
            output = root / "prepared"
            prepare_ml_source(directory, cfg, original, registration, output)
            raw = registration.read_text()
            registration.write_text(raw + "\n")
            with self.assertRaisesRegex(ValueError, "parent registration changed"):
                run_ml_study(output / "development", cfg, output / "registration.json", root / "research")
            self.assertFalse((root / "research").exists())
            registration.write_text(raw)
            with (output / "development/candles.csv").open("a") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                run_ml_study(output / "development", cfg, output / "registration.json", root / "research")
            self.assertFalse((root / "research").exists())

    def test_extended_study_registers_before_labels_and_locks_before_validation(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            directory, _ = source(root, "data", cfg)
            original, registration = protected(root, cfg)
            prepared, output = root / "prepared", root / "research"
            prepare_ml_source(directory, cfg, original, registration, prepared)
            boundary = START + timedelta(days=1450)

            def build(data, cfg_arg, spec):
                plan = json.loads((output / "registration.json").read_text())
                self.assertEqual(plan["fit_months"], 24)
                self.assertEqual(plan["calibration_months"], 6)
                self.assertEqual(data.bars["BTC/USD"][-1].end, boundary)
                return []

            def replay(data, cfg_arg, path, **kwargs):
                self.assertLessEqual(data.bars["BTC/USD"][-1].end, boundary)
                if "validation" in path.name:
                    fold = path.name.split("-")[1]
                    self.assertTrue((output / f"fold-{fold}-selection.json").exists())
                return {"net_return_pct": ZERO, "estimated_liquidation_return_pct": ZERO, "max_drawdown_pct": ZERO,
                        "closed_episodes": 0, "profit_factor": None, "net_expectancy_quote": None,
                        "fees_paid_quote_equivalent": ZERO, "start": "test_only", "end": "test_only", "open_positions": []}

            with patch("crypto_trader_v2.ml_study.build_samples", side_effect=build), \
                 patch("crypto_trader_v2.ml_study.run_backtest", side_effect=replay), \
                 patch("crypto_trader_v2.ml_study.read_diagnostics", return_value={"entry_evaluation_reasons": {}}), \
                 patch("crypto_trader_v2.ml_study.export_diagnostics"):
                report = run_ml_study(prepared / "development", cfg, prepared / "registration.json", output)
            self.assertGreater(len(report["folds"]), 1)
            self.assertEqual(report["selected_cash_windows"], len(report["folds"]))
            self.assertFalse(report["approved_for_live"])
            self.assertFalse(report["holdout_evaluated"])

    def test_legacy_window_plan_is_unchanged_and_invalid_windows_fail(self):
        cfg = config()
        timeline = demo_dataset(cfg, 1600).bars["BTC/USD"]
        self.assertEqual(ml_windows(timeline, cfg.warmup, LEGACY_PROTOCOL), chronological_windows(timeline, cfg.warmup))
        for protocol in ({**EXTENDED_PROTOCOL, "fit_months": True},
                         {**EXTENDED_PROTOCOL, "step_months": 1}):
            with self.assertRaisesRegex(ValueError, "protocol"):
                ml_windows(timeline, cfg.warmup, protocol)
        with self.assertRaisesRegex(ValueError, "35 continuous"):
            ml_windows(timeline[:1000], cfg.warmup, EXTENDED_PROTOCOL)

    def test_month_end_partitions_remain_anchored_to_the_original_day(self):
        beginning = datetime(2022, 3, 31, tzinfo=timezone.utc)
        fit_end, cal_end = ml_partition_boundaries(beginning, LEGACY_PROTOCOL)
        self.assertEqual(fit_end, datetime(2022, 11, 30, tzinfo=timezone.utc))
        self.assertEqual(cal_end, datetime(2023, 1, 31, tzinfo=timezone.utc))
        beginning = datetime(2020, 2, 29, tzinfo=timezone.utc)
        fit_end, cal_end = ml_partition_boundaries(beginning, EXTENDED_PROTOCOL)
        self.assertEqual(fit_end, datetime(2022, 2, 28, tzinfo=timezone.utc))
        self.assertEqual(cal_end, datetime(2022, 8, 29, tzinfo=timezone.utc))

    def test_cli_preparation_and_audit_have_no_live_option(self):
        args = parser().parse_args(["prepare-ml", "source", "--protected-dataset", "old",
                                    "--protected-registration", "old.json", "--output", "new"])
        self.assertEqual(args.command, "prepare-ml")
        self.assertEqual(args.protocol, "extended")
        support = parser().parse_args(["prepare-ml", "source", "--protected-dataset", "old",
                                       "--protected-registration", "old.json", "--output", "new", "--protocol", "support"])
        self.assertEqual(support.protocol, "support")
        for command in ("prepare-ml", "audit-dataset"):
            with self.assertRaises(SystemExit) as result, redirect_stdout(io.StringIO()):
                main([command, "--help"])
            self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
