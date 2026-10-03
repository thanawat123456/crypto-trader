"""Offline synthetic fixtures only. These tests are NOT market/profit evidence."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

import numpy as np
import yaml

from crypto_trader_v2.domain import Bar
from crypto_trader_v2.ml_model import load_model
from scripts import global_reference_data as data
from scripts import global_ml_research as ml
from scripts import github_global_research as job


def archive(item, *, columns=None, member=None, duplicate=False, symlink=False):
    month = datetime.fromisoformat(item["month"])
    unit = 1000000 if month.year >= 2025 else 1000
    at = int(month.timestamp()) * unit
    columns = columns or [at, "100", "102", "99", "101", "10", at + data.SECONDS * unit - 1,
                          "1010", "5", "4", "404", "0"]
    text = ",".join(str(x) for x in columns) + "\n"
    if duplicate:
        text *= 2
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zipped:
        info = zipfile.ZipInfo(member or item["name"].removesuffix(".zip") + ".csv")
        if symlink:
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zipped.writestr(info, text)
    payload = raw.getvalue()
    return payload, f"{data.sha(payload)}  {item['name']}\n".encode()


class Response(io.BytesIO):
    status = 200
    headers = {"ETag": '"synthetic-fixture"'}

    def __init__(self, payload, url):
        super().__init__(payload)
        self.url = url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, fail_at=None):
        self.calls, self.fail_at, self.hook = [], fail_at, None
        self.payloads = {}
        for item in data.archive_plan():
            zipped, checksum = archive(item)
            self.payloads[item["url"]] = zipped
            self.payloads[item["url"] + ".CHECKSUM"] = checksum

    def open(self, request, timeout):
        self.calls.append(request)
        if self.hook:
            self.hook()
        if self.fail_at == len(self.calls):
            raise HTTPError(request.full_url, 503, "DO_NOT_EXPORT", {}, None)
        return Response(self.payloads[request.full_url], request.full_url)


def client(opener=None, timer=None):
    transport = data.ArchiveClient(opener=opener or Opener(), monotonic=timer)
    transport.spacing_seconds = 0  # Offline fixture, not HTTP rate override in the job.
    return transport


def candles(count=7500):
    result = {}
    for n, symbol in enumerate(data.SYMBOLS):
        bars = []
        for i in range(count):
            price = Decimal(str(100 + n * 80 + 5 * np.sin(i / 43) + 2 * np.sin(i / 9)))
            bars.append(Bar(symbol, data.START + timedelta(seconds=i * data.SECONDS), data.SECONDS,
                            price, price + 2, price - 2, price, Decimal(10 + i % 17)))
        result[symbol] = bars
    return result


def rows(count, start, index=0):
    rng = np.random.default_rng(513)
    result = []
    for i in range(count):
        at = start + timedelta(days=i)
        feature = tuple(rng.normal(0, .5, len(ml.FEATURES)))
        target = 2.0 if i % 2 else -1.0
        result.append(ml.ForwardSample(str(index + i), data.SYMBOLS[i % 2], at, (index + i) * 6,
                                     feature, at + timedelta(days=7), target + .5, target + .2, target))
    return result


class TempTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()


class ArchiveTests(TempTest):
    def test_fixed_plan_not_native_protected_period(self):
        plan = data.archive_plan()
        self.assertEqual(len(plan), 202)
        self.assertEqual(plan[0]["name"], "BTCUSDT-4h-2018-01.zip")
        self.assertEqual(plan[-1]["name"], "ETHUSDT-4h-2026-05.zip")
        self.assertLess(len(plan) * 2, data.MAX_REQUESTS)

    def test_ms_and_microseconds_and_crc_member_checksums(self):
        for item in (data.archive_plan()[0], data.archive_plan()[-1]):
            payload, checksum = archive(item)
            bars, stats = data.decode_archive(payload, checksum, item)
            self.assertEqual(bars[0].start.isoformat(), item["month"])
            self.assertEqual(stats["rows"], 1)
            self.assertEqual(len(stats["member_sha256"]), 64)

    def test_bad_checksum_or_name_refused(self):
        item = data.archive_plan()[0]
        raw, checksum = archive(item)
        for broken in (b"0" * 64 + b"  " + item["name"].encode(), checksum.replace(b"BTCUSDT", b"ETHUSDT"), b"bad"):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                data.decode_archive(raw, broken, item)

    def test_zip_member_traversal_and_symlinks_refused(self):
        item = data.archive_plan()[0]
        for kwargs in ({"member": "../escape.csv"}, {"member": "wrong.csv"}, {"symlink": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                data.decode_archive(*archive(item, **kwargs), item)

    def test_duplicate_and_wrong_columns_refused(self):
        item = data.archive_plan()[0]
        for kwargs in ({"duplicate": True}, {"columns": [1, 2]}, {"columns": ["header"] * 12}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                data.decode_archive(*archive(item, **kwargs), item)

    def test_bad_grid_close_month_prices_and_quantities_refused(self):
        item = data.archive_plan()[0]
        at = int(data.START.timestamp()) * 1000
        base = [at, "100", "102", "99", "101", "10", at + data.SECONDS * 1000 - 1, "1010", "5", "4", "404", "0"]
        for index, value in ((0, at + 1), (0, at * 1000), (0, at - data.SECONDS * 1000),
                             (6, at + data.SECONDS * 1000), (2, "100"), (5, "-1"), (8, "-1"), (9, "11"), (7, "-1")):
            changed = base.copy()
            changed[index] = value
            with self.subTest(index=index, value=value), self.assertRaises((ValueError, OverflowError)):
                data.decode_archive(*archive(item, columns=changed), item)

    def test_bad_crc_and_unbounded_member_refused(self):
        item = data.archive_plan()[0]
        raw, _ = archive(item)
        broken = raw.replace(b"100,102", b"100,103", 1)
        checksum = f"{data.sha(broken)}  {item['name']}\n".encode()
        with self.assertRaises(zipfile.BadZipFile):
            data.decode_archive(broken, checksum, item)
        with patch.object(data, "MAX_OBJECT", 10), self.assertRaises(ValueError):
            data.decode_archive(*archive(item), item)

    def test_public_get_only_and_url_allowlist(self):
        transport = client()
        transport.fetch(data.archive_plan()[0]["url"])
        request = transport.opener.calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertFalse(any("authorization" in k.lower() for k in request.headers))
        for url in ("https://api.binance.com/api/v3/account", data.archive_plan()[0]["url"] + "?token=x", "http://data.binance.vision/"):
            with self.assertRaises(ValueError):
                transport.fetch(url)
        self.assertEqual(transport.attempts, 1)

    def test_no_redirect(self):
        with self.assertRaises(ValueError):
            data.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_request_bytes_time_budgets(self):
        for key, value in (("max_requests", 0), ("max_bytes", 0)):
            transport = client()
            setattr(transport, key, value)
            with self.assertRaises(ValueError):
                transport.fetch(data.archive_plan()[0]["url"])
            self.assertEqual(len(transport.opener.calls), 0)
        transport = client(timer=iter([0, 901]).__next__)
        with self.assertRaises(ValueError):
            transport.fetch(data.archive_plan()[0]["url"])
        transport = client()
        transport.max_bytes = 10
        with self.assertRaises(ValueError):
            transport.fetch(data.archive_plan()[0]["url"])
        self.assertEqual(transport.attempts, 1)

    def test_capture_registration_audit_and_tampering(self):
        output = self.root / "capture"
        transport = client()
        transport.opener.hook = lambda: self.assertTrue((output / "registration.json").exists())
        manifest = data.capture_reference(output, client=transport)
        audit, selected = data.audit_reference(output)
        self.assertEqual(audit["requests_verified"], 404)
        self.assertEqual(selected[data.SYMBOLS[0]][0].start, data.START)
        self.assertFalse(manifest["approved_for_live"])
        with self.assertRaises(FileExistsError):
            data.capture_reference(output, client=transport)
        first = output / "raw" / data.archive_plan()[0]["name"]
        first.write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            data.audit_reference(output)

    def test_partial_raw_preserved_no_retry_no_error_string(self):
        transport = client(Opener(fail_at=2))
        output = self.root / "capture"
        with self.assertRaises(HTTPError):
            data.capture_reference(output, client=transport)
        failure = json.loads((output / "failure.json").read_text())
        self.assertEqual(failure["successful_receipts"], 1)
        self.assertEqual(failure["requests_attempted"], 2)
        self.assertEqual(len(list((output / "raw").iterdir())), 1)
        self.assertNotIn("DO_NOT_EXPORT", (output / "failure.json").read_text())

    def test_coverage_longest_run_earliest_tie_no_gap_filling(self):
        grouped = candles(8)
        grouped = {s: [bars[i] for i in (0, 1, 3, 4, 6)] for s, bars in grouped.items()}
        summary, selected = data.coverage(grouped)
        self.assertEqual(summary["common_runs"], 3)
        self.assertEqual(summary["selected_bars_per_symbol"], 2)
        self.assertEqual(selected[data.SYMBOLS[0]][0].start, data.START)
        self.assertEqual(len(summary["quality"][data.SYMBOLS[0]]["gaps"]), 2)

    def test_coverage_duplicate_refused(self):
        grouped = candles(2)
        grouped[data.SYMBOLS[0]].append(grouped[data.SYMBOLS[0]][-1])
        with self.assertRaises(ValueError):
            data.coverage(grouped)


class ForecastTests(TempTest):
    def setUp(self):
        super().setUp()
        self.fit = rows(100, data.START)
        self.cal = rows(40, data.START + timedelta(days=110), index=200)
        self.available = data.START + timedelta(days=160)

    def fit_model(self, variant="linear"):
        return ml.fit_reference(self.fit, self.cal, self.available, variant)

    def test_causal_cross_asset_features_unaffected_by_future(self):
        grouped = candles(340)
        before = ml.feature_vectors(grouped)
        changed = {}
        for s, bars in grouped.items():
            changed[s] = [replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2,
                                  volume=b.volume * 10) if i >= 300 else b for i, b in enumerate(bars)]
        after = ml.feature_vectors(changed)
        for s in data.SYMBOLS:
            self.assertEqual({i: v for i, v in before[s].items() if i < 300}, {i: v for i, v in after[s].items() if i < 300})
            self.assertNotEqual(before[s][335], after[s][335])

    def test_cross_asset_alignment_gaps_and_wrong_timeframe_fail(self):
        for corruption in ("misaligned", "gap", "timeframe"):
            grouped = candles(240)
            if corruption == "misaligned":
                grouped[data.SYMBOLS[0]] = grouped[data.SYMBOLS[0]][1:]
            elif corruption == "gap":
                grouped = {s: bars[:215] + bars[216:] for s, bars in grouped.items()}
            else:
                grouped[data.SYMBOLS[0]][-1] = replace(grouped[data.SYMBOLS[0]][-1], seconds=3600)
            with self.subTest(corruption=corruption), self.assertRaises(ValueError):
                ml.feature_vectors(grouped)

    def test_label_horizon_next_open_and_censoring(self):
        grouped = candles(340)
        samples = ml.build_samples(grouped)
        first = samples[0]
        bars = grouped[first.symbol]
        self.assertEqual(first.at, bars[first.entry_index].start)
        self.assertEqual(first.label_end, first.at + timedelta(days=7))
        self.assertAlmostEqual(first.gross_return_pct, float((bars[first.entry_index + 42].open / bars[first.entry_index].open - 1) * 100))
        self.assertGreater(first.base_return_pct, first.stress_return_pct)
        self.assertIsNone(samples[-1].stress_return_pct)

    def test_global_boundary_purge_and_embargo(self):
        end = data.START + timedelta(days=20)
        all_rows = rows(20, data.START)
        kept, stats = ml.partition(all_rows, data.START, end)
        self.assertEqual(len(kept), 13)
        self.assertEqual(stats["purged_or_censored"], 7)
        self.assertTrue(all(s.label_end + timedelta(seconds=data.SECONDS) <= end for s in kept))

    def test_global_overlap_weights_share_correlated_assets(self):
        sample = self.fit[0]
        twin = replace(sample, id="twin", symbol=data.SYMBOLS[1])
        w = ml.weights([sample, twin, self.fit[-1]])
        self.assertAlmostEqual(w[0], w[1])
        self.assertLess(w[0], w[2])
        self.assertAlmostEqual(sum(w), 1)
        with self.assertRaises(ValueError):
            ml.weights([])

    def test_models_fit_calibrate_and_never_enable_execution(self):
        for variant in ml.VARIANTS:
            model = self.fit_model(variant)
            self.assertTrue(model["fitted"])
            self.assertTrue(model["predictive_ready"])
            self.assertFalse(model["approved_for_live"])
            self.assertIsNone(model["execution_adapter"])
            self.assertFalse(model["license"]["live_execution_allowed"])
            self.assertAlmostEqual(model["fit_stress_return_mean_pct"], float(ml.weights(self.fit) @ np.asarray([s.stress_return_pct for s in self.fit])))

    def test_future_calibration_labels_and_duplicates_fail(self):
        for fit, cal in ((self.fit, self.cal + [self.fit[0]]), (self.fit, [replace(self.cal[0], label_end=self.available)]),
                         (self.fit + [self.fit[0]], self.cal), (self.fit, rows(40, data.START + timedelta(days=90), index=200))):
            with self.subTest(), self.assertRaises(ValueError):
                ml.fit_reference(fit, cal, self.available, "linear")

    def test_insufficient_support_or_one_class_not_ready(self):
        model = ml.fit_reference(self.fit[:10], self.cal, self.available, "linear")
        self.assertFalse(model["fitted"])
        self.assertIn("fit_sample_count", model["readiness_reasons"])
        model = ml.fit_reference(self.fit, [replace(s, stress_return_pct=2.) for s in self.cal], self.available, "linear")
        self.assertTrue(model["fitted"])
        self.assertFalse(model["predictive_ready"])
        self.assertIn("calibration_class_support", model["readiness_reasons"])

    def test_calibration_ood_blocks_readiness(self):
        outside = [replace(s, features=(100.,) * len(ml.FEATURES)) for s in self.cal]
        model = ml.fit_reference(self.fit, outside, self.available, "linear")
        self.assertFalse(model["predictive_ready"])
        self.assertIn("calibration_domain_support", model["readiness_reasons"])

    def test_fit_only_scaler_priors_and_validation_mean_not_used(self):
        model = self.fit_model()
        x = np.asarray([s.features for s in self.fit])
        np.testing.assert_allclose(model["mean"], ml.weights(self.fit) @ x)
        validation = [replace(s, at=self.available + timedelta(days=i), stress_return_pct=100.) for i, s in enumerate(self.cal)]
        metric = ml.forecast_metrics(validation, model)
        self.assertAlmostEqual(metric["constant_return_mae_pct"], abs(100 - model["fit_stress_return_mean_pct"]))
        self.assertGreater(metric["constant_return_mae_pct"], 90)

    def test_predictions_do_not_use_outcomes(self):
        model = self.fit_model()
        reference = rows(10, self.available, index=400)
        altered = [replace(s, stress_return_pct=999., gross_return_pct=-999.) for s in reference]
        self.assertEqual(ml.predictions(reference, model), ml.predictions(altered, model))

    def test_gate_expiry_time_and_ood(self):
        model = self.fit_model()
        # Force a permissive offline prediction to test the time/domain guards.
        model["logistic"] = [0.] * len(model["logistic"])
        model["calibration"] = [10., 0.]
        model["ridge"] = [5.] + [0.] * (len(model["ridge"]) - 1)
        base = replace(self.fit[0], at=self.available)
        self.assertTrue(ml.predictions([base], model)[0]["reference_gate_pass"])
        for row in (replace(base, at=self.available - timedelta(seconds=1)), replace(base, at=model["expires_at"]),
                    replace(base, features=(100.,) * len(ml.FEATURES))):
            self.assertFalse(ml.predictions([row], model)[0]["reference_gate_pass"])

    def test_constant_feature_shift_is_ood(self):
        matrix, outside = ml.design(np.asarray([[1., 2.]]), np.asarray([0., 2.]), np.asarray([1., 1.]), [True, False])
        self.assertTrue(outside[0])
        self.assertEqual(matrix.shape, (1, 3))

    def test_unregistered_variant_and_invalid_features_fail(self):
        with self.assertRaises(ValueError):
            self.fit_model("neural-unregistered")
        for value in ([0.] * 12, [float("nan")] * 18):
            with self.assertRaises(ValueError):
                ml.mapped([value], "linear")

    def test_native_model_loader_cannot_load_reference_model(self):
        model = self.fit_model()
        target = self.root / "model.json"
        data.write_new(target, {"model": model, "sha256": data.sha(data.canonical(model))})
        with self.assertRaises((ValueError, TypeError, KeyError)):
            load_model(target)

    def test_fixed_fold_budget_and_insufficient_history(self):
        self.assertEqual(len(ml.fold_windows(data.START, data.END)), 31)
        with self.assertRaises(ValueError):
            ml.fold_windows(data.START, data.START + timedelta(days=365))
        with self.assertRaises(ValueError):
            ml.fold_windows(data.START, datetime(2030, 1, 1, tzinfo=timezone.utc))

    def test_full_benchmark_read_only_audit_and_mutation_detection(self):
        grouped = candles()
        output = self.root / "research"
        report = ml.run_research(grouped, output)
        before = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
        with patch.object(ml, "fit_reference", side_effect=AssertionError("Audit must not fit")):
            audit = ml.audit_research(grouped, output)
        self.assertEqual(audit["models_fitted"], report["models_fitted"])
        self.assertFalse(audit["refitted"])
        self.assertEqual(before, {p: p.read_bytes() for p in output.rglob("*") if p.is_file()})
        boundary = ml.fold_windows(grouped[data.SYMBOLS[0]][0].start, grouped[data.SYMBOLS[0]][-1].end)[0][3]
        changed = {s: [replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2,
                             volume=b.volume * 3) if b.start >= boundary else b for b in bars]
                   for s, bars in grouped.items()}
        alternative = self.root / "changed-validation"
        ml.run_research(changed, alternative)
        for filename in ("selection.json", "linear-model.json", "interactions-model.json"):
            self.assertEqual((output / "fold-01" / filename).read_bytes(), (alternative / "fold-01" / filename).read_bytes())
        selection = output / "fold-01" / "selection.json"
        raw = json.loads(selection.read_text())
        raw["selected_reference_candidate"] = "not-registered"
        selection.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            ml.audit_research(grouped, output)

    def test_training_time_budget_refused(self):
        grouped = candles()
        with self.assertRaisesRegex(ValueError, "deadline"):
            ml.run_research(grouped, self.root / "research", monotonic=iter([0, 601]).__next__)


class WrapperTests(TempTest):
    def setUp(self):
        super().setUp()
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": job.REPOSITORY,
                    "GITHUB_REF": job.REF, "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123",
                    "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "push", "GITHUB_TOKEN": "DO_NOT_EXPORT"}
        self.transport = client(Opener(fail_at=2))
        self.output = self.root / "new"

    def invoke(self, **kwargs):
        return job.run(self.output, environ=self.env, clock=lambda: datetime(2026, 10, 3, tzinfo=timezone.utc),
                       client=self.transport, **kwargs)

    def test_fixed_source_and_spec_binding(self):
        bound = job.bindings()
        self.assertEqual(bound["core_sha256"], job.EXPECTED_CORE)
        self.assertEqual(bound["specification_sha256"], job.EXPECTED_SPEC)
        self.assertEqual(len(bound["scripts_sha256"]), 3)

    def test_registration_precedes_http_and_failed_raw_preserved(self):
        self.transport.opener.hook = lambda: self.assertTrue((self.output / "registration.json").exists())
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "capture")
        self.assertEqual(result["requests_attempted"], 2)
        self.assertEqual(result["http_status"], 503)
        self.assertTrue((self.output / "inventory.json").exists())
        self.assertTrue((self.output / "DATA_LICENSE.md").exists())
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"DO_NOT_EXPORT", file.read_bytes())
        with self.assertRaises(ValueError):
            job.audit_output(self.output)

    def test_bad_context_refused_before_http_or_output(self):
        for key, value in (("GITHUB_ACTIONS", "false"), ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_REF", "refs/heads/main"),
                           ("GITHUB_SHA", "bad"), ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ATTEMPT", "2"),
                           ("GITHUB_EVENT_NAME", "workflow_dispatch")):
            with self.subTest(key=key), patch.dict(self.env, {key: value}), self.assertRaises(ValueError):
                self.invoke()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.transport.attempts, 0)

    def test_changed_parameters_core_budget_refused_before_http(self):
        for scope in (patch.dict(ml.PARAMETERS, {"probability_threshold": .1}), patch.object(job, "code_hash", return_value="bad"),
                      patch.object(self.transport, "max_requests", 999)):
            with scope, self.assertRaises(ValueError):
                self.invoke()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.transport.attempts, 0)

    def test_expired_or_naive_clock(self):
        for at in (job.DEADLINE, datetime(2026, 10, 3)):
            with self.assertRaises(ValueError):
                job.run(self.output, environ=self.env, client=self.transport, clock=lambda: at)
        self.assertEqual(self.transport.attempts, 0)

    def test_existing_output_never_reused(self):
        self.invoke()
        old = {p: p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(old, {p: p.read_bytes() for p in self.output.rglob("*") if p.is_file()})

    def test_symlink_output_refused(self):
        target = self.root / "target"
        target.mkdir()
        self.output.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual(self.transport.attempts, 0)

    def test_insufficient_continuous_history_never_claims_training_success(self):
        self.transport.opener.fail_at = None
        result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_stage"], "training")
        self.assertEqual(result["requests_attempted"], 404)
        self.assertEqual(result["models_fitted"], 0)
        self.assertTrue((self.output / "data-audit.json").exists())

    def test_successful_wrapper_inventory_audit_and_tampering_offline(self):
        grouped = candles()
        audit = {"requests_verified": 404, "csv_sha256": "offline-fixture", "selected_bars_per_symbol": 7500}
        with patch.object(data, "capture_reference"), patch.object(data, "audit_reference", return_value=(audit, grouped)):
            result = self.invoke()
            self.assertEqual(result["status"], "verified_research")
            self.assertFalse(result["portfolio_profit_evidence"])
            checked = job.audit_output(self.output)
            self.assertFalse(checked["research"]["refitted"])
            self.assertFalse(checked["approved_for_live"])
            (self.output / "DATA_LICENSE.md").write_text("changed")
            with self.assertRaisesRegex(ValueError, "inventory"):
                job.audit_output(self.output)

    def test_failed_training_retains_partial_model_counts_not_false_zero(self):
        def failure(grouped, output, **kwargs):
            directory = output / "fold-01"
            directory.mkdir(parents=True)
            data.write_new(directory / "linear-model.json", {"model": {"fitted": True}})
            raise ValueError("DO_NOT_EXPORT")
        with patch.object(data, "capture_reference"), patch.object(data, "audit_reference", return_value=({}, {})), patch.object(ml, "run_research", side_effect=failure):
            result = self.invoke()
        self.assertEqual(result["failed_stage"], "training")
        self.assertEqual(result["model_files_preserved"], 1)
        self.assertEqual(result["models_fitted"], 1)
        self.assertNotIn("DO_NOT_EXPORT", (self.output / "result.json").read_text())

    def test_workflow_safety_and_pinned_actions(self):
        raw = Path(".github/workflows/v2-global-ml.yml").read_text()
        workflow = yaml.safe_load(raw)
        trigger = workflow.get("on", workflow.get(True))
        self.assertEqual(set(trigger), {"push"})
        self.assertEqual(trigger["push"]["branches"], [job.REF.removeprefix("refs/heads/")])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", raw)
        self.assertNotIn("v2_data/", raw)
        self.assertNotIn("requirements.txt", raw)
        self.assertFalse(workflow["concurrency"]["cancel-in-progress"])
        for entry in workflow["jobs"].values():
            self.assertEqual(entry["runs-on"], "ubuntu-latest")
            self.assertLessEqual(entry["timeout-minutes"], 25)
            for step in entry["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")
        artifact = workflow["jobs"]["global-research"]["steps"][-1]
        self.assertEqual(artifact["with"]["retention-days"], 3)
        self.assertEqual(artifact["if"], "always()")


if __name__ == "__main__":
    unittest.main()
