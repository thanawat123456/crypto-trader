from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import fcntl
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.broker import PaperBroker
from crypto_trader_v2.config import CostConfig, StrategyConfig, load_config
from crypto_trader_v2.domain import Mode, Quote, ZERO
from crypto_trader_v2.dust_audit import verify_dust_ledger
from crypto_trader_v2.engine import Coordinator
from crypto_trader_v2.native_execution import AverageBasis, NativePaperBroker, NativeRules, PROFILE
from crypto_trader_v2.shadow import (ShadowFeed, canonical, observe_shadow, register_shadow, verify_shadow)
from crypto_trader_v2.storage import Store


NOW = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)


def config():
    cfg = load_config("config.binance-th.paper.yaml")
    return replace(cfg, strategy=StrategyConfig(3, 1, 3, 2, 2),
                   costs=replace(cfg.costs, slippage=ZERO, simulated_spread=ZERO))


def readonly_file_snapshot(output):
    """Bind every byte except documented SQLite reader-coordination marks.

    SQLite WAL readers can update shm bytes 100..119 even with mode=ro:
    https://www.sqlite.org/walformat.html#wal_locks
    Keep DB/WAL/raw bytes, file set and ALL other shm/header/index bytes bound.
    Only the two root ledger sidecars qualify, not arbitrary *-shm filenames.
    """
    result = {}
    for path in output.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            if path.parent == output and path.name in {"cash.sqlite-shm", "baseline.sqlite-shm"}:
                if len(raw) < 136:
                    raise ValueError("Truncated SQLite shared-memory header")
                raw = raw[:100] + bytes(20) + raw[120:]
            result[path.relative_to(output).as_posix()] = hashlib.sha256(raw).hexdigest()
    return result


def row(symbol="BTCUSDT", *, percent=True):
    result = {"symbol": symbol, "status": "TRADING", "type": "GLOBAL", "baseAsset": symbol[:-4],
              "quoteAsset": "USDT", "orderTypes": ["LIMIT", "MARKET"], "baseCommissionPrecision": 8,
              "quoteCommissionPrecision": 2, "filters": [
                  {"filterType":"LOT_SIZE", "minQty":"0.00001", "maxQty":"9000", "stepSize":"0.00001"},
                  {"filterType":"PRICE_FILTER", "minPrice":"0.01", "maxPrice":"1000000", "tickSize":"0.01"},
                  {"filterType":"MIN_NOTIONAL", "minNotional":"5", "applyToMarket":True, "avgPriceMins":5}]}
    if percent:
        result["filters"].append({"filterType":"PERCENT_PRICE", "avgPriceMins":5, "multiplierDown":"0.2", "multiplierUp":"5"})
    return result


