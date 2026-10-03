from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from crypto_trader_v2.archive import SplitHTTPFile, match_member
from crypto_trader_v2.config import Config, StrategyConfig
from crypto_trader_v2.data import Dataset, demo_dataset
from crypto_trader_v2.domain import Bar
from crypto_trader_v2.importer import import_kraken, load_dataset, parse_ohlcvt, repair_gaps, slice_contiguous
from crypto_trader_v2.research import run_backtest
from crypto_trader_v2.strategy import BreakoutState, evaluate
from crypto_trader_v2.study import add_months, block_bootstrap, run_study, select_candidate


def config():
    default = Config()
    return replace(default, instruments=(default.instruments[0],), strategy=StrategyConfig(
        trend_period=3, slope_bars=1, breakout_bars=3, exit_bars=2, atr_period=2))


def rows(cfg, count=80, missing=()):
    start = int(datetime(2022, 1, 1, tzinfo=timezone.utc).timestamp())
    return "".join(f"{start + i * cfg.seconds},100,102,99,101,1,2\n" for i in range(count) if i not in missing).encode()


class Response(io.BytesIO):
    def __init__(self, payload=b"", *, status=206, headers=None):
        super().__init__(payload)
        self.status, self.headers = status, headers or {}


class RangeTests(unittest.TestCase):
    def reader(self, *, status=206, etag='"stable"', budget=128):
        urls = [f"https://assets.kraken.com/marketing/institutions/part{i}" for i in range(2)]
        bodies = dict(zip(urls, (b"abcdef", b"ghijkl")))

        def opener(req, timeout):
            body = bodies[req.full_url]
            if req.get_method() == "HEAD":
                return Response(headers={"Content-Length": str(len(body)), "ETag": '"stable"'})
            lo, hi = map(int, req.get_header("Range")[6:].split("-"))
            return Response(body[lo:hi + 1], status=status,
                            headers={"Content-Range": f"bytes {lo}-{hi}/{len(body)}", "ETag": etag})

        return SplitHTTPFile(urls, opener=opener, budget=budget)

    def test_seek_cross_part_and_cache(self):
        with self.reader() as stream:
            stream.seek(4)
            self.assertEqual(stream.read(5), b"efghi")
            self.assertEqual(stream.downloaded, 5)
            stream.seek(4)
            self.assertEqual(stream.read(5), b"efghi")
            self.assertEqual(stream.downloaded, 5)
            stream.seek(-2, io.SEEK_END)
            self.assertEqual(stream.read(), b"kl")
            self.assertEqual(stream.read(), b"")

    def test_full_response_rejected_before_read(self):
        with self.reader(status=200) as stream:
            with self.assertRaisesRegex(ValueError, "full archive download refused"):
                stream.read(1)
            self.assertEqual(stream.downloaded, 0)

    def test_mutation_and_budget_fail_closed(self):
        with self.reader(etag='"changed"') as stream:
            with self.assertRaisesRegex(ValueError, "changed"):
                stream.read(1)
        with self.reader(budget=3) as stream:
            stream.read(3)
            with self.assertRaisesRegex(ValueError, "byte budget"):
                stream.read(1)

    def test_url_and_member_allowlist(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            SplitHTTPFile(["https://example.com/file"])
        self.assertEqual(match_member(["folder/XBTUSD_240.csv"], "BTC/USD", 240), "folder/XBTUSD_240.csv")
        for members in ([], ["XBTUSD_240.csv", "folder/XBTUSD_240.csv"]):
            with self.assertRaises(ValueError):
                match_member(members, "BTC/USD", 240)


class ImportTests(unittest.TestCase):
    def test_duplicates_conflicts_and_invalid_rows(self):
        cfg = config()
        first = rows(cfg, 1)
        bars, stats = parse_ohlcvt(io.BytesIO(first * 2), "BTC/USD", cfg.seconds, None, None)
        self.assertEqual(len(bars), 1)
        self.assertEqual(stats["identical_duplicates_removed"], 1)
        for bad in (first + first.replace(b",101,", b",100,"), first.replace(b",1,2", b",-1,2"),
                    first.replace(b",100,102,", b",100,98,"), b"1640995201,100,102,99,101,1,2\n"):
            with self.assertRaises(ValueError):
                parse_ohlcvt(io.BytesIO(bad), "BTC/USD", cfg.seconds, None, None)

    def test_zip_import_checksum_and_no_overwrite(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "source.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("XBTUSD_240.csv", rows(cfg))
            out = root / "dataset"
            manifest = import_kraken(cfg, out, archive=archive)
            self.assertTrue(manifest["replay_ready"])
            self.assertEqual(len(load_dataset(out, cfg).bars["BTC/USD"]), 80)
            with self.assertRaisesRegex(ValueError, "exists"):
                import_kraken(cfg, out, archive=archive)
            with (out / "candles.csv").open("a") as stream:
                stream.write("\n")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_dataset(out, cfg)

    def test_gaps_block_replay_and_subset_is_explicit(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.csv"
            source.write_bytes(rows(cfg, count=80, missing=(55,)))
            out = root / "dataset"
            manifest = import_kraken(cfg, out, files={"BTC/USD": source})
            self.assertFalse(manifest["replay_ready"])
            self.assertEqual(manifest["quality"]["BTC/USD"]["missing_candles"], 1)
            with self.assertRaisesRegex(ValueError, "quality gate"):
                load_dataset(out, cfg)
            sliced = slice_contiguous(out, cfg, root / "subset")
            self.assertEqual(sliced["quality"]["BTC/USD"]["bars"], 55)
            self.assertEqual(sliced["subset"]["omitted_bars_per_symbol"]["BTC/USD"], 24)
            self.assertEqual(sliced["subset"]["parent_csv_sha256"], manifest["csv_sha256"])
            load_dataset(root / "subset", cfg)

    def test_repairs_require_every_observed_subbar(self):
        cfg = config()
        high, _ = parse_ohlcvt(io.BytesIO(rows(cfg, 3, missing=(1,))), "BTC/USD", cfg.seconds, None, None)
        low, _ = parse_ohlcvt(io.BytesIO(rows(replace(cfg, timeframe_minutes=60), 12)), "BTC/USD", 3600, None, None)
        rebuilt, repairs = repair_gaps(high, low, cfg.seconds)
        self.assertEqual(len(rebuilt), 3)
        self.assertEqual(rebuilt[1].volume, D(4))
        self.assertEqual(repairs[0]["observed_subbars"], 4)
        rebuilt, repairs = repair_gaps(high, low[:5] + low[6:], cfg.seconds)
        self.assertEqual(len(rebuilt), 2)
        self.assertFalse(repairs)


class ResearchTests(unittest.TestCase):
    def test_incremental_indicators_match_independent_reference(self):
        cfg = config()
        bars = demo_dataset(cfg, 300).bars["BTC/USD"]
        moving = volatility = previous = None
        emas = []
        state = BreakoutState(cfg)
        s = cfg.strategy
        for i, bar in enumerate(bars):
            moving = bar.close if moving is None else moving + D(2) / (s.trend_period + 1) * (bar.close - moving)
            tr = max(bar.high - bar.low, abs(bar.high - (previous or bar.close)), abs(bar.low - (previous or bar.close)))
            volatility = tr if volatility is None else volatility + (tr - volatility) / s.atr_period
            previous = bar.close
            emas.append(moving)
            signal = state.update(bar)
            if i + 1 >= cfg.warmup:
                prior = max(b.high for b in bars[i - s.breakout_bars:i])
                earlier = max(b.high for b in bars[i - s.breakout_bars - 1:i - 1])
                self.assertEqual(signal.enter, bar.close > moving and moving > emas[i - s.slope_bars]
                                 and bar.close > prior and bars[i - 1].close <= earlier and volatility > 0)
                self.assertEqual(signal.exit, bar.close < min(b.low for b in bars[i - s.exit_bars:i]))
                self.assertEqual(signal.atr, volatility)
        self.assertEqual(signal, evaluate(bars, cfg))

    def test_replay_window_excludes_warmup_orders(self):
        cfg = config()
        dataset = demo_dataset(cfg, 300)
        result = run_backtest(dataset, cfg, start_index=200, end_index=250)
        self.assertEqual(result["start"], dataset.bars["BTC/USD"][200].start.isoformat())
        self.assertEqual(result["end"], dataset.bars["BTC/USD"][249].end.isoformat())
        self.assertEqual(result["bars_per_symbol"], 50)

    def test_bootstrap_no_episodes_is_not_profitable(self):
        cfg = replace(config(), timeframe_minutes=1440)
        dataset = demo_dataset(cfg, 300)
        flat = {s: [replace(b, open=D(100), high=D(100), low=D(100), close=D(100)) for b in bars]
                for s, bars in dataset.bars.items()}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "flat.sqlite"
            run_backtest(Dataset(flat, "fixture", "fixture", True), cfg, path)
            result = block_bootstrap(path, runs=100)
            for item in result["sensitivity"]:
                self.assertIsNone(item["expectancy_lower_95ci"])
                self.assertEqual(item["no_episode_samples"], 100)

    def test_registration_and_selection_precede_holdout(self):
        cfg = replace(config(), timeframe_minutes=1440)
        fixture = demo_dataset(cfg, 1100)
        dataset = replace(fixture, synthetic=False, source="UNIT TEST ONLY: mock calls")
        base = {"net_return_pct": D(0), "max_drawdown_pct": D(0), "closed_episodes": 0,
                "profit_factor": None, "net_expectancy_quote": None}
        calls = []
        with TemporaryDirectory() as folder:
            output = Path(folder) / "experiment"

            def fake_replay(data, trial, database=None, **kwargs):
                plan = json.loads((output / "registration.json").read_text())
                split_time = datetime.fromisoformat(plan["holdout_start"])
                if database is None:
                    self.assertLess(data.bars["BTC/USD"][-1].start, split_time)
                elif "holdout" in database.name:
                    self.assertTrue((output / "selection.json").exists())
                    self.assertEqual(kwargs["start_index"], plan["holdout_start_index"])
                calls.append(database)
                return base.copy()

            with patch("crypto_trader_v2.study.run_backtest", side_effect=fake_replay), patch("crypto_trader_v2.study.block_bootstrap", return_value={}):
                result = run_study(dataset, cfg, output, bootstrap_runs=100)
            self.assertEqual(sum(p is not None and p.name == "holdout.sqlite" for p in calls), 1)
            self.assertFalse(result["gates"]["approved_for_live"])
            self.assertEqual(result["selection"]["selected_index"], 0)
            with self.assertRaisesRegex(ValueError, "exists"):
                run_study(dataset, cfg, output)

    def test_reject_synthetic_and_bad_bootstrap_before_output(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            output = Path(folder) / "experiment"
            with self.assertRaisesRegex(ValueError, "non-synthetic"):
                run_study(demo_dataset(cfg), cfg, output)
            self.assertFalse(output.exists())
            with self.assertRaisesRegex(ValueError, "at least 100"):
                run_study(demo_dataset(cfg), cfg, output, bootstrap_runs=1)
            self.assertFalse(output.exists())

    def test_selection_ties_and_calendar_boundaries(self):
        self.assertEqual(select_candidate([{"net_return_pct": D(1), "max_drawdown_pct": D(2)},
                                           {"net_return_pct": D(1), "max_drawdown_pct": D(1)}]), 1)
        self.assertEqual(add_months(datetime(2024, 1, 31), 1), datetime(2024, 2, 29))


if __name__ == "__main__":
    unittest.main()
