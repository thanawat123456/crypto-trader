from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from crypto_trader_v2.__main__ import parser
from crypto_trader_v2.config import Config
from crypto_trader_v2.domain import Instrument
from crypto_trader_v2.xau_research import XauContract, import_xau_reference, load_xau_contract, trade_economics


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def contract():
    # SOFTWARE FIXTURE ONLY; these are NOT a broker's actual specifications.
    return XauContract("software fixture", "test USD", "synthetic terms", START.isoformat(),
                       (START + timedelta(days=31)).isoformat(), "test sessions", "test rollovers", "UTC",
                       D(100), D("0.01"), D("0.01"), D("0.05"), D(3), D("0.10"), D(1), D(10), D(-2))


class XauContractTests(unittest.TestCase):
    def economics(self, side="long", **kwargs):
        values = dict(opened_at=START, closed_at=START + timedelta(days=1), rollover_units=D(3))
        values.update(kwargs)
        return trade_economics(contract(), side, D("0.01"), D(2000), D("2000.20"), D(2010), D("2010.20"), **values)

    def test_long_net_includes_quote_sides_commission_slippage_and_triple_swap(self):
        row = self.economics()
        self.assertEqual(row["ounces"], D(1))
        self.assertEqual(row["gross_quote_side_pnl_usd"], D("9.80"))
        self.assertEqual(row["commission_usd"], D("0.06"))
        self.assertEqual(row["adverse_slippage_usd"], D("0.20"))
        self.assertEqual(row["signed_swap_charge_usd"], D("0.30"))
        self.assertEqual(row["net_pnl_usd"], D("9.24"))
        self.assertEqual(row["initial_margin_estimate_usd"], D("100.0100"))
        self.assertFalse(row["approved_for_live"])

    def test_short_funding_credit_is_signed_and_margin_is_not_a_fee(self):
        row = self.economics("short")
        self.assertEqual(row["gross_quote_side_pnl_usd"], D("-10.20"))
        self.assertEqual(row["signed_swap_charge_usd"], D("-0.06"))
        self.assertEqual(row["net_pnl_usd"], D("-10.40"))

    def test_lot_steps_spreads_dates_and_unknown_terms_fail_closed(self):
        c = contract()
        for kwargs in (dict(lots=D("0.001")), dict(lots=D("0.015")), dict(exit_ask=D(2020)),
                       dict(entry_ask=D(1999)), dict(rollover_units=D(-1)), dict(side="buy"),
                       dict(closed_at=START), dict(closed_at=START + timedelta(days=40)), dict(lots=D("NaN"))):
            values = dict(side="long", lots=D("0.01"), entry_bid=D(2000), entry_ask=D("2000.20"),
                          exit_bid=D(2010), exit_ask=D("2010.20"), opened_at=START,
                          closed_at=START + timedelta(days=1), rollover_units=D(0))
            values.update(kwargs)
            with self.assertRaises(ValueError):
                trade_economics(c, **values)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            load_xau_contract(Path("config.xau.research.template.json"))
        for bad in (replace(c, lot_step=D(0)), replace(c, ounces_per_lot=D("NaN")),
                    replace(c, margin_rate=D(2)), replace(c, terms_source="")):
            with self.assertRaises(ValueError):
                bad.validate()


def csv_file(path, rows):
    path.write_text("timestamp,open,high,low,close\n" + "".join(
        f"{at},2000.20,2001.20,1999.20,2000.20\n" for at in rows))


class XauImportTests(unittest.TestCase):
    def test_bid_ask_preserve_market_gaps_and_never_claim_replay_evidence(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            bids, asks, output = root / "bid.csv", root / "ask.csv", root / "gold"
            timestamps = [START.isoformat(), (START + timedelta(hours=1)).isoformat(),
                          (START + timedelta(days=3)).isoformat()]
            csv_file(asks, timestamps)
            bids.write_text(asks.read_text().replace(".20", ".00"))
            row = import_xau_reference(bids, asks, "synthetic software fixture", output)
            self.assertEqual(row["rows"], 3)
            self.assertEqual(row["gap_count"], 1)
            self.assertEqual(row["gaps"][0]["absent_intervals"], 70)
            self.assertEqual(row["fabricated_intervals"], 0)
            self.assertEqual(row["endpoint_spread_usd_per_ounce"]["mean"], D("0.20"))
            self.assertFalse(row["strategy_replay_ready"])
            self.assertFalse(row["models_trained"])
            self.assertIn("bid_open", (output / "quotes.csv").read_text())
            with self.assertRaisesRegex(ValueError, "exists"):
                import_xau_reference(bids, asks, "fixture", output)

    def test_unknown_timezone_duplicate_and_off_grid_are_rejected_before_export(self):
        for timestamps in (["2026-01-01T00:00:00"], [START.isoformat(), START.isoformat()],
                           [(START + timedelta(minutes=1)).isoformat()]):
            with TemporaryDirectory() as folder:
                root = Path(folder)
                path = root / "data.csv"
                csv_file(path, timestamps)
                with self.assertRaises(ValueError):
                    import_xau_reference(path, path, "fixture", root / "out")
                self.assertFalse((root / "out").exists())

    def test_crossed_mismatched_and_nan_quotes_are_not_repaired(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            bid, ask = root / "bid.csv", root / "ask.csv"
            csv_file(bid, [START.isoformat()])
            csv_file(ask, [(START + timedelta(hours=1)).isoformat()])
            with self.assertRaisesRegex(ValueError, "timelines"):
                import_xau_reference(bid, ask, "fixture", root / "mismatch")
            ask.write_text(bid.read_text().replace(".20", ".00"))
            with self.assertRaisesRegex(ValueError, "Crossed"):
                import_xau_reference(bid, ask, "fixture", root / "crossed")
            bid.write_text(bid.read_text().replace("2001.20", "NaN"))
            with self.assertRaisesRegex(ValueError, "row"):
                import_xau_reference(bid, bid, "fixture", root / "nan")

    def test_gold_commands_cannot_send_orders(self):
        args = parser().parse_args(["import-xau-reference", "--bid", "bid.csv", "--ask", "ask.csv",
                                    "--source", "broker export", "--output", "gold"])
        self.assertEqual(args.command, "import-xau-reference")
        self.assertFalse(hasattr(args, "live"))
        with self.assertRaisesRegex(ValueError, "not a Kraken crypto spot"):
            replace(Config(), instruments=(Instrument("XAU/USD", D("0.01"), D("0.01"), D(1)),)).validate()


if __name__ == "__main__":
    unittest.main()