class RulesTests(unittest.TestCase):
    def test_ticks_round_adversely_and_fee_ceiling_is_explicit_assumption(self):
        rule = NativeRules(row())
        self.assertEqual(rule.price(D("100.005"), "buy"), D("100.01"))
        self.assertEqual(rule.price(D("100.005"), "sell"), D("100.00"))
        self.assertEqual(rule.fee(D("0.05"), D("100"), D("0.001"), "sell"), D("0.01"))
        self.assertEqual(rule.fee(D("0.050001"), D("100"), D("0.001"), "buy"), D("0.00005001"))
        self.assertIn("UNVERIFIED", rule.payload()["fee_rounding"])
        self.assertFalse(rule.payload()["average_basis_verified"])

    def test_reference_price_does_not_bypass_missing_filter_average(self):
        rule = NativeRules(row())
        self.assertEqual(rule.reject(D("0.1"), D("100"), "buy", NOW), "filter_average_basis_unverified")
        self.assertEqual(NativeRules(row(), average=AverageBasis(D("100"), 24*60, NOW, "24h fixture")).reject(D("0.1"), D("100"), "buy", NOW), "filter_average_basis_unverified")
        basis = AverageBasis(D("100"), 5, NOW, "software fixture, NEVER real execution evidence")
        rule = NativeRules(row(), average=basis)
        self.assertEqual(rule.reject(D("0.1"), D("100"), "buy", NOW), "")
        self.assertEqual(rule.reject(D("0.1"), D("600"), "buy", NOW), "native_percent_price_rejected")
        self.assertEqual(rule.reject(D("0.1"), D("100"), "buy", NOW+timedelta(seconds=31)), "filter_average_basis_stale_or_invalid")

    def test_lot_price_notional_max_and_offset_rules(self):
        rule = NativeRules(row(percent=False))
        self.assertEqual(rule.reject(D("0.05001"), D("100"), "buy", NOW), "")
        self.assertEqual(rule.reject(D("0.050015"), D("100"), "buy", NOW), "native_lot_size_rejected")
        self.assertEqual(rule.reject(D("0.05"), D("100.005"), "buy", NOW), "native_price_filter_rejected")
        self.assertEqual(rule.reject(D("0.04"), D("100"), "buy", NOW), "native_limit_notional_rejected")
        self.assertEqual(rule.reject(D("9001"), D("100"), "buy", NOW), "native_lot_size_rejected")
        altered = row(percent=False)
        altered["filters"][0]["minQty"] = "0.000005"
        self.assertEqual(NativeRules(altered).reject(D("0.050005"), D("100"), "buy", NOW), "")
        self.assertEqual(NativeRules(altered).reject(D("0.05"), D("100"), "buy", NOW), "native_lot_size_rejected")

    def test_precision_unknown_filters_and_bad_percent_bounds_fail_closed(self):
        for mutation in ("precision", "unknown", "percent", "type"):
            value = row()
            if mutation == "precision": value["quoteCommissionPrecision"] = True
            elif mutation == "unknown": value["filters"].append({"filterType":"NEW_FILTER"})
            elif mutation == "percent": value["filters"][-1]["multiplierDown"] = "6"
            else: value["type"] = "SITE"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                NativeRules(value)

    def test_rounded_fill_fee_inventory_fifo_and_profile_resume(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            path = Path(folder)/"case.sqlite"
            rules = {i.symbol: NativeRules(row(i.symbol.replace("/", ""), percent=False)) for i in cfg.instruments}
            with Store(path, cfg, Mode.PAPER, create=True, execution_profile=PROFILE) as store:
                coordinator = Coordinator(store, cfg)
                coordinator.broker = NativePaperBroker(cfg, rules)
                store.intent("buy", "BTC/USDT", "buy", D("0.05001"), NOW, "fixture", cash_reserved=D("5.001"),
                             risk_reserved=D("0.1"), price_limit=D("100"), stop=D("95"), atr_distance=D("5"))
                coordinator.broker.execute(store, "buy", Quote("BTC/USDT", NOW, D("100"), D("100")))
                coordinator.sell("BTC/USDT", Quote("BTC/USDT", NOW+timedelta(seconds=1), D("110"), D("110")), "fixture")
                sell = store.db.execute("SELECT f.* FROM fills f JOIN orders o ON o.id=f.order_id WHERE o.side='sell'").fetchone()
                self.assertEqual(D(sell["fee"]), D("0.01"))
                self.assertEqual(sell["fee_asset"], "USDT")
                self.assertEqual(store.positions(), {})
                self.assertEqual(len(store.dust_lots()), 1)
                store.mark_nav(NOW+timedelta(seconds=1), {"BTC/USDT":D("110")})
                store.assert_balanced()
            self.assertEqual(verify_dust_ledger(path, cfg)["execution_profile"], PROFILE)
            with self.assertRaisesRegex(ValueError, "execution profile mismatch"):
                Store(path, cfg, Mode.PAPER)
            with Store(path, cfg, Mode.PAPER, execution_profile=PROFILE) as store:
                self.assertEqual(store.get_meta("execution_profile"), PROFILE)

    def test_inconclusive_average_cancels_intent_without_fake_fill(self):
        cfg = config()
        with TemporaryDirectory() as folder:
            with Store(Path(folder)/"ledger.sqlite", cfg, Mode.PAPER, create=True, execution_profile=PROFILE) as store:
                broker = NativePaperBroker(cfg, {i.symbol: NativeRules(row(i.symbol.replace("/", ""))) for i in cfg.instruments})
                store.intent("buy", "BTC/USDT", "buy", D("0.1"), NOW, "fixture", cash_reserved=D("10"),
                             risk_reserved=D("0.1"), price_limit=D("100"), stop=D("95"), atr_distance=D("5"))
                broker.execute(store, "buy", Quote("BTC/USDT", NOW, D("100"), D("100")))
                self.assertEqual(store.order("buy")["status"], "CANCELED")
                self.assertEqual(store.cash, D("300"))
                self.assertEqual(store.positions(), {})
                self.assertEqual(store.db.execute("SELECT outcome FROM execution_events").fetchone()[0], "filter_average_basis_unverified")

    def test_simulator_profile_mismatch_is_rejected_before_any_mutation(self):
        cfg = config()
        rules = {i.symbol: NativeRules(row(i.symbol.replace("/", ""))) for i in cfg.instruments}
        with TemporaryDirectory() as folder:
            for profile, broker in (("", NativePaperBroker(cfg, rules)), (PROFILE, PaperBroker(cfg))):
                with Store(Path(folder)/(profile or "default"), cfg, Mode.PAPER, create=True, execution_profile=profile) as store:
                    before = list(store.db.iterdump())
                    with self.assertRaisesRegex(ValueError, "execution profile mismatch"):
                        broker.execute(store, "nonexistent", Quote("BTC/USDT", NOW, D("100"), D("100")))
                    self.assertEqual(list(store.db.iterdump()), before)

    def test_partial_match_with_fee_ceiling_above_proceeds_is_not_fake_sale(self):
        cfg = config()
        rules = {i.symbol:NativeRules(row(i.symbol.replace("/", ""),percent=False)) for i in cfg.instruments}
        with TemporaryDirectory() as folder:
            with Store(Path(folder)/"case",cfg,Mode.PAPER,create=True,execution_profile=PROFILE) as store:
                broker = NativePaperBroker(cfg,rules)
                store.intent("buy","BTC/USDT","buy",D("1"),NOW,"fixture",cash_reserved=D("100"),risk_reserved=D("0.1"),price_limit=D("100"),stop=D("95"),atr_distance=D("5"))
                broker.execute(store,"buy",Quote("BTC/USDT",NOW,D("100"),D("100")))
                later = NOW+timedelta(seconds=1)
                store.intent("sell","BTC/USDT","sell",D("0.99"),later,"fixture",price_limit=D("100"))
                broker.observed_capacity = {("BTC/USDT","sell"):(later,D("0.00001"))}
                cash, qty = store.cash,store.positions()["BTC/USDT"]["quantity"]
                broker.execute(store,"sell",Quote("BTC/USDT",later,D("100"),D("100")))
                self.assertEqual(store.cash,cash)
                self.assertEqual(store.positions()["BTC/USDT"]["quantity"],qty)
                self.assertEqual(store.db.execute("SELECT outcome FROM execution_events WHERE order_id='sell'").fetchone()[0],"fee_exceeds_received_asset_no_fill")
                store.assert_balanced()


class Response:
    def __init__(self, url, value):
        self.url, self.raw = url, json.dumps(value).encode()
    def geturl(self): return self.url
    def read(self, n): return self.raw[:n]
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Opener:
    def __init__(self, cfg, clock, output):
        self.cfg, self.clock, self.output, self.calls, self.fail = cfg, clock, output, [], False
        self.percent = True
    def open(self, request, timeout):
        assert (self.output/"registration.json").exists()
        assert list((self.output/"attempts").glob("*/reservation.json")), "No reserved slot before network"
        self.calls.append(request)
        if self.fail:
            raise HTTPError(request.full_url, 429, "fixture", {"Retry-After":"60"}, None)
        url, at = urlsplit(request.full_url), self.clock()
        params = parse_qs(url.query)
        if url.path.endswith("exchangeInfo"):
            value = {"symbols":[row(i.symbol.replace("/", ""),percent=self.percent) for i in self.cfg.instruments]}
        elif url.path.endswith("time"):
            value = {"serverTime":int(at.timestamp())*1000}
        elif url.path.endswith("klines"):
            beginning = int(params["startTime"][0])
            value = [[beginning+j*self.cfg.seconds*1000,"100","100","100","100","1",beginning+(j+1)*self.cfg.seconds*1000-1]
                     for j in range(int(params["limit"][0]))]
        elif url.path.endswith("ticker/bookTicker"):
            value = {"symbol":params["symbol"][0],"bidPrice":"100","askPrice":"100","bidQty":"1","askQty":"1"}
        elif url.path.endswith("referencePrice"):
            value = {"symbol":params["symbol"][0],"referencePrice":"100","timestamp":int(at.timestamp())*1000}
        else:
            raise AssertionError("Unexpected/private endpoint")
        return Response(request.full_url, value)


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg, self.at = config(), NOW
        self.root = Path(self.temp.name)
        self.history, self.output = self.root/"history", self.root/"shadow"
        self.history.mkdir()
        (self.history/"manifest.json").write_text(json.dumps({"end":(NOW-timedelta(hours=1)).isoformat()}))
        self.parent = patch("crypto_trader_v2.shadow.verify_native_history", return_value={"status":"verified", "csv_sha256":"full", "development_csv_sha256":"dev", "source_scope":"software fixture reference, not evidence"}).start()
        patch("crypto_trader_v2.binance_th.time.sleep").start()
        self.addCleanup(patch.stopall)
        self.opener = Opener(self.cfg, lambda:self.at, self.output)
        self.feed = ShadowFeed(self.cfg.warmup+2, opener=self.opener, clock=lambda:self.at)
        register_shadow(self.history, self.cfg, self.output, now=NOW)

    def test_first_slot_is_bootstrap_only_and_future_slot_is_separate(self):
        self.assertEqual(observe_shadow(self.output,self.cfg,feed=self.feed)["status"], "observed")
        first = json.loads((self.output/"attempts/0000/observation.json").read_text())
        self.assertFalse(first["qualified_forward_slot"])
        self.assertFalse(first["references"]["BTC/USDT"]["verified_filter_average"])
        self.assertEqual(first["execution_probes"]["BTC/USDT"]["buy"]["rejection"], "filter_average_basis_unverified")
        self.assertEqual(verify_shadow(self.output,self.cfg)["qualified_forward_slots"], 0)
        self.at = NOW.replace(hour=12)+timedelta(seconds=1)
        self.assertEqual(observe_shadow(self.output,self.cfg,feed=self.feed)["status"], "observed")
        audit = verify_shadow(self.output,self.cfg)
        self.assertEqual(audit["qualified_forward_slots"],1)
        self.assertFalse(audit["engineering_observations_complete"])
        self.assertFalse(audit["approved_for_live"])
        self.assertEqual(audit["models_trained"],0)
        self.assertFalse(audit["selection_performed"])
        for case in audit["cases"].values():
            self.assertEqual(case["cash"],D("300"))
            self.assertEqual(case["active_orders"],[])

    def test_same_slot_and_failed_slot_are_never_retried(self):
        self.opener.fail = True
        result = observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertEqual(result["status"],"failed")
        self.assertIn("429",result["error"])
        self.assertEqual(len(self.opener.calls),1)
        with self.assertRaisesRegex(ValueError,"already reserved"):
            observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertEqual(len(self.opener.calls),1)
        self.assertEqual(verify_shadow(self.output,self.cfg)["status"],"inconclusive")

    def test_source_config_expiry_and_changed_runtime_block_before_network(self):
        with patch("crypto_trader_v2.shadow.code_hash",return_value="changed"):
            with self.assertRaisesRegex(ValueError,"package changed"):
                observe_shadow(self.output,self.cfg,feed=self.feed)
        self.at = NOW+timedelta(days=8)
        with self.assertRaisesRegex(ValueError,"Outside registered"):
            observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertFalse(self.opener.calls)
        with self.assertRaisesRegex(ValueError,"protocol/config"):
            observe_shadow(self.output,replace(self.cfg,initial_cash=D("400")),feed=self.feed)

    def test_code_change_mid_capture_cannot_update_portfolios(self):
        identity = json.loads((self.output/"registration.json").read_text())["plan"]["code_hash"]
        with patch("crypto_trader_v2.shadow.code_hash",side_effect=[identity,identity,"changed"]):
            result = observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertEqual(result["status"],"failed")
        self.assertFalse(result["portfolio_updated"])
        self.assertTrue(list((self.output/"attempts/0000/receipts").glob("*.json")))

    def test_single_writer_and_incomplete_attempt_guard(self):
        with (self.output/"writer.lock").open("rb") as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertTrue(verify_shadow(self.output,self.cfg)["worker_running"])
            with self.assertRaises(BlockingIOError):
                observe_shadow(self.output,self.cfg,feed=self.feed)
        folder = self.output/"attempts/0000"
        folder.mkdir()
        (folder/"reservation.json").write_text(json.dumps({"slot":int(NOW.timestamp())//self.cfg.seconds*self.cfg.seconds,"at":NOW.isoformat(),"registration_sha256":json.loads((self.output/"registration.json").read_text())["sha256"]}))
        self.at = NOW.replace(hour=12)
        with self.assertRaisesRegex(ValueError,"Incomplete shadow attempt"):
            observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertFalse(self.opener.calls)

    def test_readonly_audit_raw_tampering_and_default_paper_resume_guard(self):
        observe_shadow(self.output,self.cfg,feed=self.feed)
        content_before = {case: verify_dust_ledger(self.output/(case+".sqlite"), self.cfg)["content_sha256"] for case in ("cash", "baseline")}
        before = readonly_file_snapshot(self.output)
        self.assertEqual(verify_shadow(self.output,self.cfg)["status"],"verified")
        self.assertEqual(before, readonly_file_snapshot(self.output))
        self.assertEqual(content_before, {case: verify_dust_ledger(self.output/(case+".sqlite"), self.cfg)["content_sha256"] for case in ("cash", "baseline")})
        with self.assertRaisesRegex(ValueError,"execution profile mismatch"):
            Store(self.output/"baseline.sqlite",self.cfg,Mode.PAPER)
        path = self.output/"attempts/0000/receipts/0000.json"
        path.write_bytes(path.read_bytes()+b" ")
        with self.assertRaisesRegex(ValueError,"raw checksum"):
            verify_shadow(self.output,self.cfg)

    def test_readonly_snapshot_only_masks_documented_reader_marks(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/"baseline.sqlite-shm"
            initial = bytes(32768)
            path.write_bytes(initial)
            before = readonly_file_snapshot(root)
            for index in (100, 107, 119):
                changed = bytearray(initial)
                changed[index] = 1
                path.write_bytes(changed)
                self.assertEqual(before, readonly_file_snapshot(root))
            for index in (0, 16, 64, 96, 120, 136, 32767):
                changed = bytearray(initial)
                changed[index] = 1
                path.write_bytes(changed)
                self.assertNotEqual(before, readonly_file_snapshot(root))
            path.write_bytes(initial)
            raw = root/"receipts"
            raw.mkdir()
            receipt = raw/"baseline.sqlite-shm"
            receipt.write_bytes(initial)
            before = readonly_file_snapshot(root)
            receipt.write_bytes(initial[:100] + b"x" + initial[101:])
            self.assertNotEqual(before, readonly_file_snapshot(root))

    def test_budget_and_protocol_cannot_be_rehashed_to_extend_trial(self):
        path = self.output/"registration.json"
        envelope = json.loads(path.read_text())
        envelope["plan"]["budget"]["attempts"] = 1000
        envelope["sha256"] = hashlib.sha256(canonical(envelope["plan"])).hexdigest()
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError,"fixed protocol"):
            observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertFalse(self.opener.calls)

    def test_existing_registration_is_not_overwritten_and_cli_audit_is_readonly(self):
        with self.assertRaisesRegex(ValueError,"output exists"):
            register_shadow(self.history,self.cfg,self.output,now=NOW)
        with patch("crypto_trader_v2.__main__.load_config",return_value=self.cfg), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["verify-binance-th-shadow",str(self.output)]),0)
            audit = self.root/"audit"
            self.assertEqual(main(["verify-binance-th-shadow",str(self.output),"--audit-output",str(audit)]),0)
            saved = (audit/"report.json").read_bytes()
            self.assertNotEqual(main(["verify-binance-th-shadow",str(self.output),"--audit-output",str(audit)]),0)
            self.assertEqual(saved,(audit/"report.json").read_bytes())
        self.assertFalse(self.opener.calls)

    def test_bootstrap_label_and_probe_fee_tampering_are_detected(self):
        observe_shadow(self.output,self.cfg,feed=self.feed)
        path = self.output/"attempts/0000/observation.json"
        original = json.loads(path.read_text())
        value = json.loads(json.dumps(original))
        value["qualified_forward_slot"] = True
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,"bootstrap/forward"):
            verify_shadow(self.output,self.cfg)
        original["execution_probes"]["BTC/USDT"]["sell"]["fee_upper_assumption"] = "0"
        path.write_text(json.dumps(original))
        with self.assertRaisesRegex(ValueError,"probe/fee projection"):
            verify_shadow(self.output,self.cfg)

    def test_outside_observation_ledger_edit_cannot_pass_audit(self):
        observe_shadow(self.output,self.cfg,feed=self.feed)
        with Store(self.output/"cash.sqlite",self.cfg,Mode.PAPER,execution_profile=PROFILE) as store:
            with store.transaction():
                store.set_meta("unexpected_edit","tampering fixture")
        with self.assertRaisesRegex(ValueError,"changed outside"):
            verify_shadow(self.output,self.cfg)

    def test_preobservation_audit_blocks_external_edits_before_network(self):
        with Store(self.output/"cash.sqlite",self.cfg,Mode.PAPER,execution_profile=PROFILE) as store:
            with store.transaction():
                store.set_meta("unexpected_edit","before first request")
        with self.assertRaisesRegex(ValueError,"changed outside"):
            observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertFalse(self.opener.calls)

    def test_failed_read_cannot_hide_ledger_edit_before_next_slot(self):
        self.opener.fail = True
        observe_shadow(self.output,self.cfg,feed=self.feed)
        with Store(self.output/"baseline.sqlite",self.cfg,Mode.PAPER,execution_profile=PROFILE) as store:
            with store.transaction():
                store.set_meta("unexpected_edit","after failed request")
        self.at = NOW.replace(hour=12)
        self.opener.fail = False
        with self.assertRaisesRegex(ValueError,"changed outside"):
            observe_shadow(self.output,self.cfg,feed=self.feed)
        self.assertEqual(len(self.opener.calls),1)

    def test_nav_marks_are_bound_to_public_quotes_even_with_cash_only(self):
        observe_shadow(self.output,self.cfg,feed=self.feed)
        with Store(self.output/"cash.sqlite",self.cfg,Mode.PAPER,execution_profile=PROFILE) as store:
            with store.transaction():
                store.db.execute("UPDATE valuation_snapshots SET marks=?",(json.dumps({"BTC/USDT":"101","ETH/USDT":"100"}),))
        with self.assertRaisesRegex(ValueError,"NAV mark"):
            verify_shadow(self.output,self.cfg)

    def test_independent_fill_audit_checks_price_tick_fee_and_book_capacity(self):
        self.opener.percent = False  # Software fixture ONLY; not the real market's filters.
        observe_shadow(self.output,self.cfg,feed=self.feed)
        with Store(self.output/"baseline.sqlite",self.cfg,Mode.PAPER,execution_profile=PROFILE) as store:
            store.intent("fixture","BTC/USDT","buy",D("0.1"),NOW,"fixture",cash_reserved=D("10"),risk_reserved=D("0.1"),price_limit=D("100"),stop=D("95"),atr_distance=D("5"))
            rules = {i.symbol:NativeRules(row(i.symbol.replace("/", ""),percent=False)) for i in self.cfg.instruments}
            NativePaperBroker(self.cfg,rules).execute(store,"fixture",Quote("BTC/USDT",NOW,D("100"),D("100")))
        path = self.output/"attempts/0000/observation.json"
        details = json.loads(path.read_text())
        details["ledger_audits"]["baseline"] = verify_dust_ledger(self.output/"baseline.sqlite",self.cfg)
        path.write_text(json.dumps(details,default=str))
        self.assertEqual(verify_shadow(self.output,self.cfg)["status"],"verified")
        with Store(self.output/"baseline.sqlite",self.cfg,Mode.PAPER,execution_profile=PROFILE) as store:
            with store.transaction():
                store.db.execute("UPDATE fills SET price='100.01'")
        with self.assertRaisesRegex(ValueError,"adverse price/tick"):
            verify_shadow(self.output,self.cfg)
        # Direct corruption fixture: the normal writer correctly refuses to
        # reopen a ledger whose existing fill no longer conserves allocations.
        with sqlite3.connect(self.output/"baseline.sqlite") as db:
            db.execute("UPDATE fills SET price='100',quantity='1.1'")
        with self.assertRaisesRegex(ValueError,"top-of-book capacity"):
            verify_shadow(self.output,self.cfg)


if __name__ == "__main__":
    unittest.main()
