"""Bounded local scheduler for an ALREADY registered public engineering shadow.

This wrapper does not import the evolving trading package, use an account, change
the study, or retry/backfill. It invokes only the frozen verify/observe CLI.
No launchd/cron/system settings or keep-awake settings are changed.
"""
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


POLICY = "native-shadow-utc-h4-scheduler-v1"
PURPOSE = "Seven-day Binance TH public execution engineering shadow; NOT ML approval or profit validation"
PROFILE = "native-limit-ioc-ceil-fee-v1"
GRACE_SECONDS = 30
POLL_SECONDS = 30
CHILD_TIMEOUT = 150


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc)


def utc(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    require(result.tzinfo is not None, "Timezone-aware time required")
    return result.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def package_hash(runtime):
    files = sorted((runtime/"crypto_trader_v2").glob("*.py"))
    require(files and not any(p.is_symlink() for p in files), "Missing/symlinked frozen package")
    return digest(b"".join(p.read_bytes() for p in files))


def write_new(path, value):
    with path.open("x") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2)+"\n")


def state_write(output, value):
    # Only this scheduler's replaceable heartbeat; NEVER study evidence/ledgers.
    temporary = output/"state.next.json"
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2)+"\n")
    temporary.replace(output/"state.json")


def read_study(study):
    raw = (study/"registration.json").read_bytes()
    envelope = json.loads(raw)
    plan = envelope["plan"]
    require(set(envelope) == {"plan", "sha256"} and digest(canonical(plan)) == envelope["sha256"], "Study registration checksum mismatch")
    require(plan["purpose"] == PURPOSE and plan["execution_profile"] == PROFILE
            and plan["cases"] == ["cash", "baseline"] and plan["slot_seconds"] == 14400
            and plan["budget"] == {"days":7,"attempts":43,"requests":800,"bytes":33554432}
            and utc(plan["deadline"]) == utc(plan["registered_at"])+timedelta(days=7)
            and plan["models_trained"] == 0 and all(plan[key] is False for key in
                 ("approved_for_live", "selection_performed", "default_policy_changed")), "Unsupported study/safety protocol")
    return plan, digest(raw)


def child_command(plan, action):
    require(action in {"verify-binance-th-shadow", "observe-binance-th-shadow"}, "Unallowlisted scheduler child")
    return [plan["python"], "-B", "-m", "crypto_trader_v2", "-c", plan["config"], action, plan["study"]]


def run_child(plan, action):
    # No inherited API credentials, PYTHONPATH or user Python customization.
    environment = {"PATH":os.defpath, "PYTHONNOUSERSITE":"1", "OPENBLAS_NUM_THREADS":"1",
                   "OMP_NUM_THREADS":"1", "MKL_NUM_THREADS":"1"}
    result = subprocess.run(child_command(plan, action), cwd=plan["runtime"], env=environment,
                            capture_output=True, text=True, timeout=CHILD_TIMEOUT, check=False)
    require(len(result.stdout)+len(result.stderr) <= 256*1024, "Scheduler child output budget exceeded")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        payload = None
    return {"returncode":result.returncode, "payload":payload, "stderr":result.stderr, "stdout":result.stdout}


def audit_ok(result):
    payload = result["payload"] or {}
    require(result["returncode"] == 0 and payload.get("status") == "verified"
            and payload.get("current_package_matches_registration") is True
            and payload.get("models_trained") == 0 and payload.get("approved_for_live") is False
            and not payload.get("errors") and not payload.get("worker_running"), "Study is not safely auditable; no launch/continuation")


