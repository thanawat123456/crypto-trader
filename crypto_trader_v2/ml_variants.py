"""A small, fixed research family. No outcome-driven feature search.

Maps retain every raw input for domain checks. The bounded interactions are
computed only from the same closed-bar vector, never labels or future prices.
"""
import math

from .ml_labels import feature_names


INTERACTION_SCHEMA = "net-entry-interactions-v2"
VARIANTS = {
    "linear-v1": {"feature_map": "identity", "logistic_l2": 0.1, "ridge_l2": 1.0},
    "linear-shrink": {"feature_map": "identity", "logistic_l2": 0.5, "ridge_l2": 3.0},
    "interactions": {"feature_map": "bounded-interactions-v1", "logistic_l2": 0.1, "ridge_l2": 1.0},
    "interactions-shrink": {"feature_map": "bounded-interactions-v1", "logistic_l2": 0.5, "ridge_l2": 3.0},
}
INTERACTIONS = ("trend_x_efficiency", "slope_x_efficiency", "breakout_x_efficiency",
                "breakout_x_volume", "volatility_x_trend", "volatility_x_breakout")


def variant_spec(name):
    if name not in VARIANTS:
        raise ValueError("Unknown registered ML variant")
    return dict(VARIANTS[name])


def mapped_names(symbols, variant):
    spec = variant_spec(variant)
    return feature_names(symbols) + (INTERACTIONS if spec["feature_map"] != "identity" else ())


def map_features(features, symbols, variant):
    spec = variant_spec(variant)
    values = tuple(float(v) for v in features)
    if len(values) != len(feature_names(symbols)) or not all(math.isfinite(v) for v in values):
        raise ValueError("Invalid raw ML feature vector")
    if spec["feature_map"] == "identity":
        return values
    volatility, trend, slope, efficiency, volume, _, breakout = values[:7]
    trend, slope, breakout = math.tanh(trend / 5), math.tanh(slope), math.tanh(breakout)
    volatility, volume = math.tanh(volatility / 0.02), math.tanh(volume)
    return values + (trend * efficiency, slope * efficiency, breakout * efficiency,
                     breakout * volume, volatility * trend, volatility * breakout)
