"""Bounded, preregistered overnight research; no adaptive search or deployment.

One writer, immutable checkpoints, and a complete trial ledger including cash
and failures. Retraining the same observations indefinitely is not evidence.
"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

import yaml

from .config import load_config
from .development import _write_json
from .ml_model import canonical
from .ml_preparation import _code_hash, load_prepared_ml_source
from .ml_study import APPROVAL, run_ml_study
from .ml_variants import VARIANTS


TRIAL_ORDER = tuple(VARIANTS)


def now():
    return datetime.now(timezone.utc)


def register_batch(directory, cfg, registration, output, *, hours=6):
    if output.exists():
        raise ValueError("Batch output exists; use a new directory")
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 12:
        raise ValueError("Batch time budget must be 1..12 hours")
    dataset, source = load_prepared_ml_source(directory, cfg, registration)
    started = now()
    plan = {"schema": 1, "purpose": "bounded retrospective ML comparison",
            "created_at": started.isoformat(), "deadline": (started + timedelta(hours=hours)).isoformat(),
            "dataset_directory": str(directory.resolve()), "source_registration": str(registration.resolve()),
            "source_registration_sha256": hashlib.sha256(registration.read_bytes()).hexdigest(),
            "dataset_sha256": dataset.checksum, "config_hash": cfg.digest(), "code_hash": _code_hash(),
            "development_end_exclusive": source["development_end_exclusive"],
            "trial_order": TRIAL_ORDER, "variants": VARIANTS, "approval_criteria": APPROVAL,
            "selection_rule": "first individually approved variant in fixed trial order, per fold; never validation returns",
            "maximum_trials": len(TRIAL_ORDER), "repeats_per_trial": 1,
            "multiple_testing": "Four related retrospective trials; no independent significance or live permission inferred",
            "holdout_evaluated": False, "approved_for_live": False, "default_policy_changed": False}
    output.mkdir(parents=True)
    _write_json(output / "registration.json", {"plan": plan, "sha256": hashlib.sha256(canonical(plan)).hexdigest()})
    return plan


def load_batch(output, cfg):
    envelope = json.loads((output / "registration.json").read_bytes())
    if (set(envelope) != {"plan", "sha256"}
            or hashlib.sha256(canonical(envelope["plan"])).hexdigest() != envelope["sha256"]):
        raise ValueError("Batch registration checksum mismatch")
    plan = envelope["plan"]
    if (plan.get("schema") != 1 or plan.get("config_hash") != cfg.digest() or plan.get("code_hash") != _code_hash()
            or plan.get("variants") != VARIANTS or plan.get("trial_order") != list(TRIAL_ORDER)
            or plan.get("approval_criteria") != APPROVAL or plan.get("maximum_trials") != len(TRIAL_ORDER)
            or plan.get("repeats_per_trial") != 1
            or any(plan.get(k) is not False for k in ("holdout_evaluated", "approved_for_live", "default_policy_changed"))):
        raise ValueError("Batch code/config/protocol changed")
    registration = Path(plan["source_registration"])
    if hashlib.sha256(registration.read_bytes()).hexdigest() != plan["source_registration_sha256"]:
        raise ValueError("Batch source registration changed")
    dataset, source = load_prepared_ml_source(Path(plan["dataset_directory"]), cfg, registration)
    if dataset.checksum != plan["dataset_sha256"] or source["development_end_exclusive"] != plan["development_end_exclusive"]:
        raise ValueError("Batch protected source changed")
    return plan


def summarize_reports(reports):
    """Fixed-priority approval selection; validation never ranks candidates."""
    summaries = []
    for variant, report in reports:
        failures = Counter()
        for fold in report["folds"]:
            failures.update(fold["selection"]["failure_reasons"])
        summaries.append({"variant": variant, "candidate_samples": report["candidate_samples"],
                          "fitted_models": report["fitted_models"], "ready_models": report["ready_models"],
                          "selected_cash_windows": report["selected_cash_windows"],
                          "approval_failure_counts": dict(sorted(failures.items()))})
    choices = []
    if reports:
        labels = [f["fold"] for f in reports[0][1]["folds"]]
        if any([f["fold"] for f in report["folds"]] != labels for _, report in reports):
            raise ValueError("Batch fold identities differ")
        for index, label in enumerate(labels):
            eligible = [(variant, report["folds"][index]) for variant, report in reports
                        if not report["folds"][index]["selection"]["failure_reasons"]]
            variant, fold = eligible[0] if eligible else ("cash", reports[0][1]["folds"][index])
            chosen = fold["validation"]["ml" if eligible else "cash"]
            choices.append({"fold": label, "choice": variant, "approval_only_fixed_priority": True,
                            "validation_not_used_for_selection": True,
                            "retrospective_selected_validation": chosen})
    return summaries, choices


def run_registered_batch(output, cfg, *, progress=None):
    plan = load_batch(output, cfg)
    if (output / "report.json").exists():
        raise ValueError("Batch already completed; never rerun trials")
    # An abandoned lock is preserved. Do not silently restart/overwrite a trial.
    _write_json(output / "worker.json", {"pid": os.getpid(), "started_at": now().isoformat(), "single_writer": True})
    reports, status, error = [], "complete", None
    with (output / "events.jsonl").open("x", buffering=1) as events:
        def say(message):
            events.write(json.dumps({"at": now().isoformat(), "message": message}) + "\n")
            if progress:
                progress(message)
            if now() >= datetime.fromisoformat(plan["deadline"]):
                raise TimeoutError("Registered research time budget reached")

        try:
            for variant in TRIAL_ORDER:
                say(f"Starting registered trial {variant}; no threshold/risk/cost changes")
                report = run_ml_study(Path(plan["dataset_directory"]), cfg, Path(plan["source_registration"]),
                                      output / variant, variant=variant, progress=say)
                reports.append((variant, report))
                _write_json(output / f"checkpoint-{len(reports):02d}.json",
                            {"at": now().isoformat(), "variant": variant, "completed_trials": len(reports),
                             "report_sha256": hashlib.sha256((output / variant / "report.json").read_bytes()).hexdigest(),
                             "approved_for_live": False})
                say(f"Completed {variant}; ready={report['ready_models']}; cash={report['selected_cash_windows']}")
        except Exception as exc:
            status = "deadline_reached" if isinstance(exc, TimeoutError) else "failed"
            error = f"{type(exc).__name__}: {exc}"
            events.write(json.dumps({"at": now().isoformat(), "status": status, "error": error}) + "\n")
        summaries, choices = summarize_reports(reports)
        if status != "complete":
            choices = []  # A partial batch is not a completed model comparison.
        result = {"scope": "RETROSPECTIVE DEVELOPMENT; not untouched OOS or a live recommendation",
                  "status": status, "error": error, "finished_at": now().isoformat(),
                  "registered_trials": len(TRIAL_ORDER), "completed_trials": len(reports),
                  "trials": summaries, "fold_choices": choices,
                  "holdout_evaluated": False, "approved_for_live": False, "default_policy_changed": False,
                  "limitations": ["Repeated/correlated folds and four tested variants are not independent evidence",
                                  "No automatic promotion, parameter search, new funds, private API or broker connection",
                                  "Stops after the registered batch/time budget; rerunning identical data is not new evidence",
                                  "All historical models remain research-only and may be expired today"]}
        _write_json(output / "report.json", result)
        lines = ["# Bounded ML research batch", "", f"Status: {status}. Completed: {len(reports)}/{len(TRIAL_ORDER)}.",
                 "Live approved: False. Defaults unchanged. Original holdout excluded.", "",
                 "| Variant | Fitted | Ready | Cash-selected windows |", "| --- | ---: | ---: | ---: |"]
        lines += [f"| {r['variant']} | {r['fitted_models']} | {r['ready_models']} | {r['selected_cash_windows']} |" for r in summaries]
        lines += ["", "Fold choices use only registered approval gates and fixed priority; validation never ranks variants.", ""]
        lines += ["- " + item for item in result["limitations"]]
        if error:
            lines += ["", "Failure: " + error]
        with (output / "report.md").open("x") as stream:
            stream.write("\n".join(lines) + "\n")
    return result


def snapshot_runtime(output, cfg, plan):
    """Freeze only this research package, its auditor and resolved configuration.

    A later workspace edit must not change a running worker or prevent its
    audit. No secrets, V1 configuration, virtualenv or unrelated files copied.
    Dependencies still come from the existing virtualenv; this is not a VM.
    """
    runtime = output / "runtime"
    runtime.mkdir()
    package = runtime / "crypto_trader_v2"
    package.mkdir()
    files = {}
    for source in sorted(Path(__file__).parent.glob("*.py")):
        payload = source.read_bytes()
        destination = package / source.name
        with destination.open("xb") as stream:
            stream.write(payload)
        files[str(destination.relative_to(runtime))] = hashlib.sha256(payload).hexdigest()
    frozen_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(package.glob("*.py")))).hexdigest()
    if frozen_hash != plan["code_hash"]:
        raise ValueError("Research code changed while creating runtime snapshot")
    scripts = runtime / "scripts"
    scripts.mkdir()
    auditor = Path(__file__).resolve().parent.parent / "scripts" / "verify_ml_batch.py"
    payload = auditor.read_bytes()
    with (scripts / auditor.name).open("xb") as stream:
        stream.write(payload)
    files["scripts/verify_ml_batch.py"] = hashlib.sha256(payload).hexdigest()
    config = runtime / "config.yaml"
    # Canonical serialisation converts Decimal to explicit decimal strings.
    with config.open("x") as stream:
        yaml.safe_dump(json.loads(canonical(asdict(cfg))), stream, sort_keys=True)
    if load_config(str(config)).digest() != cfg.digest():
        raise ValueError("Runtime snapshot configuration does not round-trip")
    files["config.yaml"] = hashlib.sha256(config.read_bytes()).hexdigest()
    _write_json(runtime / "manifest.json", {"schema": 1, "code_hash": frozen_hash, "config_hash": cfg.digest(),
                "files": files, "python": sys.version, "interpreter": sys.executable,
                "scope": "Research source/config snapshot; existing virtualenv dependencies are not frozen"})
    return runtime


def run_batch_worker(output, cfg, config_path, *, verify_on_completion=False, progress=None):
    """Train once, then audit once. An audit failure never grants approval."""
    if verify_on_completion and (not config_path or load_config(str(config_path)).digest() != cfg.digest()):
        raise ValueError("Post-completion verification requires a matching explicit configuration")
    result = run_registered_batch(output, cfg, progress=progress)
    if not verify_on_completion or result["status"] != "complete":
        return 0 if result["status"] == "complete" else 2
    if progress:
        progress("Research batch complete; starting read-only reproducibility/accounting verification")
    command = [sys.executable, "-B", str(Path(__file__).resolve().parent.parent / "scripts" / "verify_ml_batch.py"),
               str(output.resolve()), "--config", str(Path(config_path).resolve()),
               "--output", str((output / "verification").resolve())]
    status, error, returncode = "failed", None, None
    try:
        audit = subprocess.run(command, stdin=subprocess.DEVNULL, timeout=1800, check=False)
        returncode = audit.returncode
        report_path = output / "verification" / "report.json"
        if returncode == 0 and report_path.exists():
            report = json.loads(report_path.read_bytes())
            if report.get("status") == "verified" and report.get("approved_for_live") is False:
                status = "verified"
            else:
                error = "Auditor did not produce a verified, research-only report"
        else:
            error = f"Auditor exited {returncode}; a verified report is required"
    except subprocess.TimeoutExpired:
        status, error = "timeout", "Read-only audit exceeded its separate 30-minute budget"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    _write_json(output / "verification-status.json", {"status": status, "finished_at": now().isoformat(),
                "returncode": returncode, "error": error, "approved_for_live": False,
                "research_report_unchanged": True})
    if progress:
        progress(f"Post-completion verification: {status}; live approval remains False")
    return 0 if status == "verified" else 3


def launch_registered_batch(output, cfg, config_path, *, keep_awake=False, verify_on_completion=False):
    plan = load_batch(output, cfg)
    if (output / "launch.json").exists() or (output / "worker.json").exists():
        raise ValueError("Batch already launched; no duplicate worker")
    if keep_awake and (platform.system() != "Darwin" or not Path("/usr/bin/caffeinate").is_file()):
        raise ValueError("Process-bound keep-awake requires macOS caffeinate")
    output = output.resolve()
    runtime = snapshot_runtime(output, cfg, plan)
    command = [sys.executable, "-B", "-m", "crypto_trader_v2", "-c", str(runtime / "config.yaml")]
    command += ["ml-batch-worker", str(output.resolve())]
    if verify_on_completion:
        command += ["--verify-on-completion"]
    with (output / "worker.log").open("xb") as log:
        environment = dict(os.environ)
        # Small linear models do not benefit from consuming every CPU thread.
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
            environment[key] = "1"
        worker = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  cwd=str(runtime), start_new_session=True, env=environment)
    awake = {"requested": keep_awake, "status": "not_requested"}
    if keep_awake:
        try:
            helper = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(worker.pid)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            awake = {"requested": True, "pid": helper.pid, "status": "started_unverified",
                     "scope": "Idle sleep only, process-bound; does not survive shutdown, lid closure or restart"}
        except OSError as exc:
            awake = {"requested": True, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    record = {"pid": worker.pid, "launched_at": now().isoformat(), "command": command,
              "log": str((output / "worker.log").resolve()), "numerical_worker_threads": 1,
              "runtime": str(runtime), "runtime_manifest_sha256": hashlib.sha256((runtime / "manifest.json").read_bytes()).hexdigest(),
              "original_config_path": str(Path(config_path).resolve()) if config_path else None,
              "verify_on_completion": verify_on_completion, "audit_time_budget_seconds": 1800,
              "keep_awake": awake,
              "approved_for_live": False}
    _write_json(output / "launch.json", record)
    return record


def batch_status(output):
    """Read-only progress; PID is not treated as proof a process is alive."""
    if (output / "report.json").exists():
        result = json.loads((output / "report.json").read_bytes())
        if (output / "verification-status.json").exists():
            result["verification"] = json.loads((output / "verification-status.json").read_bytes())
        elif (output / "launch.json").exists() and json.loads((output / "launch.json").read_bytes()).get("verify_on_completion"):
            result["verification"] = {"status": "pending_or_running_unverified" if result["status"] == "complete" else "not_run"}
        return result
    plan = json.loads((output / "registration.json").read_bytes())["plan"]
    launch = json.loads((output / "launch.json").read_bytes()) if (output / "launch.json").exists() else None
    events = (output / "events.jsonl").read_text().splitlines() if (output / "events.jsonl").exists() else []
    # A concurrent line write can leave the last row incomplete.
    last = None
    for line in reversed(events):
        try:
            last = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    return {"status": "registered_or_running_unverified", "launch": launch,
            "deadline": plan["deadline"], "completed_trials": len(list(output.glob("checkpoint-*.json"))),
            "latest_event": last, "approved_for_live": False}
