from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .config import Config
from .domain import ONE, Quote, ZERO
from .storage import Store


@dataclass(frozen=True)
class ExecutionScenario:
    """Registered stress assumptions, NOT measured liquidity.

    Entry fraction caps its intent. Exit capacity is the fraction of original
    executed entry units per attempt, so repeated exits can actually finish.
    """
    entry_fill_fraction: Decimal = ONE
    exit_fill_fraction: Decimal = ONE
    miss_every_entry: int = 0
    extra_slippage: Decimal = ZERO

    def validate(self):
        for fraction in (self.entry_fill_fraction, self.exit_fill_fraction):
            if not fraction.is_finite() or not ZERO <= fraction <= ONE:
                raise ValueError("Fill fractions must be finite and between 0 and 1")
        if (isinstance(self.miss_every_entry, bool) or not isinstance(self.miss_every_entry, int) or self.miss_every_entry < 0
                or not self.extra_slippage.is_finite() or not ZERO <= self.extra_slippage < Decimal("0.05")):
            raise ValueError("Invalid execution stress scenario")
        return self

    def payload(self):
        return asdict(self)


class PaperBroker:
    """Deterministic taker simulator shared by historical and public-data paper runs.

    This model does not emulate an exchange queue. Never treat simulated fills as
    proof of live execution. A price ceiling can cause an IOC non-fill.
    """
    execution_profile = ""
    def __init__(self, cfg: Config, scenario: ExecutionScenario | None = None, instruments=None):
        self.cfg = cfg
        self.scenario = (scenario or ExecutionScenario()).validate()
        self.instruments = instruments or {i.symbol: i for i in cfg.instruments}
        # Optional public top-of-book capacity. Timestamp binding prevents reuse
        # with a different quote. This is NOT an exchange queue/depth model.
        self.observed_capacity = None

    def price(self, quote: Quote, side: str) -> Decimal:
        quote.validate()
        if side not in {"buy", "sell"}:
            raise ValueError("Invalid execution side")
        slip = self.cfg.costs.slippage + self.scenario.extra_slippage
        return quote.ask * (ONE + slip) if side == "buy" else quote.bid * (ONE - slip)

    def exit_price_limit(self, quote):
        return None

    def execution_rejection(self, store, order, quote, price):
        return ""

    def commission(self, symbol, side, quantity, price):
        base_fee = self.cfg.venue == "binance_th" and side == "buy"
        return (quantity if base_fee else quantity * price) * self.cfg.costs.taker_fee

    def execute(self, store: Store, identifier: str, quote: Quote, *, max_quantity: Decimal | None = None):
        if store.get_meta("execution_profile", "") != self.execution_profile:
            raise ValueError("Simulator/ledger execution profile mismatch")
        order = store.order(identifier)
        if order["status"] in ("CANCELED", "FILLED"):
            return
        if order["status"] == "UNKNOWN":
            raise ValueError("Reconcile UNKNOWN order before attempting execution")
        if quote.symbol != order["symbol"]:
            raise ValueError("Order/quote instrument mismatch")
        quote.validate()
        if max_quantity is not None and (not max_quantity.is_finite() or max_quantity < 0):
            raise ValueError("Invalid fill capacity")
        if self.observed_capacity is not None:
            observed = self.observed_capacity.get((quote.symbol, order["side"]))
            if observed is None or observed[0] != quote.time:
                raise ValueError("Missing/stale observed fill capacity")
            capacity = observed[1]
            if not capacity.is_finite() or capacity < ZERO:
                raise ValueError("Invalid observed fill capacity")
            max_quantity = min(max_quantity, capacity) if max_quantity is not None else capacity
        exit_lots = store.exit_lots(order["symbol"]) if order["side"] == "sell" and self.cfg.venue == "binance_th" else []
        episode = order["id"] if order["side"] == "buy" else exit_lots[0]["episode_id"] if exit_lots else store.positions()[order["symbol"]]["episode_id"]
        store.record_execution(identifier, episode, quote, "attempted")
        store.acknowledge(identifier)
        quantity = Decimal(order["quantity"]) - Decimal(order["filled"])
        price = self.price(quote, order["side"])
        rejection = self.execution_rejection(store, order, quote, price)
        if rejection:
            store.cancel(identifier)
            store.record_execution(identifier, episode, quote, rejection)
            return
        limit = Decimal(order["price_limit"]) if order["price_limit"] is not None else None
        if limit is not None and ((order["side"] == "buy" and price > limit) or (order["side"] == "sell" and price < limit)):
            store.cancel(identifier)
            store.record_execution(identifier, episode, quote, "price_limit_no_fill")
            return
        if order["side"] == "buy" and self.scenario.miss_every_entry:
            with store.transaction():
                key = "execution_attempt:" + identifier
                attempt = int(store.get_meta(key, "0"))
                if not attempt:
                    attempt = int(store.get_meta("execution_entry_attempts", "0")) + 1
                    store.set_meta("execution_entry_attempts", str(attempt))
                    store.set_meta(key, str(attempt))
            if attempt % self.scenario.miss_every_entry == 0:
                store.cancel(identifier)
                store.record_execution(identifier, episode, quote, "scenario_entry_no_fill")
                return
        fraction = self.scenario.entry_fill_fraction if order["side"] == "buy" else self.scenario.exit_fill_fraction
        if order["side"] == "sell" and fraction != ONE:
            original = sum((Decimal(store.order(lot["episode_id"])["filled"]) for lot in exit_lots), ZERO) if exit_lots else Decimal(store.order(episode)["filled"])
            quantity = min(quantity, original * fraction)
        else:
            quantity *= fraction
        if max_quantity is not None:
            quantity = min(quantity, max_quantity)
        if fraction != ONE or max_quantity is not None or self.cfg.venue == "binance_th":
            quantity = self.instruments[order["symbol"]].round_quantity(quantity)
        if self.cfg.venue == "binance_th":
            instrument = self.instruments[order["symbol"]]
            requested = Decimal(order["quantity"])
            # Exchange minimums constrain the ORDER, not each individual match.
            # A valid IOC can partially fill below minimum notional. Fill units
            # are conservatively step-rounded; queue/matching remains unproven.
            if (requested != instrument.round_quantity(requested) or requested < instrument.min_quantity or requested * price < instrument.min_notional or
                    (instrument.max_quantity is not None and requested > instrument.max_quantity) or
                    (instrument.max_notional is not None and requested * price > instrument.max_notional)):
                store.cancel(identifier)
                store.record_execution(identifier, episode, quote, "exchange_quantity_or_notional_no_fill")
                return
        if quantity > ZERO:
            trade_id = identifier + ":" + order["filled"]
            base_fee = self.cfg.venue == "binance_th" and order["side"] == "buy"
            fee = self.commission(order["symbol"], order["side"], quantity, price)
            if fee >= (quantity if base_fee else quantity * price):
                store.cancel(identifier)
                store.record_execution(identifier, episode, quote, "fee_exceeds_received_asset_no_fill")
                return
            applied = store.apply_fill(identifier, trade_id, quantity, price,
                             fee,
                             order["symbol"].split("/")[0] if base_fee else self.cfg.quote_currency, quote.time)
            if applied and self.observed_capacity is not None:
                self.observed_capacity[(quote.symbol, order["side"])] = (quote.time, capacity - quantity)
        # This simulator uses IOC. Partial fills remain real positions in the ledger.
        if store.order(identifier)["status"] != "FILLED":
            store.cancel(identifier)
        final = store.order(identifier)
        outcome = "filled" if final["status"] == "FILLED" else "partial_ioc" if Decimal(final["filled"]) > 0 else "capacity_no_fill"
        store.record_execution(identifier, episode, quote, outcome)
