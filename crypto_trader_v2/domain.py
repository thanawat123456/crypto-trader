from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum


ZERO = Decimal("0")
ONE = Decimal("1")


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite monetary value")
    return result


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    max_quantity: Decimal | None = None
    max_notional: Decimal | None = None

    def round_quantity(self, quantity: Decimal) -> Decimal:
        return (quantity / self.quantity_step).to_integral_value(rounding=ROUND_DOWN) * self.quantity_step


@dataclass(frozen=True)
class Bar:
    symbol: str
    start: datetime
    seconds: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.seconds)

    def validate(self) -> None:
        utc(self.start)
        if any(not value.is_finite() for value in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError("Non-finite candle value")
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, int) or self.seconds <= 0 or min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("Invalid bar price/timeframe")
        if not (self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise ValueError("Inconsistent OHLC")
        if self.volume < 0:
            raise ValueError("Negative volume")


@dataclass(frozen=True)
class Quote:
    symbol: str
    time: datetime
    bid: Decimal
    ask: Decimal

    def validate(self) -> None:
        utc(self.time)
        if not self.bid.is_finite() or not self.ask.is_finite() or self.bid <= 0 or self.ask < self.bid:
            raise ValueError("Invalid bid/ask")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Decimal:
        return (self.ask - self.bid) / self.mid


@dataclass(frozen=True)
class Signal:
    symbol: str
    at: datetime
    enter: bool
    exit: bool
    atr: Decimal
    reason: str
