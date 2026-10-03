from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

from .config import load_config
from .broker import ExecutionScenario
from .data import KrakenPublicFeed, demo_dataset, read_csv
from .domain import Mode
from .engine import Coordinator
from .domain import utc
from .importer import import_kraken, load_dataset, slice_contiguous
from .report import build_report, read_report
from .research import run_backtest
from .storage import Store
from .study import run_study
from .development import run_development
from .diagnostics import export_diagnostics, read_diagnostics
from .entry_policy import policies
from .policy_study import run_policy_study
from .ml_study import run_ml_study
from .ml_preparation import PREPARATION_PROTOCOLS, audit_dataset, prepare_ml_source
from .ml_batch import batch_status, launch_registered_batch, register_batch, run_registered_batch, run_batch_worker
from .ml_variants import VARIANTS
from .xau_research import import_xau_reference, load_xau_contract, trade_economics
from .xau_feed import download_xau_reference
from .binance_th import BinanceTHPublicFeed, capture_binance_th, verify_binance_capture
from .dust_audit import verify_dust_ledger
from .dust_checks import run_dust_checks
from .native_history import capture_native_history, verify_native_history
from .shadow import register_shadow, observe_shadow, verify_shadow


def print_json(value):
    print(json.dumps(value, default=lambda v: str(v) if isinstance(v, Decimal) else v, indent=2, ensure_ascii=False))


def execution_arguments(command):
    command.add_argument("--entry-fill-fraction", type=Decimal, default=Decimal(1), help="Fixed stress fraction, rounded down to the quantity step; not measured liquidity")
    command.add_argument("--exit-fill-fraction", type=Decimal, default=Decimal(1))
    command.add_argument("--miss-every-entry", type=int, default=0, help="Deterministic no-fill every Nth entry attempt; 0 disables")
    command.add_argument("--extra-slippage", type=Decimal, default=Decimal(0), help="Additional adverse slippage fraction per fill")
    command.add_argument("--entry-policy", choices=list(policies()), default="baseline", help="Research entry gate; default unchanged; no probability/profit prediction")


