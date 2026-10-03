from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_binance_th_shadow as worker


NOW = datetime(2026,10,3,9,45,tzinfo=timezone.utc)


def good_audit():
    return {"returncode":0,"payload":{"status":"verified","current_package_matches_registration":True,
            "models_trained":0,"approved_for_live":False,"errors":[],"worker_running":False,
            "engineering_observations_complete":False},"stdout":"fixture","stderr":""}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.study, self.output = self.root/"study",self.root/"worker"
        self.study.mkdir()
        (self.study/"attempts").mkdir()
        self.runtime = self.study/"frozen_runtime"
        package = self.runtime/"crypto_trader_v2"
        package.mkdir(parents=True)
        (package/"__main__.py").write_text("# Synthetic scheduler fixture, NOT exchange evidence.\n")
        self.config = self.root/"config.yaml"
        self.config.write_text("# software fixture\n")
        self.source = {"purpose":worker.PURPOSE,"execution_profile":worker.PROFILE,"cases":["cash","baseline"],
                "slot_seconds":14400,"budget":{"days":7,"attempts":43,"requests":800,"bytes":33554432},
                "registered_at":(NOW-timedelta(minutes=10)).isoformat(),
                "signal_not_before":NOW.replace(hour=12,minute=0).isoformat(),
                "deadline":(NOW-timedelta(minutes=10)+timedelta(days=7)).isoformat(),
                "code_hash":worker.package_hash(self.runtime),"models_trained":0,"approved_for_live":False,
                "selection_performed":False,"default_policy_changed":False}
        worker.write_new(self.study/"registration.json", {"plan":self.source,"sha256":worker.digest(worker.canonical(self.source))})
        self.at = NOW
        self.clock = patch.object(worker,"now",side_effect=lambda:self.at).start()
        self.addCleanup(self.clock.stop)
        with patch.object(worker,"run_child",return_value=good_audit()):
            worker.prepare(self.study,self.config,self.output)
        self.plan = worker.load_control(self.output)

    def invoke(self, child, sleep):
        with redirect_stdout(io.StringIO()):
            return worker.run_worker(self.output,clock=lambda:self.at,sleep=sleep,child=child)

    def test_preparation_is_new_only_and_does_not_launch_or_request_market_data(self):
        self.assertEqual(worker.status(self.output)["state"]["phase"],"prepared")
        self.assertFalse(worker.status(self.output)["worker_running"])
        self.assertEqual((self.output/"config.yaml").read_bytes(),self.config.read_bytes())
        with self.assertRaisesRegex(ValueError,"output exists"):
            worker.prepare(self.study,self.config,self.output)
        with patch.object(worker,"run_child",return_value=good_audit()) as call:
            worker.prepare(self.study,self.config,self.root/"second")
        self.assertEqual(call.call_args.args[1],"verify-binance-th-shadow")
        self.assertFalse(list((self.study/"attempts").iterdir()))

    def test_boundary_grace_bootstrap_reserved_and_deadline(self):
        boundary = NOW.replace(hour=12,minute=0)
        self.assertEqual(worker.decision(self.plan,NOW,set())["action"],"wait")
        self.assertEqual(worker.decision(self.plan,boundary+timedelta(seconds=29),set())["action"],"wait")
        due = worker.decision(self.plan,boundary+timedelta(seconds=30),set())
        self.assertEqual(due["action"],"observe")
        self.assertEqual(worker.decision(self.plan,boundary+timedelta(seconds=30),{due["slot"]})["action"],"wait")
        self.assertEqual(worker.decision(self.plan,worker.utc(self.plan["deadline"]),set())["action"],"finish")
        self.assertEqual(worker.decision(self.plan,worker.utc(self.plan["deadline"])-timedelta(seconds=100),set())["action"],"wait")

    def test_wakeup_skips_old_slots_and_progress_discloses_gaps(self):
        self.at = NOW.replace(hour=20,minute=5)
        choice = worker.decision(self.plan,self.at,set())
        self.assertEqual(choice["slot"],int(NOW.replace(hour=20,minute=0).timestamp()))
        progress = worker.progress(self.plan,self.at)
        self.assertEqual(progress["eligible_elapsed_slots"],3)
        self.assertEqual(progress["missed_past_slots"],[NOW.replace(hour=12,minute=0).isoformat(),NOW.replace(hour=16,minute=0).isoformat()])

    def test_wait_is_cooperative_and_stop_does_not_launch_observation(self):
        calls, waits = [],[]
        def child(plan,action):
            calls.append(action)
            return good_audit()
        def sleep(seconds):
            waits.append(seconds)
            worker.stop(self.output)
        result = self.invoke(child,sleep)
        self.assertEqual(result["status"],"stopped")
        self.assertEqual(calls,["verify-binance-th-shadow"])
        self.assertEqual(waits,[30])
        self.assertFalse(worker.status(self.output)["worker_running"])

    def test_failed_observation_is_reserved_once_and_stops_without_retry(self):
        self.at = NOW.replace(hour=12,minute=1)
        calls = []
        def child(plan,action):
            calls.append(action)
            if action == "verify-binance-th-shadow": return good_audit()
            self.assertTrue(list((self.output/"slots").glob("*/reservation.json")))
            return {"returncode":2,"payload":{"status":"failed","error":"fixture HTTP429"},"stdout":"","stderr":""}
        result = self.invoke(child,lambda seconds:self.fail("Unexpected wait/retry"))
        self.assertEqual(result["status"],"failed")
        self.assertEqual(calls,["verify-binance-th-shadow","observe-binance-th-shadow"])
        self.assertEqual(len(list((self.output/"slots").iterdir())),1)
        with self.assertRaisesRegex(ValueError,"Terminal"):
            self.invoke(child,lambda seconds:None)

    def test_success_is_audited_and_current_slot_is_not_reused(self):
        self.at = NOW.replace(hour=12,minute=1)
        calls = []
        def child(plan,action):
            calls.append(action)
            if action == "verify-binance-th-shadow": return good_audit()
            return {"returncode":0,"payload":{"status":"observed","slot":int(self.at.timestamp())//14400*14400,
                    "models_trained":0,"approved_for_live":False},"stdout":"","stderr":""}
        result = self.invoke(child,lambda seconds:worker.stop(self.output))
        self.assertEqual(result["status"],"stopped")
        self.assertEqual(calls,["verify-binance-th-shadow","observe-binance-th-shadow","verify-binance-th-shadow"])
        self.assertEqual(len(list((self.output/"slots").glob("*/audit.json"))),1)

    def test_audit_failure_blocks_all_network_observation(self):
        self.at = NOW.replace(hour=12,minute=1)
        invalid = good_audit()
        invalid["payload"]["errors"] = ["fixture ledger mismatch"]
        calls = []
        def child(plan,action):
            calls.append(action)
            return invalid
        self.assertEqual(self.invoke(child,lambda seconds:None)["status"],"failed")
        self.assertEqual(calls,["verify-binance-th-shadow"])
        self.assertFalse(list((self.output/"slots").iterdir()))

    def test_crashed_scheduler_reservation_requires_manual_audit(self):
        folder = self.output/"slots"/"1791028800"
        folder.mkdir()
        worker.write_new(folder/"reservation.json",{"slot":1791028800,"at":NOW.isoformat()})
        calls = []
        def child(plan,action): calls.append(action); return good_audit()
        self.assertEqual(self.invoke(child,lambda seconds:None)["status"],"failed")
        self.assertEqual(calls,["verify-binance-th-shadow"])

    def test_incomplete_or_failed_study_is_not_retried(self):
        folder = self.study/"attempts"/"0000"
        folder.mkdir()
        worker.write_new(folder/"reservation.json",{"slot":int(NOW.timestamp())//14400*14400})
        with self.assertRaisesRegex(ValueError,"Incomplete study"):
            worker.reserved_slots(self.plan)
        worker.write_new(folder/"result.json",{"status":"failed"})
        with self.assertRaisesRegex(ValueError,"Failed study"):
            worker.reserved_slots(self.plan)

    def test_study_wide_lock_prevents_two_scheduler_writers(self):
        with (self.study/"scheduler.lock").open("rb") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertTrue(worker.status(self.output)["study_worker_lock_held"])
            self.assertFalse(worker.status(self.output)["worker_running"])
            with self.assertRaises(BlockingIOError):
                self.invoke(lambda plan,action:self.fail("Child ran despite lock"),lambda seconds:None)

    def test_runtime_config_and_rehashed_protocol_changes_are_rejected(self):
        (self.output/"config.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError,"source/config changed"):
            worker.load_control(self.output)
        (self.output/"config.yaml").write_bytes(self.config.read_bytes())
        path = self.output/"registration.json"
        envelope = json.loads(path.read_text())
        envelope["plan"]["retry"] = True
        envelope["sha256"] = worker.digest(worker.canonical(envelope["plan"]))
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError,"fixed protocol"):
            worker.load_control(self.output)

    def test_backwards_clock_stops_and_deadline_never_collects(self):
        waits = []
        def backwards(seconds): self.at -= timedelta(seconds=1); waits.append(seconds)
        self.assertEqual(self.invoke(lambda plan,action:good_audit(),backwards)["status"],"failed")
        self.assertEqual(waits,[30])
        with self.temp_context() as second:
            self.at = worker.utc(self.plan["deadline"])
            calls = []
            def child(plan,action): calls.append(action); return good_audit()
            with redirect_stdout(io.StringIO()):
                result = worker.run_worker(second,clock=lambda:self.at,sleep=lambda seconds:self.fail("Slept after expiry"),child=child)
            self.assertEqual(result["status"],"finished")
            self.assertEqual(calls,["verify-binance-th-shadow","verify-binance-th-shadow"])
            self.assertTrue((second/"final-audit.json").exists())

    def temp_context(self):
        # Fresh scheduler for the SAME fixture study, within its unchanged window.
        class Context:
            def __enter__(context):
                self.at = NOW
                context.path = self.root/"second-control"
                with patch.object(worker,"run_child",return_value=good_audit()):
                    worker.prepare(self.study,self.config,context.path)
                return context.path
            def __exit__(context,*args): pass
        return Context()

    def test_status_is_readonly_and_stale_heartbeat_is_not_reported_running(self):
        before = {p:p.read_bytes() for p in self.output.rglob("*") if p.is_file()}
        worker.status(self.output)
        self.assertEqual(before,{p:p.read_bytes() for p in self.output.rglob("*") if p.is_file()})
        worker.state_write(self.output,{"phase":"waiting","updated_at":(NOW-timedelta(seconds=300)).isoformat(),"pid":123})
        with (self.study/"scheduler.lock").open("rb") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertFalse(worker.status(self.output)["worker_running"])
            self.assertFalse(worker.status(self.output)["heartbeat_fresh"])

    def test_child_cli_is_frozen_allowlisted_and_has_no_inherited_credentials(self):
        result = SimpleNamespace(returncode=0,stdout=json.dumps(good_audit()["payload"]),stderr="")
        with patch.object(worker.subprocess,"run",return_value=result) as call:
            worker.run_child(self.plan,"verify-binance-th-shadow")
        args, kwargs = call.call_args
        self.assertEqual(args[0][2:4],["-m","crypto_trader_v2"])
        self.assertEqual(kwargs["cwd"],str(self.runtime))
        self.assertNotIn("PYTHONPATH",kwargs["env"])
        self.assertEqual(kwargs["timeout"],150)
        self.assertNotIn("HOME",kwargs["env"])
        with self.assertRaisesRegex(ValueError,"Unallowlisted"):
            worker.child_command(self.plan,"paper")

    def test_child_timeout_retains_reservation_and_reports_failure(self):
        self.at = NOW.replace(hour=12,minute=1)
        def child(plan,action):
            if action == "verify-binance-th-shadow": return good_audit()
            raise subprocess.TimeoutExpired("fixture",150)
        self.assertEqual(self.invoke(child,lambda seconds:None)["status"],"failed")
        self.assertTrue(list((self.output/"slots").glob("*/reservation.json")))
        self.assertFalse(list((self.output/"slots").glob("*/result.json")))

    def test_start_is_explicit_and_refuses_duplicate_or_prior_stop(self):
        worker.stop(self.output)
        with patch.object(worker.subprocess,"Popen") as popen:
            with self.assertRaisesRegex(ValueError,"Stop already requested"):
                worker.start(self.output)
            popen.assert_not_called()

    def test_original_config_edits_do_not_change_frozen_scheduler_config(self):
        self.config.write_text("unrelated future development configuration")
        self.assertEqual((self.output/"config.yaml").read_text(),"# software fixture\n")
        self.assertEqual(worker.load_control(self.output)["config_sha256"],self.plan["config_sha256"])

    def test_frozen_package_and_runner_mutation_fail_before_child(self):
        (self.runtime/"crypto_trader_v2"/"__main__.py").write_text("changed")
        with self.assertRaisesRegex(ValueError,"runtime binding"):
            worker.load_control(self.output)

    def test_rehashed_interpreter_change_is_rejected(self):
        path = self.output/"registration.json"
        envelope = json.loads(path.read_text())
        envelope["plan"]["python"] = "/usr/bin/arbitrary-command"
        envelope["sha256"] = worker.digest(worker.canonical(envelope["plan"]))
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError,"interpreter binding"):
            worker.load_control(self.output)

    def test_duplicate_start_and_startup_exit_do_not_relaunch(self):
        worker.state_write(self.output,{"phase":"waiting","updated_at":NOW.isoformat(),"pid":None})
        with patch.object(worker.subprocess,"Popen") as popen:
            with self.assertRaisesRegex(ValueError,"already started"):
                worker.start(self.output)
            popen.assert_not_called()
        worker.state_write(self.output,{"phase":"prepared","updated_at":NOW.isoformat(),"pid":None})
        fake = SimpleNamespace(pid=123,poll=lambda:2)
        with patch.object(worker,"run_child",return_value=good_audit()), patch.object(worker.subprocess,"Popen",return_value=fake) as popen:
            with self.assertRaisesRegex(ValueError,"exited during startup"):
                worker.start(self.output)
            self.assertEqual(popen.call_count,1)
        # Preserved worker.log prevents silently launching again into that run.
        with patch.object(worker,"run_child",return_value=good_audit()), patch.object(worker.subprocess,"Popen") as popen:
            with self.assertRaises(FileExistsError):
                worker.start(self.output)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
