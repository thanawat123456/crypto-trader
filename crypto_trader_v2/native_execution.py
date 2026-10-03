"""Opt-in native LIMIT-IOC engineering profile, never live execution.

Commission CEILING is a fixed adverse assumption, not a proved exchange rule.
referencePrice is NOT assumed to equal a filter's weightedAveragePrice.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .broker import PaperBroker
from .domain import ZERO, money, utc


PROFILE = "native-limit-ioc-ceil-fee-v1"
KNOWN_FILTERS = {"LOT_SIZE", "PRICE_FILTER", "MIN_NOTIONAL", "NOTIONAL", "PERCENT_PRICE",
                 "PERCENT_PRICE_BY_SIDE", "MARKET_LOT_SIZE", "MAX_NUM_ORDERS", "MAX_NUM_ALGO_ORDERS"}


def precision(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 18:
        raise ValueError("Missing/invalid native commission precision")
    return value


@dataclass(frozen=True)
class AverageBasis:
    """Explicit independently verified basis (software fixtures for now).

    The public observer NEVER constructs this from referencePrice/24h ticker.
    A future implementation needs evidence of the exact window/source first.
    """
    price: Decimal
    minutes: int
    at: object
    evidence: str

    def __post_init__(self):
        if (isinstance(self.minutes, bool) or not isinstance(self.minutes, int) or self.minutes < 0
                or not self.price.is_finite() or self.price <= 0 or not isinstance(self.evidence, str) or not self.evidence.strip()):
            raise ValueError("Invalid verified averaging basis")
        utc(self.at)


class NativeRules:
    def __init__(self, row, *, average=None, max_age_seconds=30):
        if row.get("status") != "TRADING" or row.get("type") != "GLOBAL" or "LIMIT" not in row.get("orderTypes", []):
            raise ValueError("Native LIMIT IOC market unavailable")
        self.row, self.average, self.max_age_seconds = row, average, max_age_seconds
        self.base_precision = precision(row.get("baseCommissionPrecision"))
        self.quote_precision = precision(row.get("quoteCommissionPrecision"))
        filters = row.get("filters", [])
        self.filters = {item["filterType"]: item for item in filters}
        if len(self.filters) != len(filters) or self.filters.keys() - KNOWN_FILTERS or not {"LOT_SIZE", "PRICE_FILTER"} <= self.filters.keys():
            raise ValueError("Missing/duplicate/unsupported native execution filter")
        if ("MIN_NOTIONAL" in self.filters) == ("NOTIONAL" in self.filters):
            raise ValueError("Missing/ambiguous native notional filter")
        for item in filters:
            for key in ("minQty", "maxQty", "stepSize", "minPrice", "maxPrice", "tickSize", "minNotional", "maxNotional",
                        "multiplierUp", "multiplierDown", "bidMultiplierUp", "bidMultiplierDown", "askMultiplierUp", "askMultiplierDown"):
                if key in item and money(item[key]) < 0:
                    raise ValueError("Negative native execution bound")
        lot = self.filters["LOT_SIZE"]
        if money(lot["stepSize"]) <= 0 or money(lot["maxQty"]) < money(lot["minQty"]):
            raise ValueError("Invalid native lot constraints")
        for name in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
            if name in self.filters:
                minutes = self.filters[name].get("avgPriceMins")
                if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
                    raise ValueError("Invalid filter averaging interval")
                prefixes = ("bid", "ask") if name == "PERCENT_PRICE_BY_SIDE" else ("",)
                for prefix in prefixes:
                    down = money(self.filters[name][prefix+"MultiplierDown" if prefix else "multiplierDown"])
                    up = money(self.filters[name][prefix+"MultiplierUp" if prefix else "multiplierUp"])
                    if not ZERO < down <= up:
                        raise ValueError("Invalid native percent-price bounds")

    def price(self, value, side):
        row = self.filters["PRICE_FILTER"]
        tick, offset = money(row["tickSize"]), money(row["minPrice"])
        if tick == 0:
            return value
        rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
        return offset + ((value-offset)/tick).to_integral_value(rounding=rounding) * tick

    def fee(self, quantity, price, rate, side):
        digits = self.base_precision if side == "buy" else self.quote_precision
        value = (quantity if side == "buy" else quantity * price) * rate
        return value.quantize(Decimal(10) ** -digits, rounding=ROUND_CEILING)

    def reject(self, quantity, limit, side, at, *, active_count=1):
        if side not in {"buy", "sell"} or not quantity.is_finite() or not limit.is_finite() or min(quantity, limit) <= 0:
            return "invalid_limit_ioc_order"
        lot = self.filters["LOT_SIZE"]
        minimum, maximum, step = (money(lot[key]) for key in ("minQty", "maxQty", "stepSize"))
        if not minimum <= quantity <= maximum or (quantity-minimum) % step:
            return "native_lot_size_rejected"
        price = self.filters["PRICE_FILTER"]
        minimum, maximum, tick = (money(price[key]) for key in ("minPrice", "maxPrice", "tickSize"))
        if ((minimum and limit < minimum) or (maximum and limit > maximum) or (tick and (limit-minimum) % tick)):
            return "native_price_filter_rejected"
        notional = self.filters.get("NOTIONAL", self.filters.get("MIN_NOTIONAL"))
        value = quantity * limit  # Explicit LIMIT order, not a MARKET average proxy.
        if value < money(notional["minNotional"]) or ("maxNotional" in notional and value > money(notional["maxNotional"])):
            return "native_limit_notional_rejected"
        for name in ("PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"):
            if name not in self.filters:
                continue
            item, average = self.filters[name], self.average
            if average is None or not average.evidence or average.minutes != item["avgPriceMins"]:
                return "filter_average_basis_unverified"
            if not average.price.is_finite() or average.price <= 0 or not 0 <= (utc(at)-utc(average.at)).total_seconds() <= self.max_age_seconds:
                return "filter_average_basis_stale_or_invalid"
            prefix = ("bid" if side == "buy" else "ask") if name == "PERCENT_PRICE_BY_SIDE" else ""
            down = money(item[prefix + "MultiplierDown" if prefix else "multiplierDown"])
            up = money(item[prefix + "MultiplierUp" if prefix else "multiplierUp"])
            if not down * average.price <= limit <= up * average.price:
                return "native_percent_price_rejected"
        maximum_orders = self.filters.get("MAX_NUM_ORDERS", {}).get("maxNumOrders")
        if maximum_orders is not None and (isinstance(maximum_orders, bool) or not isinstance(maximum_orders, int) or maximum_orders < active_count):
            return "native_max_orders_rejected"
        return ""

    def payload(self):
        return {"profile": PROFILE, "order_type": "LIMIT_IOC", "base_commission_precision": self.base_precision,
                "quote_commission_precision": self.quote_precision, "fee_rounding": "adverse CEILING assumption, account behavior UNVERIFIED",
                "average_basis_verified": self.average is not None,
                "referencePrice_interpretation": "public reference only; NOT substituted for filter weighted average"}


class NativePaperBroker(PaperBroker):
    execution_profile = PROFILE

    def __init__(self, cfg, rules, instruments=None):
        if cfg.venue != "binance_th":
            raise ValueError("Native execution profile requires Binance TH")
        super().__init__(cfg, instruments=instruments)
        self.rules = rules
        if set(rules) != {i.symbol for i in cfg.instruments} or any(rule.row["symbol"] != symbol.replace("/", "") for symbol, rule in rules.items()):
            raise ValueError("Native execution rule instrument mismatch")

    def price(self, quote, side):
        return self.rules[quote.symbol].price(super().price(quote, side), side)

    def exit_price_limit(self, quote):
        return self.price(quote, "sell")

    def commission(self, symbol, side, quantity, price):
        return self.rules[symbol].fee(quantity, price, self.cfg.costs.taker_fee, side)

    def execution_rejection(self, store, order, quote, price):
        if store.get_meta("execution_profile", "") != PROFILE:
            raise ValueError("Native profile cannot execute an unbound/default ledger")
        limit = money(order["price_limit"]) if order["price_limit"] is not None else ZERO
        return self.rules[quote.symbol].reject(money(order["quantity"]), limit, order["side"], quote.time,
                                               active_count=len(store.orders(active=True)))
