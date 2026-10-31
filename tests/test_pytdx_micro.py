import unittest
from datetime import date
from unittest.mock import patch

from xiao_a.data.pytdx_micro import summarize_minute_rows, summarize_ticks, summarize_order_book


class PytdxMicroUnitTests(unittest.TestCase):
    def test_minute_summary(self):
        rows = [{"price": 10.0, "vol": 100}, {"price": 10.5, "vol": 50}, {"price": 9.8, "vol": 20}, {"price": 10.2, "vol": 30}]
        summary = summarize_minute_rows(rows, trade_date=date(2026, 10, 30))
        self.assertEqual(summary["points"], 4)
        self.assertEqual(summary["open"], 10.0)
        self.assertEqual(summary["last"], 10.2)
        self.assertEqual(summary["high"], 10.5)
        self.assertEqual(summary["low"], 9.8)
        self.assertEqual(summary["total_vol"], 200)
        self.assertEqual(summary["trade_date"], "2026-10-30")

    def test_tick_summary_buy_sell(self):
        rows = [
            {"time": "14:50", "price": 10.1, "vol": 10, "buyorsell": 0},
            {"time": "14:51", "price": 10.0, "vol": 20, "buyorsell": 1},
            {"time": "14:52", "price": 10.0, "vol": 5, "buyorsell": 5},
        ]
        summary = summarize_ticks(rows, trade_date=date(2026, 10, 30))
        self.assertEqual(summary["buy_vol"], 10)
        self.assertEqual(summary["sell_vol"], 20)
        self.assertEqual(summary["other_vol"], 5)
        self.assertEqual(summary["recent"][-1]["side"], "其他(5)")

    def test_order_book_empty_when_zeros(self):
        self.assertIsNone(summarize_order_book({"bid1": 0, "ask1": 0, "bid_vol1": 0, "ask_vol1": 0}))

    def test_order_book_levels(self):
        book = summarize_order_book({
            "price": 10.0,
            "bid1": 9.99, "bid_vol1": 100, "ask1": 10.01, "ask_vol1": 80,
            "bid2": 9.98, "bid_vol2": 50, "ask2": 10.02, "ask_vol2": 40,
            "bid3": 0, "ask3": 0, "bid_vol3": 0, "ask_vol3": 0,
            "bid4": 0, "ask4": 0, "bid_vol4": 0, "ask_vol4": 0,
            "bid5": 0, "ask5": 0, "bid_vol5": 0, "ask_vol5": 0,
        })
        self.assertIsNotNone(book)
        self.assertEqual(book["levels"][0]["bid"], 9.99)


class SinaMoneyFlowParseTests(unittest.TestCase):
    def test_parse_sina(self):
        from xiao_a.data.supplemental import parse_sina_moneyflow_payload
        payload = '''[
          {"opendate":"2026-10-29","netamount":"100","r0_net":"40"},
          {"opendate":"2026-10-30","netamount":"-200","r0_net":"-50"}
        ]'''
        flow, provenance = parse_sina_moneyflow_payload(payload)
        self.assertTrue(flow["available"])
        self.assertEqual(flow["trade_date"], "2026-10-30")
        self.assertEqual(flow["main_net_inflow"], -200)
        self.assertEqual(flow["super_large_inflow"], -50)
        self.assertIn("估算口径", flow["source_label"])
        self.assertEqual(provenance.source, "sina-moneyflow")


if __name__ == "__main__":
    unittest.main()
