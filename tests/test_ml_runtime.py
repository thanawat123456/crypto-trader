from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

from crypto_trader_v2.config import Config, load_config
from crypto_trader_v2.ml_batch import (batch_status, launch_registered_batch, register_batch,
                                      run_batch_worker, snapshot_runtime)
from crypto_trader_v2.ml_model import canonical
from crypto_trader_v2.ml_preparation import _code_hash


class RuntimeTests(unittest.TestCase):
    def test_snapshot_has_exact_research_code_resolved_config_and_auditor_only(self):
        cfg = Config().validate()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = snapshot_runtime(root, cfg, {"code_hash": _code_hash()})
            manifest = json.loads((runtime / "manifest.json").read_bytes())
            self.assertEqual(manifest["code_hash"], _code_hash())
            self.assertEqual(load_config(str(runtime / "config.yaml")).digest(), cfg.digest())
            for relative, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256((runtime / relative).read_bytes()).hexdigest(), digest)
                self.assertTrue(relative.startswith(("crypto_trader_v2/", "scripts/")) or relative == "config.yaml")
            self.assertFalse((runtime / ".env").exists())
            self.assertFalse((runtime / "config.v1.yaml").exists())
            # A real new interpreter must resolve the package from its snapshot.
            command = [sys.executable, "-B", "-c",
                       "import crypto_trader_v2.ml_preparation as m; print(m.__file__); print(m._code_hash())"]
            result = subprocess.run(command, cwd=runtime, capture_output=True, text=True, check=True)
            self.assertIn(str(runtime / "crypto_trader_v2"), result.stdout)
            self.assertIn(manifest["code_hash"], result.stdout)

    def test_copy_time_code_change_fails_before_launch(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "changed while"):
                snapshot_runtime(Path(folder), Config(), {"code_hash": "wrong"})

    def test_background_uses_snapshot_and_process_bound_keep_awake_no_global_settings(self):
        cfg = Config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            registration = root / "source.json"
            registration.write_text("{}")
            source = {"development_end_exclusive": "2023-07-17T04:00:00+00:00"}
            with patch("crypto_trader_v2.ml_batch.load_prepared_ml_source", return_value=(SimpleNamespace(checksum="a" * 64), source)):
                output = root / "batch"
                register_batch(root, cfg, registration, output)
                with patch("crypto_trader_v2.ml_batch.platform.system", return_value="Darwin"), \
                     patch("crypto_trader_v2.ml_batch.Path.is_file", return_value=True), \
                     patch("crypto_trader_v2.ml_batch.subprocess.Popen", side_effect=[SimpleNamespace(pid=123), SimpleNamespace(pid=124)]) as popen:
                    record = launch_registered_batch(output, cfg, None, keep_awake=True, verify_on_completion=True)
                worker, awake = popen.call_args_list
                self.assertEqual(worker.kwargs["cwd"], str((output / "runtime").resolve()))
                self.assertIn(str((output / "runtime/config.yaml").resolve()), worker.args[0])
                self.assertIn("--verify-on-completion", worker.args[0])
                self.assertEqual(awake.args[0], ["/usr/bin/caffeinate", "-i", "-w", "123"])
                self.assertEqual(record["keep_awake"]["status"], "started_unverified")
                self.assertFalse(record["approved_for_live"])
                self.assertEqual(worker.kwargs["env"]["OMP_NUM_THREADS"], "1")

    def test_unsupported_keep_awake_does_not_launch_worker(self):
        cfg = Config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            registration = root / "source.json"
            registration.write_text("{}")
            source = {"development_end_exclusive": "2023-07-17T04:00:00+00:00"}
            with patch("crypto_trader_v2.ml_batch.load_prepared_ml_source", return_value=(SimpleNamespace(checksum="a" * 64), source)):
                output = root / "batch"
                register_batch(root, cfg, registration, output)
                with patch("crypto_trader_v2.ml_batch.platform.system", return_value="Linux"), \
                     patch("crypto_trader_v2.ml_batch.subprocess.Popen") as popen:
                    with self.assertRaisesRegex(ValueError, "macOS"):
                        launch_registered_batch(output, cfg, None, keep_awake=True)
                    popen.assert_not_called()
                self.assertFalse((output / "runtime").exists())

    def run_worker_case(self, root, *, research_status="complete", audit_return=0, create_report=True, timeout=False):
        cfg = Config()
        config = root / "config.yaml"
        config.write_text(yaml.safe_dump(json.loads(canonical(asdict(cfg)))))
        result = {"status": research_status, "approved_for_live": False}
        (root / "report.json").write_text(json.dumps(result))

        def audit(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 1800)
            self.assertEqual(command[-2], "--output")
            if timeout:
                raise subprocess.TimeoutExpired(command, 1800)
            if create_report:
                output = Path(command[-1])
                output.mkdir()
                (output / "report.json").write_text(json.dumps({"status": "verified", "approved_for_live": False}))
            return SimpleNamespace(returncode=audit_return)

        with patch("crypto_trader_v2.ml_batch.run_registered_batch", return_value=result), \
             patch("crypto_trader_v2.ml_batch.subprocess.run", side_effect=audit) as run:
            code = run_batch_worker(root, cfg, str(config), verify_on_completion=True)
        return code, run.call_count, batch_status(root)

    def test_successful_research_audited_once_and_status_preserves_original_report(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            code, calls, status = self.run_worker_case(root)
            self.assertEqual((code, calls), (0, 1))
            self.assertEqual(status["verification"]["status"], "verified")
            self.assertFalse(status["approved_for_live"])
            self.assertNotIn("verification", json.loads((root / "report.json").read_bytes()))

    def test_audit_missing_report_nonzero_and_timeout_fail_closed_without_retry(self):
        for settings, expected in (({"create_report": False}, "failed"),
                                   ({"audit_return": 1}, "failed"), ({"timeout": True}, "timeout")):
            with self.subTest(settings=settings), TemporaryDirectory() as folder:
                code, calls, status = self.run_worker_case(Path(folder), **settings)
                self.assertEqual((code, calls), (3, 1))
                self.assertEqual(status["verification"]["status"], expected)
                self.assertFalse(status["verification"]["approved_for_live"])

    def test_incomplete_research_never_runs_audit_and_verification_needs_explicit_config(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            code, calls, _ = self.run_worker_case(root, research_status="deadline_reached")
            self.assertEqual((code, calls), (2, 0))
            self.assertFalse((root / "verification-status.json").exists())
        with patch("crypto_trader_v2.ml_batch.run_registered_batch") as run:
            with self.assertRaisesRegex(ValueError, "explicit configuration"):
                run_batch_worker(Path("unused"), Config(), None, verify_on_completion=True)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
