"""Read-only independent artifact/causality/refit/accounting checks.

Run with the same one-thread numerical environment as the research worker.
This audit never ranks variants by validation returns or grants live access.
"""
import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crypto_trader_v2.config import load_config
from crypto_trader_v2.development import _write_json
from crypto_trader_v2.domain import utc
from crypto_trader_v2.entry_policy import policies
from crypto_trader_v2.ml_batch import load_batch, summarize_reports
from crypto_trader_v2.ml_labels import LabelSpec, build_samples, purged_partition
from crypto_trader_v2.ml_model import canonical, fit_model, load_model, prediction_metrics
from crypto_trader_v2.ml_policy import MLEntryPolicy
from crypto_trader_v2.ml_preparation import load_prepared_ml_source, ml_partition_boundaries
from crypto_trader_v2.ml_study import approval_reasons, score_candidates
from crypto_trader_v2.policy_study import _prefix_dataset
from crypto_trader_v2.report import build_report


TOLERANCE = D("1e-20")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def equivalent(actual, expected):
    """Exact identities/Decimals; 1e-12 only for numerical forecast fields."""
    if isinstance(expected, dict):
        require(set(actual) == set(expected), "Audit dictionary fields differ")
        for key in expected:
            equivalent(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        require(len(actual) == len(expected), "Audit sequence lengths differ")
        for left, right in zip(actual, expected):
            equivalent(left, right)
    elif isinstance(expected, float):
        require(isinstance(actual, (float, int)) and abs(actual - expected) <= 1e-12, "Numerical forecast mismatch")
    elif isinstance(expected, D):
        require(abs(D(str(actual)) - expected) <= TOLERANCE, "Accounting amount mismatch")
    else:
        require(actual == expected, "Audit identity/value mismatch")


def ledger_check(path, cfg, dataset, end, metrics, model=None, *, prefix=None, policy_name=None):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity failure")
        require(not db.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign key failure")
        meta = {r["key"]: r["value"] for r in db.execute("SELECT * FROM metadata")}
        prefix = prefix or _prefix_dataset(dataset, end)
        require(meta["mode"] == "BACKTEST" and meta["config_hash"] == cfg.digest(), "Ledger config/mode mismatch")
        require(meta["dataset_hash"] == prefix.checksum, "Ledger source prefix mismatch")
        require(utc(meta["evaluation_end"]) == next(iter(prefix.bars.values()))[-1].end, "Ledger ending boundary mismatch")
        if model is not None:
            equivalent(json.loads(meta["entry_policy"]), json.loads(canonical(MLEntryPolicy(model).payload())))
        elif policy_name is not None:
            equivalent(json.loads(meta["entry_policy"]), json.loads(canonical(policies()[policy_name].payload())))
        balances, totals = defaultdict(D), defaultdict(D)
        for row in db.execute("SELECT * FROM ledger"):
            balances[(row["event_id"], row["asset"])] += D(row["delta"])
            totals[(row["account"], row["asset"])] += D(row["delta"])
        require(all(abs(v) <= TOLERANCE for v in balances.values()), "Double-entry imbalance")
        require(abs(totals[("cash", cfg.quote_currency)] - D(meta["cash"])) <= TOLERANCE, "Cash reconciliation failed")
        inventory = {r["symbol"].split("/")[0]: D(r["quantity"]) for r in db.execute("SELECT * FROM positions")}
        assets = set(inventory) | {asset for account, asset in totals if account == "inventory"}
        require(all(abs(totals[("inventory", asset)] - inventory.get(asset, D(0))) <= TOLERANCE for asset in assets),
                "Inventory reconciliation failed")
        if meta.get("schema") == "4":
            parked = defaultdict(D)
            for row in db.execute("SELECT * FROM dust_lots"):
                parked[row["symbol"].split("/")[0]] += D(row["quantity"])
            assets = set(parked) | {asset for account, asset in totals if account == "dust_inventory"}
            require(all(abs(totals[("dust_inventory", asset)] - parked.get(asset, D(0))) <= TOLERANCE for asset in assets),
                    "Dust reconciliation failed")
        start, stop = utc(meta["evaluation_start"]), utc(meta["evaluation_end"])
        for table, column in (("orders", "created_at"), ("fills", "at"), ("snapshots", "at"), ("decisions", "at")):
            require(all(start <= utc(r[0]) <= stop for r in db.execute(f"SELECT {column} FROM {table}")),
                    "Ledger event outside protected replay interval")
        report = build_report(db)
        for key in ("net_return_pct", "max_drawdown_pct", "closed_episodes", "profit_factor", "net_expectancy_quote", "fees_paid_quote_equivalent"):
            equivalent(metrics[key], report[key])
        equivalent(metrics["open_positions"], len(report["open_positions"]))
        equivalent(metrics["start"], meta["evaluation_start"])
        equivalent(metrics["end"], meta["evaluation_end"])
        scenario = json.loads(meta["execution_scenario"])
        half = cfg.costs.simulated_spread / 2
        liquidation = report["cash"]
        for symbol, quantity in report["inventory_by_symbol"].items():
            price = prefix.bars[symbol][-1].close * (1 - half) * (1 - cfg.costs.slippage - D(scenario["extra_slippage"]))
            if cfg.venue == "binance_th":
                instrument = next(i for i in cfg.instruments if i.symbol == symbol)
                quantity = instrument.round_quantity(quantity)
                if (quantity < instrument.min_quantity or quantity * price < instrument.min_notional or
                        (instrument.max_quantity is not None and quantity > instrument.max_quantity) or
                        (instrument.max_notional is not None and quantity * price > instrument.max_notional)):
                    quantity = D(0)
            liquidation += quantity * price * (1 - cfg.costs.taker_fee)
        equivalent(metrics["estimated_liquidation_return_pct"], (liquidation / cfg.initial_cash - 1) * 100)
        return {"path": str(path), "orders": db.execute("SELECT count(*) FROM orders").fetchone()[0],
                "fills": db.execute("SELECT count(*) FROM fills").fetchone()[0], "integrity": "ok"}


def distribution(scored):
    predictions = [r["prediction"] for r in scored if r["prediction"]["probability"] is not None]
    result = {"candidates": len(scored), "gate_reasons": dict(Counter(r["gate_reason"] for r in scored)),
              "passing_candidates": sum(r["gate_pass_ignoring_portfolio_constraints"] for r in scored)}
    for field in ("probability", "stress_return_pct"):
        result[field + "_quantiles"] = dict(zip(("min", "q25", "median", "q75", "max"),
                                                map(float, np.quantile([p[field] for p in predictions], [0, .25, .5, .75, 1])))) if predictions else None
    return result


def verify_batch(batch, cfg, output):
    require(not output.exists(), "Audit output exists; use a new directory")
    plan = load_batch(batch, cfg)
    complete = json.loads((batch / "report.json").read_bytes())
    require(complete["status"] == "complete" and complete["completed_trials"] == plan["maximum_trials"], "Batch is not complete")
    dataset, prepared = load_prepared_ml_source(Path(plan["dataset_directory"]), cfg, Path(plan["source_registration"]))
    spec = LabelSpec()
    output.mkdir(parents=True)
    _write_json(output / "registration.json", {"scope": "read-only reproduction, not model selection",
                "batch_registration_sha256": hashlib.sha256((batch / "registration.json").read_bytes()).hexdigest(),
                "batch_report_sha256": hashlib.sha256((batch / "report.json").read_bytes()).hexdigest(),
                "auditor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "forecast_tolerance": "1e-12", "ledger_tolerance": str(TOLERANCE), "approved_for_live": False})
    samples = build_samples(dataset, cfg, spec)
    timeline = next(iter(dataset.bars.values()))
    prefixes = {index: _prefix_dataset(dataset, index) for index in sorted({i for _, end, test_end in prepared["fold_indices"] for i in (end, test_end)})}
    reports, databases, forecasts = [], [], []
    refit_identical, refit_close = 0, 0
    for variant in plan["trial_order"]:
        folder = batch / variant
        report = json.loads((folder / "report.json").read_bytes())
        checkpoint = json.loads((batch / f"checkpoint-{len(reports) + 1:02d}.json").read_bytes())
        require(checkpoint["variant"] == variant and checkpoint["report_sha256"] == hashlib.sha256((folder / "report.json").read_bytes()).hexdigest(),
                "Batch checkpoint/report mismatch")
        require(report["registration"]["variant"] == variant and report["registration"]["code_hash"] == plan["code_hash"], "Trial identity mismatch")
        require(len(report["folds"]) == len(prepared["fold_indices"]), "Trial fold count mismatch")
        require(not report["approved_for_live"] and not report["holdout_evaluated"] and not report["default_policy_changed"], "Unexpected live/holdout/default change")
        expected_features = [{"id": s.id, "symbol": s.symbol, "feature_at": s.feature_at,
                              "entry_index": s.entry_index, "features": s.features} for s in samples]
        equivalent([json.loads(line) for line in (folder / "features.jsonl").read_text().splitlines()], json.loads(canonical(expected_features)))
        expected_outcomes = [{"id": s.id, "label_end": s.label_end, "status": "labelled" if s.labelled else "censored",
                              "baseline": asdict(s.baseline) if s.labelled else None,
                              "adverse": asdict(s.adverse) if s.labelled else None} for s in samples]
        equivalent([json.loads(line) for line in (folder / "outcomes.jsonl").read_text().splitlines()], json.loads(canonical(expected_outcomes)))
        for number, ((start, end, test_end), fold) in enumerate(zip(prepared["fold_indices"], report["folds"]), 1):
            label = f"fold-{number:02d}"
            require(fold["fold"] == label, "Fold identity mismatch")
            fit_end, cal_end = ml_partition_boundaries(timeline[start].start, prepared["protocol"])
            approval_end, validation_end = timeline[end].start, timeline[test_end - 1].end
            fit, fit_scope = purged_partition(samples, timeline[start].start, fit_end, cfg, spec)
            calibration, cal_scope = purged_partition(samples, fit_end, cal_end, cfg, spec)
            approval, approval_scope = purged_partition(samples, cal_end, approval_end, cfg, spec)
            validation, validation_scope = purged_partition(samples, approval_end, validation_end, cfg, spec)
            equivalent(fold["partitions"], json.loads(canonical(dict(fit=fit_scope, calibration=cal_scope, approval=approval_scope, validation=validation_scope))))
            model = load_model(folder / f"{label}-model.json")
            require(model.digest == fold["model_sha256"] and model.config_hash == cfg.digest(), "Frozen model identity mismatch")
            require(fold["selection"]["locked_before_validation"] is True and fold["selection"]["model_sha256"] == model.digest,
                    "Approval lock/model binding mismatch")
            refit = fit_model(fit, calibration, cfg, spec, cal_end, variant=variant)
            if model.digest == refit.digest:
                refit_identical += 1
            else:
                equivalent(model.payload(), refit.payload())
                refit_close += 1
            approval_forecast = prediction_metrics(approval, model)
            equivalent(fold["selection"]["approval_forecast"], approval_forecast)
            equivalent(fold["validation_forecast"], prediction_metrics(validation, model))
            scored = score_candidates(approval, model)
            equivalent(json.loads((folder / f"{label}-approval-predictions.json").read_bytes()), json.loads(canonical(scored)))
            candidates = [s for s in samples if approval_end <= s.feature_at < validation_end]
            equivalent(json.loads((folder / f"{label}-validation-predictions.json").read_bytes()), json.loads(canonical(score_candidates(candidates, model))))
            metrics = fold["selection"]["approval_portfolios"]
            converted = {name: {key: D(value) if key in {"profit_factor", "net_expectancy_quote", "estimated_liquidation_return_pct", "max_drawdown_pct"}
                                and value is not None else value for key, value in row.items()} for name, row in metrics.items()}
            require(approval_reasons(model, approval_forecast, scored, converted) == fold["selection"]["failure_reasons"], "Approval failure reasons mismatch")
            require(fold["selection"]["selected_policy"] == ("cash" if fold["selection"]["failure_reasons"] else "ml"), "Approval selection mismatch")
            forecasts.append({"variant": variant, "fold": label, "approval": distribution(scored),
                              "validation": distribution(score_candidates(candidates, model)),
                              "fit_prior": model.summary.get("fit_positive_prior"), "calibration": model.calibration})
            for name, row in metrics.items():
                databases.append(ledger_check(folder / f"{label}-approval-{name}.sqlite", cfg, dataset, end, row, model, prefix=prefixes[end]))
            for name, row in fold["validation"].items():
                databases.append(ledger_check(folder / f"{label}-validation-{name}.sqlite", cfg, dataset, test_end, row,
                                               model if name.startswith("ml") else None, prefix=prefixes[test_end],
                                               policy_name=name if name in {"baseline", "cash"} else None))
            print(f"Verified {variant}/{label}: causal refit, forecasts, approval, six read-only ledgers", flush=True)
        reports.append((variant, report))
    summaries, choices = summarize_reports(reports)
    equivalent(complete["trials"], summaries)
    equivalent(complete["fold_choices"], choices)
    result = {"status": "verified", "verified_at": datetime.now(timezone.utc).isoformat(), "source_candidates": len(samples),
              "models_exact_refit_hash": refit_identical, "models_refit_numerically_equivalent_only": refit_close,
              "read_only_ledgers_verified": len(databases), "databases": databases, "forecasts": forecasts,
              "cash_fold_choices": sum(row["choice"] == "cash" for row in choices),
              "protected_end_exclusive": prepared["development_end_exclusive"],
              "approved_for_live": False, "holdout_evaluated": False,
              "limitations": "Reproducibility/accounting audit is not independent profit evidence; forecasts and labels overlap"}
    _write_json(output / "report.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify_batch(args.batch, load_config(args.config), args.output)
    print(json.dumps({key: result[key] for key in ("status", "source_candidates", "models_exact_refit_hash",
                                                  "models_refit_numerically_equivalent_only", "read_only_ledgers_verified",
                                                  "cash_fold_choices", "approved_for_live")}, indent=2))
