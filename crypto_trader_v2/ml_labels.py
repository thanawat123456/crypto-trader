"""Closed-bar features and independent, bounded counterfactual trade labels.

Labels deliberately contain future outcomes; features never do. A label is not
an executed portfolio trade and does not assume account minimums or liquidity.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import math

from .config import Config
from .context import ContextState, MarketContext
from .data import Dataset, validate_bars
from .domain import ONE, ZERO, utc


BASE_FEATURES = ("atr_pct", "trend_distance_atr", "slope_atr", "efficiency_20",
                 "log_volume_ratio_20", "volume_missing", "breakout_distance_atr",
                 "regime_range", "regime_trend_up", "regime_high_volatility")


def feature_names(symbols):
    return BASE_FEATURES + tuple("symbol:" + symbol for symbol in symbols)


def entry_features(context: MarketContext, symbols) -> tuple[float, ...]:
    if context.symbol not in symbols or context.regime not in {"range", "trend_up", "trend_down", "high_volatility"}:
        raise ValueError("ML context unavailable or out of domain")
    numeric = (context.atr_pct, context.trend_distance_atr, context.slope_atr,
               context.efficiency_20, context.breakout_distance_atr)
    if (not all(v.is_finite() for v in numeric) or context.atr_pct <= 0
            or not ZERO <= context.efficiency_20 <= ONE):
        raise ValueError("Invalid ML features")
    volume = context.volume_ratio_20
    if volume is not None and (not volume.is_finite() or volume < 0):
        raise ValueError("Invalid ML volume feature")
    values = (float(context.atr_pct), float(context.trend_distance_atr), float(context.slope_atr),
              float(context.efficiency_20), math.log1p(float(volume)) if volume is not None else 0.0,
              float(volume is None), float(context.breakout_distance_atr),
              float(context.regime == "range"), float(context.regime == "trend_up"),
              float(context.regime == "high_volatility")) + tuple(float(context.symbol == s) for s in symbols)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("Non-finite ML features")
    return values


@dataclass(frozen=True)
class LabelSpec:
    max_holding_bars: int = 42
    adverse_slippage: Decimal = Decimal("0.001")
    embargo_bars: int = 1

    def validate(self):
        if (isinstance(self.max_holding_bars, bool) or not isinstance(self.max_holding_bars, int)
                or not 1 <= self.max_holding_bars <= 1000 or isinstance(self.embargo_bars, bool)
                or not isinstance(self.embargo_bars, int) or not 1 <= self.embargo_bars <= 1000
                or not self.adverse_slippage.is_finite() or not ZERO <= self.adverse_slippage < Decimal("0.05")):
            raise ValueError("Invalid registered ML label specification")
        return self


@dataclass(frozen=True)
class TradeLabel:
    net_return_pct: Decimal
    exited_at: datetime
    exit_reason: str


@dataclass(frozen=True)
class Sample:
    id: str
    symbol: str
    feature_at: datetime
    entry_index: int
    features: tuple[float, ...]
    label_end: datetime
    baseline: TradeLabel | None
    adverse: TradeLabel | None

    @property
    def labelled(self):
        return self.baseline is not None and self.adverse is not None

    def payload(self):
        return asdict(self)

    def validate(self, cfg, spec):
        utc(self.feature_at)
        utc(self.label_end)
        if (isinstance(self.entry_index, bool) or not isinstance(self.entry_index, int) or self.entry_index < 1
                or self.label_end != self.feature_at + timedelta(seconds=cfg.seconds * spec.max_holding_bars)
                or not all(math.isfinite(v) for v in self.features)
                or (self.baseline is None) != (self.adverse is None)):
            raise ValueError("Invalid ML sample interval/features")
        for label in (self.baseline, self.adverse):
            if label is not None:
                utc(label.exited_at)
            if label is not None and (not label.net_return_pct.is_finite()
                                      or not self.feature_at <= label.exited_at <= self.label_end):
                raise ValueError("ML outcome is outside its declared interval")
        return self


def simulate_label(bars, signals, entry_index, cfg, spec, extra_slippage=ZERO):
    """One gross entry unit, venue-native fees and the replay's exit timing.

    This counterfactual is not a sized portfolio and ignores exchange dust.

    The additional holding limit exits at the opening quote after H full bars.
    Every label requires that opening bar even if an earlier stop is possible;
    this conservatively censors the same tail independent of outcomes.
    """
    spec.validate()
    if not extra_slippage.is_finite() or not ZERO <= extra_slippage < Decimal("0.05"):
        raise ValueError("Invalid counterfactual slippage")
    if not 1 <= entry_index < len(bars) or entry_index + spec.max_holding_bars >= len(bars):
        raise ValueError("Incomplete counterfactual horizon")
    half, slip, fee = cfg.costs.simulated_spread / 2, cfg.costs.slippage + extra_slippage, cfg.costs.taker_fee
    entry = bars[entry_index].open * (ONE + half) * (ONE + slip)
    distance = signals[entry_index - 1].atr * cfg.strategy.atr_multiple
    if distance <= 0 or entry - distance <= 0:
        raise ValueError("Invalid counterfactual stop")
    from .execution_costs import entry_cash_factor, entry_inventory_factor
    cost, peak, stop = entry * entry_cash_factor(cfg), entry, entry - distance
    deadline = entry_index + spec.max_holding_bars

    def outcome(bid, at, reason):
        proceeds = bid * (ONE - slip) * (ONE - fee) * entry_inventory_factor(cfg)
        return TradeLabel((proceeds / cost - ONE) * 100, at, reason)

    for index in range(entry_index, deadline + 1):
        bar = bars[index]
        opening_bid = bar.open * (ONE - half)
        if opening_bid <= stop:
            return outcome(opening_bid, bar.start, "protective_stop")
        if signals[index - 1].exit:
            return outcome(opening_bid, bar.start, "desired_flat")
        if index == deadline:
            return outcome(opening_bid, bar.start, "ml_holding_limit")
        if bar.low * (ONE - half) <= stop:
            return outcome(stop, bar.end, "historical_stop_approximation")
        peak = max(peak, bar.high)
        stop = max(stop, peak - distance)
    raise AssertionError("Counterfactual did not exit")


def build_samples(dataset: Dataset, cfg: Config, spec: LabelSpec = LabelSpec()) -> list[Sample]:
    cfg.validate()
    spec.validate()
    symbols = tuple(i.symbol for i in cfg.instruments)
    if set(dataset.bars) != set(symbols):
        raise ValueError("ML dataset/config symbols mismatch")
    reference = [bar.start for bar in dataset.bars[symbols[0]]]
    result = []
    for symbol in symbols:
        bars = dataset.bars[symbol]
        validate_bars(bars, cfg)
        if [bar.start for bar in bars] != reference:
            raise ValueError("ML requires an aligned multi-symbol timeline")
        state, signals, contexts = ContextState(cfg), [], []
        for bar in bars:
            signal, context = state.update(bar)
            signals.append(signal)
            contexts.append(context)
        for closed_index, signal in enumerate(signals):
            if not signal.enter or contexts[closed_index].regime == "warmup":
                continue
            entry_index = closed_index + 1
            deadline = signal.at + timedelta(seconds=cfg.seconds * spec.max_holding_bars)
            base = adverse = None
            if entry_index + spec.max_holding_bars < len(bars):
                base = simulate_label(bars, signals, entry_index, cfg, spec)
                adverse = simulate_label(bars, signals, entry_index, cfg, spec, spec.adverse_slippage)
            identifier = hashlib.sha256(f"{symbol}|{signal.at.isoformat()}".encode()).hexdigest()[:32]
            result.append(Sample(identifier, symbol, signal.at, entry_index,
                                 entry_features(contexts[closed_index], symbols), deadline, base, adverse))
    return sorted(result, key=lambda sample: (sample.feature_at, sample.symbol))


def purged_partition(samples, start, end, cfg, spec):
    """Purge every horizon crossing the next partition, across ALL symbols."""
    gap = timedelta(seconds=cfg.seconds * spec.embargo_bars)
    candidates = [sample for sample in samples if start <= sample.feature_at < end]
    kept = [sample for sample in candidates if sample.labelled and sample.label_end + gap <= end]
    return kept, {"candidates": len(candidates), "labelled_kept": len(kept),
                  "purged_or_censored": len(candidates) - len(kept),
                  "start": start.isoformat(), "end_exclusive": end.isoformat(),
                  "latest_label_end": max((s.label_end for s in kept), default=None)}


def uniqueness_weights(samples, horizon):
    """Average inverse GLOBAL interval concurrency, not independent-sample ESS."""
    concurrency = {}
    for sample in samples:
        for index in range(sample.entry_index, sample.entry_index + horizon + 1):
            concurrency[index] = concurrency.get(index, 0) + 1
    return [sum(1 / concurrency[index] for index in range(s.entry_index, s.entry_index + horizon + 1))
            / (horizon + 1) for s in samples]