def prepare(study, config, output):
    study, config, output = study.resolve(), config.resolve(), output.resolve()
    require(not output.exists(), "Scheduler output exists; use a new directory")
    source, source_hash = read_study(study)
    require(utc(source["registered_at"]) <= now() < utc(source["deadline"]), "Study expired/not started")
    runtime = study/"frozen_runtime"
    require(package_hash(runtime) == source["code_hash"], "Frozen runtime differs from registered package")
    configuration = config.read_bytes()
    interpreter = str(Path(sys.executable).absolute())  # Preserve the venv symlink/path.
    probe = {"python":interpreter,"runtime":str(runtime),"config":str(config),"study":str(study)}
    audit_ok(run_child(probe, "verify-binance-th-shadow"))  # Read-only, no network.
    require(configuration == config.read_bytes() and source_hash == read_study(study)[1]
            and package_hash(runtime) == source["code_hash"], "Source changed during preparation")
    runner = Path(__file__).read_bytes()
    plan = {"schema":1,"policy":POLICY,"prepared_at":now().isoformat(),"study":str(study),
            "study_registration_sha256":source_hash,"runtime":str(runtime),"package_sha256":source["code_hash"],
            "config":str(output/"config.yaml"),"config_sha256":digest(configuration),
            "runner":str(output/"worker.py"),"runner_sha256":digest(runner),"python":interpreter,
            "signal_not_before":source["signal_not_before"],"deadline":source["deadline"],"slot_seconds":14400,
            "grace_seconds":GRACE_SECONDS,"poll_seconds":POLL_SECONDS,"child_timeout_seconds":CHILD_TIMEOUT,
            "stop_on_failed_observation":True,"retry":False,"backfill":False,"keep_awake":False,
            "models_trained":0,"approved_for_live":False,"selection_performed":False}
    output.mkdir(parents=True, exist_ok=False)
    (output/"slots").mkdir()
    for path, content in ((output/"config.yaml", configuration),(output/"worker.py",runner)):
        with path.open("xb") as stream:
            stream.write(content)
    write_new(output/"registration.json", {"plan":plan,"sha256":digest(canonical(plan))})
    write_new(output/"launch.lock", {})
    # A study-wide lock prevents two separately prepared scheduler directories
    # from collecting this study concurrently. It is not a trading ledger.
    with (study/"scheduler.lock").open("a+"):
        pass
    state_write(output, {"phase":"prepared","updated_at":now().isoformat(),"pid":None,
                         "next_due":(utc(source["signal_not_before"])+timedelta(seconds=GRACE_SECONDS)).isoformat(),
                         "models_trained":0,"approved_for_live":False})
    return {"status":"prepared","output":str(output),"next_due":status(output)["state"]["next_due"],
            "deadline":plan["deadline"],"worker_running":False,"approved_for_live":False}


def load_control(output):
    output = output.resolve()
    envelope = json.loads((output/"registration.json").read_text())
    plan = envelope["plan"]
    require(set(envelope) == {"plan","sha256"} and digest(canonical(plan)) == envelope["sha256"], "Scheduler registration checksum mismatch")
    require(plan["policy"] == POLICY and plan["slot_seconds"] == 14400
            and plan["grace_seconds"] == GRACE_SECONDS and plan["poll_seconds"] == POLL_SECONDS
            and plan["child_timeout_seconds"] == CHILD_TIMEOUT and plan["stop_on_failed_observation"] is True
            and all(plan[k] is False for k in ("retry","backfill","keep_awake","approved_for_live","selection_performed"))
            and plan["models_trained"] == 0, "Scheduler fixed protocol mismatch")
    require(Path(plan["config"]) == output/"config.yaml" and Path(plan["runner"]) == output/"worker.py"
            and Path(plan["runtime"]) == Path(plan["study"])/"frozen_runtime"
            and plan["python"] == str(Path(sys.executable).absolute()), "Scheduler path/interpreter binding mismatch")
    require(not any((output/name).is_symlink() for name in ("config.yaml","worker.py","slots","state.json")), "Scheduler symlinked control path")
    require(digest(Path(plan["config"]).read_bytes()) == plan["config_sha256"]
            and digest(Path(plan["runner"]).read_bytes()) == plan["runner_sha256"]
            and digest(Path(__file__).read_bytes()) == plan["runner_sha256"], "Scheduler source/config changed")
    source, checksum = read_study(Path(plan["study"]))
    require(checksum == plan["study_registration_sha256"] and source["code_hash"] == plan["package_sha256"]
            and package_hash(Path(plan["runtime"])) == plan["package_sha256"]
            and source["deadline"] == plan["deadline"] and source["signal_not_before"] == plan["signal_not_before"], "Scheduler study/runtime binding changed")
    return plan


def reserved_slots(plan):
    result = set()
    for path in sorted((Path(plan["study"])/"attempts").iterdir()):
        reservation = json.loads((path/"reservation.json").read_text())
        result.add(reservation["slot"])
        require((path/"result.json").exists(), "Incomplete study attempt; manual audit required")
        outcome = json.loads((path/"result.json").read_text())
        require(outcome["status"] == "observed", "Failed study attempt; manual audit required")
    return result


