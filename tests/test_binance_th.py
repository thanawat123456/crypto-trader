from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from crypto_trader_v2.__main__ import main
from crypto_trader_v2.binance_th import (BASE_URL, BinanceTHPublicFeed, NoRedirect, _decode,
                                       capture_binance_th, parse_instruments, parse_klines, verify_binance_capture)
from crypto_trader_v2.broker import ExecutionScenario, PaperBroker
from crypto_trader_v2.config import Config, CostConfig, StrategyConfig, load_config
from crypto_trader_v2.domain import Bar, Instrument, Mode, Quote, Signal, ONE, ZERO
from crypto_trader_v2.engine import Coordinator
from crypto_trader_v2.entry_policy import EntryPolicy
from crypto_trader_v2.importer import import_kraken, load_dataset
from crypto_trader_v2.ml_labels import LabelSpec, simulate_label
from crypto_trader_v2.report import build_report
from crypto_trader_v2.risk import size_entry
from crypto_trader_v2.storage import Store


NOW = datetime(2026, 10, 3, 8, tzinfo=timezone.utc)


def config():
    return replace(Config(), venue="binance_th", quote_currency="USDT",
                   instruments=(Instrument("BTC/USDT", D("0.00001"), D("0.00001"), D("5"), D("9000")),
                                Instrument("ETH/USDT", D("0.0001"), D("0.0001"), D("5"), D("9000"))),
                   strategy=StrategyConfig(3, 1, 3, 2, 2),
                   costs=CostConfig(D("0.001"), ZERO, ZERO, D("0.003"), "unit fixture", "2026-10-10"))


