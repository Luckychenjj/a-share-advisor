from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from xiao_a.portfolio_store import PortfolioStore


class PortfolioStoreTests(unittest.TestCase):
    def test_save_and_load_round_trip(self) -> None:
        path = Path(".test_portfolio_store.json")
        path.unlink(missing_ok=True)
        try:
            store = PortfolioStore(path)
            self.assertIsNone(store.load())
            first = store.save(
                [{"symbol": "300059", "quantity": "1000", "average_cost": "20.0"}],
                5000,
            )
            second = store.save(
                [{"symbol": "300059", "quantity": 500, "average_cost": None}],
                1000,
            )

            self.assertEqual(first["version"], 1)
            self.assertEqual(second["version"], 2)
            self.assertEqual(store.load(), second)
            self.assertEqual(second["holdings"][0]["quantity"], 500)
            self.assertIsNone(second["holdings"][0]["average_cost"])
        finally:
            path.unlink(missing_ok=True)

    def test_sell_loss_is_diluted_into_remaining_cost(self) -> None:
        path = Path(".test_portfolio_trade.json")
        path.unlink(missing_ok=True)
        try:
            store = PortfolioStore(path)
            state = store.save([{"symbol": "300750", "quantity": 200, "average_cost": 450.0}], 100000)
            # This test represents 200 shares carried into today's session.
            state["day_open"] = {
                "date": datetime.now().date().isoformat(),
                "holdings": [{"symbol": "300750", "quantity": 200, "average_cost": 450.0}],
                "cash": 100000,
            }
            store._write(state)
            state, _ = store.apply_trade("300750", "sell", 100, 400.0, 0.00018)
            remaining_cost = state["holdings"][0]["average_cost"]
            self.assertAlmostEqual(remaining_cost, 500.276, places=6)
            state, _ = store.apply_trade("300750", "buy", 100, 401.0, 0.00018)
            self.assertAlmostEqual(state["holdings"][0]["average_cost"], 450.676095, places=6)
        finally:
            path.unlink(missing_ok=True)

    def test_same_day_buy_cannot_be_sold_and_sellable_quantity_is_zero(self) -> None:
        path = Path(".test_portfolio_t1.json")
        path.unlink(missing_ok=True)
        try:
            store = PortfolioStore(path)
            store.save([], 100000)
            store.apply_trade("300750", "buy", 100, 10.0, 0.0003)
            self.assertEqual(store.sellable_quantity("300750"), 0)
            with self.assertRaisesRegex(ValueError, r"T\+1"):
                store.apply_trade("300750", "sell", 100, 10.0, 0.0003)
        finally:
            path.unlink(missing_ok=True)

    def test_equity_snapshot_is_upserted_and_survives_account_edits(self) -> None:
        path = Path(".test_portfolio_equity.json")
        path.unlink(missing_ok=True)
        try:
            store = PortfolioStore(path)
            store.save([], 1000)
            store.record_equity_snapshot("2026-10-06", 1000)
            store.record_equity_snapshot("2026-10-06", 1100)
            state = store.save([], 1200)
            self.assertEqual(state["equity_history"], [{"date": "2026-10-06", "equity": 1100.0}])
            self.assertIsNone(store.record_equity_snapshot("2026-10-07", float("nan")))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
