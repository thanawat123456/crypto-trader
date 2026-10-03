from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import yaml

from .domain import Instrument, money


@dataclass(frozen=True)
class StrategyConfig:
    trend_period: int = 200
    slope_bars: int = 10
    breakout_bars: int = 20
    exit_bars: int = 10
    atr_period: int = 14
    atr_multiple: Decimal = Decimal("2")


@dataclass(frozen=True)
class RiskConfig:
    per_trade: Decimal = Decimal("0.0025")
    aggregate: Decimal = Decimal("0.0075")
    asset_cap: Decimal = Decimal("0.25")
    gross_cap: Decimal = Decimal("0.50")
    max_positions: int = 2
    daily_loss: Decimal = Decimal("0.01")
    pause_drawdown: Decimal = Decimal("0.04")
    emergency_drawdown: Decimal = Decimal("0.08")
    gap_buffer: Decimal = Decimal("0.005")


@dataclass(frozen=True)
class CostConfig:
    taker_fee: Decimal = Decimal("0.008")
    slippage: Decimal = Decimal("0.0005")
    simulated_spread: Decimal = Decimal("0.001")
    max_spread: Decimal = Decimal("0.003")
    source: str = "Published Kraken Tier 1 assumption; verify account rate"
    valid_until: str = "2026-10-07"


@dataclass(frozen=True)
class Config:
    venue: str = "kraken"
    quote_currency: str = "USD"
    initial_cash: Decimal = Decimal("300")
    timeframe_minutes: int = 240
    quote_max_age_seconds: int = 30
    candle_grace_seconds: int = 120
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    instruments: tuple[Instrument, ...] = (
        Instrument("BTC/USD", Decimal("0.00000001"), Decimal("0.0001"), Decimal("5")),
        Instrument("ETH/USD", Decimal("0.00000001"), Decimal("0.001"), Decimal("5")),
    )

    @property
    def seconds(self) -> int:
        return self.timeframe_minutes * 60

    @property
    def warmup(self) -> int:
        s = self.strategy
        return max(s.trend_period + s.slope_bars, s.breakout_bars + 2, s.exit_bars + 1, s.atr_period + 1)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), default=str, sort_keys=True).encode()).hexdigest()

    def validate(self) -> Config:
        if self.venue not in {"kraken", "binance_th"}:
            raise ValueError("Only Kraken / Binance TH public data are supported; live trading is unavailable")
        if self.venue == "binance_th" and (self.quote_currency != "USDT" or
                {i.symbol for i in self.instruments} != {"BTC/USDT", "ETH/USDT"}):
            raise ValueError("Binance TH research currently requires BTC/USDT and ETH/USDT together")
        if self.initial_cash <= 0 or isinstance(self.timeframe_minutes, bool) or self.timeframe_minutes not in (1, 5, 15, 30, 60, 240, 1440):
            raise ValueError("Invalid initial cash/timeframe")
        if self.quote_max_age_seconds <= 0 or self.candle_grace_seconds < 0:
            raise ValueError("Invalid data age limits")
        s, r, c = self.strategy, self.risk, self.costs
        for name in ("trend_period", "slope_bars", "breakout_bars", "exit_bars", "atr_period"):
            if not isinstance(getattr(s, name), int) or isinstance(getattr(s, name), bool) or getattr(s, name) < 1:
                raise ValueError(f"Invalid strategy.{name}")
        if s.atr_multiple <= 0:
            raise ValueError("Invalid ATR multiplier")
        for name in ("per_trade", "aggregate", "asset_cap", "gross_cap", "daily_loss", "pause_drawdown", "emergency_drawdown"):
            if not 0 < getattr(r, name) <= 1:
                raise ValueError(f"Invalid risk.{name}")
        if not 0 <= r.gap_buffer < 1 or not isinstance(r.max_positions, int) or isinstance(r.max_positions, bool) or not 0 < r.max_positions <= 100:
            raise ValueError("Invalid risk limits")
        if r.pause_drawdown >= r.emergency_drawdown or r.per_trade > r.aggregate or r.asset_cap > r.gross_cap:
            raise ValueError("Inconsistent risk limits")
        for name in ("taker_fee", "slippage", "simulated_spread", "max_spread"):
            if not 0 <= getattr(c, name) < Decimal("0.1"):
                raise ValueError(f"Invalid costs.{name}")
        if not c.source.strip():
            raise ValueError("Fee assumption requires a source")
        date.fromisoformat(c.valid_until)
        if not self.instruments or len({i.symbol for i in self.instruments}) != len(self.instruments):
            raise ValueError("Empty/duplicate instruments")
        for i in self.instruments:
            if i.symbol == "XAU/USD":
                raise ValueError("XAU/USD is not a Kraken crypto spot instrument; use the separate XAU reference research commands")
            parts = i.symbol.split("/")
            if len(parts) != 2 or parts[1] != self.quote_currency or min(i.quantity_step, i.min_quantity, i.min_notional) <= 0:
                raise ValueError("Invalid instrument or mixed quote currencies")
            for maximum, minimum in ((i.max_quantity, i.min_quantity), (i.max_notional, i.min_notional)):
                if maximum is not None and (not maximum.is_finite() or maximum < minimum):
                    raise ValueError("Invalid instrument maximum")
        return self


def _section(cls, raw: dict):
    defaults = cls()
    unknown = raw.keys() - defaults.__dataclass_fields__.keys()
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    values = {k: money(v) if isinstance(getattr(defaults, k), Decimal) else v for k, v in raw.items()}
    return cls(**values)


def load_config(path: str | None = None) -> Config:
    if path is None:
        return Config().validate()
    raw = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("Config must be a mapping")
    unknown = raw.keys() - Config.__dataclass_fields__.keys()
    if unknown:
        raise ValueError(f"Unknown config keys: {sorted(unknown)}")
    for key, cls in (("strategy", StrategyConfig), ("risk", RiskConfig), ("costs", CostConfig)):
        if key in raw:
            raw[key] = _section(cls, raw[key])
    if "initial_cash" in raw:
        raw["initial_cash"] = money(raw["initial_cash"])
    if "instruments" in raw:
        instruments = []
        for row in raw["instruments"]:
            if row.keys() - Instrument.__dataclass_fields__.keys():
                raise ValueError("Unknown instrument keys")
            instruments.append(Instrument(row["symbol"], *(money(row[k]) for k in
                ("quantity_step", "min_quantity", "min_notional")),
                *(money(row[k]) if row.get(k) is not None else None for k in ("max_quantity", "max_notional"))))
        raw["instruments"] = tuple(instruments)
    return Config(**raw).validate()
