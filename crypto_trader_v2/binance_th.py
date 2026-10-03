"""Binance TH PUBLIC-only, bounded snapshots and reproducible local captures.

Never loads credentials, follows redirects, submits orders, or substitutes
Binance.com / Kraken prices. A blocked/rate-limited request fails without retry.
REST bookTicker has no exchange event time: quote.time is request-start time,
not proof of upstream quote freshness. Public snapshots are NOT live approval.
"""
from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config
from .data import validate_bars
from .domain import Bar, Instrument, Quote, money


BASE_URL = "https://api.binance.th"
DOCS_URL = "https://www.binance.th/api-docs/en/"
INTERVALS = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}
PUBLIC_PARAMS = {
    "time": set(), "exchangeInfo": set(),
    "klines": {"symbol", "interval", "startTime", "endTime", "limit"},
    "ticker/bookTicker": {"symbol"},
}
MAX_WINDOW_SECONDS = 7 * 86400  # Observed native -4088 restriction, 2026-10-03.


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid integer {name}")
    return value


def _decode(raw):
    def invalid_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    result = json.loads(raw, parse_float=Decimal, parse_constant=invalid_constant)
    if isinstance(result, dict) and "code" in result:
        if _integer(result["code"], "response code") != 0 or "data" not in result:
            raise ValueError("Binance TH returned a public API error")
        result = result["data"]
    return result


def require_binance_config(cfg):
    cfg.validate()
    if cfg.venue != "binance_th":
        raise ValueError("Binance TH public data requires its own binance_th configuration")


def parse_instruments(payload, cfg):
    require_binance_config(cfg)
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise ValueError("Invalid exchangeInfo response")
    output, metadata = {}, {}
    for configured in cfg.instruments:
        base, quote = configured.symbol.split("/")
        matching = [r for r in payload["symbols"] if r.get("symbol") == base + quote]
        if len(matching) != 1:
            raise ValueError(f"Missing/ambiguous market: {configured.symbol}")
        row = matching[0]
        if (row.get("status") != "TRADING" or row.get("baseAsset") != base or row.get("quoteAsset") != quote
                or row.get("type") != "GLOBAL" or "MARKET" not in row.get("orderTypes", [])
                or row.get("isSpotTradingAllowed", True) is not True):
            raise ValueError(f"Unsupported/offline Binance TH spot market: {configured.symbol}")
        filters = row.get("filters", [])
        if not isinstance(filters, list) or any(not isinstance(r, dict) or "filterType" not in r for r in filters):
            raise ValueError("Malformed market filters")
        indexed = {r["filterType"]: r for r in filters}
        if len(indexed) != len(filters) or "LOT_SIZE" not in indexed:
            raise ValueError("Missing/duplicate market filters")
        # Unknown execution restrictions must be reviewed, not ignored.
        known = {"LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL", "PRICE_FILTER",
                 "PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE", "MAX_NUM_ORDERS", "MAX_NUM_ALGO_ORDERS"}
        if indexed.keys() - known:
            raise ValueError("Unsupported Binance TH execution filter")
        lot = indexed["LOT_SIZE"]
        minimum, maximum, step = (money(lot[k]) for k in ("minQty", "maxQty", "stepSize"))
        if min(minimum, maximum, step) <= 0 or minimum > maximum:
            raise ValueError("Invalid LOT_SIZE")
        market_lot = indexed.get("MARKET_LOT_SIZE")
        if market_lot:
            market_min, market_max, market_step = (money(market_lot[k]) for k in ("minQty", "maxQty", "stepSize"))
            if min(market_min, market_max, market_step) < 0:
                raise ValueError("Invalid MARKET_LOT_SIZE")
            if market_step and market_step != step:
                raise ValueError("Different MARKET_LOT_SIZE step requires a new execution model")
            minimum = max(minimum, market_min)
            if market_max:
                maximum = min(maximum, market_max)
        notional = indexed.get("NOTIONAL") or indexed.get("MIN_NOTIONAL")
        if not notional or ("NOTIONAL" in indexed and "MIN_NOTIONAL" in indexed):
            raise ValueError("Missing/ambiguous notional filter")
        min_notional = money(notional["minNotional"])
        max_notional = money(notional["maxNotional"]) if "maxNotional" in notional else None
        if min_notional <= 0:
            raise ValueError("Invalid minimum notional")
        output[configured.symbol] = Instrument(configured.symbol, step, minimum, min_notional, maximum, max_notional)
        metadata[configured.symbol] = row
    # Reuse config validation for finite and mutually consistent limits.
    from dataclasses import replace
    replace(cfg, instruments=tuple(output.values())).validate()
    return output, metadata


