"""Bounded Binance Vision spot archives. Reference research, never TH execution.

All downloaded data and derived models are RESEARCH ONLY under the provider's
dataset terms. No account, order, credential, retry, or alternate venue API.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

from crypto_trader_v2.domain import Bar, money


BASE = "https://data.binance.vision/data/spot/monthly/klines/"
TERMS = "https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md"
LICENSE = {
    "source": "Binance Vision", "license": "CC BY-NC-SA 4.0",
    "terms": TERMS, "terms_checked_on": "2026-10-03",
    "research_only": True, "live_execution_allowed": False,
    "notice": "Non-commercial, personal non-production research only; attribution/share-alike required. Not endorsed by Binance.",
}
SYMBOLS = ("BTC/USDT", "ETH/USDT")
START = datetime(2018, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 6, 1, tzinfo=timezone.utc)
SECONDS = 14400
QUALITY_POLICY = "Exclude and report otherwise-valid grid candles with an intra-bar shortened provider close; never normalize/fill them; reject out-of-bar closes and all other invalid data"
MAX_REQUESTS = 420
MAX_BYTES = 16 * 1024 * 1024
MAX_OBJECT = 1024 * 1024
MAX_ELAPSED = 900


def canonical(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_new(path, value):
    with Path(path).open("x") as stream:
        json.dump(value, stream, default=str, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def next_month(at):
    return at.replace(year=at.year + (at.month == 12), month=1 if at.month == 12 else at.month + 1)


def archive_plan():
    result, at = [], START
    while at < END:
        for symbol in SYMBOLS:
            pair = symbol.replace("/", "")
            name = f"{pair}-4h-{at:%Y-%m}.zip"
            result.append({"symbol": symbol, "month": at.isoformat(), "name": name,
                           "url": BASE + f"{pair}/4h/" + name})
        at = next_month(at)
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Public archive redirect refused")


class ArchiveClient:
    max_requests, max_bytes, max_elapsed = MAX_REQUESTS, MAX_BYTES, MAX_ELAPSED
    spacing_seconds = 0.15

    def __init__(self, *, opener=None, monotonic=None):
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.monotonic = monotonic or time.monotonic
        self.started = self.monotonic()
        self.attempts = self.bytes = 0
        self.allowed = {x["url"] + suffix for x in archive_plan() for suffix in ("", ".CHECKSUM")}

    def fetch(self, url):
        if url not in self.allowed or self.attempts >= self.max_requests:
            raise ValueError("Unapproved archive URL or request budget")
        if self.monotonic() - self.started >= self.max_elapsed or self.bytes >= self.max_bytes:
            raise ValueError("Archive byte/time budget exceeded")
        self.attempts += 1  # Failed HTTP requests consume the attempt budget too.
        if self.attempts > 1 and self.spacing_seconds:
            time.sleep(self.spacing_seconds)
        at = datetime.now(timezone.utc).isoformat()
        request = Request(url, headers={"User-Agent": "crypto-trader-reference-research/1", "Accept-Encoding": "identity"}, method="GET")
        with self.opener.open(request, timeout=10) as response:
            if response.status != 200 or response.geturl() != url:
                raise ValueError("Unexpected public response status/URL")
            chunks, received = [], 0
            while True:
                if self.monotonic() - self.started >= self.max_elapsed:
                    raise ValueError("Archive elapsed-time budget exceeded")
                chunk = response.read(min(65536, MAX_OBJECT + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                self.bytes += len(chunk)
                if received > MAX_OBJECT or self.bytes > self.max_bytes:
                    raise ValueError("Archive object/download byte limit exceeded")
            headers = {k: response.headers.get(k) for k in ("ETag", "Last-Modified", "Date")}
        raw = b"".join(chunks)
        return raw, {"url": url, "requested_at": at, "received_at": datetime.now(timezone.utc).isoformat(),
                     "bytes": len(raw), "sha256": sha(raw), "headers": headers}


def decode_archive(raw, checksum, item):
    match = re.fullmatch(rb"([0-9a-fA-F]{64})\s+\*?([A-Za-z0-9._-]+)\s*", checksum)
    if not match or match[2].decode() != item["name"] or match[1].decode().lower() != sha(raw):
        raise ValueError("Official archive checksum/name mismatch")
    month = datetime.fromisoformat(item["month"])
    expected_member = item["name"].removesuffix(".zip") + ".csv"
    with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
        if zipped.namelist() != [expected_member]:
            raise ValueError("Unexpected ZIP member/path/count")
        info = zipped.getinfo(expected_member)
        if (info.file_size > MAX_OBJECT or info.flag_bits & 1
                or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK):
            raise ValueError("Unbounded/encrypted/symlinked ZIP member")
        member = zipped.read(info)  # Also checks the ZIP CRC.
    unit = 1000000 if month.year >= 2025 else 1000
    bars, previous, excluded, row_count = [], None, [], 0
    for row in csv.reader(io.StringIO(member.decode("utf-8-sig"))):
        row_count += 1
        if len(row) != 12:
            raise ValueError("Expected 12 spot kline columns; no header/futures substitution")
        opening, closing, trades = int(row[0]), int(row[6]), int(row[8])
        expected_close = opening + SECONDS * unit - 1
        if (opening % (SECONDS * unit) or not opening <= closing <= expected_close
                or trades < 0 or any(money(row[i]) < 0 for i in (7, 9, 10))):
            raise ValueError("Invalid timestamp units/grid, close time or trade quantities")
        at = datetime.fromtimestamp(opening // unit, timezone.utc)
        if not month <= at < next_month(month) or (previous is not None and at <= previous):
            raise ValueError("Out-of-month, duplicate or unsorted kline")
        bar = Bar(item["symbol"], at, SECONDS, *(money(v) for v in row[1:6]))
        bar.validate()
        if money(row[9]) > bar.volume:
            raise ValueError("Taker volume exceeds total volume")
        if closing != expected_close:
            excluded.append({"start": at.isoformat(), "provider_close": closing, "expected_full_close": expected_close,
                             "unit": "microseconds" if unit == 1000000 else "milliseconds",
                             "reason": "shortened_provider_close_not_a_complete_H4_bar", "row_sha256": sha(canonical(row))})
        else:
            bars.append(bar)
        previous = at
    if not bars:
        raise ValueError("Empty monthly archive")
    return bars, {"member": expected_member, "member_sha256": sha(member), "zip_crc32": f"{info.CRC:08x}",
                  "rows": row_count, "accepted_full_bars": len(bars), "excluded_partial_closes": excluded}


def csv_payload(grouped):
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(("timestamp", "symbol", "open", "high", "low", "close", "volume"))
    for symbol in SYMBOLS:
        for bar in grouped[symbol]:
            writer.writerow((bar.start.isoformat(), symbol, bar.open, bar.high, bar.low, bar.close, bar.volume))
    return text.getvalue().encode()


def coverage(grouped):
    quality, by_time = {}, {}
    for symbol in SYMBOLS:
        bars = grouped[symbol]
        if not bars or any(a.start >= b.start for a, b in zip(bars, bars[1:])):
            raise ValueError("Empty/unsorted/duplicate aggregate history")
        gaps = [{"after": a.end.isoformat(), "before": b.start.isoformat(),
                 "missing_bars": int((b.start - a.end).total_seconds()) // SECONDS}
                for a, b in zip(bars, bars[1:]) if a.end != b.start]
        quality[symbol] = {"bars": len(bars), "gaps": gaps}
        by_time[symbol] = {b.start: b for b in bars}
    common = sorted(set.intersection(*(set(by_time[s]) for s in SYMBOLS)))
    runs = []
    for at in common:
        if not runs or at - runs[-1][-1] != timedelta(seconds=SECONDS):
            runs.append([])
        runs[-1].append(at)
    if not runs:
        raise ValueError("No common reference history")
    chosen = max(runs, key=len)  # Earliest wins ties, never price/performance selected.
    return {"quality": quality, "common_runs": len(runs),
            "selection": "longest common continuous run, earliest tie; no filling/bridging gaps",
            "selected_start": chosen[0].isoformat(), "selected_end_exclusive": (chosen[-1] + timedelta(seconds=SECONDS)).isoformat(),
            "selected_bars_per_symbol": len(chosen),
            "omitted_bars": {s: len(grouped[s]) - len(chosen) for s in SYMBOLS}}, \
        {s: [by_time[s][at] for at in chosen] for s in SYMBOLS}


def capture_reference(output, *, client=None, progress=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw").mkdir()
    client = client or ArchiveClient()
    if (client.max_requests, client.max_bytes, client.max_elapsed) != (MAX_REQUESTS, MAX_BYTES, MAX_ELAPSED):
        raise ValueError("Changed reference transport budget")
    grouped, sources, receipts = {s: [] for s in SYMBOLS}, [], []
    # This is an application output, written once before the first HTTP GET.
    write_new(output / "registration.json", {"scope": "NON-PRODUCTION GLOBAL SPOT REFERENCE", "plan": archive_plan(),
              "license": LICENSE, "quality_policy": QUALITY_POLICY, "retry": False, "approved_for_live": False})
    try:
        with (output / "receipts.jsonl").open("x") as journal:
            for number, item in enumerate(archive_plan()):
                bodies = {}
                for suffix, key in ((".CHECKSUM", "checksum"), ("", "zip")):
                    raw, receipt = client.fetch(item["url"] + suffix)
                    filename = f"raw/{item['name']}" + suffix
                    with (output / filename).open("xb") as stream:
                        stream.write(raw)
                    record = {**receipt, "file": filename}
                    journal.write(json.dumps(record, sort_keys=True) + "\n")
                    journal.flush()
                    receipts.append(record)
                    bodies[key] = raw
                bars, stats = decode_archive(bodies["zip"], bodies["checksum"], item)
                grouped[item["symbol"]].extend(bars)
                sources.append({**item, **stats, "archive_sha256": sha(bodies["zip"])})
                if progress and (number + 1) % 12 == 0:
                    progress(f"Public archives {number + 1}/{len(archive_plan())}; bytes {client.bytes}")
        summary, _ = coverage(grouped)
        payload = csv_payload(grouped)
        with (output / "candles.csv").open("xb") as stream:
            stream.write(payload)
        manifest = {"schema": 1, "status": "verified_download", "venue": "binance-global-spot-reference",
                    "source": BASE, "license": LICENSE, "synthetic": False,
                    "quality_policy": QUALITY_POLICY,
                    "timeframe_minutes": 240, "symbols": SYMBOLS, "start": START.isoformat(), "end_exclusive": END.isoformat(),
                    "csv_sha256": sha(payload), "sources": sources, "receipts": receipts,
                    "requests": client.attempts, "bytes_downloaded": client.bytes, **summary,
                    "models_trained": 0, "approved_for_live": False}
        write_new(output / "manifest.json", manifest)
        return manifest
    except Exception as error:
        write_new(output / "failure.json", {"status": "failed", "error_type": type(error).__name__,
                  "http_status": getattr(error, "code", None), "requests_attempted": client.attempts,
                  "successful_receipts": len(receipts), "bytes_downloaded": client.bytes,
                  "retry": False, "approved_for_live": False})
        raise


def audit_reference(directory):
    """Read-only raw/checksum/member/CSV/coverage consistency audit."""
    directory = Path(directory)
    if directory.is_symlink() or any((directory / p).is_symlink() for p in
                                    ("raw", "manifest.json", "registration.json", "receipts.jsonl", "candles.csv")):
        raise ValueError("Symlinked reference directory/control file")
    manifest = json.loads((directory / "manifest.json").read_text())
    registration = json.loads((directory / "registration.json").read_text())
    if (registration.get("plan") != archive_plan() or registration.get("license") != LICENSE
            or registration.get("quality_policy") != QUALITY_POLICY or manifest.get("quality_policy") != QUALITY_POLICY
            or registration.get("retry") is not False or registration.get("approved_for_live") is not False
            or registration.get("scope") != "NON-PRODUCTION GLOBAL SPOT REFERENCE"
            or manifest.get("venue") != "binance-global-spot-reference" or manifest.get("license") != LICENSE
            or manifest.get("synthetic") is not False or manifest.get("approved_for_live") is not False
            or manifest.get("status") != "verified_download" or manifest.get("schema") != 1
            or manifest.get("start") != START.isoformat() or manifest.get("end_exclusive") != END.isoformat()
            or manifest.get("symbols") != list(SYMBOLS) or manifest.get("timeframe_minutes") != 240
            or manifest.get("source") != BASE or manifest.get("models_trained") != 0):
        raise ValueError("Reference scope/registration/license mismatch")
    receipts = [json.loads(line) for line in (directory / "receipts.jsonl").read_text().splitlines()]
    expected = [(x, suffix) for x in archive_plan() for suffix in (".CHECKSUM", "")]
    if receipts != manifest.get("receipts") or len(receipts) != len(expected):
        raise ValueError("Reference receipt journal/count mismatch")
    total = 0
    for record, (item, suffix) in zip(receipts, expected):
        filename = "raw/" + item["name"] + suffix
        if record.get("file") != filename or record.get("url") != item["url"] + suffix:
            raise ValueError("Reference receipt path/public URL mismatch")
        path = directory / filename
        if path.is_symlink():
            raise ValueError("Symlinked reference raw input")
        raw = path.read_bytes()
        if len(raw) != record["bytes"] or sha(raw) != record["sha256"]:
            raise ValueError("Reference raw receipt checksum mismatch")
        total += len(raw)
    grouped, sources = {s: [] for s in SYMBOLS}, []
    for item in archive_plan():
        raw = (directory / "raw" / item["name"]).read_bytes()
        checksum = (directory / "raw" / (item["name"] + ".CHECKSUM")).read_bytes()
        bars, stats = decode_archive(raw, checksum, item)
        grouped[item["symbol"]].extend(bars)
        sources.append({**item, **stats, "archive_sha256": sha(raw)})
    summary, chosen = coverage(grouped)
    payload = csv_payload(grouped)
    if ((directory / "candles.csv").is_symlink() or (directory / "candles.csv").read_bytes() != payload
            or manifest.get("csv_sha256") != sha(payload) or manifest.get("sources") != sources
            or any(manifest.get(k) != v for k, v in summary.items())
            or manifest.get("bytes_downloaded") != total or total > MAX_BYTES
            or manifest.get("requests") != len(expected) or len(expected) > MAX_REQUESTS):
        raise ValueError("Reference export/coverage/budget mismatch")
    return {"status": "verified", "archives": len(sources), "requests_verified": len(receipts),
            "raw_bytes_verified": total, "csv_sha256": sha(payload), **summary,
            "approved_for_live": False}, chosen
