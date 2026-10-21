from __future__ import annotations

import unittest

from xiao_a.performance import summarize_equity_history


class PerformanceTests(unittest.TestCase):
    def test_reports_total_return_and_peak_to_trough_drawdown(self) -> None:
        result = summarize_equity_history([
            {"date": "2026-01-01", "equity": 1000},
            {"date": "2026-01-02", "equity": 1200},
            {"date": "2026-01-03", "equity": 900},
        ])
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["total_return"], -0.1)
        self.assertAlmostEqual(result["max_drawdown"], 0.25)
        self.assertEqual(result["positive_interval_pct"], 0.5)
        self.assertIsNone(result["sharpe_ratio"])

    def test_deduplicates_days_and_requires_two_observations(self) -> None:
        result = summarize_equity_history([
            {"date": "2026-01-01", "equity": 100},
            {"date": "2026-01-01", "equity": 110},
            {"date": "invalid", "equity": 90},
        ])
        self.assertFalse(result["available"])
        self.assertEqual(result["observations"], 1)
        self.assertIsNone(result["total_return"])

    def test_adds_annualized_statistics_after_twenty_intervals(self) -> None:
        snapshots = [
            {"date": f"2026-01-{day:02}", "equity": 1000 + day}
            for day in range(1, 22)
        ]
        result = summarize_equity_history(snapshots)
        self.assertIsNotNone(result["annualized_volatility"])
        self.assertIsNotNone(result["sharpe_ratio"])


if __name__ == "__main__":
    unittest.main()
