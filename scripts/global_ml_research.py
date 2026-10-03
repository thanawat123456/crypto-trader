"""Dense, causal, cross-asset price-forecast benchmark; NOT an execution model.

Separate schema/license/protocol from the existing breakout-entry research.
No portfolio, account balance, order generation, deployment or native fees.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
import math
import json
from pathlib import Path
import time

import numpy as np

from crypto_trader_v2.ml_model import REQUIREMENTS, _logistic, sigmoid
from crypto_trader_v2.study import add_months
from scripts.global_reference_data import LICENSE, SECONDS, SYMBOLS, canonical, sha, write_new


SCHEMA = "global-reference-forward-forecast-v1"
FEATURES = ("return_1", "return_6", "return_42", "return_180", "other_return_6", "other_return_42",
            "volatility_6", "volatility_42", "atr_14_pct", "log_volume_ratio_20", "volume_missing",
            "efficiency_42", "close_range_position", "high_low_pct", "ma_200_distance", "ma_200_slope_10",
            "symbol:BTC/USDT", "symbol:ETH/USDT")
INTERACTIONS = ((1, 4), (2, 8), (1, 9), (2, 5), (7, 14))
VARIANTS = ("linear", "interactions")
PROTOCOL = {"fit_months": 24, "calibration_months": 12, "approval_months": 3,
            "validation_months": 2, "step_months": 2}
SPEC = {"warmup_bars": 210, "sampling_stride_bars": 6, "holding_bars": 42, "embargo_bars": 1,
        "label": "one-gross-unit fixed-horizon long; next opening to opening 42 bars later; NOT ledger/fill evidence",
        "fee_each_leg": 0.001, "spread": 0.001, "slippage_each_leg": 0.0005,
        "extra_adverse_slippage_each_leg": 0.001,
        "cost_status": "UNVERIFIED REFERENCE STRESS ASSUMPTIONS, NOT account/historical/native costs"}
PARAMETERS = {"logistic_l2": 0.1, "ridge_l2": 1.0, "calibration_l2": 0.01,
              "probability_threshold": 0.60, "minimum_stress_return_pct": 0.25,
              "ood_standard_deviations": 6.0, "standardized_clip": 8.0,
              "maximum_calibration_ood_fraction": 0.20, "expires_days": 180}
SHORTLIST = {"minimum_labels": 20, "minimum_gate_pass": 10,
             "brier_and_log_loss_no_worse_than_fit_prior": True,
             "positive_mean_gate_pass_stress_label": True,
             "fixed_priority": VARIANTS, "fallback": "cash",
             "scope": "FORECAST RESEARCH SHORTLIST ONLY; no portfolio/profit/live approval"}
MAX_MODELS, MAX_TRAIN_SECONDS = 62, 600


@dataclass(frozen=True)
class ForwardSample:
    id: str
    symbol: str
    at: datetime
    entry_index: int
    features: tuple[float, ...]
    label_end: datetime
    gross_return_pct: float | None
    base_return_pct: float | None
    stress_return_pct: float | None


def feature_vectors(grouped):
    """All windows end at the closed bar; the other asset uses the SAME time."""
    starts = [b.start for b in grouped[SYMBOLS[0]]]
    if not starts or any(b.seconds != SECONDS or b.symbol != s for s in SYMBOLS for b in grouped[s]):
        raise ValueError("Empty or wrong-symbol/timeframe features")
    for symbol in SYMBOLS:
        for bar in grouped[symbol]:
            bar.validate()
    if any([b.start for b in grouped[s]] != starts for s in SYMBOLS):
        raise ValueError("Cross-asset feature timestamps differ")
    if any(a.end != b.start or a.seconds != SECONDS for s in SYMBOLS for a, b in zip(grouped[s], grouped[s][1:])):
        raise ValueError("Features cannot cross a candle gap")
    prices = {s: np.asarray([float(b.close) for b in grouped[s]]) for s in SYMBOLS}
    result = {}
    for symbol in SYMBOLS:
        bars, close = grouped[symbol], prices[symbol]
        high = np.asarray([float(b.high) for b in bars])
        low = np.asarray([float(b.low) for b in bars])
        volume = np.asarray([float(b.volume) for b in bars])
        if any(not np.all(np.isfinite(v)) for v in (close, high, low, volume)) or np.any(close <= 0):
            raise ValueError("Non-finite feature source")
        other = prices[SYMBOLS[1] if symbol == SYMBOLS[0] else SYMBOLS[0]]
        log_return = np.r_[0.0, np.diff(np.log(close))]
        previous = np.r_[close[0], close[:-1]]
        tr = np.maximum(high - low, np.maximum(abs(high - previous), abs(low - previous)))
        vectors = {}
        for index in range(SPEC["warmup_bars"], len(bars)):
            if (index + 1) % SPEC["sampling_stride_bars"]:
                continue
            path = float(np.sum(abs(np.diff(close[index - 42:index + 1]))))
            mean_volume = float(np.mean(volume[index - 20:index]))
            ma = float(np.mean(close[index - 199:index + 1]))
            old_ma = float(np.mean(close[index - 209:index - 9]))
            span = high[index] - low[index]
            values = tuple(float(math.log(close[index] / close[index - p])) for p in (1, 6, 42, 180)) + (
                float(math.log(other[index] / other[index - 6])), float(math.log(other[index] / other[index - 42])),
                float(np.std(log_return[index - 5:index + 1])), float(np.std(log_return[index - 41:index + 1])),
                float(np.mean(tr[index - 13:index + 1]) / close[index]),
                math.log1p(float(volume[index]) / mean_volume) if mean_volume > 0 else 0.0,
                float(mean_volume == 0), float(abs(close[index] - close[index - 42]) / path) if path > 0 else 0.0,
                float((close[index] - low[index]) / span) if span > 0 else 0.5,
                float(span / close[index]), float(close[index] / ma - 1), float(ma / old_ma - 1),
                float(symbol == SYMBOLS[0]), float(symbol == SYMBOLS[1]))
            if len(values) != len(FEATURES) or not all(math.isfinite(v) for v in values):
                raise ValueError("Invalid causal feature vector")
            vectors[index] = values
        result[symbol] = vectors
    return result


def proxy_return(entry, exit_price, *, adverse=False):
    fee, half = SPEC["fee_each_leg"], SPEC["spread"] / 2
    slip = SPEC["slippage_each_leg"] + (SPEC["extra_adverse_slippage_each_leg"] if adverse else 0)
    return (exit_price * (1 - half) * (1 - slip) * (1 - fee)
            / (entry * (1 + half) * (1 + slip) * (1 + fee)) - 1) * 100


def build_samples(grouped):
    vectors, samples = feature_vectors(grouped), []
    for symbol in SYMBOLS:
        bars = grouped[symbol]
        for index, features in vectors[symbol].items():
            at = bars[index].end
            horizon = at + timedelta(seconds=SECONDS * SPEC["holding_bars"])
            gross = base = stress = None
            if index + 1 + SPEC["holding_bars"] < len(bars):
                entry = bars[index + 1]
                exit_bar = bars[index + 1 + SPEC["holding_bars"]]
                if entry.start != at or exit_bar.start != horizon:
                    raise ValueError("Reference label horizon mismatch")
                entry_price, exit_price = float(entry.open), float(exit_bar.open)
                gross = (exit_price / entry_price - 1) * 100
                base = proxy_return(entry_price, exit_price)
                stress = proxy_return(entry_price, exit_price, adverse=True)
            identifier = sha(f"{SCHEMA}|{symbol}|{at.isoformat()}".encode())[:32]
            samples.append(ForwardSample(identifier, symbol, at, index + 1, features, horizon, gross, base, stress))
    return sorted(samples, key=lambda s: (s.at, s.symbol))


def partition(samples, start, end):
    candidates = [s for s in samples if start <= s.at < end]
    embargo = timedelta(seconds=SECONDS * SPEC["embargo_bars"])
    kept = [s for s in candidates if s.stress_return_pct is not None and s.label_end + embargo <= end]
    return kept, {"start": start.isoformat(), "end_exclusive": end.isoformat(),
                  "candidates": len(candidates), "labelled_kept": len(kept),
                  "purged_or_censored": len(candidates) - len(kept),
                  "latest_label_end": max((s.label_end for s in kept), default=None)}


def weights(samples):
    # Shared time-index intervals across BOTH assets. Never count venues as independent.
    if not samples:
        raise ValueError("No labelled samples to weight")
    concurrent = {}
    for sample in samples:
        for i in range(sample.entry_index, sample.entry_index + SPEC["holding_bars"] + 1):
            concurrent[i] = concurrent.get(i, 0) + 1
    raw = np.asarray([sum(1 / concurrent[i] for i in range(s.entry_index, s.entry_index + SPEC["holding_bars"] + 1))
                      / (SPEC["holding_bars"] + 1) for s in samples])
    return raw / np.sum(raw)


def mapped(values, variant):
    if variant not in VARIANTS:
        raise ValueError("Unregistered research variant")
    array = np.asarray(values, dtype=float)
    if array.shape[-1] != len(FEATURES) or not np.all(np.isfinite(array)):
        raise ValueError("Invalid research features")
    if variant == "interactions":
        array = np.concatenate((array, np.stack([array[..., a] * array[..., b] for a, b in INTERACTIONS], axis=-1)), axis=-1)
    return array


def design(values, mean, scale, constant):
    standardized = (values - mean) / scale
    allowance = np.maximum(1e-8, abs(mean) * 0.05)
    shifted = np.asarray(constant) & (abs(values - mean) > allowance)
    ood = np.any(abs(standardized) > PARAMETERS["ood_standard_deviations"], axis=-1) | np.any(shifted, axis=-1)
    bounded = np.clip(standardized, -PARAMETERS["standardized_clip"], PARAMETERS["standardized_clip"])
    return np.c_[np.ones(len(values)), bounded], ood


def fit_reference(fit, calibration, inference_from, variant):
    if variant not in VARIANTS:
        raise ValueError("Unregistered research variant")
    if set(s.id for s in fit) & set(s.id for s in calibration):
        raise ValueError("Research fit/calibration overlap")
    if any(s.stress_return_pct is None or s.label_end >= inference_from for s in (*fit, *calibration)):
        raise ValueError("Training outcome reaches inference period")
    if fit and calibration and max(s.label_end for s in fit) >= min(s.at for s in calibration):
        raise ValueError("Training/calibration horizons overlap")
    if len({s.id for s in (*fit, *calibration)}) != len(fit) + len(calibration):
        raise ValueError("Duplicate reference samples")
    reasons, support = [], {}
    for name, rows in (("fit", fit), ("calibration", calibration)):
        positive = sum(s.stress_return_pct > 0 for s in rows)
        support[name] = {"count": len(rows), "positive": positive, "unique_times": len({s.at for s in rows})}
        if len(rows) < REQUIREMENTS[name + "_samples"]:
            reasons.append(name + "_sample_count")
        if support[name]["unique_times"] < REQUIREMENTS[name + "_unique_times"]:
            reasons.append(name + "_unique_times")
        if min(positive, len(rows) - positive) < REQUIREMENTS["each_class"]:
            reasons.append(name + "_class_support")
    model = {"schema": SCHEMA, "variant": variant, "features": FEATURES,
             "interactions": INTERACTIONS if variant == "interactions" else (),
             "spec": SPEC, "parameters": PARAMETERS, "requirements": REQUIREMENTS, "license": LICENSE,
             "inference_from": inference_from, "expires_at": inference_from + timedelta(days=PARAMETERS["expires_days"]),
             "support": support, "training_sha256": sha(canonical([asdict(s) for s in (*fit, *calibration)])),
             "fitted": False, "predictive_ready": False, "readiness_reasons": reasons,
             "mean": [], "scale": [], "constant": [], "logistic": [], "ridge": [], "calibration": None,
             "fit_prior": None, "fit_stress_return_mean_pct": None,
             "execution_adapter": None, "approved_for_live": False}
    if any(r.startswith("fit_") for r in reasons):
        return model
    x = mapped([s.features for s in fit], variant)
    y = np.asarray([s.stress_return_pct > 0 for s in fit], dtype=float)
    target = np.asarray([s.stress_return_pct for s in fit])
    w = weights(fit)
    mean = w @ x
    variance = w @ ((x - mean) ** 2)
    constant = variance < 1e-16
    scale = np.where(constant, 1.0, np.sqrt(variance))
    matrix, _ = design(x, mean, scale, constant)
    theta = _logistic(matrix, y, w, PARAMETERS["logistic_l2"])
    penalty = np.eye(matrix.shape[1]) * PARAMETERS["ridge_l2"]
    penalty[0, 0] = 0
    ridge = np.linalg.solve(matrix.T @ (w[:, None] * matrix) + penalty + np.eye(matrix.shape[1]) * 1e-10,
                            matrix.T @ (w * target))
    model.update(fitted=True, mean=mean.tolist(), scale=scale.tolist(), constant=constant.tolist(),
                 logistic=theta.tolist(), ridge=ridge.tolist(), fit_prior=float(w @ y),
                 fit_stress_return_mean_pct=float(w @ target))
    if calibration:
        cm, out = design(mapped([s.features for s in calibration], variant), mean, scale, constant)
        domain_rows = [s for s, bad in zip(calibration, out) if not bad]
        positive = sum(s.stress_return_pct > 0 for s in domain_rows)
        fraction = float(np.mean(out))
        model["support"]["calibration_in_domain"] = {"count": len(domain_rows), "positive": positive, "ood_fraction": fraction}
        if (len(domain_rows) < REQUIREMENTS["calibration_samples"]
                or min(positive, len(domain_rows) - positive) < REQUIREMENTS["each_class"]
                or fraction > PARAMETERS["maximum_calibration_ood_fraction"]):
            reasons.append("calibration_domain_support")
        if not reasons:
            logits = (cm @ theta)[~out]
            calibration_matrix = np.c_[np.ones(len(logits)), logits]
            targets = np.asarray([s.stress_return_pct > 0 for s in domain_rows], dtype=float)
            model["calibration"] = _logistic(calibration_matrix, targets, weights(domain_rows), PARAMETERS["calibration_l2"]).tolist()
    model["predictive_ready"] = model["calibration"] is not None and not reasons
    return model


def predictions(rows, model):
    if not rows:
        return []
    result = []
    if model["fitted"]:
        matrix, out = design(mapped([s.features for s in rows], model["variant"]),
                             np.asarray(model["mean"]), np.asarray(model["scale"]), model["constant"])
        logits = matrix @ np.asarray(model["logistic"])
        if model["calibration"] is not None:
            logits = model["calibration"][0] + model["calibration"][1] * logits
        probabilities = sigmoid(logits)
        expected = matrix @ np.asarray(model["ridge"])
    available = datetime.fromisoformat(str(model["inference_from"])) if isinstance(model["inference_from"], str) else model["inference_from"]
    expiry = datetime.fromisoformat(str(model["expires_at"])) if isinstance(model["expires_at"], str) else model["expires_at"]
    for i, sample in enumerate(rows):
        probability, stress, bad = (float(probabilities[i]), float(expected[i]), bool(out[i])) if model["fitted"] else (None, None, True)
        passed = (model["predictive_ready"] and available <= sample.at < expiry and not bad
                  and probability >= PARAMETERS["probability_threshold"] and stress > PARAMETERS["minimum_stress_return_pct"])
        result.append({"id": sample.id, "symbol": sample.symbol, "feature_at": sample.at.isoformat(),
                       "proxy_probability": probability, "proxy_stress_return_pct": stress, "ood": bad,
                       "reference_gate_pass": bool(passed)})
    return result


def forecast_metrics(rows, model, scored=None):
    scored = predictions(rows, model) if scored is None else scored
    if not rows or not model["fitted"]:
        return {"samples": len(rows), "brier": None, "log_loss": None, "constant_brier": None, "constant_log_loss": None,
                "gate_pass": 0, "gate_pass_mean_proxy_stress_return_pct": None}
    if len(scored) != len(rows) or any(s.id != r["id"] for s, r in zip(rows, scored)):
        raise ValueError("Scored reference rows disagree")
    w = weights(rows)
    y = np.asarray([s.stress_return_pct > 0 for s in rows], dtype=float)
    p = np.clip(np.asarray([r["proxy_probability"] for r in scored]), 1e-12, 1 - 1e-12)
    prior = np.clip(model["fit_prior"], 1e-12, 1 - 1e-12)
    passing = [s.stress_return_pct for s, r in zip(rows, scored) if r["reference_gate_pass"]]
    target = np.asarray([s.stress_return_pct for s in rows])
    estimates = np.asarray([r["proxy_stress_return_pct"] for r in scored])
    return {"samples": len(rows), "unique_times": len({s.at for s in rows}),
            "brier": float(w @ ((p - y) ** 2)), "log_loss": float(-w @ (y * np.log(p) + (1 - y) * np.log(1 - p))),
            "constant_brier": float(w @ ((prior - y) ** 2)),
            "constant_log_loss": float(-w @ (y * np.log(prior) + (1 - y) * np.log(1 - prior))),
            "stress_return_mae_pct": float(w @ abs(estimates - target)),
            "constant_return_mae_pct": float(w @ abs(target - model["fit_stress_return_mean_pct"])),
            "ood_fraction": sum(r["ood"] for r in scored) / len(scored), "gate_pass": len(passing),
            "gate_pass_mean_proxy_stress_return_pct": float(np.mean(passing)) if passing else None}


def shortlist_reasons(model, metrics):
    reasons = list(model["readiness_reasons"])
    if not model["predictive_ready"]:
        reasons.append("predictive_not_ready")
    if metrics["samples"] < SHORTLIST["minimum_labels"]:
        reasons.append("approval_sample_count")
    if metrics["gate_pass"] < SHORTLIST["minimum_gate_pass"]:
        reasons.append("approval_gate_pass_count")
    if (metrics["gate_pass_mean_proxy_stress_return_pct"] is None
            or metrics["gate_pass_mean_proxy_stress_return_pct"] <= 0):
        reasons.append("approval_proxy_expectancy")
    for name in ("brier", "log_loss"):
        if metrics[name] is None or metrics[name] > metrics["constant_" + name]:
            reasons.append("approval_" + name)
    return sorted(set(reasons))


def fold_windows(start, end):
    result, cursor = [], start
    boundaries = [0]
    for phase in ("fit_months", "calibration_months", "approval_months", "validation_months"):
        boundaries.append(boundaries[-1] + PROTOCOL[phase])
    while add_months(cursor, boundaries[-1]) <= end:
        result.append(tuple(add_months(cursor, n) for n in boundaries))
        cursor = add_months(cursor, PROTOCOL["step_months"])
    if not result or len(result) * len(VARIANTS) > MAX_MODELS:
        raise ValueError("Reference coverage insufficient or model budget exceeded")
    return result


def run_research(grouped, output, *, progress=None, monotonic=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Research output exists")
    windows = fold_windows(grouped[SYMBOLS[0]][0].start, grouped[SYMBOLS[0]][-1].end)
    output.mkdir(parents=True)
    timer = monotonic or time.monotonic
    began = timer()

    def budget():
        if timer() - began > MAX_TRAIN_SECONDS:
            raise ValueError("Reference training deadline exceeded")

    write_new(output / "registration.json", {"schema": SCHEMA, "protocol": PROTOCOL, "spec": SPEC,
              "parameters": PARAMETERS, "features": FEATURES, "variants": VARIANTS, "shortlist": SHORTLIST,
              "requirements": REQUIREMENTS, "windows": windows, "license": LICENSE,
              "data_status": "RETROSPECTIVE DEVELOPMENT, including previously inspected/correlated periods; NOT untouched OOS",
              "historical_scope_change": "New GLOBAL reference project; legacy registrations/holdouts unchanged, never reclassified as fresh evidence",
              "models_limit": MAX_MODELS, "training_seconds_limit": MAX_TRAIN_SECONDS, "approved_for_live": False})
    samples = build_samples(grouped)
    with (output / "features.jsonl").open("x") as features, (output / "outcomes.jsonl").open("x") as outcomes:
        for sample in samples:
            features.write(canonical({"id": sample.id, "symbol": sample.symbol, "at": sample.at, "features": sample.features}).decode() + "\n")
            outcomes.write(canonical({"id": sample.id, "label_end": sample.label_end,
                           "gross_return_pct": sample.gross_return_pct, "base_return_pct": sample.base_return_pct,
                           "stress_return_pct": sample.stress_return_pct}).decode() + "\n")
    folds, model_count = [], 0
    for number, (start, fit_end, cal_end, approval_end, validation_end) in enumerate(windows, 1):
        budget()
        directory = output / f"fold-{number:02d}"
        directory.mkdir()
        fit, fit_scope = partition(samples, start, fit_end)
        calibration, cal_scope = partition(samples, fit_end, cal_end)
        approval, approval_scope = partition(samples, cal_end, approval_end)
        models, results, reasons = {}, {}, {}
        if progress:
            progress(f"Fold {number}/{len(windows)}; fit {len(fit)}, calibration {len(calibration)}, approval {len(approval)}")
        for variant in VARIANTS:
            budget()
            model = fit_reference(fit, calibration, cal_end, variant)
            models[variant] = model
            model_count += int(model["fitted"])
            write_new(directory / f"{variant}-model.json", {"model": model, "sha256": sha(canonical(model))})
            score = predictions(approval, model)
            results[variant] = forecast_metrics(approval, model, score)
            reasons[variant] = shortlist_reasons(model, results[variant])
            write_new(directory / f"{variant}-approval-predictions.json", score)
        selected = next((v for v in VARIANTS if not reasons[v]), "cash")
        selection = {"selected_reference_candidate": selected, "failure_reasons": reasons,
                     "locked_before_validation": True, "validation_used_for_selection": False,
                     "approved_for_live": False, "portfolio_profit_approval": False}
        write_new(directory / "selection.json", selection)
        # No validation labels or forecasts are inspected above this immutable lock.
        validation, val_scope = partition(samples, approval_end, validation_end)
        validation_metrics = {}
        for variant in VARIANTS:
            budget()
            score = predictions(validation, models[variant])
            validation_metrics[variant] = forecast_metrics(validation, models[variant], score)
            write_new(directory / f"{variant}-validation-predictions.json", score)
        fold = {"fold": number, "partitions": {"fit": fit_scope, "calibration": cal_scope,
                "approval": approval_scope, "validation": val_scope}, "selection": selection,
                "models": {v: {"sha256": sha(canonical(m)), "fitted": m["fitted"], "predictive_ready": m["predictive_ready"],
                               "readiness_reasons": m["readiness_reasons"]} for v, m in models.items()},
                "approval": results, "validation": validation_metrics}
        write_new(directory / "report.json", fold)
        folds.append(fold)
    budget()
    report = {"status": "complete", "schema": SCHEMA, "scope": "NON-PRODUCTION GLOBAL FORECAST BENCHMARK, NOT portfolio PnL",
              "samples": len(samples), "labelled_samples": sum(s.stress_return_pct is not None for s in samples),
              "models_fitted": model_count, "models_predictive_ready": sum(m["predictive_ready"] for f in folds for m in f["models"].values()),
              "folds": folds, "shortlist_cash_folds": sum(f["selection"]["selected_reference_candidate"] == "cash" for f in folds),
              "validation_brier_better_than_prior": {v: sum(f["validation"][v]["brier"] is not None and
                  f["validation"][v]["brier"] < f["validation"][v]["constant_brier"] for f in folds) for v in VARIANTS},
              "license": LICENSE, "approved_for_live": False, "portfolio_opened": False, "native_execution_validated": False,
              "limitations": ["All history is retrospective development, not new independent OOS evidence",
                "Overlapping weekly labels, assets and rolling training windows are correlated",
                "Proxy fees/spread/slippage and next-open arithmetic do not prove executable fills or profitability",
                "No stops/sizing/minimums/dust/liquidity/native routing or actual portfolio replay in this benchmark",
                "Dense all-bar fixed-horizon forecasts are a different hypothesis from earlier breakout-entry models",
                "Historical models can be expired; none may be loaded into the live/native trading system",
                "Dataset license prohibits live execution; data and derived artifacts remain NC/share-alike research only"]}
    write_new(output / "report.json", report)
    return report


def equivalent(actual, expected):
    """Numerical recomputation tolerance across NumPy/platforms; files still SHA-bound."""
    expected = json.loads(canonical(expected))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(equivalent(actual[k], v) for k, v in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(equivalent(a, b) for a, b in zip(actual, expected)))
    if isinstance(expected, float):
        return isinstance(actual, (float, int)) and not isinstance(actual, bool) and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-10)
    return type(actual) is type(expected) and actual == expected


def audit_research(grouped, directory):
    """Rebuild causal inputs/purges/predictions/metrics/selection, WITHOUT refitting.

    Saved feature values are tolerance-checked against raw bars then used exactly
    for the training-input hash. This permits floating reduction differences
    between macOS/Linux without silently changing the registered inputs.
    This is consistency verification, not proof of independent/OOS profitability.
    """
    directory = Path(directory)
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinked research artifact")
    if directory.is_symlink():
        raise ValueError("Symlinked research directory")
    load = lambda path: json.loads((directory / path).read_text())
    registration, report = load("registration.json"), load("report.json")
    windows = fold_windows(grouped[SYMBOLS[0]][0].start, grouped[SYMBOLS[0]][-1].end)
    expected_registration = {"schema": SCHEMA, "protocol": PROTOCOL, "spec": SPEC,
              "parameters": PARAMETERS, "features": FEATURES, "variants": VARIANTS, "shortlist": SHORTLIST,
              "requirements": REQUIREMENTS, "windows": windows, "license": LICENSE,
              "data_status": "RETROSPECTIVE DEVELOPMENT, including previously inspected/correlated periods; NOT untouched OOS",
              "historical_scope_change": "New GLOBAL reference project; legacy registrations/holdouts unchanged, never reclassified as fresh evidence",
              "models_limit": MAX_MODELS, "training_seconds_limit": MAX_TRAIN_SECONDS, "approved_for_live": False}
    if not equivalent(registration, expected_registration) or len(report["folds"]) != len(windows):
        raise ValueError("Research registration/fold count mismatch")
    rebuilt = build_samples(grouped)
    saved_features = [json.loads(s) for s in (directory / "features.jsonl").read_text().splitlines()]
    saved_outcomes = [json.loads(s) for s in (directory / "outcomes.jsonl").read_text().splitlines()]
    if len(rebuilt) != len(saved_features) or len(rebuilt) != len(saved_outcomes):
        raise ValueError("Research sample count mismatch")
    samples = []
    for sample, feature, outcome in zip(rebuilt, saved_features, saved_outcomes):
        if (not equivalent(feature, {"id": sample.id, "symbol": sample.symbol, "at": sample.at, "features": sample.features})
                or not equivalent(outcome, {"id": sample.id, "label_end": sample.label_end,
                    "gross_return_pct": sample.gross_return_pct, "base_return_pct": sample.base_return_pct,
                    "stress_return_pct": sample.stress_return_pct})):
            raise ValueError("Research causal feature/outcome mismatch")
        samples.append(replace(sample, features=tuple(feature["features"]),
                               gross_return_pct=outcome["gross_return_pct"], base_return_pct=outcome["base_return_pct"],
                               stress_return_pct=outcome["stress_return_pct"]))
    fitted = ready = cash = 0
    wins = {v: 0 for v in VARIANTS}
    for number, window in enumerate(windows, 1):
        prefix = f"fold-{number:02d}/"
        start, fit_end, cal_end, approval_end, validation_end = window
        fit, fit_scope = partition(samples, start, fit_end)
        calibration, cal_scope = partition(samples, fit_end, cal_end)
        approval, approval_scope = partition(samples, cal_end, approval_end)
        validation, val_scope = partition(samples, approval_end, validation_end)
        models, approval_metrics, validation_metrics, reasons = {}, {}, {}, {}
        for variant in VARIANTS:
            saved = load(prefix + variant + "-model.json")
            model = saved["model"]
            if (saved["sha256"] != sha(canonical(model))
                    or model["training_sha256"] != sha(canonical([asdict(s) for s in (*fit, *calibration)]))
                    or model["schema"] != SCHEMA or model["variant"] != variant
                    or not equivalent(model["features"], FEATURES)
                    or not equivalent(model["interactions"], INTERACTIONS if variant == "interactions" else ())
                    or any(not equivalent(model[k], value) for k, value in
                           (("spec", SPEC), ("parameters", PARAMETERS), ("requirements", REQUIREMENTS), ("license", LICENSE),
                            ("inference_from", cal_end), ("expires_at", cal_end + timedelta(days=PARAMETERS["expires_days"]))))
                    or model["approved_for_live"] is not False or model["execution_adapter"] is not None):
                raise ValueError("Research model/input/license binding mismatch")
            if model["fitted"]:
                x = mapped([s.features for s in fit], variant)
                w = weights(fit)
                mean = w @ x
                variance = w @ ((x - mean) ** 2)
                constant = variance < 1e-16
                expected = {"mean": mean.tolist(), "constant": constant.tolist(),
                            "scale": np.where(constant, 1., np.sqrt(variance)).tolist(),
                            "fit_prior": float(w @ np.asarray([s.stress_return_pct > 0 for s in fit])),
                            "fit_stress_return_mean_pct": float(w @ np.asarray([s.stress_return_pct for s in fit]))}
                if any(not equivalent(model[k], val) for k, val in expected.items()):
                    raise ValueError("Research fit-only scaler/prior mismatch")
                if any(len(model[k]) != x.shape[1] + 1 or not all(math.isfinite(v) for v in model[k]) for k in ("logistic", "ridge")):
                    raise ValueError("Invalid fitted coefficient dimensions")
            for phase, rows, bucket in (("approval", approval, approval_metrics), ("validation", validation, validation_metrics)):
                scored = load(prefix + variant + "-" + phase + "-predictions.json")
                if not equivalent(scored, predictions(rows, model)):
                    raise ValueError("Research prediction mismatch")
                bucket[variant] = forecast_metrics(rows, model, scored)
            reasons[variant] = shortlist_reasons(model, approval_metrics[variant])
            models[variant] = {"sha256": saved["sha256"], "fitted": model["fitted"], "predictive_ready": model["predictive_ready"],
                               "readiness_reasons": model["readiness_reasons"]}
            fitted += int(model["fitted"])
            ready += int(model["predictive_ready"])
            metric = validation_metrics[variant]
            wins[variant] += int(metric["brier"] is not None and metric["brier"] < metric["constant_brier"])
        selected = next((v for v in VARIANTS if not reasons[v]), "cash")
        selection = {"selected_reference_candidate": selected, "failure_reasons": reasons,
                     "locked_before_validation": True, "validation_used_for_selection": False,
                     "approved_for_live": False, "portfolio_profit_approval": False}
        cash += int(selected == "cash")
        expected_fold = {"fold": number, "partitions": {"fit": fit_scope, "calibration": cal_scope,
                         "approval": approval_scope, "validation": val_scope}, "selection": selection,
                         "models": models, "approval": approval_metrics, "validation": validation_metrics}
        if (not equivalent(load(prefix + "selection.json"), selection)
                or not equivalent(load(prefix + "report.json"), expected_fold)
                or not equivalent(report["folds"][number - 1], expected_fold)):
            raise ValueError("Research locked selection/fold report mismatch")
    if (report["status"] != "complete" or report["schema"] != SCHEMA or report["license"] != LICENSE
            or report["models_fitted"] != fitted or report["models_predictive_ready"] != ready
            or report["shortlist_cash_folds"] != cash or report["validation_brier_better_than_prior"] != wins
            or report["samples"] != len(samples) or report["labelled_samples"] != sum(s.stress_return_pct is not None for s in samples)
            or any(report[k] is not False for k in ("approved_for_live", "portfolio_opened", "native_execution_validated"))):
        raise ValueError("Research summary mismatch")
    return {"status": "verified", "folds": len(windows), "models_fitted": fitted, "models_predictive_ready": ready,
            "samples_verified": len(samples), "shortlist_cash_folds": cash, "validation_brier_better_than_prior": wins,
            "refitted": False, "approved_for_live": False, "portfolio_profit_evidence": False}
