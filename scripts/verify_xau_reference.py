"""Re-decode persisted native responses and verify reference CSV provenance.

No broker contract, trade label or profitability is inferred by this audit.
"""
import argparse
import csv
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crypto_trader_v2.development import _write_json
from crypto_trader_v2.xau_feed import ROOT, decode_hourly


def verify_xau(directory, output):
    if output.exists():
        raise ValueError("Gold audit output exists; use a new directory")
    result = json.loads((directory / "report.json").read_bytes())
    plan = json.loads((directory / "registration.json").read_bytes())
    if (result["status"] != "complete" or result["plan"] != plan or plan["endpoint_root"] != ROOT
            or result["requests_completed"] != plan["maximum_requests"] or result["approved_for_live"] is not False):
        raise ValueError("Gold reference job is incomplete or mismatched")
    output.mkdir(parents=True)
    _write_json(output / "registration.json", {"scope": "raw-to-CSV integrity only; no model, fee or live evidence",
                "source_report_sha256": hashlib.sha256((directory / "report.json").read_bytes()).hexdigest(),
                "auditor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "approved_for_live": False})
    sides, used = {"BID": [], "ASK": []}, 0
    for number, record in enumerate(result["raw_files"], 1):
        checkpoint = json.loads((directory / f"request-{number:03d}.json").read_bytes())
        if checkpoint != record or Path(record["file"]).name != record["file"]:
            raise ValueError("Gold request identity mismatch")
        raw = (directory / "raw" / record["file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"] or len(raw) != record["bytes"]:
            raise ValueError("Gold native response checksum mismatch")
        rows = decode_hourly(raw)
        side = record["file"].removesuffix(".json").split("-")[-1]
        if len(rows) != record["native_rows"] or side not in sides or f"/{side}/" not in record["url"]:
            raise ValueError("Gold native row/side mismatch")
        used += len(raw)
        sides[side].extend(rows)
    if used != result["bytes_downloaded"] or used > plan["byte_budget"]:
        raise ValueError("Gold byte budget mismatch")
    for side, expected in sides.items():
        with (directory / (side.lower() + ".csv")).open(newline="") as stream:
            actual = list(csv.DictReader(stream))
        decoded = [{k: v if k == "timestamp" else D(v) for k, v in row.items()} for row in actual]
        if decoded != expected:
            raise ValueError("Gold CSV differs from native records; inserted or changed candles")
    if [r["timestamp"] for r in sides["BID"]] != [r["timestamp"] for r in sides["ASK"]]:
        raise ValueError("Gold bid/ask alignment mismatch")
    reference = result["reference_import"]
    if reference != json.loads((directory / "reference" / "manifest.json").read_bytes()):
        raise ValueError("Gold reference manifest mismatch")
    for filename, expected_hash in (("bid.csv", reference["bid_source_sha256"]), ("ask.csv", reference["ask_source_sha256"]),
                                    ("reference/quotes.csv", reference["quotes_sha256"])):
        if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != expected_hash:
            raise ValueError("Gold exported quote checksum mismatch")
    with (directory / "reference" / "quotes.csv").open(newline="") as stream:
        exported = list(csv.DictReader(stream))
    if len(exported) != reference["rows"] or len(exported) != len(sides["BID"]):
        raise ValueError("Gold exported row count mismatch")
    for row, bid, ask in zip(exported, sides["BID"], sides["ASK"]):
        if row["symbol"] != "XAU/USD" or row["timestamp"] != bid["timestamp"]:
            raise ValueError("Gold exported symbol/time mismatch")
        for column in ("open", "high", "low", "close"):
            if D(row["bid_" + column]) != bid[column] or D(row["ask_" + column]) != ask[column]:
                raise ValueError("Gold exported quote price mismatch")
    audit = {"status": "verified", "native_files_verified": len(result["raw_files"]), "native_rows_per_side": len(exported),
             "bytes_verified": used, "quotes_sha256": reference["quotes_sha256"], "gap_count": reference["gap_count"],
             "fabricated_intervals": 0, "strategy_replay_ready": False, "models_trained": False, "approved_for_live": False,
             "limitations": "Gap classification, actual broker costs/calendar and margin-aware strategy replay remain unverified"}
    _write_json(output / "report.json", audit)
    return audit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_xau(args.directory, args.output), indent=2))
