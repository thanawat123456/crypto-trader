"""One coordinator and one accounting/risk path for both simulation modes."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import hashlib
import json

from .broker import ExecutionScenario, PaperBroker
from .config import Config
from .data import validate_bars
from .domain import Bar, Instrument, Mode, ONE, Quote, Signal, ZERO, money, utc
from .risk import size_entry
from .storage import Store
from .strategy import evaluate
from .entry_policy import EntryPolicy, context_from_bars
from .execution_costs import entry_inventory_factor


def intent_id(symbol: str, action: str, key: str) -> str:
    return hashlib.sha256(f"{symbol}|{action}|{key}".encode()).hexdigest()[:32]


class Coordinator:
    def __init__(self, store: Store, cfg: Config, instruments: dict[str, Instrument] | None = None, *,
                 execution: ExecutionScenario | None = None, entry_policy: EntryPolicy | None = None):
        self.store, self.cfg = store, cfg
        self.instruments = instruments or {i.symbol: i for i in cfg.instruments}
        self.broker = PaperBroker(cfg, execution, self.instruments)
        self.entry_policy = (entry_policy or EntryPolicy()).validate_for_config(cfg)
        identity = json.dumps(self.entry_policy.payload(), default=str, sort_keys=True)
        saved = store.get_meta("entry_policy")
        if saved and saved != identity:
            raise ValueError("Database entry policy mismatch; use its original policy or a new database")
        if not saved and self.entry_policy.enabled and not store.created:
            raise ValueError("Existing run has no bound entry policy; initialize a new database")
        if not saved and store.created:
            with store.transaction():
                store.set_meta("entry_policy", identity)

    def sell(self, symbol: str, quote: Quote, reason: str):
        position = self.store.positions().get(symbol)
        native = self.cfg.venue == "binance_th"
        lots = self.store.exit_lots(symbol) if native else []
        if not position and not lots:
            return
        quantity = position["quantity"] if position else ZERO
        if native:
            instrument = self.instruments[symbol]
            quantity = sum((lot["quantity"] for lot in lots), ZERO)
            tradable = instrument.round_quantity(quantity)
            price = self.broker.price(quote, "sell")
            with self.store.transaction():
                self.store.set_meta("exit_required:" + symbol, reason if position else "")
            if tradable < instrument.min_quantity or tradable * self.broker.price(quote, "sell") < instrument.min_notional:
                if position:
                    self.store.park_dust(symbol, quote, instrument, price, reason)
                return
            quantity = tradable
            if instrument.max_quantity is not None:
                quantity = min(quantity, instrument.round_quantity(instrument.max_quantity))
            if instrument.max_notional is not None:
                quantity = min(quantity, instrument.round_quantity(instrument.max_notional / price))
            if quantity < instrument.min_quantity or quantity * price < instrument.min_notional:
                fingerprint = f"maxima|{quantity}|{instrument.max_quantity}|{instrument.max_notional}"
                if self.store.get_meta("residual_block:" + symbol) != fingerprint:
                    with self.store.transaction():
                        self.store.set_meta("residual_block:" + symbol, fingerprint)
                    self.store.decision(symbol, quote.time, "BLOCKED", "exchange_maximum_prevents_tradable_exit")
                return
        episode_key = "|".join(lot["episode_id"] for lot in lots) if native else position["episode_id"]
        identifier = intent_id(symbol, "sell", episode_key + "|" + quote.time.isoformat())
        with self.store.transaction():
            self.store.set_meta("exit_required:" + symbol, reason)
        if not self.store.intent(identifier, symbol, "sell", quantity, quote.time, reason,
                                 price_limit=self.broker.exit_price_limit(quote)):
            return
        self.broker.execute(self.store, identifier, quote)
        filled = money(self.store.order(identifier)["filled"])
        self.store.decision(symbol, quote.time, "SELL" if filled > 0 else "NO_FILL", reason)
        remainder = self.store.positions().get(symbol)
        if native and remainder and not any(o["symbol"] == symbol for o in self.store.orders(active=True)):
            available = instrument.round_quantity(remainder["quantity"])
            if available < instrument.min_quantity or available * price < instrument.min_notional:
                self.store.park_dust(symbol, quote, instrument, price, reason)
        if symbol not in self.store.positions():
            with self.store.transaction():
                self.store.set_meta("exit_required:" + symbol, "")

    def cycle(self, now: datetime, histories: dict[str, list[Bar]], quotes: dict[str, Quote], *, trail_quotes: bool = True,
              prepared_signals: dict[str, Signal] | None = None, contexts: dict | None = None):
        now = utc(now)
        previous = self.store.get_meta("last_cycle")
        if previous and now < utc(previous):
            raise ValueError("Cannot replay a cycle earlier than the persisted watermark")
        snapshot_symbols = set(self.instruments)
        if set(quotes) - snapshot_symbols or set(histories) - snapshot_symbols:
            raise ValueError("Unconfigured market data supplied")
        positions = self.store.positions()
        active_orders = self.store.orders(active=True)
        for symbol in snapshot_symbols:
            if symbol not in positions and self.store.get_meta("exit_required:" + symbol) and not any(o["symbol"] == symbol for o in active_orders):
                with self.store.transaction():
                    self.store.set_meta("exit_required:" + symbol, "")
        valid = {}
        for symbol, quote in quotes.items():
            quote.validate()
            if quote.symbol != symbol:
                raise ValueError("Quote key mismatch")
            age = (now - quote.time).total_seconds()
            if 0 <= age <= self.cfg.quote_max_age_seconds:
                valid[symbol] = quote
            else:
                self.store.decision(symbol, now, "REJECT", "stale_or_future_quote")

        marks = {s: q.mid for s, q in valid.items()}
        last_nav_key, cached_nav = None, None

        def refresh_nav():
            nonlocal last_nav_key, cached_nav
            key = (self.store.cash, tuple(sorted(self.store.inventory().items())))
            if key != last_nav_key:
                cached_nav = self.store.mark_nav(now, marks)
                last_nav_key = key
            return cached_nav

        premark = refresh_nav()
        recovery_block = self.store.entry_block() or ("nav_unavailable" if not premark["complete"] else "")
        if set(valid) != set(self.instruments):
            recovery_block = recovery_block or "incomplete_quote_snapshot"
        if self.store.mode == Mode.PAPER and now.date() > date.fromisoformat(self.cfg.costs.valid_until):
            recovery_block = recovery_block or "fee_assumption_expired"
        for symbol in self.instruments:
            bars = histories.get(symbol, [])
            try:
                validate_bars(bars, self.cfg)
                if bars[-1].symbol != symbol:
                    raise ValueError("Candle key/instrument mismatch")
                if bars[-1].end > now or (now - bars[-1].end).total_seconds() >= self.cfg.seconds + self.cfg.candle_grace_seconds:
                    raise ValueError("Invalid candle availability")
            except ValueError:
                recovery_block = recovery_block or "incomplete_market_snapshot"

        # Paper intents are durable. Recover using a fresh quote, or expire an
        # entry after its deadline. UNKNOWN is deliberately not blindly retried.
        for order in self.store.orders(active=True):
            if order["status"] == "UNKNOWN":
                continue
            if order["side"] == "buy" and (recovery_block or (now - utc(order["created_at"])).total_seconds() > self.cfg.quote_max_age_seconds):
                self.store.cancel(order["id"])
            elif order["symbol"] in valid:
                if order["side"] == "buy" and self.entry_policy.enabled:
                    recovered_signal, recovered_context = context_from_bars(histories[order["symbol"]], self.cfg)
                    quote = valid[order["symbol"]]
                    check = self.entry_policy.evaluate(recovered_signal, recovered_context, quote, self.cfg, self.broker.scenario)
                    if (not recovered_signal.enter or
                            order["id"] != intent_id(order["symbol"], "buy", recovered_signal.at.isoformat()) or not check.allowed):
                        self.store.cancel(order["id"])
                        self.store.decision(order["symbol"], now, "REJECT", "recovered_entry_policy_rejected")
                        continue
                self.broker.execute(self.store, order["id"], valid[order["symbol"]])

        nav = refresh_nav()
        emergency = self.store.get_meta("risk_paused") == "emergency_drawdown"
        if emergency:
            for order in self.store.orders(active=True):
                if order["side"] == "buy" and order["status"] != "UNKNOWN":
                    self.store.cancel(order["id"])

        signals, data_problems, entry_contexts = {}, {}, {}
        for symbol in self.instruments:
            bars = histories.get(symbol, [])
            if not bars:
                data_problems[symbol] = "missing_closed_bars"
                continue
            try:
                validate_bars(bars, self.cfg)
                if bars[-1].symbol != symbol:
                    raise ValueError("Candle key/instrument mismatch")
                if bars[-1].end > now:
                    raise ValueError("Unclosed candle supplied to strategy")
                if (now - bars[-1].end).total_seconds() >= self.cfg.seconds + self.cfg.candle_grace_seconds:
                    raise ValueError("Stale candle history")
                if prepared_signals is not None:
                    signals[symbol] = prepared_signals[symbol]
                elif self.entry_policy.enabled:
                    signals[symbol], entry_contexts[symbol] = context_from_bars(bars, self.cfg)
                else:
                    signals[symbol] = evaluate(bars, self.cfg)
                if signals[symbol].symbol != symbol or signals[symbol].at != bars[-1].end:
                    raise ValueError("Prepared signal is not from the latest closed bar")
                if contexts is not None:
                    entry_contexts[symbol] = contexts.get(symbol)
                if entry_contexts.get(symbol) is not None:
                    self.store.record_context(entry_contexts[symbol], signals[symbol])
                signal_key = "last_signal:" + symbol
                if self.store.get_meta(signal_key) != signals[symbol].at.isoformat():
                    self.store.decision(symbol, now, "OBSERVE", signals[symbol].reason)
                    with self.store.transaction():
                        self.store.set_meta(signal_key, signals[symbol].at.isoformat())
            except ValueError as error:
                data_problems[symbol] = str(error)
                self.store.decision(symbol, now, "REJECT", str(error))

        # Stops keep working even when strategy data or fee metadata expires.
        for symbol, position in self.store.positions().items():
            if symbol not in valid or any(o["symbol"] == symbol for o in self.store.orders(active=True)):
                continue
            quote = valid[symbol]
            signal = signals.get(symbol)
            required_exit = self.store.get_meta("exit_required:" + symbol)
            holding_bars = getattr(self.entry_policy, "max_holding_bars", None)
            holding_expired = holding_bars is not None and (now - utc(position["opened_at"])).total_seconds() >= holding_bars * self.cfg.seconds
            if emergency or required_exit or quote.bid <= position["stop"] or (signal and signal.exit) or holding_expired:
                reason = "emergency_drawdown" if emergency else required_exit or ("protective_stop" if quote.bid <= position["stop"] else "desired_flat" if signal and signal.exit else "ml_holding_limit")
                self.sell(symbol, quote, reason)
            elif trail_quotes:
                self.store.update_stop(symbol, quote.mid)

        # A standalone dust sale occurs only when the aggregate is actually
        # tradable. No top-up buys and no exchange conversion are performed.
        if self.cfg.venue == "binance_th":
            for symbol in self.instruments:
                if symbol in valid and symbol not in self.store.positions() and self.store.dust_lots(symbol) and not any(o["symbol"] == symbol for o in self.store.orders(active=True)):
                    self.sell(symbol, valid[symbol], "dust_fifo_sweep")

        nav = refresh_nav()
        # A complete portfolio mark is required; unavailable inventory is never
        # silently treated as zero. Entries share the same sorted snapshot.
        block = self.store.entry_block()
        if not nav["complete"]:
            block = "nav_unavailable"
        if self.store.mode == Mode.PAPER and now.date() > date.fromisoformat(self.cfg.costs.valid_until):
            block = "fee_assumption_expired"
        if data_problems:
            block = block or "incomplete_market_snapshot"
        if set(valid) != set(self.instruments):
            block = block or "incomplete_quote_snapshot"

        for symbol in sorted(signals):
            signal = signals[symbol]
            if not signal.enter or symbol in self.store.positions():
                continue
            if block or symbol not in valid:
                self.store.decision(symbol, now, "REJECT", block or "quote_unavailable")
                continue
            if any(o["symbol"] == symbol for o in self.store.orders(active=True)):
                continue
            last_exit = self.store.get_meta("last_exit:" + symbol)
            if last_exit and signal.at <= utc(last_exit):
                self.store.decision(symbol, now, "REJECT", "wait_for_new_closed_bar")
                continue
            quote = valid[symbol]
            if quote.spread_pct > self.cfg.costs.max_spread:
                self.store.decision(symbol, now, "REJECT", "spread_exceeds_budget")
                continue
            evaluation = self.entry_policy.evaluate(signal, entry_contexts.get(symbol), quote, self.cfg, self.broker.scenario)
            self.store.record_entry_evaluation(signal, quote, evaluation)
            if not evaluation.allowed:
                self.store.decision(symbol, now, "REJECT", evaluation.reason)
                continue
            entry = self.broker.price(quote, "buy")
            distance = signal.atr * self.cfg.strategy.atr_multiple
            stop = entry - distance
            current = refresh_nav()
            positions = self.store.positions()
            cash_reserved, risk_reserved = self.store.reserved()
            pending_entries = [o for o in self.store.orders(active=True) if o["side"] == "buy"]
            # Preserve each position's original risk budget after a partial fill,
            # rather than estimating zero risk merely because its stop trailed up.
            open_risk = ZERO
            for p in positions.values():
                order = self.store.order(p["episode_id"])
                initial_qty = money(order["quantity"]) * entry_inventory_factor(self.cfg)
                original_risk = money(self.store.get_meta("initial_risk:" + order["id"], "0"))
                open_risk += original_risk * p["quantity"] / initial_qty
            # Dust has no protective stop: reserve its whole marked value as
            # potential loss, not zero merely because it left active positions.
            dust_exposure = sum((lot["quantity"] * valid[lot["symbol"]].mid for lot in self.store.dust_lots()), ZERO)
            asset_dust = sum((lot["quantity"] * quote.mid for lot in self.store.dust_lots(symbol)), ZERO)
            sized = size_entry(self.cfg, self.instruments[symbol], nav=current["nav"],
                               cash=self.store.cash - cash_reserved, entry=entry, stop=stop,
                               exposure=current["exposure"] + cash_reserved,
                               open_risk=open_risk + dust_exposure + risk_reserved,
                               open_count=len(positions) + len(pending_entries), asset_exposure=asset_dust)
            if sized.quantity <= 0:
                self.store.decision(symbol, now, "REJECT", sized.reason)
                continue
            identifier = intent_id(symbol, "buy", signal.at.isoformat())
            created = self.store.intent(identifier, symbol, "buy", sized.quantity, now, "breakout",
                                        cash_reserved=sized.cash_reserved, risk_reserved=sized.risk_reserved,
                                        price_limit=entry, stop=stop, atr_distance=distance)
            if created:
                self.broker.execute(self.store, identifier, quote)
                filled = money(self.store.order(identifier)["filled"])
                self.store.decision(symbol, now, "BUY" if filled > 0 else "NO_FILL", "breakout")
        refresh_nav()
        with self.store.transaction():
            self.store.set_meta("last_cycle", now.isoformat())

    def historical_bar(self, bars: dict[str, Bar]):
        """Conservative OHLC approximation, not an intrabar execution proof.

        Existing stop levels are checked before advancing the trailing stop.
        Gap exits execute at the worse opening price; favorable highs can only
        move the next bar's stop. No entry reads the current bar's high/low.
        """
        half_spread = self.cfg.costs.simulated_spread / 2
        for symbol, position in self.store.positions().items():
            bar = bars[symbol]
            if self.store.get_meta("exit_required:" + symbol) or any(o["symbol"] == symbol for o in self.store.orders(active=True)):
                # An unfilled/partial exit remains desired-flat. No invented
                # second fill or favorable trailing update inside this bar.
                continue
            if bar.low * (ONE - half_spread) <= position["stop"]:
                # Stops are bid triggers. Convert the trigger to a mid once;
                # do not charge spread a second time at a non-gap stop.
                mid = min(bar.open, position["stop"] / (ONE - half_spread))
                quote = Quote(symbol, bar.end, mid * (ONE - half_spread), mid * (ONE + half_spread))
                reason = "historical_gap_stop" if bar.open * (ONE - half_spread) <= position["stop"] else "historical_stop_approximation"
                self.sell(symbol, quote, reason)
            else:
                self.store.update_stop(symbol, bar.high)
        self.store.mark_nav(next(iter(bars.values())).end, {s: b.close for s, b in bars.items()})
