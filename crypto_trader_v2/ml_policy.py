"""Frozen ML inference gate. Models never fit during a trading cycle."""
from dataclasses import dataclass
from decimal import Decimal

from .broker import ExecutionScenario
from .domain import ONE, Quote, utc
from .entry_policy import EntryEvaluation, EntryPolicy
from .ml_labels import entry_features
from .ml_model import HYPERPARAMETERS, NetEntryModel


@dataclass(frozen=True)
class MLEvaluation(EntryEvaluation):
    probability_net_positive: float | None = None
    estimated_stress_return_pct: float | None = None
    probability_calibrated: bool = False
    out_of_distribution: bool = False
    model_sha256: str = ""
    return_contributions: dict | None = None


@dataclass(frozen=True)
class MLEntryPolicy:
    model: NetEntryModel
    adverse_slippage: Decimal = Decimal("0.001")

    @property
    def enabled(self):
        return True

    @property
    def max_holding_bars(self):
        return self.model.max_holding_bars

    def validate(self):
        self.model.validate()
        if not self.adverse_slippage.is_finite() or not 0 <= self.adverse_slippage < Decimal("0.05"):
            raise ValueError("Invalid ML inference cost scope")
        if self.adverse_slippage != Decimal(str(self.model.summary["label_spec"]["adverse_slippage"])):
            raise ValueError("ML inference cost scope differs from training labels")
        return self

    def validate_for_config(self, cfg):
        self.validate()
        if self.model.config_hash != cfg.digest() or self.model.symbols != tuple(i.symbol for i in cfg.instruments):
            raise ValueError("ML model/config mismatch; retrain in a new registered experiment")
        return self

    def payload(self):
        return {"name": "ml", "model_sha256": self.model.digest, "model": self.model.payload(),
                "adverse_slippage": self.adverse_slippage, "decision_thresholds": self.model.summary["hyperparameters"],
                "approved_for_live": False}

    def evaluate(self, signal, context, quote, cfg, execution=None):
        self.validate_for_config(cfg)
        scenario = (execution or ExecutionScenario()).validate()
        base = EntryPolicy().evaluate(signal, context, quote, cfg, scenario)
        reason, prediction = "ml_evidence_insufficient", None
        if not base.allowed:
            reason = base.reason
        elif context is None or context.at != signal.at or context.symbol != signal.symbol or not signal.atr.is_finite() or signal.atr <= 0:
            reason = "ml_context_unavailable"
        elif not utc(self.model.inference_from) <= utc(signal.at) < utc(self.model.expires_at):
            reason = "ml_model_not_yet_available_or_expired"
        else:
            try:
                prediction = self.model.predict(entry_features(context, self.model.symbols))
            except ValueError:
                reason = "ml_context_unavailable"
            else:
                half = cfg.costs.simulated_spread / 2
                slip, fee = cfg.costs.slippage + self.adverse_slippage, cfg.costs.taker_fee
                reference = (ONE + half) * (ONE + slip) * (ONE + fee) / ((ONE - half) * (ONE - slip) * (ONE - fee)) - ONE
                # Model values are floating-point estimates, not risk budgets;
                # portfolio accounting/sizing retain their exact Decimal checks.
                if not self.model.ready:
                    reason = "ml_evidence_insufficient"
                elif float(base.round_trip_break_even) > float(reference):
                    reason = "ml_execution_cost_out_of_scope"
                elif prediction["ood"]:
                    reason = "ml_out_of_distribution"
                elif prediction["probability"] < HYPERPARAMETERS["probability_threshold"]:
                    reason = "ml_low_net_profit_probability"
                elif prediction["stress_return_pct"] <= HYPERPARAMETERS["minimum_stress_return_pct"]:
                    reason = "ml_insufficient_expected_stress_return"
                else:
                    reason = "ml_entry_pass"
        prediction = prediction or {}
        return MLEvaluation(reason == "ml_entry_pass", reason, "ml", context.regime if context else None,
                            base.round_trip_break_even, None, None,
                            "Learned bounded counterfactual adverse-cost return; uncertain, not a guarantee",
                            prediction.get("probability"), prediction.get("stress_return_pct"),
                            prediction.get("calibrated", False), prediction.get("ood", False), self.model.digest,
                            prediction.get("contributions"))
