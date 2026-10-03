"""Causal, descriptive market context. Not a trained predictive model."""
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

from .config import Config
from .domain import Bar, Signal, ZERO
from .strategy import BreakoutState


@dataclass(frozen=True)
class MarketContext:
    symbol: str
    at: datetime
    regime: str
    atr_pct: Decimal
    trend_distance_atr: Decimal
    slope_atr: Decimal
    efficiency_20: Decimal
    volume_ratio_20: Decimal | None
    breakout_distance_atr: Decimal

    def payload(self) -> dict:
        return asdict(self)


class ContextState:
    """All inputs are closed bars; fixed descriptive rules, not optimized.

    High volatility: ATR/close >= 4%. Trend: positive/negative EMA distance
    and slope, with 20-bar efficiency >= 0.30. Everything else is range.
    These buckets feed diagnostics and optional research entry gates; they do
    not claim economic predictive power or change the default strategy.
    """
    schema = "closed-bar-context-v1"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.indicators = BreakoutState(cfg)
        self.recent = deque(maxlen=max(21, cfg.strategy.breakout_bars + 1))

    def update(self, bar: Bar) -> tuple[Signal, MarketContext]:
        signal = self.indicators.update(bar)
        self.recent.append(bar)
        state, recent = self.indicators, list(self.recent)
        atr = state.volatility
        atr_pct = atr / bar.close
        distance = (bar.close - state.moving) / atr if atr else ZERO
        slope = (state.emas[-1] - state.emas[0]) / atr if atr else ZERO
        changes = sum((abs(b.close - a.close) for a, b in zip(recent[-21:], recent[-20:])), ZERO) if len(recent) >= 21 else ZERO
        efficiency = abs(bar.close - recent[-21].close) / changes if changes else ZERO
        prior_volume = sum((b.volume for b in recent[-21:-1]), ZERO) / 20 if len(recent) >= 21 else ZERO
        volume_ratio = bar.volume / prior_volume if prior_volume else None
        prior_high = max((b.high for b in recent[-self.cfg.strategy.breakout_bars - 1:-1]), default=bar.high)
        breakout = (bar.close - prior_high) / atr if atr else ZERO
        if state.count < max(self.cfg.warmup, 21):
            regime = "warmup"
        elif atr_pct >= Decimal("0.04"):
            regime = "high_volatility"
        elif efficiency >= Decimal("0.30") and distance > 0 and slope > 0:
            regime = "trend_up"
        elif efficiency >= Decimal("0.30") and distance < 0 and slope < 0:
            regime = "trend_down"
        else:
            regime = "range"
        return signal, MarketContext(bar.symbol, bar.end, regime, atr_pct, distance, slope, efficiency, volume_ratio, breakout)
