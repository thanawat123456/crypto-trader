"""Registered Binance TH GLOBAL-reference history and quality-only ML coverage.

No features, labels, returns, models or orders. Previously inspected candles
are excluded from development. Public launch is NOT proof of pair listing or
historical TH execution. A longer GLOBAL series never acquires a TH-fill label.
"""
from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlencode

from .binance_th import (BASE_URL, DOCS_URL, INTERVALS, MAX_WINDOW_SECONDS, PUBLIC_PARAMS,
                        BinanceTHPublicFeed, _decode, _integer, parse_instruments,
                        parse_klines, require_binance_config, verify_binance_capture)
from .data import validate_bars
from .domain import utc
from .importer import load_dataset
from .ml_preparation import PREPARATION_PROTOCOLS, ml_windows
from .study import add_months


LAUNCH_URL = "https://www.binance.com/en-NG/blog/regulation/7846087428943809900"
PUBLIC_LAUNCH_DATE = "2024-01-16"
# First full UTC day AFTER public launch. Not an inferred listing timestamp.
HISTORY_START = datetime(2024, 1, 17, tzinfo=timezone.utc)
SOURCE_SCOPE = "Binance TH public API GLOBAL OHLC reference; NOT historical TH fills or verified historical contract terms"
PURPOSE = "Binance TH post-public-launch GLOBAL reference / protected quality-only preparation"


class BinanceTHHistoryFeed(BinanceTHPublicFeed):
    """Offline-only transport. No quote/account/order calls, no live snapshots."""
    allowed_params = {key: PUBLIC_PARAMS[key] for key in ("time", "exchangeInfo", "klines")}
    max_requests = 420
    max_elapsed_seconds = 600
    max_bytes = 16 * 1024 * 1024
    max_latency_seconds = 15
    request_spacing_seconds = 0.2

    def snapshot(self, cfg):
        raise ValueError("History-only reader cannot make live/paper snapshots")


def _code_hash():
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))).hexdigest()


def _write_json(path, value):
    with path.open("x") as stream:
        stream.write(json.dumps(value, default=str, sort_keys=True, indent=2) + "\n")


def _canonical(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":")).encode()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _periods(start, protocol):
    cursor, periods = 0, {}
    for key in ("fit", "calibration", "approval", "validation"):
        beginning = add_months(start, cursor)
        cursor += protocol[key + "_months"]
        periods[key] = {"start": beginning.isoformat(), "end_exclusive": add_months(start, cursor).isoformat()}
    return periods


