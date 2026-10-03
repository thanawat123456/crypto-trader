"""Causal entry gates. ATR cost-room is a heuristic, not expected profit."""
from dataclasses import asdict, dataclass
from decimal import Decimal

from .broker import ExecutionScenario
from .config import Config
from .context import ContextState, MarketContext
from .domain import ONE, ZERO, Quote, Signal, utc
from .execution_costs import entry_cash_factor, entry_inventory_factor


@dataclass(frozen=True)
class EntryEvaluation:
    allowed: bool
    reason: str
    policy_name: str
    entry_regime: str | None
    round_trip_break_even: Decimal
    atr_room_proxy: Decimal | None
    room_cost_ratio: Decimal | None
    score_kind: str = "ATR movement-room / assumed round-trip cost; not a profit forecast"

    def payload(self):
        return asdict(self)


@dataclass(frozen=True)
class EntryPolicy:
    name: str = "baseline"
    require_uptrend: bool = False
    require_cost_room: bool = False
    always_flat: bool = False
    room_atr_multiple: Decimal = Decimal("2")
    min_room_cost_ratio: Decimal = Decimal("1.5")

    @property
    def enabled(self):
        return self.require_uptrend or self.require_cost_room or self.always_flat

    def validate(self):
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 64:
            raise ValueError("Invalid entry policy name")
        if any(not isinstance(v, bool) for v in (self.require_uptrend, self.require_cost_room, self.always_flat)):
            raise ValueError("Entry policy switches must be boolean")
        if (not self.room_atr_multiple.is_finite() or self.room_atr_multiple <= 0 or
                not self.min_room_cost_ratio.is_finite() or self.min_room_cost_ratio < 1):
            raise ValueError("Invalid cost-room heuristic thresholds")
        return self

    def payload(self):
        return asdict(self)

    def validate_for_config(self, cfg):
        return self.validate()

    def evaluate(self, signal: Signal, context: MarketContext | None, quote: Quote, cfg: Config,
                 execution: ExecutionScenario | None = None) -> EntryEvaluation:
        self.validate()
        quote.validate()
        scenario = (execution or ExecutionScenario()).validate()
        slip, fee = cfg.costs.slippage + scenario.extra_slippage, cfg.costs.taker_fee
        # Assumes the same proportional spread/fee/slippage at future exit.
        # It is the required mid-price rise, not a prediction that it occurs.
        cost = quote.ask * (ONE + slip) * entry_cash_factor(cfg) / (quote.bid * (ONE - slip) * (ONE - fee) * entry_inventory_factor(cfg)) - ONE
        regime, room, ratio = None, None, None
        reason, allowed = "entry_policy_pass", True
        context_valid = context is not None and context.symbol == signal.symbol and context.at == signal.at
        if context_valid:
            values = (context.atr_pct, context.trend_distance_atr, context.slope_atr,
                      context.efficiency_20, context.breakout_distance_atr)
            context_valid = (all(v.is_finite() for v in values) and context.atr_pct >= 0
                             and ZERO <= context.efficiency_20 <= ONE
                             and context.regime in {"warmup", "range", "trend_up", "trend_down", "high_volatility"}
                             and (context.volume_ratio_20 is None or
                                  (context.volume_ratio_20.is_finite() and context.volume_ratio_20 >= 0)))
        if signal.atr.is_finite() and signal.atr > 0 and context_valid:
            regime = context.regime
            room = signal.atr * self.room_atr_multiple / quote.mid
            ratio = room / cost if cost > 0 else None
        if quote.symbol != signal.symbol or utc(signal.at) > utc(quote.time):
            allowed, reason = False, "entry_policy_time_or_symbol_mismatch"
        elif self.always_flat:
            allowed, reason = False, "entry_policy_cash_control"
        elif self.enabled and (not context_valid or context.regime == "warmup"
                               or not signal.atr.is_finite() or signal.atr <= 0):
            allowed, reason = False, "entry_policy_context_unavailable"
        elif self.require_uptrend and regime != "trend_up":
            allowed, reason = False, "entry_policy_not_uptrend"
        elif self.require_cost_room and (room is None or room <= 0 or (cost > 0 and ratio < self.min_room_cost_ratio)):
            allowed, reason = False, "entry_policy_insufficient_cost_room"
        return EntryEvaluation(allowed, reason, self.name, regime, cost, room, ratio)


def policies() -> dict[str, EntryPolicy]:
    return {"baseline": EntryPolicy(),
            "trend": EntryPolicy("trend", require_uptrend=True),
            "cost": EntryPolicy("cost", require_cost_room=True),
            "trend_cost": EntryPolicy("trend_cost", require_uptrend=True, require_cost_room=True),
            "cash": EntryPolicy("cash", always_flat=True)}


def context_from_bars(bars, cfg):
    if not bars:
        raise ValueError("No closed bars for entry policy")
    state = ContextState(cfg)
    for bar in bars:
        signal, context = state.update(bar)
    return signal, context