def markets():
    return {"symbols": [{"symbol": base + "USDT", "baseAsset": base, "quoteAsset": "USDT",
                         "status": "TRADING", "type": "GLOBAL", "orderTypes": ["LIMIT", "MARKET"],
                         "filters": [{"filterType": "LOT_SIZE", "minQty": step, "maxQty": "9000", "stepSize": step},
                                     {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True},
                                     {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"}]
                         } for base, step in (("BTC", "0.00001"), ("ETH", "0.0001"))]}


def kline(start, cfg, price="100"):
    ms = int(start.timestamp()) * 1000
    return [ms, price, price, price, price, "10", ms + cfg.seconds * 1000 - 1, "1000", 1, "1", "100", "0"]


class Response:
    def __init__(self, url, value):
        self.url = url
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode()

    def geturl(self):
        return self.url

    def read(self, count):
        return self.raw[:count]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeOpener:
    def __init__(self, cfg):
        self.cfg, self.calls = cfg, []

    def open(self, request, timeout):
        self.calls.append(request)
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        endpoint = parsed.path.removeprefix("/api/v1/")
        if endpoint == "exchangeInfo":
            value = markets()
        elif endpoint == "time":
            value = {"serverTime": int(NOW.timestamp()) * 1000}
        elif endpoint == "klines":
            start = datetime.fromtimestamp(int(query["startTime"][0]) / 1000, timezone.utc)
            value = [kline(start + timedelta(seconds=i * self.cfg.seconds), self.cfg) for i in range(int(query["limit"][0]))]
        elif endpoint == "ticker/bookTicker":
            value = {"symbol": query["symbol"][0], "bidPrice": "100", "askPrice": "100.01", "bidQty": "10", "askQty": "2"}
        else:
            raise AssertionError("Private/unexpected request")
        return Response(request.full_url, value)


def feed(cfg=None, count=12):
    cfg = cfg or config()
    return BinanceTHPublicFeed(count, opener=FakeOpener(cfg), clock=lambda: NOW)


class PublicFeedTests(unittest.TestCase):
    def test_example_load_and_scope_guards(self):
        cfg = load_config("config.binance-th.paper.yaml")
        self.assertEqual(cfg.quote_currency, "USDT")
        self.assertEqual(cfg.costs.taker_fee, D("0.001"))
        for changed in (replace(cfg, venue="binance"), replace(cfg, quote_currency="USD"),
                        replace(cfg, instruments=cfg.instruments[:1])):
            with self.assertRaises(ValueError):
                changed.validate()
        with self.assertRaises(ValueError):
            feed().instruments(Config())
        with self.assertRaisesRegex(ValueError, "Kraken configuration"):
            import_kraken(cfg, Path("unused"), files={})

    def test_public_only_native_snapshot_and_capacity(self):
        client = feed()
        self.assertEqual(client.instruments(config())["BTC/USDT"].max_quantity, D("9000"))
        now, histories, quotes = client.snapshot(config())
        self.assertEqual(now, NOW)
        self.assertEqual(len(histories["BTC/USDT"]), 12)
        self.assertEqual(histories["ETH/USDT"][-1].end, NOW)
        self.assertEqual(client.observed_capacity[("BTC/USDT", "buy")], (quotes["BTC/USDT"].time, D("2")))
        for request in client.opener.calls:
            self.assertTrue(request.full_url.startswith(BASE_URL + "/api/v1/"))
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn("X-mbx-apikey", request.headers)
            self.assertIsNone(request.data)

    def test_private_endpoints_extra_params_and_redirect_refused(self):
        client = feed()
        for endpoint, params in (("order", {}), ("account", {}), ("time", {"signature": "no"}),
                                 ("../order", {}), ("exchangeInfo", {"apiKey": "no"})):
            with self.assertRaisesRegex(ValueError, "allowlist"):
                client.request(endpoint, params)
        self.assertFalse(client.opener.calls)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://api.binance.com"))
        with patch.object(client.opener, "open", return_value=Response("https://api.binance.com", {})):
            with self.assertRaisesRegex(ValueError, "Redirected"):
                client.request("time", {})

    def test_seven_day_chunks_are_complete_and_non_overlapping(self):
        client = feed(count=100)
        client.instruments(config())
        _, histories, _ = client.snapshot(config())
        self.assertEqual(len(histories["BTC/USDT"]), 100)
        requests = [r for r in client.requests if r["endpoint"] == "klines" and r["params"]["symbol"] == "BTCUSDT"]
        self.assertEqual([r["params"]["limit"] for r in requests], [42, 42, 16])
        for previous, current in zip(requests, requests[1:]):
            self.assertEqual(previous["params"]["endTime"] + 1, current["params"]["startTime"])

    def test_rate_limit_is_one_request_without_retry(self):
        client = feed()
        error = HTTPError(BASE_URL, 429, "rate limit", {"Retry-After": "120"}, None)
        with patch.object(client.opener, "open", side_effect=error) as opened:
            with self.assertRaisesRegex(ValueError, "429.*without retry.*120"):
                client.request("time", {})
            self.assertEqual(opened.call_count, 1)

    def test_byte_latency_clock_and_warmup_limits(self):
        client = feed()
        with patch.object(client.opener, "open", return_value=Response(BASE_URL + "/api/v1/time", b" " * (2 * 1024 * 1024 + 1))):
            with self.assertRaisesRegex(ValueError, "byte budget"):
                client.request("time", {})
        client = feed()
        client.clock = unittest.mock.Mock(side_effect=[NOW, NOW + timedelta(seconds=6)])
        with self.assertRaisesRegex(ValueError, "latency"):
            client.request("time", {})
        client = feed()
        with patch.object(client, "request", return_value={"serverTime": int((NOW - timedelta(minutes=1)).timestamp()) * 1000}):
            with self.assertRaisesRegex(ValueError, "clock mismatch"):
                client.snapshot(config())
        with self.assertRaisesRegex(ValueError, "warmup"):
            feed(count=3).snapshot(config())
        for count in (True, 0, 1001):
            with self.assertRaises(ValueError):
                BinanceTHPublicFeed(count)

    def test_metadata_offline_missing_unknown_and_duplicate_filters(self):
        for mutation in ("offline", "missing", "unknown", "duplicate", "wrong_asset", "negative_max", "market_step"):
            payload = markets()
            row = payload["symbols"][0]
            if mutation == "offline": row["status"] = "HALT"
            elif mutation == "missing": row["filters"] = row["filters"][:1]
            elif mutation == "unknown": row["filters"].append({"filterType": "UNKNOWN_NEW_FILTER"})
            elif mutation == "duplicate": row["filters"].append(row["filters"][0].copy())
            elif mutation == "wrong_asset": row["quoteAsset"] = "USD"
            elif mutation == "negative_max": row["filters"][0]["maxQty"] = "-1"
            else: row["filters"].append({"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "9", "stepSize": "0.0001"})
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                parse_instruments(payload, config())

    def test_market_lot_and_notional_maximum_are_retained(self):
        payload = markets()
        filters = payload["symbols"][0]["filters"]
        filters[1] = {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "1000"}
        filters.append({"filterType": "MARKET_LOT_SIZE", "minQty": "0", "maxQty": "1", "stepSize": "0"})
        instruments, _ = parse_instruments(payload, config())
        self.assertEqual(instruments["BTC/USDT"].max_quantity, ONE)
        self.assertEqual(instruments["BTC/USDT"].max_notional, D("1000"))

    def test_bad_candles_and_api_envelopes(self):
        cfg = config()
        start = NOW - timedelta(seconds=cfg.seconds)
        valid = kline(start, cfg)
        for rows in ([valid, valid], [kline(NOW, cfg)], [valid[:5]],
                     [[True, *valid[1:]]], [[valid[0], "NaN", *valid[2:]]],
                     [valid[:6] + [valid[6] - 1] + valid[7:]]):
            with self.assertRaises(ValueError):
                parse_klines(rows, "BTC/USDT", cfg, NOW)
        self.assertEqual(_decode(b'{"code":0,"data":{"serverTime":123}}'), {"serverTime": 123})
        for raw in (b'{"code":-1,"msg":"failed"}', b'{"code":true,"data":[]}', b'{"x":NaN}'):
            with self.assertRaises(ValueError):
                _decode(raw)

    def test_capture_round_trip_read_only_audit_and_tamper(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "capture"
            manifest = capture_binance_th(config(), output, feed=feed(count=100))
            before = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
            result = verify_binance_capture(output, config())
            self.assertEqual(result["status"], "verified")
            self.assertFalse(result["approved_for_live"])
            self.assertEqual(before, {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()})
            self.assertEqual(len(load_dataset(output, config()).bars["BTC/USDT"]), 100)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                capture_binance_th(config(), output, feed=feed())
            raw = output / manifest["receipts"][0]["file"]
            raw.write_bytes(raw.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_binance_capture(output, config())

    def test_capture_does_not_write_partial_dataset_on_network_failure(self):
        client = feed()
        with TemporaryDirectory() as folder, patch.object(client, "request", side_effect=ValueError("HTTP failed")):
            output = Path(folder) / "capture"
            with self.assertRaises(ValueError):
                capture_binance_th(config(), output, feed=client)
            self.assertFalse(output.exists())

    def test_cli_public_paper_uses_native_feed_and_refreshes_rules(self):
        cfg, client = config(), feed()
        with TemporaryDirectory() as folder, patch("crypto_trader_v2.__main__.load_config", return_value=cfg), \
                patch("crypto_trader_v2.__main__.BinanceTHPublicFeed", return_value=client), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(folder) / "paper.sqlite"
            args = ["paper", "--db", str(path), "--once"]
            self.assertEqual(main(args + ["--initialize", "--entry-policy", "cash"]), 0)
            self.assertEqual(main(args + ["--entry-policy", "cash"]), 0)
            with Store(path, cfg, Mode.PAPER) as store:
                self.assertEqual(store.get_meta("venue"), "binance_th")
                self.assertIn("received base", store.get_meta("fee_asset_policy"))
                self.assertEqual(len(json.loads(store.get_meta("last_public_receipts"))), 6)
                self.assertEqual(store.cash, D("300"))
                self.assertEqual(build_report(store.db)["quote_currency"], "USDT")

    def test_first_network_failure_keeps_policy_bound_and_can_resume(self):
        cfg, client = config(), feed()
        with TemporaryDirectory() as folder, patch("crypto_trader_v2.__main__.load_config", return_value=cfg), \
                patch("crypto_trader_v2.__main__.BinanceTHPublicFeed", return_value=client), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            path = Path(folder) / "paper.sqlite"
            args = ["paper", "--db", str(path), "--once", "--entry-policy", "cash"]
            with patch.object(client, "instruments", side_effect=ValueError("network failure")):
                self.assertEqual(main(args + ["--initialize"]), 1)
            with Store(path, cfg, Mode.PAPER) as store:
                self.assertEqual(json.loads(store.get_meta("entry_policy"))["name"], "cash")
                self.assertEqual(store.orders(), [])
            self.assertEqual(main(args), 0)

    def test_native_ml_profit_pipeline_is_blocked_before_any_output(self):
        with TemporaryDirectory() as folder, patch("crypto_trader_v2.__main__.load_config", return_value=config()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            output = Path(folder) / "study"
            self.assertEqual(main(["research", "unused", "--output", str(output)]), 1)
            self.assertIn("adequate native history", errors.getvalue())
            self.assertFalse(output.exists())


class NativeAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.cfg = config()
        self.store = Store(Path(self.temp.name) / "paper.sqlite", self.cfg, Mode.PAPER, create=True, source="software fixture only")
        self.broker = PaperBroker(self.cfg)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def buy(self, quantity=ONE):
        self.store.intent("entry", "BTC/USDT", "buy", quantity, NOW, "fixture",
                          cash_reserved=quantity * D("100"), risk_reserved=D("1"),
                          stop=D("95"), atr_distance=D("5"), price_limit=D("100"))
        self.broker.execute(self.store, "entry", Quote("BTC/USDT", NOW, D("100"), D("100")))

    def test_received_fee_buy_sell_inventory_cash_and_idempotence(self):
        self.buy()
        self.assertEqual(self.store.cash, D("200"))
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], D("0.999"))
        self.assertEqual(self.store.positions()["BTC/USDT"]["cost"], D("100"))
        fill = self.store.db.execute("SELECT * FROM fills").fetchone()
        self.assertEqual(fill["fee_asset"], "BTC")
        self.broker.execute(self.store, "entry", Quote("BTC/USDT", NOW, D("100"), D("100")))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)
        self.store.intent("exit", "BTC/USDT", "sell", D("0.999"), NOW, "fixture")
        self.broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        self.assertEqual(self.store.cash, D("309.780110"))
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(build_report(self.store.db)["realized_net_pnl"], D("9.780110"))
        self.store.assert_balanced()

    def test_received_base_fee_dust_not_invented_as_exit_cash(self):
        self.buy(D("0.1"))
        # .0999 divides the BTC step; choose an entry which produces actual dust.
        self.store.intent("exit", "BTC/USDT", "sell", D("0.0999"), NOW, "fixture")
        self.broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        self.store.intent("new", "BTC/USDT", "buy", D("0.05001"), NOW, "fixture", cash_reserved=D("5.001"),
                          risk_reserved=D("1"), stop=D("95"), atr_distance=D("5"))
        self.broker.execute(self.store, "new", Quote("BTC/USDT", NOW, D("100"), D("100")))
        held = self.store.positions()["BTC/USDT"]["quantity"]
        self.store.intent("dust-exit", "BTC/USDT", "sell", self.cfg.instruments[0].round_quantity(held), NOW, "fixture")
        self.broker.execute(self.store, "dust-exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        residual = self.store.positions()["BTC/USDT"]["quantity"]
        self.assertEqual(residual, D("0.00000999"))
        cash = self.store.cash
        self.store.intent("below-min", "BTC/USDT", "sell", residual, NOW, "fixture")
        self.broker.execute(self.store, "below-min", Quote("BTC/USDT", NOW, D("110"), D("110")))
        self.assertEqual(self.store.cash, cash)
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], residual)
        self.assertEqual(self.store.order("below-min")["status"], "CANCELED")
        self.store.assert_balanced()

    def test_observed_quote_capacity_rounded_partial_ioc_and_timestamp_binding(self):
        self.broker.observed_capacity = {("BTC/USDT", "buy"): (NOW, D("0.123456"))}
        self.buy()
        self.assertEqual(D(self.store.order("entry")["filled"]), D("0.12345"))
        self.assertEqual(self.store.order("entry")["status"], "CANCELED")
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], D("0.12332655"))
        self.assertEqual(self.store.reserved(), (ZERO, ZERO))
        self.store.intent("exit", "BTC/USDT", "sell", D("0.12332655"), NOW, "fixture")
        with self.assertRaisesRegex(ValueError, "Missing/stale"):
            self.broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("100"), D("100")))
        self.store.assert_balanced()

    def test_sizing_native_fees_risk_reservation_and_maxima(self):
        instrument = replace(self.cfg.instruments[0], max_quantity=D("0.5"), max_notional=D("12"))
        result = size_entry(self.cfg, instrument, nav=D("100000"), cash=D("100000"), entry=D("100"), stop=D("95"),
                            exposure=ZERO, open_risk=ZERO, open_count=0)
        self.assertEqual(result.quantity, D("0.12"))
        self.assertEqual(result.cash_reserved, D("12"))
        expected = D("100") - D("95") * D("0.999") ** 2 + D("100") * self.cfg.risk.gap_buffer
        self.assertEqual(result.risk_reserved, result.quantity * expected)

    def test_break_even_matches_native_two_leg_fee_formula(self):
        quote = Quote("BTC/USDT", NOW, D("100"), D("100"))
        signal = Signal("BTC/USDT", NOW, True, False, D("1"), "fixture")
        result = EntryPolicy().evaluate(signal, None, quote, self.cfg)
        self.assertEqual(result.round_trip_break_even, ONE / D("0.999") ** 2 - ONE)

    def test_native_label_one_unit_matches_executed_round_trip(self):
        self.buy()
        self.store.intent("exit", "BTC/USDT", "sell", D("0.999"), NOW, "fixture")
        self.broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        start = NOW - timedelta(seconds=self.cfg.seconds)
        bars = [Bar("BTC/USDT", start + timedelta(seconds=i * self.cfg.seconds), self.cfg.seconds,
                    D(price), D(price), D(price), D(price), ONE) for i, price in enumerate(("100", "100", "110", "110"))]
        signals = [Signal("BTC/USDT", b.end, i == 0, i == 1, D("2.5"), "fixture") for i, b in enumerate(bars)]
        label = simulate_label(bars, signals, 1, self.cfg, LabelSpec(max_holding_bars=2))
        self.assertEqual(label.net_return_pct, build_report(self.store.db)["realized_net_pnl"])
        self.assertEqual(label.exit_reason, "desired_flat")

    def test_partial_exit_then_restart_preserves_remaining_net_inventory(self):
        self.buy()
        broker = PaperBroker(self.cfg, ExecutionScenario(exit_fill_fraction=D("0.4")))
        self.store.intent("exit", "BTC/USDT", "sell", D("0.999"), NOW, "fixture")
        broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        remaining = self.store.positions()["BTC/USDT"]["quantity"]
        self.assertEqual(remaining, D("0.599"))
        path = self.store.path
        self.store.close()
        self.store = Store(path, self.cfg, Mode.PAPER)
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], remaining)
        self.store.assert_balanced()

    def test_below_minimum_entry_and_zero_capacity_make_no_fake_fill(self):
        self.buy(D("0.01"))
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.cash, D("300"))
        self.assertEqual(self.store.order("entry")["status"], "CANCELED")

    def test_quote_capacity_zero_exit_and_maximum_entry_remain_no_fill(self):
        self.buy()
        self.broker.observed_capacity = {("BTC/USDT", "sell"): (NOW, ZERO)}
        self.store.intent("exit", "BTC/USDT", "sell", D("0.999"), NOW, "fixture")
        self.broker.execute(self.store, "exit", Quote("BTC/USDT", NOW, D("110"), D("110")))
        self.assertEqual(self.store.cash, D("200"))
        self.assertEqual(self.store.positions()["BTC/USDT"]["quantity"], D("0.999"))
        self.assertEqual(self.store.order("exit")["status"], "CANCELED")
        self.store.assert_balanced()

    def test_dust_exit_is_explicit_and_does_not_generate_order_each_poll(self):
        self.buy(D("0.05001"))
        coordinator = Coordinator(self.store, self.cfg)
        coordinator.sell("BTC/USDT", Quote("BTC/USDT", NOW, D("110"), D("110")), "desired_flat")
        count = len(self.store.orders())
        for seconds in (1, 2, 3):
            coordinator.sell("BTC/USDT", Quote("BTC/USDT", NOW + timedelta(seconds=seconds), D("110"), D("110")), "desired_flat")
        self.assertEqual(len(self.store.orders()), count)
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.dust_lots("BTC/USDT")[0]["quantity"], D("0.00000999"))
        decisions = self.store.db.execute("SELECT * FROM decisions WHERE action='PARK_DUST'").fetchall()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(self.store.get_meta("exit_required:BTC/USDT"), "")
        self.store.assert_balanced()


if __name__ == "__main__":
    unittest.main()