def _requests(start, end, cfg):
    count = min(1000, MAX_WINDOW_SECONDS // cfg.seconds)
    cursor = int(start.timestamp())
    while cursor < int(end.timestamp()):
        size = min(count, (int(end.timestamp()) - cursor) // cfg.seconds)
        stop = cursor + size * cfg.seconds
        yield {"interval": INTERVALS[cfg.timeframe_minutes], "startTime": cursor * 1000,
               "endTime": stop * 1000 - 1, "limit": size}
        cursor = stop


def _plan(cfg, protected_capture, at):
    require_binance_config(cfg)
    _require(cfg.timeframe_minutes == 240, "Registered history protocol requires native 4h data")
    audit = verify_binance_capture(protected_capture, cfg)
    manifest_path = protected_capture / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    start, end = utc(manifest["start"]), utc(manifest["end_exclusive"])
    _require(HISTORY_START < start < end <= at, "Protected capture is outside post-launch/closed history bounds")
    requests = 2 + len(list(_requests(HISTORY_START, end, cfg))) * len(cfg.instruments)
    _require(requests <= BinanceTHHistoryFeed.max_requests, "Requested history exceeds fixed request budget")
    return {"schema": 1, "purpose": PURPOSE, "registered_at": at.isoformat(), "source_scope": SOURCE_SCOPE,
            "code_hash": _code_hash(), "config_hash": cfg.digest(), "config": asdict(cfg),
            "public_launch": {"date": PUBLIC_LAUNCH_DATE, "source": LAUNCH_URL,
                              "limitation": "General-public launch only; pair listing/date and historical GLOBAL routing unverified"},
            "requested_start": HISTORY_START.isoformat(), "requested_end_exclusive": end.isoformat(),
            "protected_capture": str(protected_capture.resolve()),
            "protected_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "protected_csv_sha256": audit["csv_sha256"],
            "development_end_exclusive": start.isoformat(),
            "inspected_suffix": {"start": start.isoformat(), "end_exclusive": end.isoformat(),
                                 "status": "Previously inspected engineering data; excluded, NEVER certified untouched"},
            "protocols": {name: {"months": protocol, "first_window": _periods(HISTORY_START, protocol)}
                          for name, protocol in PREPARATION_PROTOCOLS.items()},
            "expected_requests": requests,
            "budget": {"requests": BinanceTHHistoryFeed.max_requests, "bytes": BinanceTHHistoryFeed.max_bytes,
                       "elapsed_seconds": BinanceTHHistoryFeed.max_elapsed_seconds,
                       "latency_seconds": BinanceTHHistoryFeed.max_latency_seconds,
                       "spacing_seconds": BinanceTHHistoryFeed.request_spacing_seconds},
            "gap_policy": "Strict complete UTC grid; stop on missing/changed data, no repair, retries or venue substitution",
            "data_status": "RETROSPECTIVE REFERENCE DEVELOPMENT; no downloaded candle is certified untouched OOS",
            "labels_computed": False, "models_trained": 0, "approved_for_live": False,
            "default_policy_changed": False, "ml_enabled": False}


def _csv(path, histories):
    with path.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "symbol", "open", "high", "low", "close", "volume"))
        for symbol in sorted(histories):
            for bar in histories[symbol]:
                writer.writerow((bar.start.isoformat(), symbol, bar.open, bar.high, bar.low, bar.close, bar.volume))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(cfg, histories, checksum, plan):
    bars = next(iter(histories.values()))
    return {"schema": 1, "venue": cfg.venue, "timeframe_minutes": cfg.timeframe_minutes,
            "symbols": sorted(histories), "source": SOURCE_SCOPE + "; " + DOCS_URL, "source_scope": SOURCE_SCOPE,
            "dataset_file": "candles.csv", "csv_sha256": checksum, "start": bars[0].start.isoformat(),
            "end": bars[-1].end.isoformat(), "synthetic": False, "synchronized": True, "replay_ready": True,
            "quality": {symbol: {"bars": len(rows), "gap_count": 0, "missing_candles": 0, "gaps": []}
                        for symbol, rows in histories.items()},
            "config_hash": cfg.digest(), "registration_sha256": hashlib.sha256(_canonical(plan)).hexdigest(),
            "models_trained": 0, "approved_for_live": False}


def _coverage(histories, development, plan, cfg):
    result = {}
    for name, protocol in PREPARATION_PROTOCOLS.items():
        def count(rows):
            if add_months(rows[0].start, sum(protocol[k] for k in ("fit_months", "calibration_months", "approval_months", "validation_months"))) > rows[-1].end:
                return 0
            return len(ml_windows(rows, cfg.warmup, protocol))
        result[name] = {"protocol": protocol, "first_window": plan["protocols"][name]["first_window"],
                        "complete_full_reference_windows": count(next(iter(histories.values()))),
                        "complete_permitted_development_windows": count(next(iter(development.values()))),
                        "selection_performed": False, "ready_for_ml": False}
    return result


