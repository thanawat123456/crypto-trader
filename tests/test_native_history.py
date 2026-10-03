from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.binance_th import BASE_URL, BinanceTHPublicFeed, capture_binance_th
from crypto_trader_v2.config import StrategyConfig, load_config
from crypto_trader_v2.importer import load_dataset
from crypto_trader_v2.native_history import (HISTORY_START, SOURCE_SCOPE, BinanceTHHistoryFeed,
                                           _canonical, capture_native_history, verify_native_history)


NOW = datetime(2024, 1, 22, tzinfo=timezone.utc)


class Response:
    def __init__(self, url, value):
        self.url, self.raw = url, json.dumps(value).encode()

    def geturl(self):
        return self.url

    def read(self, n):
        return self.raw[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, cfg):
        self.cfg, self.calls, self.output, self.fail_at, self.missing = cfg, [], None, None, False

    def open(self, request, timeout):
        if self.output is not None:
            assert (self.output / "registration.json").exists(), "Network before registration"
            assert not (self.output / "report.json").exists(), "Complete report before collection"
        self.calls.append(request)
        if self.fail_at == len(self.calls):
            raise HTTPError(BASE_URL, 429, "fixture rate limit", {"Retry-After": "120"}, None)
        url = urlsplit(request.full_url)
        params = parse_qs(url.query)
        if url.path.endswith("exchangeInfo"):
            value = {"symbols": [{"symbol": i.symbol.replace("/", ""), "baseAsset": i.symbol.split("/")[0],
                                  "quoteAsset": "USDT", "status": "TRADING", "type": "GLOBAL", "orderTypes": ["MARKET"],
                                  "filters": [{"filterType": "LOT_SIZE", "minQty": str(i.min_quantity),
                                               "maxQty": "9000", "stepSize": str(i.quantity_step)},
                                              {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True}]}
                                 for i in self.cfg.instruments]}
        elif url.path.endswith("time"):
            value = {"serverTime": int(NOW.timestamp()) * 1000}
        elif url.path.endswith("klines"):
            begin = int(params["startTime"][0])
            value = [[begin+j*self.cfg.seconds*1000, "100", "100", "100", "100", "1",
                      begin+(j+1)*self.cfg.seconds*1000-1, "100", 1, "1", "100", "0"]
                     for j in range(int(params["limit"][0]))]
            if self.missing:
                value = value[1:]
        elif url.path.endswith("ticker/bookTicker"):
            value = {"symbol": params["symbol"][0], "bidPrice": "100", "askPrice": "100", "bidQty": "1", "askQty": "1"}
        else:
            raise AssertionError("Unexpected/private endpoint")
        return Response(request.full_url, value)


class NativeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = replace(load_config("config.binance-th.paper.yaml"), strategy=StrategyConfig(3, 1, 3, 2, 2))
        self.root = Path(self.temp.name)
        self.protected = self.root / "protected"
        self.output = self.root / "history"
        self.sleep = patch("crypto_trader_v2.binance_th.time.sleep").start()
        self.addCleanup(patch.stopall)
        capture_binance_th(self.cfg, self.protected, history_bars=12,
                           feed=BinanceTHPublicFeed(12, opener=Opener(self.cfg), clock=lambda: NOW))
        self.opener = Opener(self.cfg)
        self.opener.output = self.output
        self.feed = BinanceTHHistoryFeed(opener=self.opener, clock=lambda: NOW)

    def capture(self):
        return capture_native_history(self.cfg, self.protected, self.output, feed=self.feed)

    def test_registered_public_history_preserves_rows_and_physically_excludes_suffix(self):
        result = self.capture()
        self.assertEqual(result["status"], "complete", result.get("error"))
        self.assertEqual(result["bars_per_symbol"], {i.symbol: 30 for i in self.cfg.instruments})
        self.assertEqual(result["development_bars_per_symbol"], {i.symbol: 18 for i in self.cfg.instruments})
        self.assertEqual(result["source_scope"], SOURCE_SCOPE)
        self.assertEqual(result["models_trained"], 0)
        self.assertFalse(result["ml_enabled"])
        self.assertFalse(result["approved_for_live"])
        self.assertFalse(result["worker_running"])
        for protocol in result["coverage"].values():
            self.assertEqual(protocol["complete_permitted_development_windows"], 0)
            self.assertFalse(protocol["selection_performed"])
        full, dev = load_dataset(self.output, self.cfg), load_dataset(self.output / "development", self.cfg)
        for symbol in full.bars:
            self.assertEqual(full.bars[symbol][0].start, HISTORY_START)
            self.assertEqual(dev.bars[symbol], full.bars[symbol][:18])
        for request in self.opener.calls:
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn("X-mbx-apikey", request.headers)
            self.assertNotIn("bookTicker", request.full_url)
        before = {path: path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        audit = verify_native_history(self.output, self.cfg)
        self.assertTrue(audit["read_only"])
        self.assertTrue(audit["current_package_matches_registration"])
        self.assertEqual(before, {path: path.read_bytes() for path in self.output.rglob("*") if path.is_file()})

    def test_rate_limit_stops_without_retry_retains_registration_raw_and_failure(self):
        self.opener.fail_at = 3
        result = self.capture()
        self.assertEqual(result["status"], "failed")
        self.assertIn("429", result["error"])
        self.assertEqual(len(self.opener.calls), 3)
        self.assertEqual(result["requests_completed"], 2)
        self.assertEqual(len(list((self.output / "raw").glob("*.json"))), 2)
        self.assertTrue((self.output / "registration.json").exists())
        self.assertTrue((self.output / "receipts.json").exists())
        self.assertFalse((self.output / "candles.csv").exists())
        self.assertFalse((self.output / "manifest.json").exists())

    def test_missing_chunk_is_failure_never_fabricates_or_skips_to_next_symbol(self):
        self.opener.missing = True
        result = self.capture()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Incomplete historical chunk", result["error"])
        self.assertEqual(result["requests_completed"], 3)
        self.assertFalse((self.output / "candles.csv").exists())

    def test_existing_output_is_preserved_before_any_network(self):
        self.capture()
        original = (self.output / "report.json").read_bytes()
        self.opener.calls.clear()
        with self.assertRaisesRegex(ValueError, "output exists"):
            self.capture()
        self.assertFalse(self.opener.calls)
        self.assertEqual((self.output / "report.json").read_bytes(), original)

    def test_history_reader_rejects_quotes_orders_credentials_and_live_snapshots(self):
        for endpoint, params in (("ticker/bookTicker", {}), ("order", {}), ("account", {}), ("time", {"signature": "fixture"})):
            with self.assertRaisesRegex(ValueError, "allowlist"):
                self.feed.request(endpoint, params)
        with self.assertRaisesRegex(ValueError, "History-only"):
            self.feed.snapshot(self.cfg)
        self.assertFalse(self.opener.calls)
        self.assertEqual(BinanceTHPublicFeed.max_elapsed_seconds, 120)
        self.assertEqual(BinanceTHPublicFeed.max_latency_seconds, 5)
        self.assertEqual(BinanceTHHistoryFeed.max_elapsed_seconds, 600)

    def test_prelaunch_protected_boundary_or_future_capture_is_rejected_before_network(self):
        self.feed.clock = lambda: NOW - timedelta(days=1)
        with self.assertRaisesRegex(ValueError, "closed history bounds"):
            self.capture()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.opener.calls)
        self.feed.clock = lambda: NOW
        earlier = self.root / "prelaunch"
        capture_binance_th(self.cfg, earlier, history_bars=100,
                           feed=BinanceTHPublicFeed(100, opener=Opener(self.cfg), clock=lambda: NOW))
        with self.assertRaisesRegex(ValueError, "post-launch"):
            capture_native_history(self.cfg, earlier, self.output, feed=self.feed)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.opener.calls)

    def test_wrong_venue_or_snapshot_reader_cannot_enter_history_collection(self):
        with self.assertRaisesRegex(ValueError, "history-only"):
            capture_native_history(self.cfg, self.protected, self.output, feed=BinanceTHPublicFeed())
        with self.assertRaisesRegex(ValueError, "Binance TH"):
            capture_native_history(load_config("config.v2.example.yaml"), self.protected, self.output, feed=self.feed)
        self.assertFalse(self.output.exists())

    def test_package_change_during_capture_prevents_complete_dataset(self):
        with patch("crypto_trader_v2.native_history._code_hash", side_effect=["before", "after"]):
            result = self.capture()
        self.assertEqual(result["status"], "failed")
        self.assertIn("Package changed", result["error"])
        self.assertFalse((self.output / "manifest.json").exists())

    def test_raw_checksum_tampering_is_detected_without_rewriting(self):
        self.capture()
        raw = self.output / "raw/0002.json"
        raw.write_bytes(raw.read_bytes() + b" ")
        before = raw.read_bytes()
        with self.assertRaisesRegex(ValueError, "raw checksum"):
            verify_native_history(self.output, self.cfg)
        self.assertEqual(before, raw.read_bytes())

    def test_manifest_quality_scope_and_development_flags_cannot_be_relabelled(self):
        self.capture()
        path = self.output / "development/manifest.json"
        payload = json.loads(path.read_text())
        payload["source_scope"] = "verified TH fills"
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "source/safety"):
            verify_native_history(self.output, self.cfg)

    def test_excluded_prices_cannot_be_appended_to_development_even_with_new_hashes(self):
        self.capture()
        path = self.output / "development/candles.csv"
        full = (self.output / "candles.csv").read_text().splitlines()
        boundary = NOW - timedelta(seconds=11*self.cfg.seconds)
        rows = [row for row in full[1:] if datetime.fromisoformat(row.split(",")[0]) < boundary]
        path.write_text("\n".join([full[0], *rows]) + "\n")
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        dev_path = self.output / "development/manifest.json"
        development = json.loads(dev_path.read_text())
        development["csv_sha256"] = checksum
        development["end"] = boundary.isoformat()
        for quality in development["quality"].values():
            quality["bars"] = 19
        dev_path.write_text(json.dumps(development))
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["development_csv_sha256"] = checksum
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "development includes excluded"):
            verify_native_history(self.output, self.cfg)

    def test_protocol_cannot_be_shortened_by_rehashing_registration(self):
        self.capture()
        path = self.output / "registration.json"
        envelope = json.loads(path.read_text())
        envelope["plan"]["protocols"]["extended"]["months"]["fit_months"] = 12
        envelope["sha256"] = hashlib.sha256(_canonical(envelope["plan"])).hexdigest()
        path.write_text(json.dumps(envelope))
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["registration_sha256"] = envelope["sha256"]
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "protocol binding"):
            verify_native_history(self.output, self.cfg)

    def test_request_elapsed_byte_and_latency_budgets_fail_without_retry(self):
        self.opener.output = None
        self.feed.requests = [{}] * BinanceTHHistoryFeed.max_requests
        with self.assertRaisesRegex(ValueError, "request budget"):
            self.feed.request("time", {})
        self.assertFalse(self.opener.calls)
        self.feed.requests = [{"requested_at": (NOW-timedelta(seconds=601)).isoformat()}]
        with self.assertRaisesRegex(ValueError, "elapsed-time"):
            self.feed.request("time", {})
        self.assertFalse(self.opener.calls)
        self.feed.reset_request_budget()
        self.feed.bytes_downloaded = BinanceTHHistoryFeed.max_bytes
        with self.assertRaisesRegex(ValueError, "byte budget"):
            self.feed.request("time", {})
        self.assertEqual(len(self.opener.calls), 1)
        self.feed.reset_request_budget()
        self.feed.clock = unittest.mock.Mock(side_effect=[NOW, NOW+timedelta(seconds=16)])
        with self.assertRaisesRegex(ValueError, "latency"):
            self.feed.request("time", {})
        self.assertEqual(len(self.opener.calls), 2)

    def test_changed_protected_source_blocks_audit(self):
        self.capture()
        path = self.protected / "manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "Protected capture manifest changed"):
            verify_native_history(self.output, self.cfg)

    def test_raw_request_boundaries_and_receipt_time_are_audited(self):
        self.capture()
        path = self.output / "manifest.json"
        original = json.loads(path.read_text())
        mutated = json.loads(json.dumps(original))
        mutated["receipts"][2]["params"]["startTime"] += 1
        path.write_text(json.dumps(mutated))
        with self.assertRaisesRegex(ValueError, "request boundary"):
            verify_native_history(self.output, self.cfg)
        original["receipts"][2]["received_at"] = (NOW + timedelta(seconds=16)).isoformat()
        path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError, "chronology/latency"):
            verify_native_history(self.output, self.cfg)

    def test_cli_failed_capture_returns_nonzero_and_ml_stays_locked(self):
        with patch("crypto_trader_v2.__main__.load_config", return_value=self.cfg), \
                patch("crypto_trader_v2.__main__.capture_native_history", return_value={"status": "failed", "models_trained": 0}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["capture-binance-th-history", "--protected-capture", str(self.protected), "--output", str(self.output)]), 2)
            self.assertEqual(main(["ml-research", str(self.output), "--registration", "unused", "--output", "unused"]), 1)


if __name__ == "__main__":
    unittest.main()
