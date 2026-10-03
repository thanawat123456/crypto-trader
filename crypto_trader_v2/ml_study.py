"""Registered offline ML research; chronological fit/calibrate/approve/validate."""
from bisect import bisect_left
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .broker import ExecutionScenario
from .development import _write_json, load_development_source
from .diagnostics import export_diagnostics, read_diagnostics
from .domain import utc
from .entry_policy import policies
from .ml_labels import LabelSpec, build_samples, feature_names, purged_partition
from .ml_model import HYPERPARAMETERS, MODEL_SCHEMA, REQUIREMENTS, fit_model, prediction_metrics
from .ml_policy import MLEntryPolicy
from .ml_variants import INTERACTION_SCHEMA, mapped_names, variant_spec
from .ml_preparation import LEGACY_PROTOCOL, load_prepared_ml_source, ml_partition_boundaries, ml_windows
from .policy_study import _metrics, _prefix_dataset
from .research import run_backtest


APPROVAL = {"minimum_labelled_candidates": 20, "minimum_gate_pass_candidates": 10,
            "minimum_closed_portfolio_episodes_each_scenario": 10, "minimum_profit_factor": "1.20",
            "maximum_drawdown_pct": "6", "positive_net_return_and_expectancy_both_cost_scenarios": True,
            "positive_mean_adverse_label_for_gate_pass": True,
            "brier_and_log_loss_no_worse_than_fit_prior": True,
            "fallback": "cash", "live_approval": "never in this command"}


def score_candidates(samples, model):
    rows = []
    for sample in samples:
        prediction = model.predict(sample.features)
        in_time = utc(model.inference_from) <= utc(sample.feature_at) < utc(model.expires_at)
        passed = (model.ready and in_time and not prediction["ood"]
                  and prediction["probability"] >= HYPERPARAMETERS["probability_threshold"]
                  and prediction["stress_return_pct"] > HYPERPARAMETERS["minimum_stress_return_pct"])
        reason = ("evidence_insufficient" if not model.ready else "model_not_available_or_expired" if not in_time
                  else "out_of_distribution" if prediction["ood"] else "low_net_profit_probability"
                  if prediction["probability"] < HYPERPARAMETERS["probability_threshold"]
                  else "insufficient_expected_stress_return" if prediction["stress_return_pct"] <= HYPERPARAMETERS["minimum_stress_return_pct"]
                  else "pass")
        rows.append({"id": sample.id, "symbol": sample.symbol, "feature_at": sample.feature_at,
                     "gate_reason": reason,
                     "gate_pass_ignoring_portfolio_constraints": bool(passed), "prediction": prediction,
                     "label_status": "labelled" if sample.labelled else "censored",
                     "adverse_net_return_pct": sample.adverse.net_return_pct if sample.labelled else None})
    return rows


def approval_reasons(model, forecast, scored, portfolios):
    reasons = list(model.readiness_reasons)
    if forecast["scored_samples"] < APPROVAL["minimum_labelled_candidates"]:
        reasons.append("approval_label_sample_count")
    passing = [r for r in scored if r["gate_pass_ignoring_portfolio_constraints"]]
    if len(passing) < APPROVAL["minimum_gate_pass_candidates"]:
        reasons.append("approval_gate_pass_sample_count")
    if not passing or sum(Decimal(str(r["adverse_net_return_pct"])) for r in passing) <= 0:
        reasons.append("approval_gate_pass_adverse_expectancy")
    for score, baseline in (("brier", "constant_brier"), ("log_loss", "constant_log_loss")):
        if forecast[score] is None or forecast[score] > forecast[baseline]:
            reasons.append("approval_" + score + "_not_better_than_prior")
    for name, metrics in portfolios.items():
        if (metrics["closed_episodes"] < APPROVAL["minimum_closed_portfolio_episodes_each_scenario"]
                or metrics["profit_factor"] is None or metrics["profit_factor"] < Decimal(APPROVAL["minimum_profit_factor"])
                or metrics["net_expectancy_quote"] is None or metrics["net_expectancy_quote"] <= 0
                or metrics["estimated_liquidation_return_pct"] <= 0
                or metrics["max_drawdown_pct"] > Decimal(APPROVAL["maximum_drawdown_pct"])):
            reasons.append("approval_portfolio_" + name)
    return sorted(set(reasons))


