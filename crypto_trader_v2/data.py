from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from .config import Config
from .domain import Bar, Instrument, Quote, money, utc


@dataclass(frozen=True)
class Dataset:
    bars: dict[str, list[Bar]]
    source: str
    checksum: str
    synthetic: bool = False


def validate_bars(bars: list[Bar], cfg: Config):
    if not bars:
        raise ValueError("Empty candle data")
    for index, bar in enumerate(bars):
        bar.validate()
        if bar.seconds != cfg.seconds:
            raise ValueError("Candle timeframe mismatch")
        if bar.start.microsecond or int(bar.start.timestamp()) % cfg.seconds:
            raise ValueError("Candle must align to the UTC timeframe grid")
        if index and (bar.symbol != bars[index - 1].symbol or bar.start != bars[index - 1].end):
            raise ValueError(f"Unsorted, duplicate or missing candle: {bar.symbol} {bar.start}")


def read_csv(path: Path, cfg: Config, source: str) -> Dataset:
    """Long-format CSV. timestamp is the bar's UTC opening time."""
    if not source.strip():
        raise ValueError("A dataset source is required")
    configured = {i.symbol for i in cfg.instruments}
    grouped = {s: [] for s in configured}
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"CSV requires columns: {sorted(required)}")
        for row in reader:
            if row["symbol"] not in grouped:
                raise ValueError(f"Unconfigured symbol: {row['symbol']}")
            grouped[row["symbol"]].append(Bar(row["symbol"], utc(row["timestamp"]), cfg.seconds,
                *(money(row[k]) for k in ("open", "high", "low", "close", "volume"))))
    for bars in grouped.values():
        validate_bars(bars, cfg)
    timeline = [b.start for b in next(iter(grouped.values()))]
    if any([b.start for b in bars] != timeline for bars in grouped.values()):
        raise ValueError("All configured symbols must cover the same timeline")
    if len(timeline) <= cfg.warmup:
        raise ValueError(f"Need more than {cfg.warmup} bars per symbol")
    return Dataset(grouped, source, hashlib.sha256(path.read_bytes()).hexdigest())


def demo_dataset(cfg: Config, count: int = 600) -> Dataset:
    """Deterministic mixed-regime fixture, never evidence of a profitable edge."""
    from math import sin

    count = max(count, cfg.warmup + 100)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    grouped = {}
    for asset_index, instrument in enumerate(cfg.instruments):
        price = Decimal("100") * (asset_index + 1)
        bars = []
        for index in range(count):
            regime = (index // 70) % 4
            drift = ("0.006", "-0.008", "0.001", "0.010")[regime]
            if regime in (0, 3) and index % 70 < 20:
                drift = "0"
            elif regime in (0, 3) and index % 70 == 20:
                drift = "0.04"
            change = money(drift) + money(sin(index * 0.7 + asset_index)) * Decimal("0.004")
            close = price * (1 + change)
            high, low = max(price, close) * Decimal("1.002"), min(price, close) * Decimal("0.998")
            bars.append(Bar(instrument.symbol, start + timedelta(seconds=index * cfg.seconds), cfg.seconds,
                            price, high, low, close, Decimal("10000")))
            price = close
        grouped[instrument.symbol] = bars
    checksum = hashlib.sha256(repr(grouped).encode()).hexdigest()
    return Dataset(grouped, "SYNTHETIC: deterministic software fixture", checksum, True)


class KrakenPublicFeed:
    """Read-only public market data; has no credential or order interface."""
    base_url = "https://api.kraken.com/0/public/"

    def request(self, endpoint: str, params: dict) -> dict:
        if endpoint not in {"AssetPairs", "OHLC", "Ticker"}:
            raise ValueError("Endpoint is not in the public-data allowlist")
        with urlopen(self.base_url + endpoint + "?" + urlencode(params), timeout=15) as response:
            payload = json.load(response, parse_float=Decimal)
        if payload.get("error"):
            raise ValueError(f"Kraken public-data error: {payload['error']}")
        return payload["result"]

    @staticmethod
    def pair(symbol: str) -> str:
        return symbol.replace("BTC/", "XBT/").replace("/", "")

    def instruments(self, cfg: Config) -> dict[str, Instrument]:
        result = self.request("AssetPairs", {"pair": ",".join(self.pair(i.symbol) for i in cfg.instruments)})
        output = {}
        for symbol in (i.symbol for i in cfg.instruments):
            matching = [v for v in result.values() if v.get("wsname", "").replace("XBT/", "BTC/") == symbol]
            if len(matching) != 1:
                raise ValueError(f"Missing/ambiguous instrument metadata: {symbol}")
            row = matching[0]
            if row.get("status") not in (None, "online"):
                raise ValueError(f"Instrument is not online: {symbol}")
            if "costmin" not in row:
                raise ValueError(f"Minimum notional is unavailable: {symbol}")
            output[symbol] = Instrument(symbol, Decimal(10) ** -int(row["lot_decimals"]),
                                        money(row["ordermin"]), money(row["costmin"]))
        return output

    def snapshot(self, cfg: Config) -> tuple[datetime, dict[str, list[Bar]], dict[str, Quote]]:
        histories, quotes = {}, {}
        for instrument in cfg.instruments:
            symbol = instrument.symbol
            result = self.request("OHLC", {"pair": self.pair(symbol), "interval": cfg.timeframe_minutes})
            arrays = [v for k, v in result.items() if k != "last"]
            if len(arrays) != 1:
                raise ValueError("Unexpected OHLC response")
            received = datetime.now(timezone.utc)
            bars = [Bar(symbol, datetime.fromtimestamp(int(r[0]), timezone.utc), cfg.seconds,
                        *(money(r[i]) for i in (1, 2, 3, 4, 6))) for r in arrays[0]]
            histories[symbol] = [b for b in bars if b.end <= received]
            validate_bars(histories[symbol], cfg)
        # Quotes fetched last so histories do not age quotes before the decision.
        tickers = self.request("Ticker", {"pair": ",".join(self.pair(i.symbol) for i in cfg.instruments)})
        received = datetime.now(timezone.utc)
        for instrument in cfg.instruments:
            candidates = [v for k, v in tickers.items() if k.replace("XXBTZUSD", "XBTUSD").replace("XETHZUSD", "ETHUSD") == self.pair(instrument.symbol)]
            if len(candidates) != 1:
                raise ValueError(f"Missing ticker: {instrument.symbol}")
            row = candidates[0]
            quotes[instrument.symbol] = Quote(instrument.symbol, received, money(row["b"][0]), money(row["a"][0]))
            quotes[instrument.symbol].validate()
        return received, histories, quotes
