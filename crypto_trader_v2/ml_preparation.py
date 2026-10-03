"""Quality-only source audit and immutable, holdout-safe ML preparation.

No return, signal, label or model is computed here. Later research consumes only
the exported development CSV, never the reserved rows in the original source.
"""
from bisect import bisect_left
import csv
from dataclasses import asdict
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path

from .development import _write_json, load_development_source
from .domain import utc
from .importer import common_contiguous_runs, load_dataset, read_imported_source
from .ml_labels import LabelSpec, feature_names
from .ml_model import HYPERPARAMETERS, MODEL_SCHEMA, REQUIREMENTS, canonical
from .study import add_months


LEGACY_PROTOCOL = {"fit_months": 8, "calibration_months": 2, "approval_months": 2,
                   "validation_months": 3, "step_months": 3}
EXTENDED_PROTOCOL = {"fit_months": 24, "calibration_months": 6, "approval_months": 2,
                     "validation_months": 3, "step_months": 3}
SUPPORT_PROTOCOL = {"fit_months": 24, "calibration_months": 12, "approval_months": 3,
                    "validation_months": 2, "step_months": 2}
# Named, prespecified coverage experiments, not an arbitrary month optimizer.
# The support protocol responds to sparse calibration/approval observations;
# its results still reuse inspected development history, not fresh OOS data.
# Approval + validation stays five months, within the unchanged 180-day model
# lifetime. Widening approval to six months would expire models before testing.
PREPARATION_PROTOCOLS = {"extended": EXTENDED_PROTOCOL, "support": SUPPORT_PROTOCOL}
PREPARATION_PURPOSE = "protected extended ML development source"


def _code_hash():
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()


def ml_partition_boundaries(beginning, protocol):
    # Anchor both dates to the same opening day. Chaining calendar additions
    # would shift Jan 31 to Jan 30 after passing through a 30-day month.
    return (add_months(beginning, protocol["fit_months"]),
            add_months(beginning, protocol["fit_months"] + protocol["calibration_months"]))


def ml_windows(timeline, warmup, protocol):
    if (set(protocol) != set(LEGACY_PROTOCOL)
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in protocol.values())
            or protocol["step_months"] != protocol["validation_months"]):
        raise ValueError("Invalid chronological ML window protocol")
    training = sum(protocol[k] for k in ("fit_months", "calibration_months", "approval_months"))
    total = training + protocol["validation_months"]
    dates, cursor, windows = [b.start for b in timeline], timeline[0].start, []
    while add_months(cursor, total) <= timeline[-1].end:
        start, end, test_end = (bisect_left(dates, at) for at in
                                (cursor, add_months(cursor, training), add_months(cursor, total)))
        if end <= warmup or not start < end < test_end:
            raise ValueError("Insufficient chronological ML observations")
        windows.append((start, end, test_end))
        cursor = add_months(cursor, protocol["step_months"])
    if not windows:
        raise ValueError(f"ML protocol requires at least {total} continuous calendar months")
    return windows