def run_ml_study(directory, cfg, registration, output, *, progress=None, variant="linear-v1"):
    registered = variant_spec(variant)
    if output.exists():
        raise ValueError("ML experiment output exists; use a new directory")
    raw = registration.read_bytes()
    if "plan" in json.loads(raw):
        dataset, prepared = load_prepared_ml_source(directory, cfg, registration)
        development = dataset
        split = len(next(iter(dataset.bars.values())))
        development_end = prepared["development_end_exclusive"]
        excluded = prepared["original_excluded_holdout_bars_per_symbol"]
        protocol = prepared["protocol"]
    else:
        dataset, raw, previous, split = load_development_source(directory, cfg, registration)
        development = _prefix_dataset(dataset, split)
        development_end = previous["holdout_start"]
        excluded = len(next(iter(dataset.bars.values()))) - split
        protocol = LEGACY_PROTOCOL
    timeline = next(iter(development.bars.values()))
    windows = ml_windows(timeline, cfg.warmup, protocol)
    spec = LabelSpec().validate()
    symbols = tuple(i.symbol for i in cfg.instruments)
    output.mkdir(parents=True)
    plan = {"schema": 1, "model_schema": MODEL_SCHEMA if variant == "linear-v1" else INTERACTION_SCHEMA,
            "variant": variant, "variant_spec": registered,
            "model_family": "weighted L2 logistic + sigmoid calibration + ridge stress-net-return",
            "feature_names": mapped_names(symbols, variant), "raw_feature_names": feature_names(symbols), "label_spec": asdict(spec),
            "hyperparameters": {**HYPERPARAMETERS, "logistic_l2": registered["logistic_l2"], "ridge_l2": registered["ridge_l2"]},
            "readiness_requirements": REQUIREMENTS, "approval_criteria": APPROVAL,
            "dataset_sha256": dataset.checksum, "development_prefix_json_sha256": development.checksum,
            "parent_registration": str(registration.resolve()), "parent_registration_sha256": hashlib.sha256(raw).hexdigest(),
            "development_end_exclusive": development_end, "development_bars_per_symbol": split,
            "excluded_holdout_bars_per_symbol": excluded,
            "source_manifest": json.loads((directory / "manifest.json").read_text()),
            "config": asdict(cfg), "config_hash": cfg.digest(), "fold_indices": windows,
            **protocol,
            "purging": "fixed complete horizon + one-bar gap before each chronological boundary, global across symbols",
            "label_semantics": "independent one-gross-unit counterfactual, venue-native proportional fees, strategy exits + 42-bar holding limit; ignores sized portfolio/dust/minimums/liquidity; not a ledger trade",
            "label_scope": "ALL fresh breakout candidates including signals never executed by the old portfolio; final incomplete horizons censored",
            "data_status": "Previously inspected retrospective development; not fresh OOS or live execution evidence",
            "code_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(),
            "holdout_evaluated": False, "approved_for_live": False, "default_policy_changed": False}
    _write_json(output / "registration.json", plan)

    def say(message):
        if progress:
            progress(message)

    say("Building causal features and separate counterfactual labels; original holdout excluded")
    samples = build_samples(development, cfg, spec)
    with (output / "features.jsonl").open("x") as features, (output / "outcomes.jsonl").open("x") as outcomes:
        for sample in samples:
            features.write(json.dumps({"id": sample.id, "symbol": sample.symbol, "feature_at": sample.feature_at,
                                       "entry_index": sample.entry_index, "features": sample.features}, default=str) + "\n")
            outcomes.write(json.dumps({"id": sample.id, "label_end": sample.label_end,
                                       "status": "labelled" if sample.labelled else "censored",
                                       "baseline": asdict(sample.baseline) if sample.labelled else None,
                                       "adverse": asdict(sample.adverse) if sample.labelled else None}, default=str) + "\n")
    folds, dates = [], [bar.start for bar in timeline]
    for number, (start, end, test_end) in enumerate(windows, 1):
        label = f"fold-{number:02d}"
        beginning = timeline[start].start
        fit_end, cal_end = ml_partition_boundaries(beginning, protocol)
        approval_end, validation_end = timeline[end].start, timeline[test_end - 1].end
        fit, fit_scope = purged_partition(samples, beginning, fit_end, cfg, spec)
        calibration, cal_scope = purged_partition(samples, fit_end, cal_end, cfg, spec)
        approval, approval_scope = purged_partition(samples, cal_end, approval_end, cfg, spec)
        say(f"{label}: fitting {len(fit)} samples; calibrating {len(calibration)}; approval labels {len(approval)}")
        model = fit_model(fit, calibration, cfg, spec, cal_end, variant=variant)
        model.save(output / f"{label}-model.json")
        ml_policy = MLEntryPolicy(model, spec.adverse_slippage)
        forecast = prediction_metrics(approval, model)
        scored = score_candidates(approval, model)
        _write_json(output / f"{label}-approval-predictions.json", scored)
        approval_data = _prefix_dataset(development, end)
        portfolios = {}
        for name, execution in (("base", ExecutionScenario()), ("adverse", ExecutionScenario(extra_slippage=spec.adverse_slippage))):
            say(f"{label}: chronological approval portfolio {name}; model-ready {model.ready}")
            result = run_backtest(approval_data, cfg, output / f"{label}-approval-{name}.sqlite",
                                  start_index=bisect_left(dates, cal_end), entry_policy=ml_policy, execution=execution,
                                  analysis_scope="RETROSPECTIVE DEVELOPMENT APPROVAL; only past fit/calibration labels")
            portfolios[name] = _metrics(result)
        reasons = approval_reasons(model, forecast, scored, portfolios)
        selected = "cash" if reasons else "ml"
        selection = {"fold": label, "selected_policy": selected, "model_sha256": model.digest,
                     "locked_before_validation": True, "failure_reasons": reasons,
                     "approval_forecast": forecast, "approval_portfolios": portfolios}
        _write_json(output / f"{label}-selection.json", selection)
        # No validation score/outcome is consulted before this immutable choice.
        validation, validation_scope = purged_partition(samples, approval_end, validation_end, cfg, spec)
        candidates = [s for s in samples if approval_end <= s.feature_at < validation_end]
        validation_forecast = prediction_metrics(validation, model)
        _write_json(output / f"{label}-validation-predictions.json", score_candidates(candidates, model))
        validation_data = _prefix_dataset(development, test_end)
        results = {}
        for name, policy, execution in (("baseline", policies()["baseline"], ExecutionScenario()),
                                        ("ml", ml_policy, ExecutionScenario()),
                                        ("ml_adverse", ml_policy, ExecutionScenario(extra_slippage=spec.adverse_slippage)),
                                        ("cash", policies()["cash"], ExecutionScenario())):
            say(f"{label}: retrospective validation {name}; locked choice {selected}")
            path = output / f"{label}-validation-{name}.sqlite"
            replay = run_backtest(validation_data, cfg, path, start_index=end, entry_policy=policy, execution=execution,
                                  analysis_scope="RETROSPECTIVE ML DEVELOPMENT VALIDATION; original holdout excluded")
            diagnostic = read_diagnostics(path)
            export_diagnostics(diagnostic, output / f"{label}-{name}")
            results[name] = {**_metrics(replay), "entry_reasons": diagnostic["entry_evaluation_reasons"]}
        fold = {"fold": label, "partitions": {"fit": fit_scope, "calibration": cal_scope,
                                                "approval": approval_scope, "validation": validation_scope},
                "model_fitted": model.fitted, "model_ready": model.ready, "model_sha256": model.digest,
                "model_readiness_reasons": model.readiness_reasons, "selection": selection,
                "validation_forecast": validation_forecast, "validation": results,
                "selected_validation": results[selected]}
        folds.append(fold)
        _write_json(output / f"{label}.json", fold)
    report = {"experiment": str(output), "scope": "RETROSPECTIVE ML DEVELOPMENT; NOT untouched OOS",
              "registration": plan, "candidate_samples": len(samples), "labelled_samples": sum(s.labelled for s in samples),
              "censored_samples": sum(not s.labelled for s in samples), "folds": folds,
              "fitted_models": sum(f["model_fitted"] for f in folds), "ready_models": sum(f["model_ready"] for f in folds),
              "selected_cash_windows": sum(f["selection"]["selected_policy"] == "cash" for f in folds),
              "holdout_evaluated": False, "approved_for_live": False, "default_policy_changed": False,
              "limitations": ["Previous inspection makes all reported periods retrospective, not untouched evidence",
                              "Independent filled-trade labels ignore wallet/minimum/portfolio/queue constraints; portfolio replays are separate",
                              "ML adds its registered holding limit; baseline strategy/exits/risk limits are unchanged",
                              "Overlapping labels and cross-asset correlations remain; concurrency weights/Kish ESS do not prove independence",
                              "Uncalibrated/low-support/expired/OOD models abstain; numerical fit does not mean reliable intelligence",
                              "Brier/log-loss/binned calibration measure limited conditional evidence, not assured future calibration",
                              "Fixed registered model variant; no validation-driven retuning or automatic learning/deployment",
                              "Configured account fees and execution assumptions are unverified; real money remains unavailable"]}
    _write_json(output / "report.json", report)
    lines = ["# ML entry research — retrospective development", "", "Original holdout excluded. Live approved: False. Default unchanged.", "",
             f"Candidates: {len(samples)}; labelled: {report['labelled_samples']}; censored: {report['censored_samples']}.", "",
             "| Fold | Fit / calibration labels | Model fitted / ready | Locked choice | Validation Brier / constant |",
             "| --- | --- | --- | --- | --- |"]
    for fold in folds:
        parts, metrics = fold["partitions"], fold["validation_forecast"]
        lines.append(f"| {fold['fold']} | {parts['fit']['labelled_kept']} / {parts['calibration']['labelled_kept']} | {fold['model_fitted']} / {fold['model_ready']} | {fold['selection']['selected_policy']} | {metrics['brier']} / {metrics['constant_brier']} |")
    lines += ["", "| Fold | Policy | Estimated liquidation return (%) | Closed episodes |", "| --- | --- | ---: | ---: |"]
    for fold in folds:
        for name, metrics in fold["validation"].items():
            lines.append(f"| {fold['fold']} | {name} | {metrics['estimated_liquidation_return_pct']:.4f} | {metrics['closed_episodes']} |")
    lines += ["", "## Limits", ""] + ["- " + item for item in report["limitations"]]
    with (output / "report.md").open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    return report
