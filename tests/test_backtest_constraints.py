from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from xiao_a.backtest import AShareBacktester, FeeSchedule, TradeSide
from xiao_a.models import DailyBar, SignalAction
from xiao_a.research_diagnostics import run_research_backtest_spec


def _bar(day: date, open_: float, high: float, low: float, close: float, previous: float, **kwargs) -> DailyBar:
    return DailyBar(day, open_, high, low, close, previous_close=previous, **kwargs)


class AShareConstraintTests(unittest.TestCase):
    def test_buy_is_not_sellable_until_the_next_session(self):
        days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
        bars = [
            _bar(days[0], 10, 10.2, 9.8, 10, 9.5),
            _bar(days[1], 10, 10.4, 9.8, 10, 10),
            _bar(days[2], 10, 10.4, 9.8, 10.2, 10),
        ]
        result = AShareBacktester(
            initial_cash=100_000,
            fees=FeeSchedule(0, 0, 0, 0),
            slippage_bps=0,
        ).run(bars, {days[0]: SignalAction.BUY, days[1]: SignalAction.SELL}, target_position_pct=1)
        self.assertEqual([(trade.trading_date, trade.side) for trade in result.trades], [
            (days[1], TradeSide.BUY),
            (days[2], TradeSide.SELL),
        ])
        self.assertEqual(result.ending_quantity, 0)
        self.assertFalse(result.assumptions["vectorbt_used"])
        self.assertEqual(result.assumptions["authority"], "AShareBacktester")
        self.assertIn("not_sellable", result.assumptions["t_plus_1"])

    def test_suspension_limit_down_cash_lot_and_slippage_are_explicit(self):
        suspended = AShareBacktester(initial_cash=100_000, fees=FeeSchedule(0, 0, 0, 0), slippage_bps=0).run(
            [
                _bar(date(2026, 1, 2), 10, 10, 10, 10, 9.5),
                _bar(date(2026, 1, 5), 10, 10, 10, 10, 10, suspended=True),
            ],
            {date(2026, 1, 2): SignalAction.BUY},
        )
        self.assertEqual(suspended.trades, [])
        self.assertIn("suspended", suspended.rejected_orders[0])

        held = AShareBacktester(initial_cash=100_000, fees=FeeSchedule(0, 0, 0, 0), slippage_bps=0).run(
            [
                _bar(date(2026, 1, 2), 10, 10.2, 9.8, 10, 9.5),
                _bar(date(2026, 1, 5), 10, 10.4, 9.8, 10, 10),
                _bar(date(2026, 1, 6), 9, 9, 9, 9, 10, limit_rate=0.10),
            ],
            {date(2026, 1, 2): SignalAction.BUY, date(2026, 1, 5): SignalAction.SELL},
            target_position_pct=1,
        )
        self.assertEqual([trade.side for trade in held.trades], [TradeSide.BUY])
        self.assertGreater(held.ending_quantity, 0)
        self.assertIn("locked limit-down", held.rejected_orders[0])

        poor = AShareBacktester(initial_cash=50, fees=FeeSchedule(0, 0, 0, 0), slippage_bps=0).run(
            [
                _bar(date(2026, 1, 2), 10, 10, 10, 10, 9.5),
                _bar(date(2026, 1, 5), 10, 10, 10, 10, 10),
            ],
            {date(2026, 1, 2): SignalAction.BUY},
            target_position_pct=1,
        )
        self.assertEqual(poor.trades, [])
        self.assertIn("insufficient cash for one lot", poor.rejected_orders[0])

        slipped = AShareBacktester(initial_cash=100_000, fees=FeeSchedule(0, 0, 0, 0), slippage_bps=100).run(
            [
                _bar(date(2026, 1, 2), 10, 10, 10, 10, 9.5),
                _bar(date(2026, 1, 5), 10, 11, 9, 10.5, 10),
            ],
            {date(2026, 1, 2): SignalAction.BUY},
        )
        self.assertAlmostEqual(slipped.trades[0].price, 10.1)

    def test_board_limit_requires_a_one_price_lock_and_st_uses_five_percent(self):
        locked_st = DailyBar(date(2026, 1, 5), 10.5, 10.5, 10.5, 10.5, previous_close=10, is_st=True)
        ordinary = DailyBar(date(2026, 1, 5), 10.5, 10.5, 10.5, 10.5, previous_close=10, is_st=False)
        touched = DailyBar(date(2026, 1, 5), 11, 11, 10.8, 11, previous_close=10, limit_rate=0.10)
        self.assertTrue(AShareBacktester._locked_limit(locked_st, TradeSide.BUY))
        self.assertFalse(AShareBacktester._locked_limit(ordinary, TradeSide.BUY))
        self.assertFalse(AShareBacktester._locked_limit(touched, TradeSide.BUY))

    def test_reduce_below_one_lot_is_rejected_and_fees_follow_the_supplied_schedule(self):
        days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
        bars = [
            _bar(days[0], 10, 10, 10, 10, 9.5),
            _bar(days[1], 10, 10, 10, 10, 10),
            _bar(days[2], 10, 10, 10, 10, 10),
        ]
        reduced = AShareBacktester(initial_cash=1_000, fees=FeeSchedule(0, 0, 0, 0), slippage_bps=0).run(
            bars,
            {days[0]: SignalAction.BUY, days[1]: SignalAction.REDUCE},
            target_position_pct=1,
        )
        self.assertEqual(reduced.ending_quantity, 100)
        self.assertIn("reduce quantity rounded below one lot", reduced.rejected_orders[0])

        def sell_fee(stamp: float) -> float:
            result = AShareBacktester(
                initial_cash=100_000,
                fees=FeeSchedule(0, 0, stamp, 0),
                slippage_bps=0,
            ).run(
                bars,
                {days[0]: SignalAction.BUY, days[1]: SignalAction.SELL},
                target_position_pct=1,
            )
            self.assertEqual(result.trades[1].side, TradeSide.SELL)
            return result.trades[1].fees

        self.assertGreater(sell_fee(0.001), sell_fee(0.0))

    def test_research_spec_uses_explicit_fees_and_refuses_account_files(self):
        spec = json.loads((Path(__file__).parent / "fixtures" / "research_backtest_spec.json").read_text(encoding="utf-8"))
        report = run_research_backtest_spec(spec)
        self.assertEqual(report["schema"], "xiao_a_research_backtest_v1")
        self.assertEqual(report["fees_source"], "explicit_spec_not_account_fees_file")
        self.assertFalse(report["vectorbt_used"])
        self.assertFalse(report["quantstats_used"])
        self.assertFalse(report["promoted_to_production"])
        self.assertEqual([trade["date"] for trade in report["trades"]], ["2026-01-05", "2026-01-06"])
        self.assertEqual(report["ending_quantity"], 0)
        self.assertGreater(sum(trade["fees"] for trade in report["trades"]), 0)
        self.assertFalse(report["uses_audit_direction_hit_as_pnl"])
        with self.assertRaisesRegex(ValueError, "账户费率"):
            run_research_backtest_spec({**spec, "account_fees_path": "private/account_fees.json"})
        reversed_bars = list(reversed(spec["bars"]))
        with self.assertRaisesRegex(ValueError, "严格递增"):
            run_research_backtest_spec({**spec, "bars": reversed_bars})


if __name__ == "__main__":
    unittest.main()
