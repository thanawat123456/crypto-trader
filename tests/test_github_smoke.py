from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml

from crypto_trader_v2.config import load_config
from scripts import github_v2_smoke as smoke
from tests.test_binance_th import FakeOpener, NOW


class GitHubSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_bytes(Path("config.binance-th.paper.yaml").read_bytes())
        self.cfg = load_config(str(self.config_path))
        self.output = self.root / "new-smoke"
        self.client = smoke.SmokeFeed(212, opener=FakeOpener(self.cfg), clock=lambda: NOW)
        self.client.request_spacing_seconds = 0  # Offline fixture only.
        self.env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": smoke.REPOSITORY,
                    "GITHUB_REF": smoke.TEST_REF, "GITHUB_SHA": "a" * 40,
                    "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_EVENT_NAME": "push", "GITHUB_TOKEN": "DO_NOT_EXPORT",
                    "EXCHANGE_API_KEY": "DO_NOT_EXPORT"}

    def invoke(self, **kwargs):
        return smoke.run_smoke(self.config_path, self.output, feed=self.client,
                               clock=lambda: NOW, environ=self.env, **kwargs)

    def test_public_read_is_registered_first_and_audited_without_a_portfolio(self):
        opened = self.client.opener.open

        def check_reservation(*args, **kwargs):
            self.assertTrue((self.output / "registration.json").exists())
            return opened(*args, **kwargs)

        with patch.object(self.client.opener, "open", side_effect=check_reservation):
            result = self.invoke()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["requests_verified"], 16)
        self.assertEqual(result["bars_per_symbol"], {"BTC/USDT": 212, "ETH/USDT": 212})
        self.assertTrue(result["context"]["github_verified"])
        self.assertEqual(result["models_trained"], 0)
        for flag in ("portfolio_opened", "local_worker_migrated", "approved_for_live"):
            self.assertIs(result[flag], False)
        self.assertFalse(list(self.output.rglob("*.sqlite")))
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"DO_NOT_EXPORT", file.read_bytes())
        self.assertTrue((self.output / "audit.json").exists())

    def test_existing_output_is_never_overwritten_or_retried(self):
        self.invoke()
        before = {p: p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        calls = len(self.client.opener.calls)
        with self.assertRaises(FileExistsError):
            self.invoke()
        self.assertEqual(calls, len(self.client.opener.calls))
        self.assertEqual(before, {p: p.read_bytes() for p in self.output.rglob("*") if p.is_file()})

    def test_bad_github_context_and_reruns_refused_before_http(self):
        for key, value in (("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_REF", "refs/heads/main"),
                           ("GITHUB_SHA", "not-a-commit"), ("GITHUB_RUN_ID", "0"),
                           ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_EVENT_NAME", "pull_request")):
            with self.subTest(key=key), patch.dict(self.env, {key: value}):
                with self.assertRaisesRegex(ValueError, "GitHub context"):
                    self.invoke()
                self.assertFalse(self.output.exists())
                self.assertFalse(self.client.opener.calls)

    def test_local_run_is_never_claimed_as_a_github_run(self):
        self.env.clear()
        result = self.invoke()
        self.assertEqual(result["context"], {"runner": "local", "github_verified": False})

    def test_expiry_naive_clock_and_changed_budget_refused_before_http(self):
        for at in (smoke.DEADLINE, smoke.DEADLINE + timedelta(seconds=1), datetime(2026, 10, 3)):
            with self.subTest(at=at), self.assertRaisesRegex(ValueError, "clock"):
                smoke.run_smoke(self.config_path, self.output, feed=self.client,
                                clock=lambda: at, environ=self.env)
        self.client.max_requests = 17
        with self.assertRaisesRegex(ValueError, "feed"):
            self.invoke()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.client.opener.calls)

    def test_wrong_config_or_extra_secret_key_refused_before_http(self):
        self.config_path.write_text("api_key: DO_NOT_EXPORT\n")
        with self.assertRaises(ValueError):
            self.invoke()
        self.config_path.write_bytes(Path("config.v2.example.yaml").read_bytes())
        with self.assertRaisesRegex(ValueError, "configuration"):
            self.invoke()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.client.opener.calls)

    def test_http_failure_retains_successful_receipts_and_never_retries(self):
        opened = self.client.opener.open
        calls = []

        def fail_second(request, **kwargs):
            calls.append(request)
            if len(calls) == 2:
                raise HTTPError(request.full_url, 429, "rate limit", {}, None)
            return opened(request, **kwargs)

        with patch.object(self.client.opener, "open", side_effect=fail_second):
            result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["requests_completed"], 1)
        self.assertEqual(len(calls), 2)
        receipts = json.loads((self.output / "partial-receipts.json").read_text())
        raw = (self.output / "partial-raw" / receipts[0]["file"]).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipts[0]["sha256"])
        self.assertTrue((self.output / "result.json").exists())
        self.assertFalse((self.output / "audit.json").exists())

    def test_audit_failure_is_not_reported_as_success(self):
        with patch.object(smoke, "verify_binance_capture", side_effect=ValueError("private detail")):
            result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["requests_completed"], 16)
        self.assertEqual(result["error_type"], "ValueError")
        self.assertNotIn("private detail", (self.output / "result.json").read_text())

    def test_code_change_during_capture_is_rejected(self):
        with patch.object(smoke, "code_hash", side_effect=["original", "changed"]):
            result = self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertFalse((self.output / "audit.json").exists())

    def test_config_change_during_capture_is_rejected(self):
        audit = smoke.verify_binance_capture

        def mutate(*args):
            result = audit(*args)
            self.config_path.write_text(self.config_path.read_text() + "\n# mutation\n")
            return result

        with patch.object(smoke, "verify_binance_capture", side_effect=mutate):
            self.assertEqual(self.invoke()["status"], "failed")

    def test_workflow_is_isolated_bounded_read_only_and_has_no_schedule_or_secrets(self):
        path = Path(".github/workflows/v2-github-smoke.yml")
        text = path.read_text()
        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"push", "workflow_dispatch"})
        self.assertEqual(workflow["on"]["push"]["branches"], [smoke.TEST_REF.removeprefix("refs/heads/")])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", text)
        self.assertNotIn("config.yaml", text)
        self.assertNotIn("actions/cache", text)
        self.assertNotIn("v2_data/", text)
        self.assertEqual(workflow["jobs"]["public-data"]["needs"], "correctness")
        for job in workflow["jobs"].values():
            self.assertEqual(job["runs-on"], "ubuntu-latest")
            self.assertLessEqual(int(job["timeout-minutes"]), 10)
            for step in job["steps"]:
                if "uses" in step:
                    self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