def decision(plan, at, reserved):
    at = utc(at)
    first, deadline = utc(plan["signal_not_before"]), utc(plan["deadline"])
    if at >= deadline:
        return {"action":"finish","reason":"registered_deadline","next_due":None}
    if (deadline-at).total_seconds() <= CHILD_TIMEOUT:
        return {"action":"wait","reason":"insufficient_final_child_budget","next_due":deadline.isoformat()}
    require(at >= utc(read_study(Path(plan["study"]))[0]["registered_at"]), "Clock moved before registration")
    seconds = plan["slot_seconds"]
    slot = int(at.timestamp())//seconds*seconds
    beginning = datetime.fromtimestamp(slot,timezone.utc)
    due = beginning+timedelta(seconds=GRACE_SECONDS)
    if beginning < first or slot in reserved:
        due = max(first,beginning+timedelta(seconds=seconds))+timedelta(seconds=GRACE_SECONDS)
    if due >= deadline:
        return {"action":"wait","reason":"await_deadline","next_due":deadline.isoformat()}
    if at < due:
        return {"action":"wait","reason":"await_next_slot","next_due":due.isoformat()}
    # Only the current slot. Waking after sleep/reboot NEVER backfills gaps.
    return {"action":"observe","slot":slot,"next_due":due.isoformat()}


def progress(plan, at):
    first, deadline = utc(plan["signal_not_before"]), utc(plan["deadline"])
    final_slot = min(int(utc(at).timestamp()-GRACE_SECONDS)//14400*14400,
                     int(deadline.timestamp()-GRACE_SECONDS)//14400*14400)
    expected = list(range(int(first.timestamp()),final_slot+1,14400))
    require(len(expected) <= 42, "Unexpected forward slot count")
    reserved, observed, failed = set(), set(), 0
    for path in (Path(plan["study"])/"attempts").iterdir():
        reserved.add(json.loads((path/"reservation.json").read_text())["slot"])
        if (path/"result.json").exists():
            result = json.loads((path/"result.json").read_text())
            failed += int(result["status"] != "observed")
            if result["status"] == "observed" and (path/"observation.json").exists():
                details = json.loads((path/"observation.json").read_text())
                if details["qualified_forward_slot"]:
                    observed.add(result["slot"])
    current = int(utc(at).timestamp())//14400*14400
    return {"eligible_elapsed_slots":len(expected),"qualified_observed_slots":len(observed),
            "failed_attempts":failed,"missed_past_slots":[datetime.fromtimestamp(s,timezone.utc).isoformat()
                    for s in expected if s < current and s not in reserved],
            "note":"Progress summary only; use frozen raw/ledger verifier for evidence. Missed slots are never backfilled."}


def status(output):
    plan = load_control(output)
    active = False
    with (Path(plan["study"])/"scheduler.lock").open("rb") as lock:
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_SH|fcntl.LOCK_NB)
        except BlockingIOError:
            active = True
    state = json.loads((output/"state.json").read_text())
    age = (now()-utc(state["updated_at"])).total_seconds()
    # An observation can take up to 150s; heartbeat is otherwise every <=30s.
    fresh = 0 <= age <= CHILD_TIMEOUT+2*POLL_SECONDS
    return {"worker_running":active and fresh and state["phase"] in {"waiting","observing","auditing"},
            "study_worker_lock_held":active,"heartbeat_fresh":fresh,"state":state,
            "progress":progress(plan,now()),
            "deadline":plan["deadline"],"models_trained":0,"approved_for_live":False,
            "limitations":"Local process only: machine sleep/shutdown stops observations; no OS scheduler/keep-awake or ML/profit approval"}


