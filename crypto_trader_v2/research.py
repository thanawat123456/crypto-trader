from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import asdict
import json

from .config import Config
from .broker import ExecutionScenario
from .context import ContextState
from .data import Dataset, validate_bars
from .domain import Mode, ONE, ZERO, Quote, money
from .engine import Coordinator
from .report import build_report
from .storage import Store
from .entry_policy import EntryPolicy


def run_backtest(dataset: Dataset, cfg: Config, database: Path | None = None, *, mode: Mode = Mode.BACKTEST,
                 start_index: int = 1, end_index: int | None = None,
                 execution: ExecutionScenario | None = None, analysis_scope: str = "",
                 entry_policy: EntryPolicy | None = None) -> dict:
    """Chronological replay, next-open entries and conservative bar stops.

    Public PAPER does not invoke this historical stop path; replay parity tests
    compare the same event stream under the same simulator, not real fills.
    """
    if database is None:
        with TemporaryDirectory(prefix="crypto-v2-research-") as folder:
            return run_backtest(dataset, cfg, Path(folder) / "run.sqlite", mode=mode, start_index=start_index, end_index=end_index, execution=execution, analysis_scope=analysis_scope, entry_policy=entry_policy)
    scenario = (execution or ExecutionScenario()).validate()
    policy = (entry_policy or EntryPolicy()).validate_for_config(cfg)
    configured = {i.symbol for i in cfg.instruments}
    if set(dataset.bars) != configured:
        raise ValueError("Dataset/config symbol mismatch")
    for bars in dataset.bars.values():
        validate_bars(bars, cfg)
    timeline = [b.start for b in next(iter(dataset.bars.values()))]
    if any([b.start for b in bars] != timeline for bars in dataset.bars.values()):
        raise ValueError("Inconsistent multi-symbol timeline")
    if len(timeline) <= cfg.warmup:
        raise ValueError("Insufficient warmup/data")
    stop_index = len(timeline) if end_index is None else end_index
    if not 1 <= start_index < stop_index <= len(timeline):
        raise ValueError("Invalid evaluation window")
    with Store(database, cfg, mode, create=True, source=dataset.source) as store:
        with store.transaction():
            store.set_meta("dataset_hash", dataset.checksum)
            store.set_meta("synthetic", str(dataset.synthetic))
            store.set_meta("execution_scenario", json.dumps(asdict(scenario), default=str, sort_keys=True))
            store.set_meta("execution_model_version", "received-fee-dust-fifo-v1" if cfg.venue == "binance_th" else "bid-stop-ioc-v2")
            store.set_meta("context_schema", ContextState.schema)
            store.set_meta("evaluation_start", timeline[start_index].isoformat())
            store.set_meta("evaluation_end", next(iter(dataset.bars.values()))[stop_index - 1].end.isoformat())
            if analysis_scope:
                store.set_meta("analysis_scope", analysis_scope)
        coordinator = Coordinator(store, cfg, execution=scenario, entry_policy=policy)
        states = {s: ContextState(cfg) for s in dataset.bars}
        prepared, contexts = {}, {}
        half = cfg.costs.simulated_spread / 2
        for index in range(1, stop_index):
            for symbol, bars in dataset.bars.items():
                prepared[symbol], contexts[symbol] = states[symbol].update(bars[index - 1])
            if index < start_index:
                continue
            now = timeline[index]
            current = {s: bars[index] for s, bars in dataset.bars.items()}
            # Full dataset was validated once. The live coordinator checks the
            # bounded recent history; indicator state includes the older prefix.
            histories = {s: bars[max(0, index - cfg.warmup - 2):index] for s, bars in dataset.bars.items()}
            quotes = {s: Quote(s, now, b.open * (ONE - half), b.open * (ONE + half)) for s, b in current.items()}
            coordinator.cycle(now, histories, quotes, trail_quotes=False, prepared_signals=prepared, contexts=contexts)
            coordinator.historical_bar(current)
        result = build_report(store.db)
        liquidation, residual_mark, residuals = result["cash"], ZERO, []
        for symbol, held in result["inventory_by_symbol"].items():
            instrument = next(i for i in cfg.instruments if i.symbol == symbol)
            mid = dataset.bars[symbol][stop_index - 1].close
            price = mid * (ONE - half) * (ONE - cfg.costs.slippage - scenario.extra_slippage)
            sellable = held
            if cfg.venue == "binance_th":
                sellable = instrument.round_quantity(held)
                if sellable < instrument.min_quantity or sellable * price < instrument.min_notional:
                    sellable = ZERO
                # A single hypothetical liquidation must respect native maxima.
                if ((instrument.max_quantity is not None and sellable > instrument.max_quantity) or
                        (instrument.max_notional is not None and sellable * price > instrument.max_notional)):
                    sellable = ZERO
            liquidation += sellable * price * (ONE - cfg.costs.taker_fee)
            residual = held - sellable
            residual_mark += residual * mid
            if residual:
                residuals.append({"symbol": symbol, "quantity": residual, "marked_value": residual * mid,
                                  "reason": "quantity_step_or_exchange_minimum/maximum; not counted as liquidation cash"})
        result.update({
            "dataset_hash": dataset.checksum,
            "synthetic": dataset.synthetic,
            "bars_per_symbol": stop_index - start_index,
            "warmup_bars": start_index,
            "start": timeline[start_index].isoformat(),
            "end": next(iter(dataset.bars.values()))[stop_index - 1].end.isoformat(),
            "execution_model": "Next bar open; fixed spread/slippage; registered IOC stress; bid-trigger OHLC stops",
            "execution_scenario": scenario.payload(),
            "execution_model_version": "received-fee-dust-fifo-v1" if cfg.venue == "binance_th" else "bid-stop-ioc-v2",
            "entry_policy": policy.payload(),
            "estimated_liquidation_nav": liquidation,
            "estimated_liquidation_return_pct": (liquidation / cfg.initial_cash - ONE) * 100,
            "non_liquidatable_residuals": residuals,
            "non_liquidatable_residual_marked_value": residual_mark,
            "limitations": [
                "No order-book queue, depth, market-impact or latency measurement",
                "Intrabar stop ordering approximated; trailing changes take effect next bar",
                "Open inventory marked at mid; estimated exit fees not deducted from NAV",
                "Received-base entry fees can leave dust; dust is retained as inventory, not invented liquidation cash",
                "No evidence of future profitability; synthetic data only tests software" if dataset.synthetic else "Historical replay is not untouched out-of-sample validation",
            ],
        })
        return result
