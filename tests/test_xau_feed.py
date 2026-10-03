from datetime import datetime, timezone
from decimal import Decimal as D
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from crypto_trader_v2.xau_feed import ROOT, decode_hourly, download_xau_reference


START = datetime(2022, 1, 1, tzinfo=timezone.utc)
END = datetime(2022, 2, 1, tzinfo=timezone.utc)


def payload(side="BID"):
    offset = 0.2 if side == "ASK" else 0
    return {"timestamp": int(START.timestamp()) * 1000, "shift": 3600000, "multiplier": 0.001,
            "open": 2000 + offset, "high": 2001 + offset, "low": 1999 + offset, "close": 2000 + offset,
            "times": [0, 1, 5], "opens": [0, 10, 20], "highs": [0, 10, 20],
            "lows": [0, 10, 20], "closes": [0, 10, 20], "volumes": [1, 2, 3]}


class Response:
    status = 200

    def __init__(self, url, raw):
        self.url, self.raw = url, raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, count):
        return self.raw[:count]


class GoldDecodeTests(unittest.TestCase):
    def test_delta_prices_are_exact_and_missing_hours_are_not_filled(self):
        rows = decode_hourly(json.dumps(payload()))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["timestamp"], "2022-01-01T06:00:00+00:00")
        self.assertEqual(rows[-1]["open"], D("2000.030"))
        self.assertEqual(rows[-1]["high"], D("2001.030"))
        self.assertEqual(rows[1]["volume"], D(2))

    def test_invalid_units_lengths_time_order_nan_and_ohlc_fail(self):
        cases = [{"shift": 60}, {"timestamp": True}, {"times": [0, 0, 1]}, {"times": [0, 1]},
                 {"opens": [0, 0.1, 1]}, {"multiplier": 0}, {"highs": [0, -10000, 0]},
                 {"volumes": [1, -1, 0]}, {"open": float("nan")}, {"times": []}]
        for changes in cases:
            with self.assertRaises((ValueError, ArithmeticError)):
                decode_hourly(json.dumps(payload() | changes))


class GoldDownloadTests(unittest.TestCase):
    def test_plan_and_native_raw_checkpoints_precede_downstream_quote_audit(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "gold"
            calls = []

            def fetch(request, timeout):
                self.assertTrue((output / "registration.json").exists())
                calls.append(request.full_url)
                side = "ASK" if "/ASK/" in request.full_url else "BID"
                return Response(request.full_url, json.dumps(payload(side)).encode())

            with patch("crypto_trader_v2.xau_feed.urlopen", side_effect=fetch), patch("crypto_trader_v2.xau_feed.time.sleep"):
                result = download_xau_reference(START, END, output)
            self.assertEqual(calls, [ROOT + "/BID/2022/1", ROOT + "/ASK/2022/1"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["requests_completed"], 2)
            self.assertEqual(result["reference_import"]["rows"], 3)
            self.assertEqual(result["reference_import"]["gap_count"], 1)
            self.assertFalse(result["reference_import"]["strategy_replay_ready"])
            self.assertEqual(len(list((output / "raw").glob("*.json"))), 2)
            self.assertFalse(result["approved_for_live"])
            with self.assertRaisesRegex(ValueError, "exists"):
                download_xau_reference(START, END, output)

    def test_http_failure_preserves_failure_evidence_without_retry_or_bypass(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "gold"
            with patch("crypto_trader_v2.xau_feed.urlopen", side_effect=OSError("HTTP 429")) as fetch:
                result = download_xau_reference(START, END, output)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["requests_completed"], 0)
            self.assertFalse((output / "reference").exists())
            self.assertTrue((output / "report.json").exists())

    def test_oversized_response_or_redirect_is_refused(self):
        with TemporaryDirectory() as folder:
            for name, raw, destination in (("oversize", b"x" * 20, None), ("redirect", b"{}", "https://example.org")):
                output = Path(folder) / name
                with patch("crypto_trader_v2.xau_feed.urlopen", side_effect=lambda req, timeout: Response(destination or req.full_url, raw)):
                    result = download_xau_reference(START, END, output, byte_budget=10)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["requests_completed"], 0)

    def test_partial_active_month_or_unbounded_budget_refused_before_requests(self):
        with TemporaryDirectory() as folder:
            for begin, end, budget in ((START.replace(day=2), END, 1024),
                                       (START, datetime(2099, 1, 1, tzinfo=timezone.utc), 1024),
                                       (START, END, 0), (START, END, 40 * 1024 * 1024)):
                with self.assertRaises(ValueError), patch("crypto_trader_v2.xau_feed.urlopen") as fetch:
                    download_xau_reference(begin, end, Path(folder) / "gold", byte_budget=budget)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
