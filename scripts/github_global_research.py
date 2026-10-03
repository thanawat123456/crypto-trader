"""One fixed, bounded, NON-PRODUCTION GitHub benchmark. No order/account API."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from crypto_trader_v2.shadow import code_hash
from scripts import global_ml_research as ml
from scripts import global_reference_data as data


REPOSITORY = "thanawat123456/crypto-trader"
REF = "refs/heads/codex/v2-global-ml-20261003"
DEADLINE = datetime(2026, 10, 10, tzinfo=timezone.utc)
EXPECTED_CORE = "fb02d4e3de3caa0cded139c25f16bceaaccade804f82c0473ff1b5605403723b"
EXPECTED_SPEC = "f6cfd446ebaad33ab1530150f66661689504ceb802e69ea04e8324055e23fc7f"
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
SCRIPTS = ("global_reference_data.py", "global_ml_research.py", "github_global_research.py")
PURPOSE = "NON-COMMERCIAL GLOBAL REFERENCE FORECAST RESEARCH; NOT profit validation or deployment"


def context(environ):
    """Never serialize the environment, tokens or user-supplied event payload."""
    if (environ.get("GITHUB_ACTIONS") != "true"
            or environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or environ.get("GITHUB_REF") != REF
            or environ.get("GITHUB_EVENT_NAME") != "push"
            or environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or not re.fullmatch(r"[0-9a-f]{40}", environ.get("GITHUB_SHA", ""))
            or not re.fullmatch(r"[1-9][0-9]*", environ.get("GITHUB_RUN_ID", ""))):
        raise ValueError("Unapproved GitHub context/rerun; no request made")
    return {"repository": REPOSITORY, "ref": REF, "commit": environ["GITHUB_SHA"],
            "run_id": environ["GITHUB_RUN_ID"], "attempt": 1, "event": "push"}


def specification():
    return {"archive_plan": data.archive_plan(), "start": data.START.isoformat(),
            "end_exclusive": data.END.isoformat(), "seconds": data.SECONDS,
            "symbols": data.SYMBOLS, "license": data.LICENSE, "schema": ml.SCHEMA,
            "quality_policy": data.QUALITY_POLICY,
            "features": ml.FEATURES, "interactions": ml.INTERACTIONS, "variants": ml.VARIANTS,
            "protocol": ml.PROTOCOL, "labels": ml.SPEC, "parameters": ml.PARAMETERS,
            "requirements": ml.REQUIREMENTS, "shortlist": ml.SHORTLIST,
            "limits": {"requests": data.MAX_REQUESTS, "bytes": data.MAX_BYTES,
                       "object_bytes": data.MAX_OBJECT, "download_seconds": data.MAX_ELAPSED,
                       "models": ml.MAX_MODELS, "training_seconds": ml.MAX_TRAIN_SECONDS,
                       "artifact_bytes": MAX_ARTIFACT_BYTES},
            "retry": False, "approved_for_live": False, "portfolio_opened": False,
            "local_worker_migrated": False, "default_policy_changed": False}


def bindings():
    spec = specification()
    core = code_hash()
    digest = data.sha(data.canonical(spec))
    if core != EXPECTED_CORE or digest != EXPECTED_SPEC:
        raise ValueError("Unapproved core/protocol/feature/budget change")
    return {"core_sha256": core, "specification_sha256": digest,
            "scripts_sha256": {name: data.sha((Path(__file__).parent / name).read_bytes()) for name in SCRIPTS}}


def require_bindings(expected):
    if bindings() != expected:
        raise ValueError("Code/specification changed during the run")


def inventory(output):
    files, total = [], 0
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise ValueError("Artifact contains a symlink")
        if path.is_file() and path.name != "inventory.json":
            payload = path.read_bytes()
            total += len(payload)
            if total > MAX_ARTIFACT_BYTES - 1024 * 1024:
                raise ValueError("Artifact exceeds its reserved size budget")
            files.append({"file": path.relative_to(output).as_posix(), "bytes": len(payload), "sha256": data.sha(payload)})
    return {"files": files, "bytes": total, "approved_for_live": False}


def run(output, *, environ=None, clock=None, client=None, progress=None):
    at = (clock or (lambda: datetime.now(timezone.utc)))()
    if at.tzinfo is None or at.utcoffset() is None or at >= DEADLINE:
        raise ValueError("Expired/naive clock; no request made")
    provenance = context(os.environ if environ is None else environ)
    bound = bindings()
    client = client or data.ArchiveClient()
    if (client.max_requests, client.max_bytes, client.max_elapsed) != (data.MAX_REQUESTS, data.MAX_BYTES, data.MAX_ELAPSED):
        raise ValueError("Unapproved transport limits; no request made")
    output = Path(output)
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):
        raise ValueError("Symlinked output/ancestor refused")
    output.mkdir(parents=True, exist_ok=False)
    registration = {"purpose": PURPOSE, "registered_at": at.isoformat(), "deadline": DEADLINE.isoformat(),
                    "context": provenance, "bindings": bound, "specification": specification(),
                    "scope": "All history is retrospective development, NOT untouched OOS; legacy holdouts unchanged"}
    data.write_new(output / "registration.json", registration)
    with (output / "DATA_LICENSE.md").open("x") as stream:
        stream.write("# Research artifacts — attribution and restrictions\n\n"
                     "Source: Binance Vision / Binance public spot data. Not endorsed by Binance.\n\n"
                     "Downloaded data and derived data/models: CC BY-NC-SA 4.0, with the provider's dataset terms. "
                     "Personal non-commercial non-production research only; no live execution or commercialization. "
                     "Redistribution must preserve attribution, restrictions and share-alike obligations.\n\n"
                     "Terms checked 2026-10-03: " + data.TERMS + "\n\n"
                     "License: https://creativecommons.org/licenses/by-nc-sa/4.0/\n\n"
                     "Changes: checksum-verified CSV normalization, causal features, proxy labels, fitted forecast models "
                     "and retrospective benchmark reports. This notice concerns data/derived artifacts, not ownership of source code.\n")
    stage = "capture"
    result = {"status": "failed", "purpose": PURPOSE, "context": provenance,
              "models_fitted": 0, "approved_for_live": False, "portfolio_opened": False,
              "local_worker_migrated": False, "default_policy_changed": False, "retry": False}
    try:
        data.capture_reference(output / "capture", client=client, progress=progress)
        require_bindings(bound)
        stage = "raw-audit"
        audit, grouped = data.audit_reference(output / "capture")
        data.write_new(output / "data-audit.json", audit)
        stage = "training"
        require_bindings(bound)
        report = ml.run_research(grouped, output / "research", progress=progress)
        require_bindings(bound)
        stage = "research-audit"
        research_audit = ml.audit_research(grouped, output / "research")
        data.write_new(output / "research-audit.json", research_audit)
        require_bindings(bound)
        # Check size before writing successful final state; reserve room for result/inventory.
        inventory(output)
        result.update(status="verified_research", requests_verified=audit["requests_verified"],
                      csv_sha256=audit["csv_sha256"], selected_bars_per_symbol=audit["selected_bars_per_symbol"],
                      folds=len(report["folds"]), models_fitted=report["models_fitted"],
                      models_predictive_ready=report["models_predictive_ready"],
                      shortlist_cash_folds=report["shortlist_cash_folds"],
                      validation_brier_better_than_prior=report["validation_brier_better_than_prior"],
                      license_restricts_live_execution=True, portfolio_profit_evidence=False)
    except Exception as error:
        partial = []
        for path in (output / "research").glob("fold-*/*-model.json"):
            partial.append(json.loads(path.read_text())["model"])
        result.update(failed_stage=stage, error_type=type(error).__name__, http_status=getattr(error, "code", None),
                      requests_attempted=client.attempts, bytes_downloaded=client.bytes,
                      model_files_preserved=len(partial), models_fitted=sum(m["fitted"] for m in partial))
    data.write_new(output / "result.json", result)
    data.write_new(output / "inventory.json", inventory(output))
    return result


def audit_output(output):
    """Read-only verification of a downloaded run; does not refit or fetch data."""
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Symlinked artifact directory")
    saved = json.loads((output / "inventory.json").read_text())
    if saved != inventory(output):
        raise ValueError("Artifact inventory mismatch")
    registered = json.loads((output / "registration.json").read_text())
    if (registered["bindings"] != bindings()
            or data.canonical(registered["specification"]) != data.canonical(specification())):
        raise ValueError("Artifact source/specification mismatch")
    result = json.loads((output / "result.json").read_text())
    if result["context"] != registered["context"] or result["status"] != "verified_research":
        raise ValueError("Failed/unbound artifact is not a complete research run")
    audit, grouped = data.audit_reference(output / "capture")
    research_audit = ml.audit_research(grouped, output / "research")
    if (audit != json.loads((output / "data-audit.json").read_text())
            or research_audit != json.loads((output / "research-audit.json").read_text())
            or result["csv_sha256"] != audit["csv_sha256"]
            or result["models_fitted"] != research_audit["models_fitted"]
            or any(result.get(key) is not False for key in
                   ("approved_for_live", "portfolio_opened", "local_worker_migrated", "default_policy_changed", "portfolio_profit_evidence"))):
        raise ValueError("Artifact result/audit/safety flag mismatch")
    return {"status": "verified", "context": result["context"], "data": audit, "research": research_audit,
            "bytes_verified": saved["bytes"], "approved_for_live": False, "portfolio_profit_evidence": False}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=PURPOSE)
    parser.add_argument("--output", required=True, type=Path, help="NEW research directory only")
    parser.add_argument("--audit-only", action="store_true", help="Read-only local artifact verification; no HTTP/refit")
    args = parser.parse_args(argv)
    try:
        result = audit_output(args.output) if args.audit_only else run(args.output, progress=lambda s: print(s, flush=True))
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["status"] in {"verified", "verified_research"} else 2
    except Exception as error:
        print(json.dumps({"status": "refused", "error_type": type(error).__name__, "approved_for_live": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
