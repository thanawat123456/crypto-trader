"""Registered, retrospective inner-development comparison of fixed entry gates."""
from bisect import bisect_left
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .config import Config
from .data import Dataset
from .development import _write_json, load_development_source
from .diagnostics import export_diagnostics, read_diagnostics
from .entry_policy import policies
from .research import run_backtest
from .study import add_months


CRITERIA = {"minimum_training_episodes": 20, "minimum_training_profit_factor": "1.20",
            "maximum_training_drawdown_pct": "6", "require_positive_net_liquidation_return": True,
            "require_positive_net_expectancy": True,
            "objective": "highest training estimated liquidation return; lower drawdown; registered order",
            "fallback": "cash; no candidate forced when eligibility fails"}


def select_policy(scores: dict[str, dict]) -> str:
    eligible = []
    for index, (name, score) in enumerate(scores.items()):
        pf, expectancy = score["profit_factor"], score["net_expectancy_quote"]
        if (score["closed_episodes"] >= CRITERIA["minimum_training_episodes"]
                and pf is not None and pf >= Decimal(CRITERIA["minimum_training_profit_factor"])
                and expectancy is not None and expectancy > 0
                and score["estimated_liquidation_return_pct"] > 0
                and score["max_drawdown_pct"] <= Decimal(CRITERIA["maximum_training_drawdown_pct"])):
            eligible.append((score["estimated_liquidation_return_pct"], -score["max_drawdown_pct"], -index, name))
    return max(eligible)[3] if eligible else "cash"


def chronological_windows(timeline, warmup):
    windows, cursor = [], timeline[0].start
    dates = [bar.start for bar in timeline]
    while add_months(cursor, 15) <= timeline[-1].end:
        start, end, test_end = (bisect_left(dates, at) for at in (cursor, add_months(cursor, 12), add_months(cursor, 15)))
        if end <= warmup or not start < end < test_end:
            raise ValueError("Insufficient chronological training/validation observations")
        windows.append((start, end, test_end))
        cursor = add_months(cursor, 3)
    if not windows:
        raise ValueError("Development prefix requires at least 15 calendar months")
    return windows


def _prefix_dataset(dataset: Dataset, count: int) -> Dataset:
    bars = {s: rows[:count] for s, rows in dataset.bars.items()}
    serialized = json.dumps({s: [asdict(b) for b in rows] for s, rows in bars.items()}, sort_keys=True, default=str).encode()
    return Dataset(bars, dataset.source + "; RETROSPECTIVE DEVELOPMENT PREFIX ONLY",
                   hashlib.sha256(serialized).hexdigest(), dataset.synthetic)


def _metrics(report):
    fields = ("net_return_pct", "estimated_liquidation_return_pct", "max_drawdown_pct", "closed_episodes",
              "profit_factor", "net_expectancy_quote", "fees_paid_quote_equivalent", "start", "end")
    result = {key: report[key] for key in fields}
    result["open_positions"] = len(report["open_positions"])
    return result


