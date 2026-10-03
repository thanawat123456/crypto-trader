from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, localcontext

from .config import Config
from .domain import Instrument, ONE, ZERO
from .execution_costs import entry_cash_factor, entry_inventory_factor


@dataclass(frozen=True)
class Sizing:
    quantity: Decimal
    cash_reserved: Decimal
    risk_reserved: Decimal
    reason: str


def size_entry(cfg: Config, instrument: Instrument, *, nav: Decimal, cash: Decimal,
               entry: Decimal, stop: Decimal, exposure: Decimal, open_risk: Decimal,
               open_count: int, asset_exposure: Decimal = ZERO) -> Sizing:
    reject = lambda reason: Sizing(ZERO, ZERO, ZERO, reason)
    r, c = cfg.risk, cfg.costs
    if nav <= 0 or entry <= stop or stop <= 0 or not asset_exposure.is_finite() or asset_exposure < 0:
        return reject("invalid_nav_or_stop")
    if open_count >= r.max_positions:
        return reject("max_positions")
    cash_factor, inventory_factor = entry_cash_factor(cfg), entry_inventory_factor(cfg)
    loss_per_unit = entry * cash_factor - stop * (ONE - c.slippage) * (ONE - c.taker_fee) * inventory_factor
    loss_per_unit += entry * r.gap_buffer
    risk_budget = min(nav * r.per_trade, nav * r.aggregate - open_risk)
    cash_budget = min(cash, nav * r.asset_cap - asset_exposure, nav * r.gross_cap - exposure)
    if risk_budget <= 0 or cash_budget <= 0:
        return reject("portfolio_budget")
    ceiling = min(risk_budget / loss_per_unit, cash_budget / (entry * cash_factor))
    if instrument.max_quantity is not None:
        ceiling = min(ceiling, instrument.max_quantity)
    if instrument.max_notional is not None:
        ceiling = min(ceiling, instrument.max_notional / entry)
    quantity = instrument.round_quantity(ceiling)
    if quantity < instrument.min_quantity or quantity * entry < instrument.min_notional:
        return reject("below_exchange_minimum")
    with localcontext() as precision:
        precision.prec = 64
        exact_cash = quantity * entry * cash_factor
    with localcontext() as rounding:
        rounding.rounding = ROUND_CEILING
        cash_reserved = +exact_cash
    if cash_reserved > cash_budget:
        return reject("reservation_rounding_budget")
    return Sizing(quantity, cash_reserved, quantity * loss_per_unit, "approved")
