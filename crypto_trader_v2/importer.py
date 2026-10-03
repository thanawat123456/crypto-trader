"""Convert official headerless OHLCVT into a reproducible V2 dataset."""
from __future__ import annotations

from contextlib import ExitStack
import csv
from datetime import datetime, timezone
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import zipfile

from .archive import SUPPORT_URL, SplitHTTPFile, match_member
from .config import Config
from .data import Dataset
from .domain import Bar, money, utc


def parse_ohlcvt(stream, symbol: str, seconds: int, start: datetime | None, end: datetime | None):
    bars = []
    seen = {}
    duplicates = raw_count = 0
    digest = hashlib.sha256()
    previous = None
    for line in stream:
        if isinstance(line, str):
            line = line.encode("utf-8")
        digest.update(line)
        if not line.strip():
            continue
        raw_count += 1
        row = next(csv.reader([line.decode("utf-8-sig")]))
        if len(row) != 7:
            raise ValueError(f"{symbol}: expected 7 OHLCVT columns at row {raw_count}")
        timestamp = int(row[0])
        if timestamp % seconds:
            raise ValueError(f"{symbol}: candle not aligned to the UTC grid")
        if int(row[6]) < 0:
            raise ValueError("Negative trade count")
        at = datetime.fromtimestamp(timestamp, timezone.utc)
        bar = Bar(symbol, at, seconds, *(money(v) for v in row[1:6]))
        bar.validate()
        if previous is not None and at < previous:
            raise ValueError(f"{symbol}: unsorted source rows")
        previous = at
        if (start is not None and at < start) or (end is not None and bar.end > end):
            continue
        if at in seen:
            if seen[at] != bar:
                raise ValueError(f"{symbol}: conflicting duplicate candle")
            duplicates += 1
            continue
        seen[at] = bar
        bars.append(bar)
    if not bars:
        raise ValueError(f"{symbol}: no complete candles in requested range")
    return bars, {"raw_rows": raw_count, "selected_rows": len(bars), "identical_duplicates_removed": duplicates,
                  "member_sha256": digest.hexdigest()}


def import_kraken(cfg: Config, output: Path, *, archive: Path | None = None,
                  files: dict[str, Path] | None = None, official=False,
                  start: datetime | None = None, end: datetime | None = None,
                  byte_budget=128 * 1024 * 1024, repair_minutes: int | None = None) -> dict:
    if sum((archive is not None, files is not None, official)) != 1:
        raise ValueError("Select exactly one official archive, local ZIP or CSV mapping")
    cfg.validate()
    if cfg.venue != "kraken":
        raise ValueError("Kraken import requires a Kraken configuration; never relabel another venue's history")
    if output.exists():
        raise ValueError("Dataset directory exists; use a new output directory")
    start, end = utc(start) if start is not None else None, utc(end) if end is not None else None
    if start and end and start >= end:
        raise ValueError("start must precede end")
    if repair_minutes is not None and (repair_minutes <= 0 or repair_minutes >= cfg.timeframe_minutes or cfg.timeframe_minutes % repair_minutes or files is not None):
        raise ValueError("Gap repair requires a lower dividing timeframe in the same ZIP")
    selected, sources = {}, {}
    repairs = {}
    transport = {"integrity": "Selected-source SHA256 and validation; source provenance supplied by operator"}
    with ExitStack() as stack:
        if official:
            remote = stack.enter_context(SplitHTTPFile(budget=byte_budget))
            zipped = stack.enter_context(zipfile.ZipFile(remote))
        elif archive is not None:
            zipped = stack.enter_context(zipfile.ZipFile(archive))
        else:
            zipped = None
            if set(files) != {i.symbol for i in cfg.instruments}:
                raise ValueError("CSV mapping must match configured instruments exactly")
        for instrument in cfg.instruments:
            symbol = instrument.symbol
            if zipped is not None:
                member = match_member(zipped.namelist(), symbol, cfg.timeframe_minutes)
                info = zipped.getinfo(member)
                if info.file_size > 512 * 1024 * 1024:
                    raise ValueError("Selected member exceeds the uncompressed-size limit")
                stream = stack.enter_context(zipped.open(member))
                provenance = {"archive": str(archive) if archive else "Kraken official split archive 2026Q2",
                              "member": member, "zip_crc32": f"{info.CRC:08x}"}
            else:
                stream = stack.enter_context(files[symbol].open("rb"))
                provenance = {"path": str(files[symbol].resolve())}
            bars, stats = parse_ohlcvt(stream, symbol, cfg.seconds, start, end)
            if repair_minutes is not None:
                lower_member = match_member(zipped.namelist(), symbol, repair_minutes)
                lower_info = zipped.getinfo(lower_member)
                if lower_info.file_size > 512 * 1024 * 1024:
                    raise ValueError("Lower-timeframe member exceeds the uncompressed-size limit")
                lower_stream = stack.enter_context(zipped.open(lower_member))
                lower, lower_stats = parse_ohlcvt(lower_stream, symbol, repair_minutes * 60, start, end)
                bars, repaired = repair_gaps(bars, lower, cfg.seconds)
                repairs[symbol] = repaired
                provenance["repair_source"] = {"member": lower_member, "zip_crc32": f"{lower_info.CRC:08x}", **lower_stats}
            selected[symbol], sources[symbol] = bars, {**provenance, **stats}
        if official:
            transport = remote.manifest()
    common_start = max(bars[0].start for bars in selected.values())
    common_end = min(bars[-1].end for bars in selected.values())
    if common_start >= common_end:
        raise ValueError("No overlapping source coverage")
    selected = {s: [b for b in bars if common_start <= b.start and b.end <= common_end] for s, bars in selected.items()}
    quality = {}
    for symbol, bars in selected.items():
        gaps = [{"after": a.end.isoformat(), "before": b.start.isoformat(), "missing_candles": int((b.start - a.end).total_seconds()) // cfg.seconds}
                for a, b in zip(bars, bars[1:]) if a.end != b.start]
        quality[symbol] = {"bars": len(bars), "gap_count": len(gaps), "missing_candles": sum(g["missing_candles"] for g in gaps), "gaps": gaps}
    timelines = [{b.start for b in bars} for bars in selected.values()]
    synchronized = all(t == timelines[0] for t in timelines)
    replay_ready = synchronized and all(q["gap_count"] == 0 for q in quality.values()) and all(len(bars) > cfg.warmup for bars in selected.values())
    # Even a failed-quality import is useful: keep raw gaps visible and report
    # them. The CSV loader and research runner will refuse to trade across them.
    output.mkdir(parents=True)
    destination = output / "candles.csv"
    with destination.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "symbol", "open", "high", "low", "close", "volume"))
        for symbol in sorted(selected):
            for b in selected[symbol]:
                writer.writerow((b.start.isoformat(), symbol, b.open, b.high, b.low, b.close, b.volume))
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest = {"schema": 1, "venue": cfg.venue, "timeframe_minutes": cfg.timeframe_minutes,
                "symbols": sorted(selected), "source": SUPPORT_URL if official else "Operator-supplied Kraken-format files; provenance not independently authenticated",
                "synthetic": False, "start": common_start.isoformat(), "end": common_end.isoformat(),
                "requested_start": start.isoformat() if start else None, "requested_end": end.isoformat() if end else None,
                "csv_sha256": checksum, "sources": sources, "transport": transport,
                "quality": quality, "synchronized": synchronized, "replay_ready": replay_ready,
                "repairs": repairs, "gap_policy": "Reject unrepaired gaps; reconstruct missing bars only from complete observed lower-timeframe bars", "dataset_file": "candles.csv"}
    with (output / "manifest.json").open("x") as stream:
        json.dump(manifest, stream, indent=2)
    return manifest


