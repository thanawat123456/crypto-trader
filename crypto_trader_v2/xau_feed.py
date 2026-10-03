"""Bounded public hourly gold reference import; never fabricate flat bars.

Delta format verified against dukascopy-node commit
519a79017b49431c21049944934cce525f708ba5 (normaliser, URL generator,
instrument metadata). We intentionally do NOT copy its gap-filling behaviour.
"""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import time
from urllib.request import Request, urlopen

from .development import _write_json
from .domain import utc
from .study import add_months
from .xau_research import import_xau_reference


ROOT = "https://jetta.dukascopy.com/v1/candles/hour/XAU-USD"
FORMAT_REFERENCE = "https://github.com/Leo4815162342/dukascopy-node/tree/519a79017b49431c21049944934cce525f708ba5/src"


def decode_hourly(payload):
    """Decode only actual delta records. API times/shift are milliseconds."""
    def invalid_constant(value):
        raise ValueError("Non-finite JSON value: " + value)

    data = json.loads(payload, parse_float=Decimal, parse_constant=invalid_constant)
    if not isinstance(data, dict):
        raise ValueError("Invalid gold reference response")
    for key in ("timestamp", "shift"):
        if isinstance(data.get(key), bool) or not isinstance(data.get(key), int):
            raise ValueError("Invalid gold response time units")
    if data["timestamp"] < 0 or data["timestamp"] % 3600000 or data["shift"] != 3600000:
        raise ValueError("Gold feed must have aligned UTC hourly timestamps")
    multiplier = Decimal(str(data.get("multiplier")))
    if not multiplier.is_finite() or multiplier <= 0:
        raise ValueError("Invalid gold response price multiplier")
    columns = ("times", "opens", "highs", "lows", "closes", "volumes")
    if any(not isinstance(data.get(key), list) for key in columns):
        raise ValueError("Missing gold delta columns")
    length = len(data["times"])
    if not length or any(len(data[key]) != length for key in columns) or length > 31 * 24:
        raise ValueError("Gold delta column lengths are invalid")
    units = {}
    for key in ("open", "high", "low", "close"):
        base = Decimal(str(data.get(key))) / multiplier
        if not base.is_finite() or base <= 0 or base != base.to_integral_value():
            raise ValueError("Gold base candle is not aligned to its price multiplier")
        units[key] = int(base)
    rows, stamp = [], data["timestamp"]
    for i in range(length):
        delta = data["times"][i]
        if isinstance(delta, bool) or not isinstance(delta, int) or delta < (0 if i == 0 else 1):
            raise ValueError("Invalid gold time delta; source order must be strict")
        stamp += delta * data["shift"]
        row = {"timestamp": datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()}
        for key in units:
            change = data[key + "s"][i]
            if isinstance(change, bool) or not isinstance(change, int):
                raise ValueError("Gold price deltas must be integer units")
            units[key] += change
            row[key] = units[key] * multiplier
        volume = Decimal(str(data["volumes"][i]))
        if not volume.is_finite() or volume < 0:
            raise ValueError("Invalid gold reference volume")
        row["volume"] = volume
        if (min(row[key] for key in units) <= 0
                or not row["low"] <= min(row["open"], row["close"]) <= max(row["open"], row["close"]) <= row["high"]):
            raise ValueError("Invalid decoded gold OHLC")
        rows.append(row)
    return rows


def download_xau_reference(start, end, output, *, byte_budget=8 * 1024 * 1024, progress=None):
    start, end = utc(start), utc(end)
    if output.exists():
        raise ValueError("Gold download output exists; use a new directory")
    if (start >= end or any((at.day, at.hour, at.minute, at.second, at.microsecond) != (1, 0, 0, 0, 0) for at in (start, end))
            or end > datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            or isinstance(byte_budget, bool) or not isinstance(byte_budget, int) or not 0 < byte_budget <= 32 * 1024 * 1024):
        raise ValueError("Gold import requires closed complete UTC months and a 1..32 MiB byte budget")
    months, cursor = [], start
    while cursor < end:
        months.append(cursor)
        cursor = add_months(cursor, 1)
    if len(months) > 120:
        raise ValueError("Gold reference import is limited to 120 months per registered job")
    output.mkdir(parents=True)
    raw_directory = output / "raw"
    raw_directory.mkdir()
    plan = {"schema": 1, "symbol": "XAU/USD", "start": start.isoformat(), "end_exclusive": end.isoformat(),
            "endpoint_root": ROOT, "format_reference": FORMAT_REFERENCE, "timeframe_minutes": 60,
            "maximum_requests": len(months) * 2, "byte_budget": byte_budget,
            "minimum_pause_seconds": 1, "rate_limit_policy": "Stop on HTTP failure, including 429; no bypass or aggressive retries",
            "gap_policy": "Decode native records only; never insert flat/zero-volume candles",
            "scope": "PUBLIC REFERENCE QUOTES; not account execution, ML labels or profitability",
            "approved_for_live": False}
    _write_json(output / "registration.json", plan)
    records, sides, used = [], {"BID": [], "ASK": []}, 0
    status, error, imported = "complete", None, None
    try:
        for month in months:
            for side in sides:
                url = f"{ROOT}/{side}/{month.year}/{month.month}"
                if progress:
                    progress(f"Gold native hourly reference {side} {month:%Y-%m}; no fabricated bars")
                request = Request(url, headers={"User-Agent": "V2-offline-research/1.0", "Accept": "application/json"})
                remaining = byte_budget - used
                if remaining <= 0:
                    raise ValueError("Gold download byte budget reached")
                with urlopen(request, timeout=30) as response:
                    if response.status != 200 or response.geturl() != url:
                        raise ValueError("Unexpected gold download status/redirect")
                    payload = response.read(min(remaining, 1024 * 1024) + 1)
                if len(payload) > min(remaining, 1024 * 1024):
                    raise ValueError("Gold response exceeds the registered byte budget")
                used += len(payload)
                filename = f"{month:%Y-%m}-{side}.json"
                with (raw_directory / filename).open("xb") as stream:
                    stream.write(payload)
                decoded = decode_hourly(payload)
                following = add_months(month, 1)
                if any(not month <= utc(row["timestamp"]) < following for row in decoded):
                    raise ValueError("Gold feed records exceed their requested UTC month")
                sides[side].extend(decoded)
                record = {"url": url, "file": filename, "bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest(), "native_rows": len(decoded)}
                records.append(record)
                _write_json(output / f"request-{len(records):03d}.json", record)
                time.sleep(1)
        for side, rows in sides.items():
            with (output / f"{side.lower()}.csv").open("x", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("timestamp", "open", "high", "low", "close", "volume"))
                writer.writeheader()
                writer.writerows(rows)
        imported = import_xau_reference(output / "bid.csv", output / "ask.csv", ROOT + "; native public reference, not user broker",
                                       output / "reference")
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    result = {"status": status, "error": error, "plan": plan, "requests_completed": len(records),
              "bytes_downloaded": used, "raw_files": records, "reference_import": imported,
              "fabricated_intervals": 0, "models_trained": False, "approved_for_live": False}
    _write_json(output / "report.json", result)
    return result