def execution_from_args(args):
    return ExecutionScenario(args.entry_fill_fraction, args.exit_fill_fraction, args.miss_every_entry, args.extra_slippage).validate()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="V2 research / paper only. Live orders are unavailable.")
    root.add_argument("-c", "--config", help="V2 YAML configuration (never loads V1 config.yaml)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("config", help="Inspect resolved config, excluding any credentials")
    demo = commands.add_parser("demo", help="Offline synthetic software demonstration")
    demo.add_argument("--bars", type=int, default=600)
    demo.add_argument("--db", type=Path, help="Optional new database; existing files are refused")
    execution_arguments(demo)
    backtest = commands.add_parser("backtest", help="Replay a validated multi-symbol OHLCV CSV")
    backtest.add_argument("csv", type=Path)
    backtest.add_argument("--source", required=True, help="Dataset provenance / URL / identifier")
    backtest.add_argument("--db", type=Path, help="Optional new database")
    execution_arguments(backtest)
    importer = commands.add_parser("import-kraken", help="Import official headerless OHLCVT into a quality-checked V2 dataset")
    source = importer.add_mutually_exclusive_group(required=True)
    source.add_argument("--official", action="store_true", help="Read selected CSV members from Kraken's 2026Q2 split ZIP using bounded HTTP ranges")
    source.add_argument("--archive", type=Path, help="Local reassembled ZIP")
    source.add_argument("--file", action="append", help="Local OHLCVT CSV: SYMBOL=/path/to/file (repeat for all symbols)")
    importer.add_argument("--output", type=Path, required=True, help="New dataset directory")
    importer.add_argument("--start", type=utc, help="Inclusive UTC candle opening time")
    importer.add_argument("--end", type=utc, help="Exclusive UTC boundary; only candles ending by this time")
    importer.add_argument("--max-download-mb", type=int, default=128)
    importer.add_argument("--repair-from-minutes", type=int, help="Reconstruct missing bars only when all lower-timeframe bars are present in the same ZIP")
    native = commands.add_parser("capture-binance-th", help="Bounded BTC/ETH public-only native snapshot with raw receipts; no account or models")
    native.add_argument("--output", type=Path, required=True, help="New immutable capture directory")
    native.add_argument("--bars", type=int, default=720, help="1..1000 closed candles per symbol; must exceed warmup")
    native_verify = commands.add_parser("verify-binance-th", help="Read-only raw -> candles/quotes/market-rule audit; no live approval")
    native_verify.add_argument("dataset", type=Path)
    native_history = commands.add_parser("capture-binance-th-history", help="Register bounded post-launch GLOBAL reference history; exclude inspected suffix; NO ML")
    native_history.add_argument("--protected-capture", type=Path, required=True, help="Previously inspected native capture; preserve exact candles and exclude its suffix")
    native_history.add_argument("--output", type=Path, required=True, help="New directory only; raw receipts and failures retained")
    native_history_verify = commands.add_parser("verify-binance-th-history", help="Read-only raw/full/development/protected-boundary quality audit; NO profit approval")
    native_history_verify.add_argument("dataset", type=Path)
    shadow_register = commands.add_parser("register-binance-th-shadow", help="Preregister seven-day execution engineering/cash-baseline controls; NOT ML/profit approval")
    shadow_register.add_argument("--history", type=Path, required=True)
    shadow_register.add_argument("--output", type=Path, required=True)
    shadow_observe = commands.add_parser("observe-binance-th-shadow", help="Collect ONE registered UTC slot with raw evidence/strict simulation; no background/retries")
    shadow_observe.add_argument("output", type=Path)
    shadow_verify = commands.add_parser("verify-binance-th-shadow", help="Read-only shadow raw/FIFO/fee/boundary audit; never selects/promotes")
    shadow_verify.add_argument("output", type=Path)
    shadow_verify.add_argument("--audit-output", type=Path, help="Optional new-only directory for the read-only audit report")
    dust_verify = commands.add_parser("verify-dust-ledger", help="Read-only independent FIFO/cost-basis/NAV replay; not a profit gate")
    dust_verify.add_argument("--db", type=Path, required=True)
    dust_verify.add_argument("--output", type=Path, help="Optional new directory; refuses overwrite")
    dust_checks = commands.add_parser("dust-checks", help="Four fixed engineering replays/audits on native snapshot; NO training/selection")
    dust_checks.add_argument("dataset", type=Path)
    dust_checks.add_argument("--output", type=Path, required=True)
    subset = commands.add_parser("slice-dataset", help="Explicitly select a continuous subset; never invent gap candles")
    subset.add_argument("dataset", type=Path)
    subset.add_argument("--longest-contiguous", action="store_true", required=True)
    subset.add_argument("--output", type=Path, required=True)
    audit = commands.add_parser("audit-dataset", help="Verify imported candles and report gaps/common runs; no labels, scores or trading")
    audit.add_argument("dataset", type=Path)
    audit.add_argument("--output", type=Path, help="Optional new directory for the quality-only JSON report")
    preparation = commands.add_parser("prepare-ml", help="Register extended-history ML development while preserving an existing protected boundary")
    preparation.add_argument("dataset", type=Path)
    preparation.add_argument("--protected-dataset", type=Path, required=True, help="Exact source of the earlier registration")
    preparation.add_argument("--protected-registration", type=Path, required=True)
    preparation.add_argument("--protocol", choices=list(PREPARATION_PROTOCOLS), default="extended",
                             help="Fixed fit/calibration/approval/validation months: extended=24/6/2/3; support=24/12/3/2. No approval thresholds change")
    preparation.add_argument("--output", type=Path, required=True, help="New immutable preparation directory; no fitting or outcome inspection")
    dataset_test = commands.add_parser("backtest-dataset", help="Verify an import manifest and replay its CSV")
    dataset_test.add_argument("dataset", type=Path)
    dataset_test.add_argument("--db", type=Path)
    execution_arguments(dataset_test)
    diagnostics = commands.add_parser("diagnostics", help="Read-only episode/cost/regime attribution; never approves a model")
    diagnostics.add_argument("--db", type=Path, required=True)
    diagnostics.add_argument("--output", type=Path, help="Optional new directory for reports and episode CSV")
    development = commands.add_parser("develop", help="Run fixed diagnostic stress scenarios on the training prefix only")
    development.add_argument("dataset", type=Path)
    development.add_argument("--registration", type=Path, required=True, help="Earlier research registration fixing the holdout boundary for the exact same dataset")
    development.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare-policies", help="Registered retrospective train/validation entry-gate comparison; original holdout excluded")
    comparison.add_argument("dataset", type=Path)
    comparison.add_argument("--registration", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    ml = commands.add_parser("ml-research", help="Offline purged chronological ML research; no live approval or default changes")
    ml.add_argument("dataset", type=Path)
    ml.add_argument("--registration", type=Path, required=True, help="Earlier source registration (8/2/2/3 months) or immutable named prepare-ml plan")
    ml.add_argument("--output", type=Path, required=True)
    ml.add_argument("--variant", choices=list(VARIANTS), default="linear-v1")
    batch = commands.add_parser("ml-batch", help="Register/run four fixed offline trials once; no threshold tuning or live orders")
    batch.add_argument("dataset", type=Path)
    batch.add_argument("--registration", type=Path, required=True)
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--hours", type=int, default=6, help="Research budget 1..12 hours checked at fold/scenario boundaries; stops earlier when finished")
    batch.add_argument("--background", action="store_true", help="Detach a bounded local worker; durable progress/logs, no automatic deployment")
    batch.add_argument("--keep-awake", action="store_true", help="Background only: macOS process-bound idle-sleep prevention; no permanent setting changes")
    batch.add_argument("--verify-on-completion", action="store_true", help="Background only: read-only causal refit/ledger audit once after successful completion; no promotion")
    worker = commands.add_parser("ml-batch-worker", help="Run an already registered batch once; duplicate workers refused")
    worker.add_argument("output", type=Path)
    worker.add_argument("--verify-on-completion", action="store_true")
    batch_read = commands.add_parser("ml-batch-status", help="Read a registered batch without modifying it")
    batch_read.add_argument("output", type=Path)
    xau = commands.add_parser("import-xau-reference", help="Audit native UTC bid/ask gold CSVs; never uses crypto spot accounting")
    xau.add_argument("--bid", type=Path, required=True)
    xau.add_argument("--ask", type=Path, required=True)
    xau.add_argument("--source", required=True)
    xau.add_argument("--timeframe-minutes", type=int, default=60)
    xau.add_argument("--output", type=Path, required=True)
    gold_download = commands.add_parser("download-xau-reference", help="Bounded public native hourly bid/ask reference; no synthetic gap bars or broker account")
    gold_download.add_argument("--start", type=utc, required=True, help="Inclusive first day of a UTC month")
    gold_download.add_argument("--end", type=utc, required=True, help="Exclusive first day of a closed UTC month")
    gold_download.add_argument("--output", type=Path, required=True)
    gold_download.add_argument("--max-download-mb", type=int, default=8)
    economics = commands.add_parser("xau-economics", help="Explicit broker-contract PnL example; not a recommendation or executable order")
    economics.add_argument("--contract", type=Path, required=True)
    economics.add_argument("--side", choices=["long", "short"], required=True)
    for name in ("lots", "entry-bid", "entry-ask", "exit-bid", "exit-ask", "rollover-units"):
        economics.add_argument("--" + name, type=Decimal, required=True)
    economics.add_argument("--opened-at", type=utc, required=True)
    economics.add_argument("--closed-at", type=utc, required=True)
    study = commands.add_parser("research", help="Register candidates, walk-forward, lock a holdout candidate, and report evidence gates")
    study.add_argument("dataset", type=Path)
    study.add_argument("--output", type=Path, required=True, help="New immutable experiment directory")
    study.add_argument("--bootstrap-runs", type=int, default=500)
    paper = commands.add_parser("paper", help="Simulate using configured venue PUBLIC data; never submits real orders")
    paper.add_argument("--db", type=Path, required=True)
    paper.add_argument("--initialize", action="store_true", help="Create new simulation; refuse existing DB")
    paper.add_argument("--once", action="store_true")
    paper.add_argument("--poll-seconds", type=int, default=60)
    paper.add_argument("--entry-policy", choices=list(policies()), default="baseline", help="Must match the simulation database; filtered policies require a new run")
    status = commands.add_parser("status", help="Read a simulation database without modifying it")
    status.add_argument("--db", type=Path, required=True)
    backup = commands.add_parser("backup", help="Create a consistent SQLite backup; refuse overwrite")
    backup.add_argument("--db", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--mode", choices=[m.value for m in Mode], required=True)
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            print_json(read_report(args.db))
            return 0
        if args.command == "ml-batch-status":
            print_json(batch_status(args.output))
            return 0
        if args.command == "import-xau-reference":
            print_json(import_xau_reference(args.bid, args.ask, args.source, args.output, timeframe_minutes=args.timeframe_minutes))
            return 0
        if args.command == "download-xau-reference":
            result = download_xau_reference(args.start, args.end, args.output, byte_budget=args.max_download_mb * 1024 * 1024,
                                            progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json({key: result[key] for key in ("status", "error", "requests_completed", "bytes_downloaded",
                                                    "fabricated_intervals", "models_trained", "approved_for_live")})
            return 0 if result["status"] == "complete" else 2
        if args.command == "xau-economics":
            print_json(trade_economics(load_xau_contract(args.contract), args.side, args.lots,
                                      args.entry_bid, args.entry_ask, args.exit_bid, args.exit_ask,
                                      opened_at=args.opened_at, closed_at=args.closed_at, rollover_units=args.rollover_units))
            return 0
        if args.command == "diagnostics":
            result = read_diagnostics(args.db)
            if args.output:
                export_diagnostics(result, args.output)
            print_json({key: result[key] for key in ("scope", "summary", "groups", "execution", "approved_for_live")})
            return 0
        cfg = load_config(args.config)
        if cfg.venue == "binance_th" and args.command in {
                "research", "develop", "compare-policies", "prepare-ml", "ml-research", "ml-batch", "ml-batch-worker"}:
            raise ValueError("Binance TH is an engineering/public-paper stage only: register adequate native history and validated execution assumptions before profit/ML studies")
        if args.command == "config":
            print_json(asdict(cfg))
        elif args.command == "demo":
            print_json(run_backtest(demo_dataset(cfg, args.bars), cfg, args.db, execution=execution_from_args(args), entry_policy=policies()[args.entry_policy]))
        elif args.command == "backtest":
            print_json(run_backtest(read_csv(args.csv, cfg, args.source), cfg, args.db, execution=execution_from_args(args), entry_policy=policies()[args.entry_policy]))
        elif args.command == "backtest-dataset":
            print_json(run_backtest(load_dataset(args.dataset, cfg), cfg, args.db, execution=execution_from_args(args), entry_policy=policies()[args.entry_policy]))
        elif args.command == "compare-policies":
            result = run_policy_study(args.dataset, cfg, args.registration, args.output,
                                      progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json({key: result[key] for key in ("experiment", "scope", "selected_cash_windows", "holdout_evaluated", "approved_for_live", "default_policy_changed")})
        elif args.command == "ml-research":
            result = run_ml_study(args.dataset, cfg, args.registration, args.output,
                                 variant=args.variant,
                                 progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json({key: result[key] for key in ("experiment", "scope", "candidate_samples", "labelled_samples", "fitted_models",
                                                    "ready_models", "selected_cash_windows", "holdout_evaluated", "approved_for_live")})
        elif args.command == "ml-batch":
            if (args.keep_awake or args.verify_on_completion) and not args.background:
                raise ValueError("Keep-awake and post-completion verification require --background")
            register_batch(args.dataset, cfg, args.registration, args.output, hours=args.hours)
            if args.background:
                print_json(launch_registered_batch(args.output, cfg, args.config,
                                                   keep_awake=args.keep_awake, verify_on_completion=args.verify_on_completion))
            else:
                result = run_registered_batch(args.output, cfg, progress=lambda message: print(message, file=sys.stderr, flush=True))
                print_json(result)
                return 0 if result["status"] == "complete" else 2
        elif args.command == "ml-batch-worker":
            code = run_batch_worker(args.output, cfg, args.config, verify_on_completion=args.verify_on_completion,
                                    progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json(batch_status(args.output))
            return code
        elif args.command == "develop":
            result = run_development(args.dataset, cfg, args.registration, args.output,
                                     progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json({key: result[key] for key in ("experiment", "scope", "scenarios", "holdout_evaluated", "approved_for_live")})
        elif args.command == "slice-dataset":
            print_json(slice_contiguous(args.dataset, cfg, args.output))
        elif args.command == "audit-dataset":
            print_json(audit_dataset(args.dataset, cfg, args.output))
        elif args.command == "prepare-ml":
            result = prepare_ml_source(args.dataset, cfg, args.protected_dataset, args.protected_registration, args.output,
                                       protocol_name=args.protocol)
            print_json({key: result[key] for key in ("development_start", "development_end_exclusive", "development_bars_per_symbol",
                                                    "protocol_name", "protocol", "reserved_bars_per_symbol", "labels_computed", "models_trained", "approved_for_live")})
        elif args.command == "research":
            result = run_study(load_dataset(args.dataset, cfg), cfg, args.output, bootstrap_runs=args.bootstrap_runs,
                               progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json({key: result[key] for key in ("experiment", "holdout", "cost_stress", "benchmark", "gates")})
        elif args.command == "import-kraken":
            if args.max_download_mb <= 0:
                raise ValueError("Download budget must be positive")
            files = None
            if args.file:
                files = {}
                for entry in args.file:
                    symbol, separator, filename = entry.partition("=")
                    if not separator or not filename or symbol in files:
                        raise ValueError("Each --file must be a unique SYMBOL=path mapping")
                    files[symbol] = Path(filename)
            result = import_kraken(cfg, args.output, archive=args.archive, files=files, official=args.official,
                                   start=args.start, end=args.end, byte_budget=args.max_download_mb * 1024 * 1024,
                                   repair_minutes=args.repair_from_minutes)
            print_json(result)
            if not result["replay_ready"]:
                return 2
        elif args.command == "backup":
            with Store(args.db, cfg, Mode(args.mode)) as store:
                store.backup(args.output)
            print_json({"backup": str(args.output)})
        elif args.command == "capture-binance-th":
            result = capture_binance_th(cfg, args.output, history_bars=args.bars)
            print_json({key: result[key] for key in ("venue", "bars_per_symbol", "start", "end_exclusive", "csv_sha256",
                                                    "bytes_downloaded", "fabricated_intervals", "models_trained", "approved_for_live")})
        elif args.command == "verify-binance-th":
            print_json(verify_binance_capture(args.dataset, cfg))
        elif args.command == "capture-binance-th-history":
            result = capture_native_history(cfg, args.protected_capture, args.output,
                                            progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json(result)
            return 0 if result["status"] == "complete" else 2
        elif args.command == "verify-binance-th-history":
            print_json(verify_native_history(args.dataset, cfg))
        elif args.command == "register-binance-th-shadow":
            print_json(register_shadow(args.history, cfg, args.output))
        elif args.command == "observe-binance-th-shadow":
            result = observe_shadow(args.output, cfg)
            print_json(result)
            return 0 if result["status"] == "observed" else 2
        elif args.command == "verify-binance-th-shadow":
            if args.audit_output and args.audit_output.exists():
                raise ValueError("Shadow audit output exists; use a new directory")
            result = verify_shadow(args.output, cfg)
            if args.audit_output:
                args.audit_output.mkdir(parents=True, exist_ok=False)
                with (args.audit_output/"report.json").open("x") as stream:
                    stream.write(json.dumps(result, default=str, sort_keys=True, indent=2)+"\n")
            print_json(result)
        elif args.command == "verify-dust-ledger":
            if args.output and args.output.exists():
                raise ValueError("Dust audit output exists; use a new directory")
            result = verify_dust_ledger(args.db, cfg)
            if args.output:
                args.output.mkdir(parents=True, exist_ok=False)
                (args.output / "report.json").write_text(json.dumps(result, default=str, sort_keys=True, indent=2) + "\n")
            print_json(result)
        elif args.command == "dust-checks":
            result = run_dust_checks(args.dataset, cfg, args.output,
                                     progress=lambda message: print(message, file=sys.stderr, flush=True))
            print_json({key: result[key] for key in ("status", "scope", "cases", "models_trained", "approved_for_live", "selection_performed")})
        elif args.command == "paper":
            if args.poll_seconds < 1:
                raise ValueError("Poll interval must be positive")
            if cfg.warmup >= 719:
                raise ValueError("Warmup exceeds available public snapshot history")
            if cfg.venue == "binance_th" and args.poll_seconds < 60:
                raise ValueError("Binance TH REST paper polling requires at least 60 seconds")
            feed = BinanceTHPublicFeed() if cfg.venue == "binance_th" else KrakenPublicFeed()
            source = "Binance TH public REST; received-asset fee simulation" if cfg.venue == "binance_th" else "Kraken public OHLC/Ticker; simulated fills"
            with Store(args.db, cfg, Mode.PAPER, create=args.initialize, source=source) as store:
                # Bind the policy before any network call: a failed first fetch
                # must not leave a new run unbound/unresumable. No cycle occurs
                # until fresh venue rules and a complete snapshot are available.
                coordinator = Coordinator(store, cfg, entry_policy=policies()[args.entry_policy])
                while True:
                    if cfg.venue == "binance_th":
                        feed.reset_request_budget()
                    instruments = feed.instruments(cfg)
                    now, histories, quotes = feed.snapshot(cfg)
                    with store.transaction():
                        store.set_meta("effective_market_rules", json.dumps({s: asdict(i) for s, i in instruments.items()}, default=str, sort_keys=True))
                        store.set_meta("market_rules_received_at", datetime.now(timezone.utc).isoformat())
                        if cfg.venue == "binance_th":
                            store.set_meta("native_market_metadata", json.dumps(feed.market_metadata, default=str, sort_keys=True))
                            store.set_meta("last_public_receipts", json.dumps([{k: v for k, v in r.items() if k != "raw"}
                                                                              for r in feed.requests], sort_keys=True))
                            store.set_meta("quote_freshness_basis", "REST request-start time; no exchange quote event timestamp")
                            store.set_meta("fee_asset_policy", "received base on BUY, received quote on SELL; precision unverified")
                    coordinator.instruments = instruments
                    coordinator.broker.instruments = instruments
                    if cfg.venue == "binance_th":
                        coordinator.broker.observed_capacity = feed.observed_capacity
                    coordinator.cycle(now, histories, quotes)
                    with store.transaction():
                        store.set_meta("last_successful_cycle", datetime.now(timezone.utc).isoformat())
                    print_json(build_report(store.db))
                    if args.once:
                        break
                    time.sleep(args.poll_seconds)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"V2 error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