def repair_gaps(bars: list[Bar], lower: list[Bar], seconds: int) -> tuple[list[Bar], list[dict]]:
    if not bars or not lower or seconds % lower[0].seconds:
        raise ValueError("Invalid reconstruction timeframes")
    by_time = {b.start: b for b in lower}
    result = {b.start: b for b in bars}
    repaired = []
    cursor = bars[0].start
    while cursor <= bars[-1].start:
        if cursor not in result:
            steps = [by_time.get(cursor + timedelta(seconds=i * lower[0].seconds)) for i in range(seconds // lower[0].seconds)]
            if all(b is not None and b.seconds == lower[0].seconds and b.symbol == bars[0].symbol for b in steps):
                result[cursor] = Bar(bars[0].symbol, cursor, seconds, steps[0].open, max(b.high for b in steps),
                                     min(b.low for b in steps), steps[-1].close, sum((b.volume for b in steps), money(0)))
                repaired.append({"at": cursor.isoformat(), "observed_subbars": len(steps), "subbar_minutes": lower[0].seconds // 60})
        cursor += timedelta(seconds=seconds)
    return [result[t] for t in sorted(result)], repaired


def load_dataset(directory: Path, cfg: Config) -> Dataset:
    from .data import read_csv

    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != 1 or manifest.get("venue") != cfg.venue or manifest.get("timeframe_minutes") != cfg.timeframe_minutes:
        raise ValueError("Dataset metadata/config mismatch")
    if manifest.get("symbols") != sorted(i.symbol for i in cfg.instruments):
        raise ValueError("Dataset instrument mismatch")
    if manifest.get("dataset_file") != "candles.csv":
        raise ValueError("Unexpected dataset file")
    if not manifest.get("replay_ready"):
        raise ValueError("Dataset quality gate failed; inspect manifest gaps/coverage")
    csv_path = directory / "candles.csv"
    dataset = read_csv(csv_path, cfg, manifest["source"])
    if dataset.checksum != manifest["csv_sha256"]:
        raise ValueError("Dataset checksum mismatch")
    return Dataset(dataset.bars, dataset.source, dataset.checksum, bool(manifest.get("synthetic")))


def read_imported_source(directory: Path, cfg: Config):
    """Verify imported rows without requiring continuity or computing returns.

    A failed replay-quality flag never bypasses integrity checks. Consumers must
    explicitly select a continuous interval before indicators/labels/replay.
    """
    cfg.validate()
    parent = json.loads((directory / "manifest.json").read_text())
    if (parent.get("schema") != 1 or parent.get("venue") != cfg.venue or
            parent.get("timeframe_minutes") != cfg.timeframe_minutes or
            parent.get("symbols") != sorted(i.symbol for i in cfg.instruments) or
            parent.get("dataset_file") != "candles.csv"):
        raise ValueError("Dataset metadata/config mismatch")
    path = directory / "candles.csv"
    if hashlib.sha256(path.read_bytes()).hexdigest() != parent.get("csv_sha256"):
        raise ValueError("Dataset checksum mismatch")
    grouped = {i.symbol: {} for i in cfg.instruments}
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "symbol", "open", "high", "low", "close", "volume"}
        if set(reader.fieldnames or []) != required:
            raise ValueError("Unexpected imported CSV columns")
        for row in reader:
            symbol, at = row["symbol"], utc(row["timestamp"])
            if symbol not in grouped or at in grouped[symbol] or int(at.timestamp()) % cfg.seconds or at.microsecond:
                raise ValueError("Unexpected symbol, duplicate or misaligned candle")
            if grouped[symbol] and at <= next(reversed(grouped[symbol])):
                raise ValueError("Unsorted imported candle")
            bar = Bar(symbol, at, cfg.seconds, *(money(row[k]) for k in ("open", "high", "low", "close", "volume")))
            bar.validate()
            grouped[symbol][at] = bar
    if any(not bars for bars in grouped.values()):
        raise ValueError("Missing configured instrument candles")
    start, end = utc(parent["start"]), utc(parent["end"])
    if (start >= end or min(min(bars) for bars in grouped.values()) != start
            or max(max(bars) for bars in grouped.values()) + timedelta(seconds=cfg.seconds) != end
            or any(b.start < start or b.end > end for bars in grouped.values() for b in bars.values())):
        raise ValueError("Dataset declared coverage disagrees with candles")
    return parent, grouped


def common_contiguous_runs(grouped, seconds):
    common = sorted(set.intersection(*(set(bars) for bars in grouped.values())))
    runs = []
    for at in common:
        if not runs or at != runs[-1][-1] + timedelta(seconds=seconds):
            runs.append([])
        runs[-1].append(at)
    return runs


def slice_contiguous(directory: Path, cfg: Config, output: Path) -> dict:
    """Explicitly select the longest complete common run, never fill gaps.

    Retains parent provenance and lists all omitted periods. This changes the
    research question: results apply only to the selected period, not the whole
    requested archive. Input can have a failed quality gate but must be intact.
    """
    if output.exists():
        raise ValueError("Dataset directory exists; use a new output directory")
    parent, grouped = read_imported_source(directory, cfg)
    runs = common_contiguous_runs(grouped, cfg.seconds)
    if not runs:
        raise ValueError("No common complete candles")
    # Earliest wins ties. No price/return-based selection is involved.
    selected = max(runs, key=len)
    if len(selected) <= cfg.warmup:
        raise ValueError("Continuous window lacks sufficient warmup")
    output.mkdir(parents=True)
    destination = output / "candles.csv"
    with destination.open("x", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("timestamp", "symbol", "open", "high", "low", "close", "volume"))
        for symbol in sorted(grouped):
            for at in selected:
                b = grouped[symbol][at]
                writer.writerow((at.isoformat(), symbol, b.open, b.high, b.low, b.close, b.volume))
    manifest = {**parent, "start": selected[0].isoformat(),
                "end": (selected[-1] + timedelta(seconds=cfg.seconds)).isoformat(),
                "csv_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "synchronized": True, "replay_ready": True,
                "quality": {s: {"bars": len(selected), "gap_count": 0, "missing_candles": 0, "gaps": []} for s in grouped},
                "subset": {"policy": "Longest common contiguous window, earliest on ties; no performance-based selection",
                           "parent_directory": str(directory.resolve()), "parent_csv_sha256": parent["csv_sha256"],
                           "parent_start": parent["start"], "parent_end": parent["end"],
                           "parent_quality": parent["quality"],
                           "omitted_bars_per_symbol": {s: len(bars) - len(selected) for s, bars in grouped.items()},
                           "common_windows": [{"start": run[0].isoformat(), "end": (run[-1] + timedelta(seconds=cfg.seconds)).isoformat(), "bars": len(run), "selected": run is selected} for run in runs],
                           "limitation": "Not a full-period replay; missing and later regimes are excluded. Do not extrapolate the result to the entire archive."}}
    with (output / "manifest.json").open("x") as stream:
        json.dump(manifest, stream, indent=2)
    return manifest
