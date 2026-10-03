"""Registered chronological research, with a single locked final candidate."""
from __future__ import annotations

from bisect import bisect_left
from calendar import monthrange
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np

from .config import Config
from .data import Dataset
from .domain import ZERO
from .research import run_backtest


def write_json(path: Path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, default=str)


def add_months(at: datetime, months: int) -> datetime:
    count = at.year * 12 + at.month - 1 + months
    year, month = divmod(count, 12)
    month += 1
    return at.replace(year=year, month=month, day=min(at.day, monthrange(year, month)[1]))


def candidates(cfg: Config) -> list[Config]:
    return [replace(cfg, strategy=replace(cfg.strategy, breakout_bars=lookback, atr_multiple=Decimal(multiplier)))
            for lookback in (20, 40) for multiplier in (2, 3)]


def metrics(report: dict) -> dict:
    fields = ("net_return_pct", "realized_net_pnl", "fees_paid_quote_equivalent", "max_drawdown_pct",
              "closed_episodes", "profit_factor", "net_expectancy_quote", "win_rate_pct", "risk_pause", "start", "end")
    return {key: report.get(key) for key in fields}


def select_candidate(scores: list[dict]) -> int:
    # Locked objective: maximize net marked return in the training window,
    # breaking ties by lower drawdown, then lower registered index.
    return max(range(len(scores)), key=lambda i: (scores[i]["net_return_pct"], -scores[i]["max_drawdown_pct"], -i))