def run_worker(output, *, clock=now, sleep=time.sleep, child=run_child):
    plan = load_control(output)
    require(not (output/"stop.request.json").exists(), "Stop already requested")
    phase = json.loads((output/"state.json").read_text())["phase"]
    require(phase not in {"failed","stopped","finished"}, "Terminal scheduler state; review before any new registration")
    with (Path(plan["study"])/"scheduler.lock").open("rb") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        last_clock = utc(clock())
        last_transition = None
        def update(phase, **values):
            nonlocal last_transition
            state_write(output, {"phase":phase,"updated_at":utc(clock()).isoformat(),"pid":os.getpid(),
                                 "models_trained":0,"approved_for_live":False,**values})
            transition = (phase,canonical(values))
            if transition != last_transition:
                print(json.dumps({"at":utc(clock()).isoformat(),"phase":phase,**values}),flush=True)
                last_transition = transition
        try:
            update("auditing")
            audit_ok(child(plan,"verify-binance-th-shadow"))
            local = set()
            for path in (output/"slots").iterdir():
                reservation = json.loads((path/"reservation.json").read_text())
                require((path/"result.json").exists(), "Incomplete scheduler collection; manual audit required")
                result = json.loads((path/"result.json").read_text())
                require(result["returncode"] == 0, "Failed scheduler collection; manual audit required")
                local.add(reservation["slot"])
            while True:
                plan = load_control(output)
                at = utc(clock())
                require(at >= last_clock, "Clock moved backwards; stop to protect slot ordering")
                last_clock = at
                if (output/"stop.request.json").exists():
                    update("stopped",reason="user_stop_request")
                    return {"status":"stopped","approved_for_live":False}
                choice = decision(plan,at,reserved_slots(plan)|local)
                if choice["action"] == "finish":
                    update("auditing",reason="deadline_final_audit")
                    audit = child(plan,"verify-binance-th-shadow")
                    audit_ok(audit)
                    write_new(output/"final-audit.json",audit["payload"])
                    update("finished",reason=choice["reason"],engineering_observations_complete=audit["payload"].get("engineering_observations_complete",False))
                    return {"status":"finished","approved_for_live":False}
                if choice["action"] == "wait":
                    update("waiting",next_due=choice["next_due"])
                    seconds = (utc(choice["next_due"])-at).total_seconds()
                    sleep(min(POLL_SECONDS,max(.1,seconds)))
                    continue
                # Reserve BEFORE child launch, even if child fails pre-network.
                # No automatically retrying either scheduler or study slots.
                slot = choice["slot"]
                destination = output/"slots"/str(slot)
                destination.mkdir(exist_ok=False)
                write_new(destination/"reservation.json", {"slot":slot,"at":at.isoformat()})
                local.add(slot)
                update("observing",slot=slot)
                result = child(plan,"observe-binance-th-shadow")
                write_new(destination/"result.json",result)
                require(result["returncode"] == 0 and (result["payload"] or {}).get("status") == "observed"
                        and result["payload"].get("slot") == slot and result["payload"].get("models_trained") == 0
                        and result["payload"].get("approved_for_live") is False, "Observation failed/mismatched; stop without retry")
                update("auditing",slot=slot)
                audit = child(plan,"verify-binance-th-shadow")
                write_new(destination/"audit.json",audit)
                audit_ok(audit)
        except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as exc:
            update("failed",error=f"{type(exc).__name__}: {exc}")
            return {"status":"failed","error":f"{type(exc).__name__}: {exc}","approved_for_live":False}


def start(output):
    plan = load_control(output)
    require(now() < utc(plan["deadline"]), "Study expired")
    require(not (output/"stop.request.json").exists(), "Stop already requested")
    with (output/"launch.lock").open("rb") as launch:
        fcntl.flock(launch.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        require(json.loads((output/"state.json").read_text())["phase"] == "prepared", "Scheduler was already started; no automatic relaunch")
        require(not status(output)["study_worker_lock_held"], "Another scheduler owns this study")
        audit_ok(run_child(plan,"verify-binance-th-shadow"))
        with (output/"worker.log").open("x") as log:
            process = subprocess.Popen([plan["python"],"-B",plan["runner"],"run",str(output.resolve())],
                                       cwd=plan["runtime"], stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                       start_new_session=True,close_fds=True,
                                       env={"PATH":os.defpath,"PYTHONNOUSERSITE":"1","OPENBLAS_NUM_THREADS":"1","OMP_NUM_THREADS":"1"})
        write_new(output/"launch.json", {"pid":process.pid,"at":now().isoformat(),"argv":[plan["python"],"-B",plan["runner"],"run",str(output.resolve())]})
        for _ in range(100):
            result = status(output)
            if result["worker_running"] and result["state"]["pid"] == process.pid:
                return {"status":"started","pid":process.pid,"output":str(output),**result}
            if process.poll() is not None:
                raise ValueError("Scheduler exited during startup; inspect worker.log/state.json")
            time.sleep(.1)
        raise ValueError("Scheduler startup unconfirmed; inspect status, never relaunch blindly")


def stop(output):
    load_control(output)
    path = output/"stop.request.json"
    if not path.exists():
        write_new(path, {"at":now().isoformat(),"reason":"explicit user stop request"})
    return {"status":"stop_requested","maximum_idle_delay_seconds":POLL_SECONDS,
            "in_progress_observation":"May finish its bounded child before stopping; no further slots","approved_for_live":False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command",required=True)
    preparation = commands.add_parser("prepare",help="Read-only study audit, then new-only local scheduler registration; NO network/launch")
    preparation.add_argument("--study",type=Path,required=True)
    preparation.add_argument("--config",type=Path,required=True)
    preparation.add_argument("--output",type=Path,required=True)
    for name in ("run","start","status","stop"):
        command = commands.add_parser(name)
        command.add_argument("output",type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.study,args.config,args.output)
        else:
            result = {"run":run_worker,"start":start,"status":status,"stop":stop}[args.command](args.output.resolve())
        print(json.dumps(result,sort_keys=True,indent=2))
        return 2 if result.get("status") == "failed" else 0
    except (ValueError,OSError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        print(f"{type(exc).__name__}: {exc}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