def _quality(grouped, cfg):
    result = {}
    for symbol, by_time in grouped.items():
        bars = list(by_time.values())
        gaps = [{"after": a.end.isoformat(), "before": b.start.isoformat(),
                 "missing_candles": int((b.start - a.end).total_seconds()) // cfg.seconds}
                for a, b in zip(bars, bars[1:]) if a.end != b.start]
        result[symbol] = {"bars": len(bars), "gap_count": len(gaps),
                          "missing_candles": sum(g["missing_candles"] for g in gaps), "gaps": gaps}
    return result


def source_audit(directory, cfg):
    parent, grouped = read_imported_source(directory, cfg)
    quality = _quality(grouped, cfg)
    if parent.get("quality") != quality:
        raise ValueError("Imported quality manifest disagrees with verified candles")
    runs = common_contiguous_runs(grouped, cfg.seconds)
    synchronized = len({tuple(bars) for bars in grouped.values()}) == 1
    replay_ready = synchronized and all(q["gap_count"] == 0 and q["bars"] > cfg.warmup for q in quality.values())
    if parent.get("synchronized") is not synchronized or parent.get("replay_ready") is not replay_ready:
        raise ValueError("Imported replay flags disagree with verified candles")
    summary = {"schema": 1, "scope": "SOURCE INTEGRITY AND TIMELINE ONLY; not model or profit evidence",
               "dataset_directory": str(directory.resolve()), "dataset_sha256": parent["csv_sha256"],
               "manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
               "source": parent["source"], "synthetic": bool(parent.get("synthetic")),
               "start": parent["start"], "end_exclusive": parent["end"], "quality": quality,
               "synchronized": synchronized, "replay_ready": replay_ready,
               "common_contiguous_runs": [{"start": run[0].isoformat(),
                                            "end_exclusive": (run[-1] + timedelta(seconds=cfg.seconds)).isoformat(),
                                            "bars_per_symbol": len(run)} for run in runs],
               "gap_policy": "Never fabricate/forward-fill candles or join runs across gaps",
               "labels_computed": False, "models_trained": False, "approved_for_live": False}
    return summary, parent, grouped


def audit_dataset(directory, cfg, output=None):
    if output is not None and output.exists():
        raise ValueError("Audit output exists; use a new directory")
    summary, _, _ = source_audit(directory, cfg)
    if output is not None:
        output.mkdir(parents=True)
        _write_json(output / "report.json", summary)
    return summary


def prepare_ml_source(directory, cfg, protected_dataset, protected_registration, output, *, protocol_name="extended"):
    """Extend history while keeping the earlier registered boundary unchanged.

    Choose the longest continuous DEVELOPMENT run, earliest on ties. This is a
    coverage-based selection, not a performance search. The reserved suffix may
    contain a consumed holdout; preparation never declares it untouched OOS.
    """
    from .ml_study import APPROVAL

    if output.exists():
        raise ValueError("ML preparation output exists; use a new directory")
    if not isinstance(protocol_name, str) or protocol_name not in PREPARATION_PROTOCOLS:
        raise ValueError("Unknown registered ML preparation protocol")
    protocol = dict(PREPARATION_PROTOCOLS[protocol_name])
    audit, parent, grouped = source_audit(directory, cfg)
    if audit["synthetic"]:
        raise ValueError("ML preparation requires non-synthetic source data")
    old, raw_parent, previous, old_split = load_development_source(protected_dataset, cfg, protected_registration)
    if previous.get("config_hash", cfg.digest()) != cfg.digest():
        raise ValueError("Protected registration/config mismatch")
    # Check all original candles, not just timestamps or the development rows.
    # This verifies the extension did not rewrite the consumed historical source.
    for symbol, bars in old.bars.items():
        if any(grouped[symbol].get(bar.start) != bar for bar in bars):
            raise ValueError("Extended source changes or omits protected original candles")
    boundary = utc(previous["holdout_start"])
    development = {s: {at: bar for at, bar in bars.items() if bar.end <= boundary}
                   for s, bars in grouped.items()}
    runs = common_contiguous_runs(development, cfg.seconds)
    if not runs:
        raise ValueError("No common continuous development candles")
    selected = max(runs, key=len)
    if selected[-1] + timedelta(seconds=cfg.seconds) != boundary:
        raise ValueError("Longest development run must reach the protected boundary; not silently shortened")
    timeline = [grouped[next(iter(grouped))][at] for at in selected]
    windows = ml_windows(timeline, cfg.warmup, protocol)
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(("timestamp", "symbol", "open", "high", "low", "close", "volume"))
    for symbol in sorted(grouped):
        for at in selected:
            b = grouped[symbol][at]
            writer.writerow((b.start.isoformat(), symbol, b.open, b.high, b.low, b.close, b.volume))
    payload = text.getvalue().encode("utf-8")
    checksum = hashlib.sha256(payload).hexdigest()
    spec = LabelSpec().validate()
    plan = {"schema": 2, "purpose": PREPARATION_PURPOSE, "protocol_name": protocol_name, "protocol": protocol,
            "model_schema": MODEL_SCHEMA, "hyperparameters": HYPERPARAMETERS,
            "readiness_requirements": REQUIREMENTS, "label_spec": asdict(spec),
            "approval_criteria": APPROVAL,
            "feature_names": feature_names(tuple(i.symbol for i in cfg.instruments)),
            "config_hash": cfg.digest(), "config": asdict(cfg),
            "dataset_sha256": checksum, "development_dataset": "development",
            "development_start": selected[0].isoformat(), "development_end_exclusive": boundary.isoformat(),
            "development_bars_per_symbol": len(selected), "fold_indices": windows,
            "parent_registration": str(protected_registration.resolve()),
            "parent_registration_sha256": hashlib.sha256(raw_parent).hexdigest(),
            "protected_original_dataset_sha256": old.checksum,
            "original_development_bars_per_symbol": old_split,
            "original_excluded_holdout_bars_per_symbol": len(next(iter(old.bars.values()))) - old_split,
            "original_candles_match": True, "extended_source_audit": audit,
            "source_manifest": parent,
            "selection_policy": "Longest common continuous development run; earliest on ties; only timeline coverage",
            "omitted_development_bars_per_symbol": {s: len(bars) - len(selected) for s, bars in development.items()},
            "reserved_bars_per_symbol": {s: sum(b.start >= boundary for b in bars.values()) for s, bars in grouped.items()},
            "reserved_data_status": "Excluded, NOT certified untouched; includes a previously consumed holdout",
            "data_status": "RETROSPECTIVE DEVELOPMENT; extended training history, not new untouched OOS evidence",
            "code_hash": _code_hash(),
            "labels_computed": False, "models_trained": False, "holdout_evaluated": False,
            "approved_for_live": False, "default_policy_changed": False}
    output.mkdir(parents=True)
    # Register the period and protocol before any downstream feature/label work.
    _write_json(output / "registration.json", {"plan": plan, "sha256": hashlib.sha256(canonical(plan)).hexdigest()})
    destination = output / "development"
    destination.mkdir()
    with (destination / "candles.csv").open("xb") as stream:
        stream.write(payload)
    manifest = {**parent, "start": selected[0].isoformat(), "end": boundary.isoformat(),
                "csv_sha256": checksum, "synchronized": True, "replay_ready": True,
                "quality": {s: {"bars": len(selected), "gap_count": 0, "missing_candles": 0, "gaps": []} for s in grouped},
                "subset": {"policy": plan["selection_policy"], "parent_csv_sha256": parent["csv_sha256"],
                           "parent_directory": str(directory.resolve()),
                           "development_only": True, "reserved_suffix_excluded": True,
                           "limitation": "Earlier incomplete regimes are omitted; not a full-archive replay"}}
    _write_json(destination / "manifest.json", manifest)
    _write_json(output / "report.json", {key: plan[key] for key in
                ("development_start", "development_end_exclusive", "development_bars_per_symbol", "protocol_name", "protocol",
                 "original_development_bars_per_symbol", "reserved_bars_per_symbol", "reserved_data_status",
                 "omitted_development_bars_per_symbol", "labels_computed", "models_trained", "approved_for_live")}
                | {"complete_windows": len(windows), "registration": str(output / "registration.json")})
    return plan


def load_prepared_ml_source(directory, cfg, registration):
    from .ml_study import APPROVAL

    envelope = json.loads(registration.read_bytes())
    if (set(envelope) != {"plan", "sha256"}
            or hashlib.sha256(canonical(envelope["plan"])).hexdigest() != envelope["sha256"]):
        raise ValueError("Prepared ML registration checksum mismatch")
    plan = envelope["plan"]
    # Older preparations had no name and supported only the extended preset.
    name = plan.get("protocol_name", "extended")
    protocol = PREPARATION_PROTOCOLS.get(name) if isinstance(name, str) else None
    if (plan.get("schema") != 2 or plan.get("purpose") != PREPARATION_PURPOSE
            or plan.get("config_hash") != cfg.digest() or protocol is None or plan.get("protocol") != protocol
            or plan.get("model_schema") != MODEL_SCHEMA or plan.get("hyperparameters") != HYPERPARAMETERS
            or plan.get("readiness_requirements") != REQUIREMENTS
            or plan.get("approval_criteria") != APPROVAL or plan.get("code_hash") != _code_hash()
            or canonical(plan.get("label_spec")) != canonical(asdict(LabelSpec()))
            or plan.get("feature_names") != list(feature_names(tuple(i.symbol for i in cfg.instruments)))
            or plan.get("development_dataset") != "development" or plan.get("original_candles_match") is not True
            or any(plan.get(flag) is not False for flag in
                   ("labels_computed", "models_trained", "holdout_evaluated", "approved_for_live", "default_policy_changed"))):
        raise ValueError("Prepared ML protocol/config mismatch")
    if directory.resolve() != (registration.parent / "development").resolve():
        raise ValueError("Prepared ML dataset must be bound to its registration directory")
    raw_parent = Path(plan["parent_registration"]).read_bytes()
    if hashlib.sha256(raw_parent).hexdigest() != plan["parent_registration_sha256"]:
        raise ValueError("Protected parent registration changed")
    previous = json.loads(raw_parent)
    if (previous.get("dataset_sha256") != plan["protected_original_dataset_sha256"]
            or utc(previous["holdout_start"]) != utc(plan["development_end_exclusive"])):
        raise ValueError("Prepared ML boundary differs from protected registration")
    dataset = load_dataset(directory, cfg)
    timeline = next(iter(dataset.bars.values()))
    if (dataset.synthetic or dataset.checksum != plan["dataset_sha256"]
            or timeline[0].start != utc(plan["development_start"])
            or timeline[-1].end != utc(plan["development_end_exclusive"])
            or len(timeline) != plan["development_bars_per_symbol"]
            or canonical(ml_windows(timeline, cfg.warmup, protocol)) != canonical(plan["fold_indices"])):
        raise ValueError("Prepared ML source/boundary/window mismatch")
    return dataset, plan
