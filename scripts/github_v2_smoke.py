"""One bounded, public-data-only portability check; NOT a trading worker.

Does not open any portfolio, fit a model, use credentials, resume a shadow,
or treat the previously inspected data as new profit evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from crypto_trader_v2.binance_th import (
    BinanceTHPublicFeed, capture_binance_th, verify_binance_capture,
)
from crypto_trader_v2.config import load_config
from crypto_trader_v2.shadow import code_hash


REPOSITORY = "thanawat123456/crypto-trader"
TEST_REF = "refs/heads/codex/v2-github-smoke-20261003"
DEADLINE = datetime(2026, 10, 10, tzinfo=timezone.utc)
PURPOSE = "V2 GitHub Linux public-data smoke; NOT profit validation or scheduler migration"
MAX_REQUESTS = 16
MAX_BYTES = 2 * 1024 * 1024


class SmokeFeed(BinanceTHPublicFeed):
    max_requests = MAX_REQUESTS
    max_bytes = MAX_BYTES
    max_elapsed_seconds = 90


def write_new(path, payload):
    with path.open("x") as stream:
        stream.write(json.dumps(payload, default=str, sort_keys=True, indent=2) + "\n")


def execution_context(environ):
    """Whitelist provenance, never serialize the inherited environment."""
    if environ.get("GITHUB_ACTIONS") != "true":
        return {"runner": "local", "github_verified": False}
    repo = environ.get("GITHUB_REPOSITORY")
    ref = environ.get("GITHUB_REF")
    sha = environ.get("GITHUB_SHA", "")
    run_id = environ.get("GITHUB_RUN_ID", "")
    if (repo != REPOSITORY or ref != TEST_REF
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not re.fullmatch(r"[1-9][0-9]*", run_id)
            or environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or environ.get("GITHUB_EVENT_NAME") not in {"push", "workflow_dispatch"}):
        raise ValueError("Unapproved GitHub context or rerun: no public request made")
    return {"runner": "github-actions", "github_verified": True,
            "repository": repo, "ref": ref, "commit": sha, "run_id": run_id,
            "attempt": 1, "event": environ["GITHUB_EVENT_NAME"]}


def run_smoke(config_path, output, *, feed=None, clock=None, environ=None):
    at = (clock or (lambda: datetime.now(timezone.utc)))()
    if at.tzinfo is None or at.utcoffset() is None or at >= DEADLINE:
        raise ValueError("Expired or naive smoke clock: no public request made")
    context = execution_context(os.environ if environ is None else environ)
    config_path, output = Path(config_path), Path(output)
    cfg = load_config(str(config_path))
    if (cfg.venue != "binance_th" or cfg.timeframe_minutes != 240
            or cfg.warmup != 210 or len(cfg.instruments) != 2):
        raise ValueError("Smoke requires the fixed BTC/ETH native 4h configuration")
    client = feed or SmokeFeed(212)
    if (client.history_bars != 212 or client.max_requests != MAX_REQUESTS
            or client.max_bytes != MAX_BYTES or client.max_elapsed_seconds != 90):
        raise ValueError("Unbounded or changed smoke feed")
    # This reservation and provenance exist BEFORE the first public HTTP read.
    # A failed output is never reused or overwritten.
    output.mkdir(parents=True, exist_ok=False)
    registered_code = code_hash()
    registered_config = hashlib.sha256(config_path.read_bytes()).hexdigest()
    registration = {"schema": 1, "purpose": PURPOSE, "registered_at": at.isoformat(),
                    "deadline": DEADLINE.isoformat(), "context": context,
                    "code_hash": registered_code, "config_hash": cfg.digest(),
                    "config_file_sha256": registered_config,
                    "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "bars_per_symbol": 212,
                    "budget": {"requests": MAX_REQUESTS, "bytes": MAX_BYTES, "seconds": 90},
                    "retry": False, "portfolio_opened": False, "models_trained": 0,
                    "approved_for_live": False, "local_worker_migrated": False}
    write_new(output / "registration.json", registration)
    result = {"status": "failed", "purpose": PURPOSE, "context": context,
              "models_trained": 0, "approved_for_live": False,
              "portfolio_opened": False, "local_worker_migrated": False}
    try:
        manifest = capture_binance_th(cfg, output / "capture", history_bars=212, feed=client)
        audit = verify_binance_capture(output / "capture", cfg)
        if (code_hash() != registered_code
                or hashlib.sha256(config_path.read_bytes()).hexdigest() != registered_config
                or manifest["bars_per_symbol"] != {"BTC/USDT": 212, "ETH/USDT": 212}
                or audit["requests_verified"] != MAX_REQUESTS
                or manifest["bytes_downloaded"] > MAX_BYTES):
            raise ValueError("Smoke source, projection or budget binding changed")
        write_new(output / "audit.json", audit)
        result.update(status="verified", requests_verified=audit["requests_verified"],
                      bars_per_symbol=audit["bars_per_symbol"],
                      csv_sha256=audit["csv_sha256"])
    except Exception as error:
        # Preserve successful raw reads even if a later HTTP/read/audit fails.
        # Do not dump error strings or environment values into public artifacts.
        raw_dir = output / "partial-raw"
        raw_dir.mkdir()
        receipts = []
        for index, receipt in enumerate(client.requests):
            name = f"{index:02d}.json"
            (raw_dir / name).write_bytes(receipt["raw"])
            receipts.append({k: v for k, v in receipt.items() if k != "raw"} | {"file": name})
        write_new(output / "partial-receipts.json", receipts)
        result.update(error_type=type(error).__name__, requests_completed=len(receipts))
    write_new(output / "result.json", result)
    return result


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=PURPOSE)
    parser.add_argument("--config", default="config.binance-th.paper.yaml")
    parser.add_argument("--output", type=Path, required=True, help="NEW directory only; never a shadow directory")
    args = parser.parse_args(argv)
    try:
        result = run_smoke(args.config, args.output)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0 if result["status"] == "verified" else 2
    except Exception as error:
        print(json.dumps({"status": "refused", "error_type": type(error).__name__,
                          "approved_for_live": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