def parse_klines(payload, symbol, cfg, closed_at):
    if not isinstance(payload, list) or not payload:
        raise ValueError("Missing/invalid Binance TH klines")
    bars = []
    for row in payload:
        if not isinstance(row, list) or len(row) < 7:
            raise ValueError("Malformed kline row")
        opened, closed = _integer(row[0], "open time"), _integer(row[6], "close time")
        if opened < 0 or opened % (cfg.seconds * 1000) or closed != opened + cfg.seconds * 1000 - 1:
            raise ValueError("Kline close time / UTC timeframe grid mismatch")
        bar = Bar(symbol, datetime.fromtimestamp(opened / 1000, timezone.utc), cfg.seconds,
                  *(money(value) for value in row[1:6]))
        bar.validate()
        if bar.end > closed_at:
            raise ValueError("Unclosed/future candle returned for closed-only request")
        bars.append(bar)
    validate_bars(bars, cfg)
    return bars


class BinanceTHPublicFeed:
    # Historical-only readers use a separate bounded subclass. These live
    # snapshot defaults must not be relaxed when extending offline coverage.
    allowed_params = PUBLIC_PARAMS
    max_requests = 300
    max_elapsed_seconds = 120
    max_bytes = 8 * 1024 * 1024
    max_latency_seconds = 5
    request_spacing_seconds = 0.1

    def __init__(self, history_bars=720, *, opener=None, clock=None):
        if isinstance(history_bars, bool) or not isinstance(history_bars, int) or not 1 <= history_bars <= 1000:
            raise ValueError("History size must be 1..1000 bars (bounded seven-day chunks)")
        self.history_bars = history_bars
        self.opener = opener or build_opener(NoRedirect())
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.requests = []
        self.bytes_downloaded = 0
        self.market_metadata = {}
        self.observed_capacity = {}

    def request(self, endpoint, params):
        if endpoint not in self.allowed_params or params.keys() - self.allowed_params[endpoint]:
            raise ValueError("Request is not in the Binance TH public-data allowlist")
        if len(self.requests) >= self.max_requests:
            raise ValueError("Snapshot request budget exhausted")
        if self.requests and (self.clock() - datetime.fromisoformat(self.requests[0]["requested_at"])).total_seconds() > self.max_elapsed_seconds:
            raise ValueError("Snapshot elapsed-time budget exhausted")
        if self.requests:
            time.sleep(self.request_spacing_seconds)  # Bounded public reads; never a rate-limit retry.
        url = BASE_URL + "/api/v1/" + endpoint
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache",
                                       "User-Agent": "crypto-trader-v2/public-research"}, method="GET")
        started = self.clock()
        try:
            with self.opener.open(request, timeout=15) as response:
                if response.geturl() != url:
                    raise ValueError("Redirected public data is not allowed")
                raw = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as error:
            retry = error.headers.get("Retry-After", "unspecified") if error.headers else "unspecified"
            raise ValueError(f"Binance TH public {endpoint} HTTP {error.code}; stopped without retry; Retry-After={retry}") from error
        self.bytes_downloaded += len(raw)
        if len(raw) > 2 * 1024 * 1024 or self.bytes_downloaded > self.max_bytes:
            raise ValueError("Public response byte budget exceeded")
        received = self.clock()
        if not 0 <= (received - started).total_seconds() <= self.max_latency_seconds:
            raise ValueError("Public request exceeded freshness latency budget")
        self.requests.append({"endpoint": endpoint, "params": params.copy(), "url": url,
                              "requested_at": started.isoformat(), "received_at": received.isoformat(),
                              "sha256": hashlib.sha256(raw).hexdigest(), "raw": raw})
        return _decode(raw)

    def instruments(self, cfg):
        require_binance_config(cfg)
        result, self.market_metadata = parse_instruments(self.request("exchangeInfo", {}), cfg)
        return result

    def snapshot(self, cfg):
        require_binance_config(cfg)
        if self.history_bars <= cfg.warmup:
            raise ValueError("Snapshot history must exceed strategy warmup")
        self.observed_capacity = {}
        payload = self.request("time", {})
        if not isinstance(payload, dict):
            raise ValueError("Invalid server time response")
        server = datetime.fromtimestamp(_integer(payload.get("serverTime"), "server time") / 1000, timezone.utc)
        if abs((self.clock() - server).total_seconds()) > 5:
            raise ValueError("Local/server clock mismatch; no paper decisions made")
        boundary = int(server.timestamp()) // cfg.seconds * cfg.seconds
        closed_at = datetime.fromtimestamp(boundary, timezone.utc)
        histories, quotes = {}, {}
        for instrument in cfg.instruments:
            bars, cursor = [], boundary - self.history_bars * cfg.seconds
            chunk_limit = min(1000, MAX_WINDOW_SECONDS // cfg.seconds)
            while cursor < boundary:
                count = min(chunk_limit, (boundary - cursor) // cfg.seconds)
                end = cursor + count * cfg.seconds
                params = {"symbol": instrument.symbol.replace("/", ""), "interval": INTERVALS[cfg.timeframe_minutes],
                          "startTime": cursor * 1000, "endTime": end * 1000 - 1, "limit": count}
                chunk = parse_klines(self.request("klines", params), instrument.symbol, cfg, datetime.fromtimestamp(end, timezone.utc))
                if len(chunk) != count or int(chunk[0].start.timestamp()) != cursor or int(chunk[-1].end.timestamp()) != end:
                    raise ValueError("Incomplete native history chunk; no automatic gap repair")
                bars.extend(chunk)
                cursor = end
            validate_bars(bars, cfg)
            if len(bars) != self.history_bars or bars[-1].end != closed_at or int(bars[0].start.timestamp()) != boundary - self.history_bars * cfg.seconds:
                raise ValueError("Incomplete native history; never fills gaps or substitutes another venue")
            histories[instrument.symbol] = bars
        # Fetch quotes last; each quote binds its own conservative request time.
        for instrument in cfg.instruments:
            symbol = instrument.symbol
            row = self.request("ticker/bookTicker", {"symbol": symbol.replace("/", "")})
            if not isinstance(row, dict) or row.get("symbol") != symbol.replace("/", ""):
                raise ValueError("Book ticker symbol mismatch")
            requested_at = datetime.fromisoformat(self.requests[-1]["requested_at"])
            quote = Quote(symbol, requested_at, money(row["bidPrice"]), money(row["askPrice"]))
            quote.validate()
            quotes[symbol] = quote
            for side, key in (("buy", "askQty"), ("sell", "bidQty")):
                capacity = money(row[key])
                if capacity < 0:
                    raise ValueError("Negative book ticker capacity")
                self.observed_capacity[(symbol, side)] = (quote.time, capacity)
        now = self.clock()
        if any(not 0 <= (now - q.time).total_seconds() <= cfg.quote_max_age_seconds for q in quotes.values()):
            raise ValueError("Incomplete fresh quote snapshot")
        return now, histories, quotes

    def reset_request_budget(self):
        self.requests = []
        self.bytes_downloaded = 0


def capture_binance_th(cfg, output: Path, *, history_bars=720, feed=None):
    """One auditable native snapshot, not a training/untouched holdout study."""
    require_binance_config(cfg)
    if output.exists():
        raise ValueError("Refusing to overwrite a public capture directory")
    client = feed or BinanceTHPublicFeed(history_bars)
    client.reset_request_budget()
    instruments = client.instruments(cfg)
    now, histories, quotes = client.snapshot(cfg)
    output.mkdir(parents=True, exist_ok=False)
    raw_dir = output / "raw"
    raw_dir.mkdir()
    receipts = []
    for index, receipt in enumerate(client.requests):
        filename = f"raw/{index:02d}.json"
        (output / filename).write_bytes(receipt["raw"])
        receipts.append({k: v for k, v in receipt.items() if k != "raw"} | {"file": filename})
    with (output / "candles.csv").open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
        for symbol in sorted(histories):
            for bar in histories[symbol]:
                writer.writerow([bar.start.isoformat(), symbol, bar.open, bar.high, bar.low, bar.close, bar.volume])
    manifest = {"schema": 1, "venue": cfg.venue, "timeframe_minutes": cfg.timeframe_minutes,
                "symbols": sorted(histories), "source": "Binance TH public REST API; " + DOCS_URL,
                "dataset_file": "candles.csv", "csv_sha256": hashlib.sha256((output / "candles.csv").read_bytes()).hexdigest(),
                "captured_at": now.isoformat(), "config_hash": cfg.digest(),
                "bars_per_symbol": {s: len(b) for s, b in histories.items()},
                "start": next(iter(histories.values()))[0].start.isoformat(),
                "end_exclusive": next(iter(histories.values()))[-1].end.isoformat(),
                "effective_market_rules": {s: asdict(i) for s, i in instruments.items()},
                "native_market_metadata": client.market_metadata,
                "quotes": {s: asdict(q) for s, q in quotes.items()}, "receipts": receipts,
                "bytes_downloaded": client.bytes_downloaded, "fabricated_intervals": 0,
                "synthetic": False, "synchronized": True, "replay_ready": True,
                "models_trained": 0, "approved_for_live": False,
                "limitations": ["Recent native snapshot only; not sufficient multi-year ML or untouched OOS evidence",
                                "REST book ticker has no exchange event timestamp; receipt is not freshness proof",
                                "Assumed proportional received-asset fees; actual account precision/rate unverified",
                                "USDT-denominated only; excludes THB conversion, tax and funding costs"]}
    (output / "manifest.json").write_text(json.dumps(manifest, default=str, sort_keys=True, indent=2) + "\n")
    return manifest


def verify_binance_capture(directory: Path, cfg):
    """Read-only raw -> candle/quote/rule verification; never rewrites files."""
    from .importer import load_dataset

    require_binance_config(cfg)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("config_hash") != cfg.digest() or manifest.get("approved_for_live") is not False:
        raise ValueError("Capture config / safety binding mismatch")
    receipts = manifest.get("receipts", [])
    chunk_limit = min(1000, MAX_WINDOW_SECONDS // cfg.seconds)
    counts = [manifest["bars_per_symbol"][i.symbol] for i in cfg.instruments]
    if not all(isinstance(n, int) and not isinstance(n, bool) and cfg.warmup < n <= 1000 for n in counts):
        raise ValueError("Invalid capture candle count")
    chunks = [(n + chunk_limit - 1) // chunk_limit for n in counts]
    if len(receipts) != 2 + sum(chunks) + len(cfg.instruments):
        raise ValueError("Unexpected capture request count")
    decoded, byte_count = [], 0
    for index, receipt in enumerate(receipts):
        expected = f"raw/{index:02d}.json"
        if receipt.get("file") != expected or (directory / expected).is_symlink():
            raise ValueError("Unexpected raw capture path")
        endpoint, params = receipt["endpoint"], receipt["params"]
        if endpoint not in PUBLIC_PARAMS or params.keys() - PUBLIC_PARAMS[endpoint]:
            raise ValueError("Raw request is not public allowlisted")
        url = BASE_URL + "/api/v1/" + endpoint + ("?" + urlencode(params) if params else "")
        # JSON sort_keys changes query ordering; compare canonical parameters.
        from urllib.parse import parse_qs, urlsplit
        recorded, canonical = urlsplit(receipt["url"]), urlsplit(url)
        if ((recorded.scheme, recorded.netloc, recorded.path) != (canonical.scheme, canonical.netloc, canonical.path)
                or recorded.fragment or parse_qs(recorded.query) != parse_qs(canonical.query)):
            raise ValueError("Raw public URL mismatch")
        raw = (directory / expected).read_bytes()
        byte_count += len(raw)
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("Raw response checksum mismatch")
        decoded.append(_decode(raw))
    if byte_count != manifest["bytes_downloaded"]:
        raise ValueError("Raw byte count mismatch")
    if receipts[0]["endpoint"] != "exchangeInfo" or receipts[1]["endpoint"] != "time":
        raise ValueError("Unexpected native snapshot request sequence")
    instruments, metadata = parse_instruments(decoded[0], cfg)
    normalize = lambda value: json.loads(json.dumps(value, default=str))
    if normalize({s: asdict(i) for s, i in instruments.items()}) != manifest["effective_market_rules"] or metadata != manifest["native_market_metadata"]:
        raise ValueError("Native market rule projection mismatch")
    server_ms = _integer(decoded[1]["serverTime"], "server time")
    closed_at = datetime.fromtimestamp(server_ms // (cfg.seconds * 1000) * cfg.seconds, timezone.utc)
    dataset = load_dataset(directory, cfg)
    cursor_index = 2
    ticker_index = 2 + sum(chunks)
    for index, instrument in enumerate(cfg.instruments):
        ticker_receipt = receipts[ticker_index + index]
        pair = instrument.symbol.replace("/", "")
        bars, start_seconds = [], int(closed_at.timestamp()) - counts[index] * cfg.seconds
        for _ in range(chunks[index]):
            receipt = receipts[cursor_index]
            count = min(chunk_limit, counts[index] - len(bars))
            end_seconds = start_seconds + count * cfg.seconds
            expected_params = {"symbol": pair, "interval": INTERVALS[cfg.timeframe_minutes], "limit": count,
                               "startTime": start_seconds * 1000, "endTime": end_seconds * 1000 - 1}
            if receipt["endpoint"] != "klines" or receipt["params"] != expected_params:
                raise ValueError("Native candle receipt / request boundary mismatch")
            chunk = parse_klines(decoded[cursor_index], instrument.symbol, cfg, datetime.fromtimestamp(end_seconds, timezone.utc))
            if len(chunk) != count or int(chunk[0].start.timestamp()) != start_seconds or int(chunk[-1].end.timestamp()) != end_seconds:
                raise ValueError("Incomplete native history chunk")
            bars.extend(chunk)
            start_seconds = end_seconds
            cursor_index += 1
        if bars != dataset.bars[instrument.symbol] or len(bars) != manifest["bars_per_symbol"][instrument.symbol]:
            raise ValueError("Raw kline / exported candle mismatch")
        if (bars[-1].end != closed_at or manifest["start"] != bars[0].start.isoformat() or
                manifest["end_exclusive"] != bars[-1].end.isoformat()):
            raise ValueError("Raw candle request boundary mismatch")
        ticker = decoded[ticker_index + index]
        if ticker_receipt["endpoint"] != "ticker/bookTicker" or ticker_receipt["params"] != {"symbol": pair} or ticker["symbol"] != pair:
            raise ValueError("Raw ticker receipt mismatch")
        q = Quote(instrument.symbol, datetime.fromisoformat(ticker_receipt["requested_at"]), money(ticker["bidPrice"]), money(ticker["askPrice"]))
        q.validate()
        if normalize(asdict(q)) != manifest["quotes"][instrument.symbol]:
            raise ValueError("Raw ticker / exported quote mismatch")
    if manifest.get("synthetic") is not False or manifest.get("fabricated_intervals") != 0 or manifest.get("models_trained") != 0:
        raise ValueError("Capture evidence scope mismatch")
    return {"status": "verified", "venue": cfg.venue, "requests_verified": len(receipts),
            "bars_per_symbol": manifest["bars_per_symbol"], "csv_sha256": dataset.checksum,
            "models_trained": 0, "approved_for_live": False}
