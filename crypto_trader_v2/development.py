"""Training-only diagnostic experiments, bounded by an earlier registration."""
from dataclasses import asdict
from decimal import Decimal
import csv
import hashlib
import json
from pathlib import Path

from .broker import ExecutionScenario
from .config import Config
from .data import Dataset
from .diagnostics import export_diagnostics, read_diagnostics
from .domain import utc
from .importer import load_dataset
from .research import run_backtest


def _write_json(path, data):
    with path.open("x") as stream:
        json.dump(data, stream, indent=2, default=str)


def load_development_source(directory: Path, cfg: Config, registration: Path):
    """One shared checksum/time-boundary guard for development experiments."""
    dataset = load_dataset(directory, cfg)
    if dataset.synthetic:
        raise ValueError("Development diagnostics requires non-synthetic source data")
    raw_registration = registration.read_bytes()
    previous = json.loads(raw_registration)
    if previous.get("schema") != 1 or previous.get("dataset_sha256") != dataset.checksum:
        raise ValueError("Registration does not match the source dataset")
    split = previous.get("holdout_start_index")
    timeline = next(iter(dataset.bars.values()))
    if (isinstance(split, bool) or not isinstance(split, int) or not cfg.warmup < split < len(timeline)
            or utc(previous["holdout_start"]) != timeline[split].start):
        raise ValueError("Invalid registered training/holdout boundary")
    return dataset, raw_registration, previous, split


def run_development(directory: Path, cfg: Config, registration: Path, output: Path, *, progress=None) -> dict:
    """No search/selection, no holdout performance or automatic promotion."""
    if output.exists():
        raise ValueError("Development output exists; use a new directory")
    dataset, raw_registration, previous, split = load_development_source(directory, cfg, registration)
    timeline = next(iter(dataset.bars.values()))
    prefix = {s: bars[:split] for s, bars in dataset.bars.items()}
    scenarios = [("baseline", ExecutionScenario()),
                 ("partial_ioc", ExecutionScenario(entry_fill_fraction=Decimal("0.5"), exit_fill_fraction=Decimal("0.5"))),
                 ("missed_and_adverse", ExecutionScenario(miss_every_entry=3, extra_slippage=Decimal("0.001")))]
    output.mkdir(parents=True)
    csv_path = output / "training.csv"
    with csv_path.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "symbol", "open", "high", "low", "close", "volume"))
        for symbol in sorted(prefix):
            for bar in prefix[symbol]:
                writer.writerow((bar.start.isoformat(), symbol, bar.open, bar.high, bar.low, bar.close, bar.volume))
    checksum = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    source_manifest = json.loads((directory / "manifest.json").read_text())
    plan = {"schema": 1, "purpose": "training-only descriptive attribution and fixed execution stress; no model selection",
            "dataset_sha256": dataset.checksum, "training_csv_sha256": checksum,
            "parent_registration": str(registration.resolve()), "parent_registration_sha256": hashlib.sha256(raw_registration).hexdigest(),
            "config_hash": cfg.digest(), "config": asdict(cfg),
            "training_start": timeline[0].start.isoformat(), "training_end_exclusive": timeline[split].start.isoformat(),
            "training_bars_per_symbol": split, "excluded_holdout_bars_per_symbol": len(timeline) - split,
            "scenarios": {name: scenario.payload() for name, scenario in scenarios},
            "fee_evidence": "Configured scenario only; user's account tier is not authenticated",
            "context_policy": "Fixed descriptive regime rules, not an entry filter or trained predictor",
            "source_coverage": {key: source_manifest.get(key) for key in ("start", "end", "quality", "subset", "repairs", "gap_policy")},
            "code_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(),
            "holdout_evaluated": False, "approved_for_live": False}
    _write_json(output / "registration.json", plan)
    training = Dataset(prefix, dataset.source + "; TRAINING PREFIX ONLY", checksum)
    results = {}
    for name, scenario in scenarios:
        if progress:
            progress(f"training-only: {name} (no holdout replay)")
        path = output / f"{name}.sqlite"
        replay = run_backtest(training, cfg, path, execution=scenario,
                              analysis_scope="TRAINING ONLY; performance is in-sample and not a model approval gate")
        diagnostics = read_diagnostics(path)
        diagnostics["scope"] = "TRAINING ONLY; performance is in-sample and not a model approval gate"
        export_diagnostics(diagnostics, output / name)
        results[name] = {"net_return_pct": replay["net_return_pct"], "max_drawdown_pct": replay["max_drawdown_pct"],
                         "realized_net_pnl": replay["realized_net_pnl"], "marked_nav": replay["marked_nav"],
                         "closed_episodes": replay["closed_episodes"], "open_positions": len(replay["open_positions"]),
                         "summary": diagnostics["summary"], "execution": diagnostics["execution"],
                         "diagnostic_report": str(output / name / "report.md")}
    report = {"experiment": str(output), "scope": "TRAINING ONLY — not OOS evidence", "registration": plan,
              "scenarios": results, "holdout_evaluated": False, "approved_for_live": False,
              "limitations": ["No candidate selection or claimed profitability improvement",
                              "Fill fractions, missed entries and extra slippage are fixed stress assumptions, not measured exchange behavior",
                              "Only the verified continuous subset is evaluated; omitted gaps/regimes remain outside the experiment",
                              "Marked return may include open inventory; closed-episode attribution is reported separately",
                              "No training of ML yet; descriptive causal context prepares the next milestone"]}
    _write_json(output / "report.json", report)
    lines = ["# V2 training diagnostics and execution stress", "", "TRAINING ONLY. Holdout evaluated: False. Approved for live: False.", "",
             f"Training window: {plan['training_start']} to {plan['training_end_exclusive']} (exclusive).", "",
             "| Scenario | Marked return (%) | Max DD (%) | Closed episodes | Entry no-fills | Partial exits |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for name, result in results.items():
        lines.append(f"| {name} | {result['net_return_pct']:.4f} | {result['max_drawdown_pct']:.4f} | {result['closed_episodes']} | {result['execution']['no_fill_entries']} | {result['execution']['partial_exits']} |")
    lines += ["", "## Limits", ""] + [f"- {item}" for item in report["limitations"]]
    with (output / "report.md").open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    return report
