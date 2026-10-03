"""Broker-neutral XAU/USD reference data and explicit CFD trade arithmetic.

This is NOT the crypto spot ledger or an order adapter. Unknown broker terms
are never filled with attractive defaults; reference quotes are not executable
quotes from the user's account. No live permission is represented here.
"""
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .development import _write_json
from .domain import utc


ZERO = Decimal(0)
ONE = Decimal(1)
PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class XauContract:
    broker: str
    account_type: str
    terms_source: str
    checked_at: str
    valid_until: str
    session_calendar_source: str
    rollover_calendar_source: str
    broker_timezone: str
    ounces_per_lot: Decimal
    lot_step: Decimal
    minimum_lots: Decimal
    margin_rate: Decimal
    commission_usd_per_lot_per_side: Decimal
    adverse_slippage_usd_per_ounce_per_side: Decimal
    maximum_spread_usd_per_ounce: Decimal
    swap_long_usd_per_lot_per_rollover_unit: Decimal
    swap_short_usd_per_lot_per_rollover_unit: Decimal
    symbol: str = "XAU/USD"
    account_currency: str = "USD"

    def validate(self, at=None):
        if (self.symbol != "XAU/USD" or self.account_currency != "USD"
                or any(not isinstance(v, str) or not v.strip() for v in
                       (self.broker, self.account_type, self.terms_source, self.session_calendar_source,
                        self.rollover_calendar_source, self.broker_timezone))):
            raise ValueError("XAU requires explicit broker/account/USD contract and calendar sources")
        ZoneInfo(self.broker_timezone)
        start, end = utc(self.checked_at), utc(self.valid_until)
        if start >= end or (at is not None and not start <= utc(at) < end):
            raise ValueError("XAU broker terms are unavailable or expired for this time")
        numbers = [getattr(self, k) for k in self.__dataclass_fields__ if isinstance(getattr(self, k), Decimal)]
        if len(numbers) != 9 or not all(v.is_finite() for v in numbers):
            raise ValueError("XAU contract requires finite explicit Decimal terms")
        if (min(self.ounces_per_lot, self.lot_step, self.minimum_lots, self.maximum_spread_usd_per_ounce) <= 0
                or not ZERO < self.margin_rate <= ONE or self.minimum_lots % self.lot_step
                or min(self.commission_usd_per_lot_per_side, self.adverse_slippage_usd_per_ounce_per_side) < 0):
            raise ValueError("Invalid XAU lot, margin, spread or execution cost terms")
        # Signed swap is a charge: positive subtracts from PnL, negative is a credit.
        return self


def load_xau_contract(path):
    raw = json.loads(path.read_bytes())
    numeric = {"ounces_per_lot", "lot_step", "minimum_lots", "margin_rate",
               "commission_usd_per_lot_per_side", "adverse_slippage_usd_per_ounce_per_side",
               "maximum_spread_usd_per_ounce", "swap_long_usd_per_lot_per_rollover_unit",
               "swap_short_usd_per_lot_per_rollover_unit"}
    if any(key not in raw or raw[key] is None or isinstance(raw[key], bool) for key in numeric):
        raise ValueError("Unknown XAU broker terms; fill the contract from verified broker specifications")
    return XauContract(**{key: Decimal(str(value)) if key in numeric else value for key, value in raw.items()}).validate()


def trade_economics(contract, side, lots, entry_bid, entry_ask, exit_bid, exit_ask, *,
                    opened_at, closed_at, rollover_units):
    """Exact USD PnL using both quote sides; not a backtest or sizing advice.

    Caller must supply rollover units from its broker calendar (e.g. triple
    charge), not merely the number of elapsed dates. Net is after commission,
    adverse slippage and signed funding; margin is collateral, not a fee.
    """
    contract.validate(opened_at).validate(closed_at)
    values = (lots, entry_bid, entry_ask, exit_bid, exit_ask, rollover_units)
    if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
        raise ValueError("XAU economics requires finite Decimal inputs")
    if (side not in {"long", "short"} or utc(opened_at) >= utc(closed_at) or rollover_units < 0
            or lots < contract.minimum_lots or lots % contract.lot_step
            or min(entry_bid, exit_bid) <= 0 or entry_ask < entry_bid or exit_ask < exit_bid):
        raise ValueError("Invalid XAU side/lot/time/quotes/rollover")
    if max(entry_ask - entry_bid, exit_ask - exit_bid) > contract.maximum_spread_usd_per_ounce:
        raise ValueError("XAU spread exceeds the declared execution scope")
    ounces = lots * contract.ounces_per_lot
    gross = ounces * (exit_bid - entry_ask if side == "long" else entry_bid - exit_ask)
    commission = lots * contract.commission_usd_per_lot_per_side * 2
    slippage = ounces * contract.adverse_slippage_usd_per_ounce_per_side * 2
    swap = lots * rollover_units * (contract.swap_long_usd_per_lot_per_rollover_unit if side == "long"
                                   else contract.swap_short_usd_per_lot_per_rollover_unit)
    return {"symbol": "XAU/USD", "side": side, "lots": lots, "ounces": ounces,
            "gross_quote_side_pnl_usd": gross, "commission_usd": commission,
            "adverse_slippage_usd": slippage, "signed_swap_charge_usd": swap,
            "net_pnl_usd": gross - commission - slippage - swap,
            "initial_margin_estimate_usd": ounces * (entry_ask if side == "long" else entry_bid) * contract.margin_rate,
            "terms": asdict(contract), "approved_for_live": False,
            "limitations": "Explicit quote example only; excludes path-dependent equity/margin calls, fill rejection and liquidation"}


