"""Deterministic regularized linear ML; no pickle, random CV or online fitting."""
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .domain import utc
from .ml_labels import feature_names, uniqueness_weights
from .ml_variants import INTERACTION_SCHEMA, map_features, mapped_names, variant_spec


MODEL_SCHEMA = "net-entry-linear-v1"
REQUIREMENTS = {"fit_samples": 80, "fit_unique_times": 40, "calibration_samples": 30,
                "calibration_unique_times": 15, "each_class": 5}
HYPERPARAMETERS = {"logistic_l2": 0.1, "ridge_l2": 1.0, "calibration_l2": 0.01,
                   "probability_threshold": 0.60, "minimum_stress_return_pct": 0.25,
                   "ood_standard_deviations": 6.0, "standardized_clip": 8.0,
                   "constant_feature_relative_allowance": 0.05,
                   "maximum_calibration_ood_fraction": 0.20,
                   "expires_days_after_calibration": 180}


def canonical(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sigmoid(values):
    return 1 / (1 + np.exp(-np.clip(values, -40, 40)))


def _standardize_and_domain(values, mean, scale, constant_mask):
    standardized = (values - np.asarray(mean)) / np.asarray(scale)
    allowance = np.maximum(1e-8, np.abs(mean) * HYPERPARAMETERS["constant_feature_relative_allowance"])
    constant_shift = np.asarray(constant_mask, dtype=bool) & (np.abs(values - np.asarray(mean)) > allowance)
    out_of_domain = (np.any(np.abs(standardized) > HYPERPARAMETERS["ood_standard_deviations"], axis=-1)
                     | np.any(constant_shift, axis=-1))
    return standardized, out_of_domain


def _logistic(design, target, weights, penalty):
    """Weighted mean log loss + L2/2; intercept is never penalized."""
    regularizer = np.eye(design.shape[1]) * penalty
    regularizer[0, 0] = 0
    theta = np.zeros(design.shape[1])
    prior = float(weights @ target)
    theta[0] = math.log(prior / (1 - prior))

    def loss(coefficients):
        logits = design @ coefficients
        return float(weights @ (np.logaddexp(0, logits) - target * logits)
                     + coefficients @ regularizer @ coefficients / 2)

    for _ in range(100):
        probability = sigmoid(design @ theta)
        gradient = design.T @ (weights * (probability - target)) + regularizer @ theta
        if np.max(np.abs(gradient)) < 1e-7:
            return theta
        curvature = weights * probability * (1 - probability)
        hessian = design.T @ (curvature[:, None] * design) + regularizer + np.eye(len(theta)) * 1e-10
        direction = np.linalg.solve(hessian, gradient)
        old, rate = loss(theta), 1.0
        for _ in range(40):
            proposed = theta - rate * direction
            if loss(proposed) <= old - 1e-4 * rate * float(gradient @ direction):
                theta = proposed
                break
            rate /= 2
        else:
            raise ValueError("ML logistic optimization did not converge")
    raise ValueError("ML logistic iteration limit reached")


@dataclass(frozen=True)
class NetEntryModel:
    schema: str
    symbols: tuple[str, ...]
    feature_names: tuple[str, ...]
    config_hash: str
    max_holding_bars: int
    inference_from: str
    expires_at: str
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    logistic: tuple[float, ...]
    ridge: tuple[float, ...]
    calibration: tuple[float, float] | None
    fitted: bool
    ready: bool
    readiness_reasons: tuple[str, ...]
    summary: dict

    def payload(self):
        return asdict(self)

    @property
    def digest(self):
        return hashlib.sha256(canonical(self.payload())).hexdigest()

    def validate(self):
        variant = self.summary.get("variant", "linear-v1")
        registered = variant_spec(variant)
        expected_schema = MODEL_SCHEMA if variant == "linear-v1" else INTERACTION_SCHEMA
        expected_hyperparameters = {**HYPERPARAMETERS, "logistic_l2": registered["logistic_l2"],
                                    "ridge_l2": registered["ridge_l2"]}
        if (self.schema != expected_schema or not self.symbols or len(set(self.symbols)) != len(self.symbols)
                or self.feature_names != mapped_names(self.symbols, variant)
                or not isinstance(self.fitted, bool) or not isinstance(self.ready, bool)
                or isinstance(self.max_holding_bars, bool) or not isinstance(self.max_holding_bars, int)
                or not 1 <= self.max_holding_bars <= 1000 or len(self.config_hash) != 64
                or utc(self.inference_from) >= utc(self.expires_at)):
            raise ValueError("Invalid ML model schema/identity")
        width = len(self.feature_names)
        if self.fitted:
            if (len(self.mean) != width or len(self.scale) != width or len(self.logistic) != width + 1
                    or len(self.ridge) != width + 1 or any(s <= 0 for s in self.scale)
                    or not all(math.isfinite(v) for v in (*self.mean, *self.scale, *self.logistic, *self.ridge))):
                raise ValueError("Invalid ML coefficients/scaler")
            if len(self.summary.get("constant_feature_mask", ())) != width:
                raise ValueError("ML constant-feature domain metadata missing")
        elif self.mean or self.scale or self.logistic or self.ridge or self.calibration is not None:
            raise ValueError("Unfitted model must not have coefficients")
        if self.calibration is not None and (len(self.calibration) != 2 or not all(math.isfinite(v) for v in self.calibration)):
            raise ValueError("Invalid ML calibrator")
        if self.ready and (not self.fitted or self.calibration is None or self.readiness_reasons):
            raise ValueError("Ready ML model lacks evidence/calibration")
        if (self.summary.get("requirements") != REQUIREMENTS or self.summary.get("hyperparameters") != expected_hyperparameters
                or (variant != "linear-v1" and self.summary.get("variant_spec") != registered)):
            raise ValueError("ML artifact protocol mismatch")
        if self.summary.get("label_spec", {}).get("max_holding_bars") != self.max_holding_bars:
            raise ValueError("ML artifact holding horizon mismatch")
        if self.ready:
            for label in ("fit", "calibration"):
                count, positive = self.summary[label + "_count"], self.summary[label + "_positive"]
                if (count < REQUIREMENTS[label + "_samples"] or self.summary[label + "_unique_times"] < REQUIREMENTS[label + "_unique_times"]
                        or min(positive, count - positive) < REQUIREMENTS["each_class"]):
                    raise ValueError("ML readiness flag disagrees with sample evidence")
            if (self.summary["calibration_in_domain_count"] < REQUIREMENTS["calibration_samples"]
                    or min(self.summary["calibration_in_domain_positive"], self.summary["calibration_in_domain_count"]
                           - self.summary["calibration_in_domain_positive"]) < REQUIREMENTS["each_class"]
                    or self.summary["calibration_ood_fraction"] > HYPERPARAMETERS["maximum_calibration_ood_fraction"]):
                raise ValueError("ML calibration domain evidence is insufficient")
        canonical(self.payload())
        return self

    def predict(self, features):
        self.validate()
        if not self.fitted:
            return {"probability": None, "stress_return_pct": None, "ood": True,
                    "calibrated": False, "contributions": {}}
        values = np.asarray(map_features(features, self.symbols, self.summary.get("variant", "linear-v1")), dtype=float)
        if values.shape != (len(self.feature_names),) or not np.all(np.isfinite(values)):
            raise ValueError("Invalid prediction feature vector")
        standardized, ood = _standardize_and_domain(values, self.mean, self.scale, self.summary["constant_feature_mask"])
        bounded = np.clip(standardized, -HYPERPARAMETERS["standardized_clip"], HYPERPARAMETERS["standardized_clip"])
        design = np.r_[1.0, bounded]
        logit = float(design @ np.asarray(self.logistic))
        if self.calibration is not None:
            logit = self.calibration[0] + self.calibration[1] * logit
        probability = float(sigmoid(logit))
        contributions = bounded * np.asarray(self.ridge[1:])
        expected = float(design @ np.asarray(self.ridge))
        if not math.isfinite(expected):
            raise ValueError("Non-finite ML prediction")
        return {"probability": probability, "stress_return_pct": expected, "ood": bool(ood),
                "calibrated": self.calibration is not None,
                "contributions": dict(zip(self.feature_names, map(float, contributions))),
                "return_intercept_pct": self.ridge[0]}

    def save(self, path: Path):
        self.validate()
        with path.open("x") as stream:
            json.dump({"model": self.payload(), "sha256": self.digest}, stream, indent=2, default=str, allow_nan=False)


def load_model(path: Path) -> NetEntryModel:
    raw = json.loads(path.read_text())
    if set(raw) != {"model", "sha256"} or hashlib.sha256(canonical(raw["model"])).hexdigest() != raw["sha256"]:
        raise ValueError("ML artifact checksum mismatch")
    values = dict(raw["model"])
    for key in ("symbols", "feature_names", "mean", "scale", "logistic", "ridge", "readiness_reasons"):
        values[key] = tuple(values[key])
    if values["calibration"] is not None:
        values["calibration"] = tuple(values["calibration"])
    return NetEntryModel(**values).validate()


def fit_model(fit_samples, calibration_samples, cfg, spec, inference_from, *, variant="linear-v1") -> NetEntryModel:
    """Only purged fit/calibration samples may enter this function."""
    cfg.validate()
    spec.validate()
    registered = variant_spec(variant)
    hyperparameters = {**HYPERPARAMETERS, "logistic_l2": registered["logistic_l2"], "ridge_l2": registered["ridge_l2"]}
    boundary = utc(inference_from)
    if any(not s.labelled or s.label_end >= boundary for s in (*fit_samples, *calibration_samples)):
        raise ValueError("ML fitting label reaches inference period")
    if set(s.id for s in fit_samples) & set(s.id for s in calibration_samples):
        raise ValueError("Fit/calibration sample overlap")
    if fit_samples and calibration_samples and max(s.label_end for s in fit_samples) >= min(s.feature_at for s in calibration_samples):
        raise ValueError("Fit/calibration outcome intervals overlap")
    symbols = tuple(i.symbol for i in cfg.instruments)
    names = mapped_names(symbols, variant)
    all_samples = (*fit_samples, *calibration_samples)
    for sample in all_samples:
        sample.validate(cfg, spec)
    if len({s.id for s in all_samples}) != len(all_samples):
        raise ValueError("Duplicate ML training samples")
    if any(len(s.features) != len(feature_names(symbols)) or not all(math.isfinite(v) for v in s.features) for s in all_samples):
        raise ValueError("Invalid ML training features")
    fit_positive = sum(s.adverse.net_return_pct > 0 for s in fit_samples)
    cal_positive = sum(s.adverse.net_return_pct > 0 for s in calibration_samples)
    reasons = []
    for label, samples, positive, minimum, times in (
            ("fit", fit_samples, fit_positive, REQUIREMENTS["fit_samples"], REQUIREMENTS["fit_unique_times"]),
            ("calibration", calibration_samples, cal_positive, REQUIREMENTS["calibration_samples"], REQUIREMENTS["calibration_unique_times"])):
        if len(samples) < minimum:
            reasons.append(label + "_sample_count")
        if len({s.feature_at for s in samples}) < times:
            reasons.append(label + "_distinct_times")
        if min(positive, len(samples) - positive) < REQUIREMENTS["each_class"]:
            reasons.append(label + "_class_support")
    summary = {"fit_count": len(fit_samples), "fit_positive": fit_positive,
               "calibration_count": len(calibration_samples), "calibration_positive": cal_positive,
               "fit_unique_times": len({s.feature_at for s in fit_samples}),
               "calibration_unique_times": len({s.feature_at for s in calibration_samples}),
               "label_spec": asdict(spec),
               "training_rows_sha256": hashlib.sha256(canonical([s.payload() for s in all_samples])).hexdigest(),
               "requirements": REQUIREMENTS, "hyperparameters": hyperparameters,
               "calibration_in_domain_count": 0, "calibration_in_domain_positive": 0, "calibration_ood_fraction": 0.0,
               "target": "adverse-cost counterfactual net return; probability means return > 0",
               "weighting": "inverse global horizon concurrency; not independent-sample evidence"}
    if variant != "linear-v1":
        summary.update(variant=variant, variant_spec=registered)
    mean = scale = logistic = ridge = ()
    calibration = None
    fitted = len(fit_samples) >= 12 and min(fit_positive, len(fit_samples) - fit_positive) >= 2
    if fitted:
        raw = np.asarray([map_features(s.features, symbols, variant) for s in fit_samples], dtype=float)
        weights = np.asarray(uniqueness_weights(fit_samples, spec.max_holding_bars), dtype=float)
        summary["kish_weight_ess_not_independent"] = float(weights.sum() ** 2 / (weights @ weights))
        weights /= weights.sum()
        average = weights @ raw
        deviation = np.sqrt(weights @ ((raw - average) ** 2))
        summary["constant_feature_mask"] = list(map(bool, deviation < 1e-8))
        deviation = np.where(deviation < 1e-8, 1.0, deviation)
        standardized = np.clip((raw - average) / deviation, -HYPERPARAMETERS["standardized_clip"], HYPERPARAMETERS["standardized_clip"])
        design = np.c_[np.ones(len(raw)), standardized]
        target = np.asarray([float(s.adverse.net_return_pct) for s in fit_samples])
        binary = (target > 0).astype(float)
        summary["fit_positive_prior"] = float(weights @ binary)
        fitted_logistic = _logistic(design, binary, weights, hyperparameters["logistic_l2"])
        regularizer = np.eye(design.shape[1]) * hyperparameters["ridge_l2"]
        regularizer[0, 0] = 0
        fitted_ridge = np.linalg.solve(design.T @ (weights[:, None] * design) + regularizer, design.T @ (weights * target))
        mean, scale, logistic, ridge = (tuple(map(float, values)) for values in (average, deviation, fitted_logistic, fitted_ridge))
        if calibration_samples:
            cal_raw = np.asarray([map_features(s.features, symbols, variant) for s in calibration_samples])
            cal_standardized, cal_ood = _standardize_and_domain(cal_raw, average, deviation, summary["constant_feature_mask"])
            in_domain = [s for s, ood in zip(calibration_samples, cal_ood) if not ood]
            in_domain_positive = sum(s.adverse.net_return_pct > 0 for s in in_domain)
            summary.update(calibration_in_domain_count=len(in_domain), calibration_in_domain_positive=in_domain_positive,
                           calibration_ood_fraction=float(np.mean(cal_ood)))
            if (len(in_domain) < REQUIREMENTS["calibration_samples"]
                    or min(in_domain_positive, len(in_domain) - in_domain_positive) < REQUIREMENTS["each_class"]):
                reasons.append("calibration_in_domain_support")
            if summary["calibration_ood_fraction"] > HYPERPARAMETERS["maximum_calibration_ood_fraction"]:
                reasons.append("calibration_domain_shift")
            if len(in_domain) >= 8 and min(in_domain_positive, len(in_domain) - in_domain_positive) >= 2:
                cal_scaled = np.clip(cal_standardized[~cal_ood], -HYPERPARAMETERS["standardized_clip"], HYPERPARAMETERS["standardized_clip"])
                logits = np.c_[np.ones(len(in_domain)), cal_scaled] @ fitted_logistic
                cal_target = np.asarray([float(s.adverse.net_return_pct > 0) for s in in_domain])
                cal_weights = np.asarray(uniqueness_weights(in_domain, spec.max_holding_bars))
                cal_weights /= cal_weights.sum()
                calibration = tuple(map(float, _logistic(np.c_[np.ones(len(logits)), logits], cal_target, cal_weights,
                                                         HYPERPARAMETERS["calibration_l2"])))
    if not fitted:
        reasons.append("numerical_fit_support")
    if calibration is None:
        reasons.append("calibration_unavailable")
    schema = MODEL_SCHEMA if variant == "linear-v1" else INTERACTION_SCHEMA
    model = NetEntryModel(schema, symbols, names, cfg.digest(), spec.max_holding_bars, boundary.isoformat(),
                          (boundary + timedelta(days=HYPERPARAMETERS["expires_days_after_calibration"])).isoformat(),
                          mean, scale, logistic, ridge, calibration, fitted, not reasons, tuple(reasons), summary)
    return model.validate()


def prediction_metrics(samples, model):
    if not samples or not model.fitted:
        return {"labelled_samples": len(samples), "scored_samples": 0, "brier": None,
                "log_loss": None, "constant_brier": None, "constant_log_loss": None,
                "stress_return_mae_pct": None, "calibration_bins": []}
    predictions = [model.predict(s.features) for s in samples]
    probability = np.clip([p["probability"] for p in predictions], 1e-12, 1 - 1e-12)
    target = np.asarray([float(s.adverse.net_return_pct > 0) for s in samples])
    returns = np.asarray([float(s.adverse.net_return_pct) for s in samples])
    constant = np.clip(model.summary["fit_positive_prior"], 1e-12, 1 - 1e-12)
    bins = []
    for index in range(5):
        selected = (probability >= index / 5) & (probability < (index + 1) / 5)
        if selected.any():
            bins.append({"lower": index / 5, "upper": (index + 1) / 5, "count": int(selected.sum()),
                         "mean_probability": float(probability[selected].mean()), "positive_rate": float(target[selected].mean())})
    return {"labelled_samples": len(samples), "scored_samples": len(predictions),
            "calibrated": model.calibration is not None, "observed_positive_rate": float(target.mean()),
            "brier": float(np.mean((probability - target) ** 2)),
            "log_loss": float(-np.mean(target * np.log(probability) + (1 - target) * np.log(1 - probability))),
            "constant_brier": float(np.mean((constant - target) ** 2)),
            "constant_log_loss": float(-np.mean(target * np.log(constant) + (1 - target) * np.log(1 - constant))),
            "stress_return_mae_pct": float(np.mean(np.abs(np.asarray([p["stress_return_pct"] for p in predictions]) - returns))),
            "ood_samples": sum(p["ood"] for p in predictions), "calibration_bins": bins,
            "limits": "Raw overlapping observations; scores/bins alone do not establish calibration or independent significance"}