def block_bootstrap(db_path: Path, *, runs=500, lengths=(1, 3, 7), seed=42) -> dict:
    if runs < 100 or any(b < 1 for b in lengths):
        raise ValueError("Bootstrap requires at least 100 runs and positive block lengths")
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        initial = float(db.execute("SELECT delta FROM ledger WHERE event_id='initial_deposit' AND account='cash'").fetchone()[0])
        snapshots = db.execute("SELECT at,nav FROM snapshots WHERE nav IS NOT NULL ORDER BY at").fetchall()
        episodes = db.execute("SELECT closed_at,pnl FROM episodes WHERE closed_at IS NOT NULL ORDER BY closed_at").fetchall()
    daily = {}
    for at, nav in snapshots:
        daily[at[:10]] = float(nav)
    days = list(daily)
    if len(days) < max(lengths) * 3:
        return {"status": "INCONCLUSIVE", "reason": "Too few daily observations"}
    values = np.array(list(daily.values()))
    returns = values / np.r_[initial, values[:-1]] - 1
    pnl, counts = np.zeros(len(days)), np.zeros(len(days))
    indexes = {day: i for i, day in enumerate(days)}
    for at, net in episodes:
        if at[:10] in indexes:
            pnl[indexes[at[:10]]] += float(net)
            counts[indexes[at[:10]]] += 1
    rng = np.random.default_rng(seed)
    sensitivity = []
    for block in lengths:
        expected, drawdowns = [], []
        invalid = 0
        for _ in range(runs):
            starts = rng.integers(0, len(days), size=(len(days) + block - 1) // block)
            sample = ((starts[:, None] + np.arange(block)) % len(days)).ravel()[:len(days)]
            n = counts[sample].sum()
            if n:
                expected.append(float(pnl[sample].sum() / n))
            else:
                invalid += 1
            equity = np.r_[1.0, np.cumprod(1 + returns[sample])]
            drawdowns.append(float((1 - equity / np.maximum.accumulate(equity)).max() * 100))
        sensitivity.append({"block_days": block, "runs": runs, "no_episode_samples": invalid,
                            "expectancy_lower_95ci": float(np.percentile(expected, 2.5)) if expected else None,
                            "expectancy_upper_95ci": float(np.percentile(expected, 97.5)) if expected else None,
                            "drawdown_p95_pct": float(np.percentile(drawdowns, 95))})
    return {"method": "circular moving blocks on portfolio days; realized episodes grouped by exit day",
            "daily_observations": len(days), "closed_episodes": len(episodes), "seed": seed,
            "sensitivity": sensitivity, "effective_sample_size": "not estimated; raw observations are dependent",
            "limitations": "Conditional on observed regimes; does not simulate unseen crashes or prove a profit edge"}


def benchmark(dataset: Dataset, cfg: Config, start: int, end: int) -> dict:
    from .execution_costs import entry_cash_factor, entry_inventory_factor
    half, fee, slip = cfg.costs.simulated_spread / 2, cfg.costs.taker_fee, cfg.costs.slippage
    budget = min(cfg.risk.asset_cap, cfg.risk.gross_cap / len(cfg.instruments))
    units, cash = {}, cfg.initial_cash
    for symbol, bars in dataset.bars.items():
        entry = bars[start].open * (1 + half) * (1 + slip)
        quantity = cfg.initial_cash * budget / (entry * entry_cash_factor(cfg))
        units[symbol] = quantity * entry_inventory_factor(cfg)
        cash -= quantity * entry * entry_cash_factor(cfg)
    final = cash + sum((units[s] * bars[end - 1].close * (1 - half) * (1 - slip) * (1 - fee)
                        for s, bars in dataset.bars.items()), ZERO)
    return {"cash_return_pct": "0", "capped_buy_hold_net_liquidation_return_pct": (final / cfg.initial_cash - 1) * 100,
            "allocation_per_asset": budget, "model": "One initial purchase per asset; exit fees included; fractional benchmark ignoring minimum order sizes"}


def evaluate_gates(final: dict, stress: dict, bootstrap: dict, folds: list[dict], holdout_days: float) -> dict:
    closed = final["closed_episodes"] + sum(f["test"]["closed_episodes"] for f in folds)
    winning = sum(f["test"]["net_return_pct"] > 0 for f in folds)
    checks = []

    def check(name, passed, detail, *, inconclusive=False):
        checks.append({"name": name, "status": "PASS" if passed else "INCONCLUSIVE" if inconclusive else "FAIL", "detail": detail})

    check("holdout_net_return", final["net_return_pct"] > 0, str(final["net_return_pct"]))
    check("holdout_profit_factor", final["profit_factor"] is not None and final["profit_factor"] >= Decimal("1.20"), str(final["profit_factor"]), inconclusive=final["profit_factor"] is None)
    check("holdout_drawdown", final["max_drawdown_pct"] <= 6, str(final["max_drawdown_pct"]))
    check("oos_episode_count", closed >= 200, f"{closed}/200", inconclusive=True)
    check("holdout_duration", holdout_days >= 365, f"{holdout_days:.1f} days", inconclusive=True)
    check("cost_stress_expectancy", stress["net_expectancy_quote"] is not None and stress["net_expectancy_quote"] > 0, str(stress["net_expectancy_quote"]), inconclusive=stress["net_expectancy_quote"] is None)
    check("positive_walkforward_windows", bool(folds) and winning / len(folds) >= 0.6, f"{winning}/{len(folds)}", inconclusive=not folds)
    sensitivity = bootstrap.get("sensitivity", [])
    check("bootstrap_expectancy", bool(sensitivity) and all(s["expectancy_lower_95ci"] is not None and s["expectancy_lower_95ci"] > 0 and s["no_episode_samples"] / s["runs"] <= 0.02 for s in sensitivity),
          "95% CI lower bound > 0 across 1/3/7-day blocks", inconclusive=not sensitivity)
    check("bootstrap_drawdown", bool(sensitivity) and all(s["drawdown_p95_pct"] <= 8 for s in sensitivity), "95th percentile <= 8% across block lengths", inconclusive=not sensitivity)
    check("forward_paper", False, "90 days / 30 closed episodes not collected", inconclusive=True)
    check("measured_execution", False, "Historical OHLC fills lack measured order-book/latency evidence", inconclusive=True)
    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else "INCONCLUSIVE" if any(c["status"] == "INCONCLUSIVE" for c in checks) else "PASS"
    return {"status": status, "approved_for_live": False, "checks": checks}


def run_study(dataset: Dataset, cfg: Config, output: Path, *, bootstrap_runs=500, progress=None) -> dict:
    if bootstrap_runs < 100:
        raise ValueError("Bootstrap requires at least 100 runs")
    if dataset.synthetic:
        raise ValueError("A profitability study requires non-synthetic data")
    if output.exists():
        raise ValueError("Research output exists; use a new experiment directory")
    timeline = [b.start for b in next(iter(dataset.bars.values()))]
    split = len(timeline) * 3 // 4
    if split <= cfg.warmup or len(timeline) - split < cfg.warmup:
        raise ValueError("Insufficient data for development / final holdout")
    grid = candidates(cfg)
    output.mkdir(parents=True)
    plan = {"schema": 1, "dataset_sha256": dataset.checksum, "config_hash": cfg.digest(),
            "candidate_configs": [asdict(c.strategy) for c in grid], "holdout_start_index": split,
            "holdout_start": timeline[split].isoformat(), "selection_objective": "training marked net return, lower DD, lower grid index",
            "train_months": 12, "test_months": 3, "bootstrap_runs": bootstrap_runs,
            "fold_policy": "independent flat-start test accounts; do not sum their returns as one portfolio",
            "fee_model": asdict(cfg.costs), "code_hash": hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()}
    write_json(output / "registration.json", plan)
    folds = []

    def say(message):
        if progress:
            progress(message)

    def score(train_start: int, train_end: int, label: str):
        scores = []
        # Use sufficient prior history as warmup, but only count training PnL.
        history_start = max(0, train_start - max(c.warmup for c in grid))
        sliced = Dataset({s: bars[history_start:train_end] for s, bars in dataset.bars.items()}, dataset.source, dataset.checksum)
        for i, trial in enumerate(grid):
            say(f"{label}: training candidate {i + 1}/{len(grid)}")
            scores.append(metrics(run_backtest(sliced, trial, start_index=max(1, train_start - history_start))))
        return scores

    cursor = timeline[0]
    while True:
        train_end_at, test_end_at = add_months(cursor, 12), add_months(cursor, 15)
        train_start = bisect_left(timeline, cursor)
        train_end, test_end = bisect_left(timeline, train_end_at), bisect_left(timeline, test_end_at)
        if test_end > split or train_end >= split:
            break
        label = f"fold-{len(folds) + 1:02d}"
        scores = score(train_start, train_end, label)
        best = select_candidate(scores)
        say(f"{label}: testing selected candidate {best}")
        result = run_backtest(dataset, grid[best], output / f"{label}-test.sqlite", start_index=train_end, end_index=test_end)
        fold = {"train_start": timeline[train_start].isoformat(), "train_end": timeline[train_end].isoformat(),
                "selected_index": best, "training_scores": scores, "test": metrics(result)}
        folds.append(fold)
        write_json(output / f"{label}.json", fold)
        cursor = add_months(cursor, 3)
    last_train_start = bisect_left(timeline, add_months(timeline[split], -12))
    final_scores = score(last_train_start, split, "final-selection")
    best = select_candidate(final_scores)
    selection = {"selected_index": best, "selected_config_hash": grid[best].digest(), "training_scores": final_scores,
                 "locked_before_final_holdout": True, "holdout_start": timeline[split].isoformat()}
    write_json(output / "selection.json", selection)
    say("final holdout: replaying the locked candidate")
    final = run_backtest(dataset, grid[best], output / "holdout.sqlite", start_index=split)
    say("final holdout: stress with doubled spread and slippage")
    stressed = replace(grid[best], costs=replace(cfg.costs, simulated_spread=cfg.costs.simulated_spread * 2, slippage=cfg.costs.slippage * 2))
    stress = run_backtest(dataset, stressed, output / "holdout-cost-stress.sqlite", start_index=split)
    bootstrap = block_bootstrap(output / "holdout.sqlite", runs=bootstrap_runs)
    duration = (next(iter(dataset.bars.values()))[-1].end - timeline[split]).total_seconds() / 86400
    gates = evaluate_gates(final, stress, bootstrap, folds, duration)
    summary = {"experiment": str(output), "dataset_sha256": dataset.checksum, "selection": selection,
               "walkforward": folds, "holdout": metrics(final), "cost_stress": metrics(stress),
               "benchmark": benchmark(dataset, cfg, split, len(timeline)), "bootstrap": bootstrap, "gates": gates,
               "limitations": ["Flat-start folds do not implement carry-over positions between parameter versions",
                               "Holdout withheld within this registered run only; prior use of historical data cannot be ruled out. Reruns are retrospective",
                               "Constant fee scenario does not reconstruct historical account tiers",
                               "4h OHLC execution approximation; intrabar/queue/latency remain unmeasured"]}
    write_json(output / "report.json", summary)
    lines = ["# V2 registered research result", "", f"Gate: **{gates['status']}**. Approved for live: **False**.", "",
             f"Dataset SHA256: `{dataset.checksum}`", f"Final candidate index: {best}; selected from training only.", "",
             "| Period | Net marked return (%) | Max drawdown (%) | Closed episodes | Profit factor |", "| --- | ---: | ---: | ---: | ---: |"]
    for name, report in (("Final holdout", final), ("Cost stress", stress)):
        lines.append(f"| {name} | {report['net_return_pct']:.4f} | {report['max_drawdown_pct']:.4f} | {report['closed_episodes']} | {report['profit_factor']} |")
    lines += ["", "## Gates", ""]
    lines += [f"- {c['name']}: {c['status']} — {c['detail']}" for c in gates["checks"]]
    lines += ["", "## Limits", ""] + [f"- {item}" for item in summary["limitations"]]
    with (output / "report.md").open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    return summary