def _read_side(path, seconds):
    raw = path.read_bytes()
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if (reader.fieldnames is None or not {"timestamp", *PRICE_COLUMNS}.issubset(reader.fieldnames)
                or len(set(reader.fieldnames)) != len(reader.fieldnames)):
            raise ValueError("XAU CSV requires timestamp,open,high,low,close with explicit UTC offsets")
        for number, row in enumerate(reader, 2):
            try:
                at = utc(row["timestamp"])
                prices = {key: Decimal(row[key]) for key in PRICE_COLUMNS}
                if (int(at.timestamp()) % seconds or at.microsecond
                        or not all(v.is_finite() and v > 0 for v in prices.values())
                        or not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]
                        or (rows and at <= rows[-1]["at"])):
                    raise ValueError("invalid grid/OHLC/order")
            except (ValueError, ArithmeticError, TypeError) as exc:
                raise ValueError(f"Invalid XAU row {number}: {exc}") from exc
            rows.append({"at": at, **prices})
    if not rows:
        raise ValueError("Empty XAU quote source")
    return rows, hashlib.sha256(raw).hexdigest()


def import_xau_reference(bid_path, ask_path, source, output, *, timeframe_minutes=60):
    """Import native bid/ask candles, preserving all closures/gaps verbatim.

    Crossed open/close quotes are checked. Independent intrabar extrema do not
    give synchronous spread measurements or exact stop-fill sequencing.
    Unknown gaps remain unclassified until the actual session calendar exists.
    """
    if output.exists():
        raise ValueError("XAU output exists; use a new directory")
    if (isinstance(timeframe_minutes, bool) or timeframe_minutes not in {1, 5, 15, 30, 60, 240, 1440}
            or not isinstance(source, str) or not source.strip()):
        raise ValueError("XAU requires timeframe and quote provenance")
    seconds = timeframe_minutes * 60
    bids, bid_hash = _read_side(bid_path, seconds)
    asks, ask_hash = _read_side(ask_path, seconds)
    if [r["at"] for r in bids] != [r["at"] for r in asks]:
        raise ValueError("XAU bid/ask timelines differ; never fabricate or align by forward fill")
    spreads, gaps = [], []
    for i, (bid, ask) in enumerate(zip(bids, asks)):
        if any(ask[key] < bid[key] for key in ("open", "close")):
            raise ValueError("Crossed XAU bid/ask quote endpoints")
        spreads += [ask[key] - bid[key] for key in ("open", "close")]
        if i and (bid["at"] - bids[i - 1]["at"]).total_seconds() != seconds:
            gaps.append({"after": bids[i - 1]["at"].isoformat(), "before": bid["at"].isoformat(),
                         "absent_intervals": int((bid["at"] - bids[i - 1]["at"]).total_seconds()) // seconds - 1,
                         "classification": "UNKNOWN: market closure vs missing data requires verified session calendar"})
    output.mkdir(parents=True)
    destination = output / "quotes.csv"
    with destination.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "symbol", *("bid_" + c for c in PRICE_COLUMNS), *("ask_" + c for c in PRICE_COLUMNS)))
        for bid, ask in zip(bids, asks):
            writer.writerow((bid["at"].isoformat(), "XAU/USD", *(bid[c] for c in PRICE_COLUMNS), *(ask[c] for c in PRICE_COLUMNS)))
    report = {"schema": 1, "symbol": "XAU/USD", "scope": "REFERENCE QUOTE QUALITY ONLY; not executed-trade or ML evidence",
              "source": source, "imported_at": datetime.now(timezone.utc).isoformat(), "timeframe_minutes": timeframe_minutes,
              "bid_source_sha256": bid_hash, "ask_source_sha256": ask_hash,
              "quotes_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "rows": len(bids),
              "first_open": bids[0]["at"].isoformat(), "last_open": bids[-1]["at"].isoformat(),
              "gap_count": len(gaps), "gaps": gaps, "fabricated_intervals": 0,
              "endpoint_spread_usd_per_ounce": {"minimum": min(spreads), "maximum": max(spreads),
                                                "mean": sum(spreads) / len(spreads)},
              "broker_contract_verified": False, "strategy_replay_ready": False, "models_trained": False,
              "approved_for_live": False,
              "blockers": ["Actual broker/account contract, lot/margin/commission/swap terms are unknown",
                           "Verified session and rollover calendar, including holidays and DST, is missing",
                           "Reference feed is not executable account bid/ask; intrabar timing is unknown",
                           "CFD margin ledger and purged out-of-sample study are not implemented for gold"]}
    _write_json(output / "manifest.json", report)
    _write_json(output / "report.json", report)
    return report