def capture_native_history(cfg, protected_capture: Path, output: Path, *, feed=None, progress=None):
    """Register before network; preserve raw receipts even if the run fails."""
    _require(not output.exists(), "History output exists; use a new directory")
    client = feed or BinanceTHHistoryFeed()
    _require(isinstance(client, BinanceTHHistoryFeed), "History capture requires the history-only reader")
    at = utc(client.clock())
    plan = _plan(cfg, protected_capture, at)
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    _write_json(output / "registration.json", {"plan": plan, "sha256": hashlib.sha256(_canonical(plan)).hexdigest()})
    client.reset_request_budget()
    persisted = []

    def persist():
        for index in range(len(persisted), len(client.requests)):
            receipt = client.requests[index]
            filename = f"raw/{index:04d}.json"
            with (output / filename).open("xb") as stream:
                stream.write(receipt["raw"])
            persisted.append({key: value for key, value in receipt.items() if key != "raw"} | {"file": filename})

    def request(endpoint, params):
        try:
            return client.request(endpoint, params)
        finally:
            persist()

    failure = None
    try:
        instruments, metadata = parse_instruments(request("exchangeInfo", {}), cfg)
        server = datetime.fromtimestamp(_integer(request("time", {})["serverTime"], "server time") / 1000, timezone.utc)
        _require(abs((client.clock() - server).total_seconds()) <= 5, "Local/server clock mismatch")
        end = utc(plan["requested_end_exclusive"])
        _require(end <= server, "Requested history includes future/unclosed candles")
        histories = {}
        for instrument in cfg.instruments:
            bars = []
            for params in _requests(HISTORY_START, end, cfg):
                params = {"symbol": instrument.symbol.replace("/", ""), **params}
                boundary = datetime.fromtimestamp((params["endTime"] + 1) / 1000, timezone.utc)
                chunk = parse_klines(request("klines", params), instrument.symbol, cfg, boundary)
                _require(len(chunk) == params["limit"] and int(chunk[0].start.timestamp()) * 1000 == params["startTime"]
                         and chunk[-1].end == boundary, "Incomplete historical chunk; no gap repair/substitution")
                bars.extend(chunk)
                if progress and (len(persisted) % 25 == 0):
                    progress(f"History {len(persisted)}/{plan['expected_requests']} public reads; {instrument.symbol} through {boundary.isoformat()}")
            validate_bars(bars, cfg)
            histories[instrument.symbol] = bars
        previous = load_dataset(protected_capture, cfg)
        for symbol, old_rows in previous.bars.items():
            by_time = {bar.start: bar for bar in histories[symbol]}
            _require(all(by_time.get(bar.start) == bar for bar in old_rows), "History rewrites/omits protected inspected candles")
        development = {symbol: [bar for bar in rows if bar.end <= utc(plan["development_end_exclusive"])]
                       for symbol, rows in histories.items()}
        _require(all(len(rows) > cfg.warmup for rows in development.values()), "Not enough permitted development history")
        _require(_code_hash() == plan["code_hash"], "Package changed during historical capture")
        checksum = _csv(output / "candles.csv", histories)
        destination = output / "development"
        destination.mkdir()
        development_checksum = _csv(destination / "candles.csv", development)
        dev_manifest = _manifest(cfg, development, development_checksum, plan)
        dev_manifest["subset"] = {"development_only": True, "reserved_suffix_excluded": True,
                                  "parent_csv_sha256": checksum, "excluded_start": plan["development_end_exclusive"],
                                  "limitation": plan["data_status"]}
        _write_json(destination / "manifest.json", dev_manifest)
        manifest = _manifest(cfg, histories, checksum, plan)
        manifest.update({"receipts": persisted, "bytes_downloaded": client.bytes_downloaded,
                         "captured_at": client.clock().isoformat(), "native_market_metadata": metadata,
                         "effective_market_rules": {symbol: asdict(instrument) for symbol, instrument in instruments.items()},
                         "market_rules_status": "Current observed GLOBAL metadata only; historical rules/listing dates unverified",
                         "development_csv_sha256": development_checksum, "coverage": _coverage(histories, development, plan, cfg)})
        _write_json(output / "manifest.json", manifest)
        audit = verify_native_history(output, cfg)
    except (ValueError, OSError, KeyError, TypeError) as error:
        failure = f"{type(error).__name__}: {error}"
    _write_json(output / "receipts.json", {"receipts": persisted, "bytes_downloaded": client.bytes_downloaded})
    report = {"status": "failed" if failure else "complete", "error": failure, "output": str(output),
              "source_scope": SOURCE_SCOPE, "requests_completed": len(persisted), "bytes_downloaded": client.bytes_downloaded,
              "registration_sha256": hashlib.sha256(_canonical(plan)).hexdigest(),
              "labels_computed": False, "models_trained": 0, "ml_enabled": False, "approved_for_live": False,
              "default_policy_changed": False, "worker_running": False}
    if not failure:
        report.update({"audit": audit, "coverage": manifest["coverage"],
                       "bars_per_symbol": {symbol: len(rows) for symbol, rows in histories.items()},
                       "development_bars_per_symbol": {symbol: len(rows) for symbol, rows in development.items()},
                       "development_end_exclusive": plan["development_end_exclusive"],
                       "barriers": ["35/41-month presets need complete permitted development windows, not inspected-suffix reuse",
                                    "GLOBAL OHLC is reference, not proof of TH historical fills/routing/listing",
                                    "Historical filters, fee precision and intrabar execution remain unverified",
                                    "No model selected/trained; no untouched or forward profit evidence"]})
    _write_json(output / "report.json", report)
    return report