def run_policy_study(directory: Path, cfg: Config, registration: Path, output: Path, *, progress=None) -> dict:
    if output.exists():
        raise ValueError("Policy study output exists; use a new directory")
    dataset, raw, previous, split = load_development_source(directory, cfg, registration)
    development = _prefix_dataset(dataset, split)
    timeline = next(iter(development.bars.values()))
    windows = chronological_windows(timeline, cfg.warmup)
    registered = policies()
    names = ("baseline", "trend", "cost", "trend_cost")
    output.mkdir(parents=True)
    plan = {"schema": 1, "dataset_sha256": dataset.checksum,
            "development_prefix_json_sha256": development.checksum,
            "parent_registration": str(registration.resolve()), "parent_registration_sha256": hashlib.sha256(raw).hexdigest(),
            "development_end_exclusive": previous["holdout_start"], "excluded_holdout_bars_per_symbol": len(next(iter(dataset.bars.values()))) - split,
            "config": asdict(cfg), "config_hash": cfg.digest(),
            "entry_policies": [registered[name].payload() for name in names], "cash_fallback": registered["cash"].payload(),
            "selection_criteria": CRITERIA, "train_months": 12, "validation_months": 3, "step_months": 3,
            "fold_indices": windows, "flat_start_each_window": True,
            "score_semantics": "2 ATR of movement room / assumed round-trip cost, threshold 1.5; NOT expected profit or a probability",
            "data_status": "Retrospective development: these historical periods were previously inspected; not untouched OOS evidence",
            "source_manifest": json.loads((directory / "manifest.json").read_text()),
            "code_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest(),
            "holdout_evaluated": False, "approved_for_live": False}
    _write_json(output / "registration.json", plan)
    folds = []
    scope = "RETROSPECTIVE DEVELOPMENT VALIDATION; original holdout excluded; no live approval"

    def say(message):
        if progress:
            progress(message)

    for number, (start, end, test_end) in enumerate(windows, 1):
        label = f"fold-{number:02d}"
        train_data, validation_data = _prefix_dataset(development, end), _prefix_dataset(development, test_end)
        scores = {}
        for name in names:
            say(f"{label}: training {name}")
            result = run_backtest(train_data, cfg, output / f"{label}-train-{name}.sqlite", start_index=max(1, start),
                                  entry_policy=registered[name], analysis_scope="RETROSPECTIVE TRAINING; not validation evidence")
            scores[name] = _metrics(result)
        selected = select_policy(scores)
        selection = {"fold": label, "selected_policy": selected, "training_scores": scores,
                     "locked_before_validation": True, "criteria": CRITERIA}
        _write_json(output / f"{label}-selection.json", selection)
        tests = {}
        for name in (*names, "cash"):
            say(f"{label}: retrospective validation {name}; locked choice {selected}")
            path = output / f"{label}-validation-{name}.sqlite"
            result = run_backtest(validation_data, cfg, path, start_index=end,
                                  entry_policy=registered[name], analysis_scope=scope)
            diagnostic = read_diagnostics(path)
            export_diagnostics(diagnostic, output / f"{label}-{name}")
            tests[name] = {**_metrics(result), "entry_reasons": diagnostic["entry_evaluation_reasons"]}
        fold = {"fold": label, "training_start": timeline[start].start.isoformat(), "training_end": timeline[end].start.isoformat(),
                "validation_end": timeline[test_end - 1].end.isoformat(), "selection": selection, "validation": tests,
                "selected_validation": tests[selected]}
        folds.append(fold)
        _write_json(output / f"{label}.json", fold)
    report = {"experiment": str(output), "scope": scope, "registration": plan, "folds": folds,
              "selected_cash_windows": sum(f["selection"]["selected_policy"] == "cash" for f in folds),
              "holdout_evaluated": False, "retrospective_validation": True, "approved_for_live": False,
              "default_policy_changed": False,
              "limitations": ["Previously inspected development data cannot become untouched validation by splitting it again",
                              "Fixed heuristic entry gates, not learned/calibrated ML or a prediction of net profit",
                              "Full portfolio replays, not sums of filtered historical episodes; each window starts flat",
                              "Do not sum fold returns as a continuous portfolio or choose a live policy from validation ranking",
                              "Eligibility counts raw episodes, not independent samples or statistical proof",
                              "Estimated liquidation deducts assumed exit costs without executing a closing order",
                              "Current configured fee scenario is not authenticated account fees; OHLC execution remains approximate",
                              "No final holdout, new forward data or measured execution approval collected here"]}
    _write_json(output / "report.json", report)
    lines = ["# Entry policy comparison — retrospective development", "",
             "Original holdout excluded. Approved for live: False. Default policy unchanged.", "",
             "Score: ATR movement room / assumed round-trip cost; NOT expected profit.", "",
             "| Fold | Policy | Estimated net liquidation return (%) | Closed episodes | Profit factor | Locked choice |",
             "| --- | --- | ---: | ---: | ---: | --- |"]
    for fold in folds:
        for name, score in fold["validation"].items():
            lines.append(f"| {fold['fold']} | {name} | {score['estimated_liquidation_return_pct']:.4f} | {score['closed_episodes']} | {score['profit_factor']} | {fold['selection']['selected_policy']} |")
    lines += ["", "## Limits", ""] + [f"- {item}" for item in report["limitations"]]
    with (output / "report.md").open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    return report
