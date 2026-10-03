"""Causal trend breakout. Input consists exclusively of closed bars."""
from decimal import Decimal
from collections import deque

from .config import Config
from .domain import Bar, Signal, ZERO


def evaluate(bars: list[Bar], cfg: Config) -> Signal:
    if not bars:
        raise ValueError("No closed bars")
    state = BreakoutState(cfg)
    for bar in bars:
        signal = state.update(bar)
    return signal


class BreakoutState:
    """Incremental version of the same causal indicator formulas.

    Historical replay updates once per closed bar rather than recalculating the
    entire history. Public paper can still rebuild from its closed-bar window.
    """
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.count = 0
        self.bars = deque(maxlen=max(cfg.strategy.breakout_bars + 2, cfg.strategy.exit_bars + 1))
        self.emas = deque(maxlen=cfg.strategy.slope_bars + 1)
        self.moving = self.volatility = self.previous = None
        self.symbol = None
        self.last_end = None

    def update(self, bar: Bar) -> Signal:
        if self.symbol is not None and (bar.symbol != self.symbol or bar.start != self.last_end):
            raise ValueError("Indicator stream must be contiguous and single-symbol")
        self.symbol, self.last_end = bar.symbol, bar.end
        s = self.cfg.strategy
        alpha = Decimal(2) / (s.trend_period + 1)
        self.moving = bar.close if self.moving is None else self.moving + alpha * (bar.close - self.moving)
        previous = self.previous if self.previous is not None else bar.close
        true_range = max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous))
        self.volatility = true_range if self.volatility is None else self.volatility + (true_range - self.volatility) / s.atr_period
        self.previous = bar.close
        self.bars.append(bar)
        self.emas.append(self.moving)
        self.count += 1
        if self.count < self.cfg.warmup:
            return Signal(bar.symbol, bar.end, False, False, ZERO, "warmup")
        recent = list(self.bars)
        prior_high = max(b.high for b in recent[-s.breakout_bars - 1:-1])
        prior_low = min(b.low for b in recent[-s.exit_bars - 1:-1])
        earlier_high = max(b.high for b in recent[-s.breakout_bars - 2:-2])
        trend = bar.close > self.emas[-1] and self.emas[-1] > self.emas[0]
        fresh = recent[-2].close <= earlier_high
        enter = trend and bar.close > prior_high and fresh and self.volatility > 0
        exit_position = bar.close < prior_low
        return Signal(bar.symbol, bar.end, enter, exit_position, self.volatility,
                      "breakout" if enter else "desired_flat" if exit_position else "hold")