def verify_native_history(directory: Path, cfg):
    """Read-only raw -> full/development projections, protected rows and dates."""
    require_binance_config(cfg)
    envelope = json.loads((directory / "registration.json").read_text())
    plan = envelope["plan"]
    _require(set(envelope) == {"plan", "sha256"} and hashlib.sha256(_canonical(plan)).hexdigest() == envelope["sha256"],
             "History registration checksum mismatch")
    manifest = json.loads((directory / "manifest.json").read_text())
    expected_protocols = {name: {"months": protocol, "first_window": _periods(HISTORY_START, protocol)}
                          for name, protocol in PREPARATION_PROTOCOLS.items()}
    _require(plan.get("purpose") == PURPOSE and plan.get("source_scope") == SOURCE_SCOPE
             and plan.get("config_hash") == cfg.digest() and plan.get("protocols") == expected_protocols
             and _canonical(plan.get("config")) == _canonical(asdict(cfg)) and plan.get("schema") == 1
             and plan.get("requested_start") == HISTORY_START.isoformat()
             and manifest.get("registration_sha256") == envelope["sha256"], "History scope/config/protocol binding mismatch")
    _require(all(plan.get(key) is False for key in ("labels_computed", "ml_enabled", "approved_for_live", "default_policy_changed"))
             and plan.get("models_trained") == 0, "History safety scope mismatch")
    protected = Path(plan["protected_capture"])
    _require(hashlib.sha256((protected / "manifest.json").read_bytes()).hexdigest() == plan["protected_manifest_sha256"],
             "Protected capture manifest changed")
    protected_audit = verify_binance_capture(protected, cfg)
    _require(protected_audit["csv_sha256"] == plan["protected_csv_sha256"], "Protected capture checksum changed")
    old = load_dataset(protected, cfg)
    protected_rows = next(iter(old.bars.values()))
    _require(plan["development_end_exclusive"] == protected_rows[0].start.isoformat()
             and plan["requested_end_exclusive"] == protected_rows[-1].end.isoformat()
             and plan["inspected_suffix"] == {"start": protected_rows[0].start.isoformat(),
                                              "end_exclusive": protected_rows[-1].end.isoformat(),
                                              "status": "Previously inspected engineering data; excluded, NEVER certified untouched"},
             "History protected boundary mismatch")
    end = utc(plan["requested_end_exclusive"])
    _require(HISTORY_START < utc(plan["development_end_exclusive"]) < end <= utc(plan["registered_at"])
             and cfg.timeframe_minutes == 240, "History post-launch/closed/4h boundary mismatch")
    expected = [("exchangeInfo", {}), ("time", {})] + [
        ("klines", {"symbol": instrument.symbol.replace("/", ""), **params})
        for instrument in cfg.instruments for params in _requests(HISTORY_START, end, cfg)]
    receipts = manifest["receipts"]
    _require(len(receipts) == len(expected) == plan["expected_requests"] <= BinanceTHHistoryFeed.max_requests,
             "History request count mismatch")
    decoded, byte_count, last_received = [], 0, utc(plan["registered_at"])
    for index, (receipt, (endpoint, params)) in enumerate(zip(receipts, expected)):
        raw_path = directory / f"raw/{index:04d}.json"
        _require(receipt["file"] == f"raw/{index:04d}.json" and not raw_path.is_symlink(), "Unexpected raw history path")
        _require(receipt["endpoint"] == endpoint and receipt["params"] == params, "History raw request boundary mismatch")
        actual = urlsplit(receipt["url"])
        expected_url = urlsplit(BASE_URL + "/api/v1/" + endpoint + ("?" + urlencode(params) if params else ""))
        _require((actual.scheme, actual.netloc, actual.path) == (expected_url.scheme, expected_url.netloc, expected_url.path)
                 and not actual.fragment and parse_qs(actual.query) == parse_qs(expected_url.query), "History public URL mismatch")
        requested, received = utc(receipt["requested_at"]), utc(receipt["received_at"])
        _require(last_received <= requested <= received and (received-requested).total_seconds() <= BinanceTHHistoryFeed.max_latency_seconds,
                 "History receipt chronology/latency mismatch")
        last_received = received
        raw = raw_path.read_bytes()
        byte_count += len(raw)
        _require(len(raw) <= 2*1024*1024 and hashlib.sha256(raw).hexdigest() == receipt["sha256"], "History raw checksum/size mismatch")
        decoded.append(_decode(raw))
    _require(byte_count == manifest["bytes_downloaded"] <= BinanceTHHistoryFeed.max_bytes, "History byte budget mismatch")
    _require(last_received <= utc(manifest["captured_at"])
             and (last_received-utc(receipts[0]["requested_at"])).total_seconds() <= BinanceTHHistoryFeed.max_elapsed_seconds,
             "History elapsed/capture time mismatch")
    server = datetime.fromtimestamp(_integer(decoded[1]["serverTime"], "server time")/1000, timezone.utc)
    _require(end <= server and abs((server-utc(receipts[1]["requested_at"])).total_seconds()) <= 20,
             "History server/closed boundary mismatch")
    instruments, metadata = parse_instruments(decoded[0], cfg)
    _require(json.loads(json.dumps({symbol: asdict(instrument) for symbol, instrument in instruments.items()}, default=str)) == manifest["effective_market_rules"]
             and metadata == manifest["native_market_metadata"], "History GLOBAL market rule projection mismatch")
    dataset, development_dataset = load_dataset(directory, cfg), load_dataset(directory / "development", cfg)
    for relative, rows, checksum in (("", dataset.bars, dataset.checksum),
                                     ("development", development_dataset.bars, development_dataset.checksum)):
        root = directory / relative
        saved = json.loads((root / "manifest.json").read_text())
        _require(not (root / "candles.csv").is_symlink() and not (root / "manifest.json").is_symlink(),
                 "Symlinked history projection is not allowed")
        for key, value in _manifest(cfg, rows, checksum, plan).items():
            _require(saved.get(key) == value, "History manifest quality/source/safety projection mismatch")
    development_manifest = json.loads((directory / "development" / "manifest.json").read_text())
    _require(development_manifest.get("subset") == {"development_only": True, "reserved_suffix_excluded": True,
             "parent_csv_sha256": dataset.checksum, "excluded_start": plan["development_end_exclusive"],
             "limitation": plan["data_status"]}, "History development exclusion metadata mismatch")
    raw_bars = {instrument.symbol: [] for instrument in cfg.instruments}
    for index in range(2, len(expected)):
        params = expected[index][1]
        symbol = next(instrument.symbol for instrument in cfg.instruments if instrument.symbol.replace("/", "") == params["symbol"])
        raw_bars[symbol].extend(parse_klines(decoded[index], symbol, cfg, datetime.fromtimestamp((params["endTime"]+1)/1000, timezone.utc)))
    boundary = utc(plan["development_end_exclusive"])
    for symbol, rows in raw_bars.items():
        validate_bars(rows, cfg)
        _require(rows == dataset.bars[symbol] and rows[0].start == HISTORY_START and rows[-1].end == end,
                 "History raw/full candle projection mismatch")
        _require([bar for bar in rows if bar.end <= boundary] == development_dataset.bars[symbol],
                 "History development includes excluded/mismatched candles")
        by_time = {bar.start: bar for bar in rows}
        _require(all(by_time.get(bar.start) == bar for bar in old.bars[symbol]), "History changes inspected candle")
    _require(dataset.checksum == manifest["csv_sha256"] and development_dataset.checksum == manifest["development_csv_sha256"]
             and manifest["source_scope"] == SOURCE_SCOPE and not dataset.synthetic and not development_dataset.synthetic,
             "History dataset scope/checksum mismatch")
    _require(_coverage(dataset.bars, development_dataset.bars, plan, cfg) == manifest["coverage"], "History coverage/protocol projection mismatch")
    _require(all(not manifest.get(key) for key in ("models_trained", "approved_for_live")), "History manifest claims trading/ML approval")
    return {"status": "verified", "read_only": True, "source_scope": SOURCE_SCOPE,
            "requests_verified": len(receipts), "bytes_verified": byte_count,
            "bars_per_symbol": {symbol: len(rows) for symbol, rows in dataset.bars.items()},
            "development_bars_per_symbol": {symbol: len(rows) for symbol, rows in development_dataset.bars.items()},
            "csv_sha256": dataset.checksum, "development_csv_sha256": development_dataset.checksum,
            "protected_candles_match": True, "inspected_suffix_excluded": True,
            "current_package_matches_registration": _code_hash() == plan["code_hash"],
            "labels_computed": False, "models_trained": 0, "approved_for_live": False}
