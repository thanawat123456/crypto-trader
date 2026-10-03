"""Four fixed retrospective engineering checks, never model/strategy selection."""
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from .binance_th import require_binance_config, verify_binance_capture
from .broker import ExecutionScenario
from .dust_audit import verify_dust_ledger
from .entry_policy import policies
from .importer import load_dataset
from .research import run_backtest


CHECKS = (
    ("cash", "cash", ExecutionScenario()),
    ("baseline", "baseline", ExecutionScenario()),
    ("adverse_cost", "baseline", ExecutionScenario(extra_slippage=Decimal("0.001"))),
    ("partial_ioc", "baseline", ExecutionScenario(entry_fill_fraction=Decimal("0.5"), exit_fill_fraction=Decimal("0.4"))),
)


def code_hash():
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()


def run_dust_checks(directory, cfg, output, *, progress=None):
    require_binance_config(cfg)
    if output.exists():
        raise ValueError("Engineering output exists; use a new directory")
    capture_audit = verify_binance_capture(directory, cfg)
    dataset = load_dataset(directory, cfg)
    start = cfg.warmup + 1
    if len(next(iter(dataset.bars.values()))) <= start:
        raise ValueError("Not enough post-warmup candles for engineering checks")
    registered_code = code_hash()
    plan = {"schema": 1, "scope": "retrospective dust accounting/execution engineering; previously inspected native data; NOT OOS or selection",
            "config": asdict(cfg), "config_hash": cfg.digest(), "code_hash": registered_code,
            "dataset_sha256": dataset.checksum, "start_index": start,
            "cases": [{"name": name, "entry_policy": policy, "execution": scenario.payload()} for name, policy, scenario in CHECKS],
            "models_trained": 0, "approved_for_live": False, "default_policy_changed": False}
    output.mkdir(parents=True, exist_ok=False)
    (output / "registration.json").write_text(json.dumps(plan, default=str, sort_keys=True, indent=2) + "\n")
    cases = []
    for name, policy, scenario in CHECKS:
        if progress:
            progress(f"Engineering {name}: replay / independent FIFO / NAV audit")
        path = output / (name + ".sqlite")
        result = run_backtest(dataset, cfg, path, start_index=start, execution=scenario, entry_policy=policies()[policy],
                              analysis_scope=plan["scope"])
        audit = verify_dust_ledger(path, cfg)
        (output / (name + "-report.json")).write_text(json.dumps(result, default=str, sort_keys=True, indent=2) + "\n")
        (output / (name + "-audit.json")).write_text(json.dumps(audit, default=str, sort_keys=True, indent=2) + "\n")
        cases.append({"name": name, "cash": result["cash"], "marked_nav": result["marked_nav"],
                      "net_return_pct": result["net_return_pct"], "estimated_liquidation_return_pct": result["estimated_liquidation_return_pct"],
                      "realized_net_pnl": result["realized_net_pnl"], "fees_paid_quote_equivalent": result["fees_paid_quote_equivalent"],
                      "max_drawdown_pct": result["max_drawdown_pct"], "closed_episodes": result["closed_episodes"],
                      "dust_marked_value": result["dust_marked_value"], "fills": audit["fills_replayed"],
                      "dust_transfers": audit["dust_transfers_replayed"], "audit_status": audit["status"]})
    if code_hash() != registered_code:
        raise ValueError("Package changed during engineering checks; no complete report emitted")
    report = {**plan, "status": "complete", "capture_audit": capture_audit, "cases": cases,
              "selection_performed": False, "limitations": [
                  "Four fixed engineering scenarios on already inspected ~120-day data; not a validation or training study",
                  "Marked dust can make NAV positive while executable liquidation cash is below starting capital",
                  "Realistic dust exposure can block new trades under the unchanged risk limits",
                  "FIFO closure may occur later than desired-flat; closed-only statistics exclude outstanding dust episodes",
                  "Top-of-book/fees/slippage/queue/fills/fee precision remain simulation assumptions; no guarantee of profit"]}
    (output / "report.json").write_text(json.dumps(report, default=str, sort_keys=True, indent=2) + "\n")
    return report
