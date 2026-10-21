"""Synthetic fixed signals establish independence, not real forecast accuracy."""
from datetime import datetime, timezone
import unittest

from xiao_a.decision import AdvisorEngine
from xiao_a.market_prediction import independent_market_prediction
from xiao_a.models import MarketSnapshot, SignalAction, StrategySignal
from xiao_a.risk import RiskContext


class FixedRule:
    name = "synthetic-fixed-rule"

    def evaluate(self, snapshot):
        return StrategySignal(self.name, .6, .8, SignalAction.BUY, "synthetic signal")


class MarketPredictionTests(unittest.TestCase):
    def test_account_risk_can_change_advice_but_not_independent_forecast(self):
        snapshot = MarketSnapshot("SZ.300750", datetime(2026, 10, 7, tzinfo=timezone.utc))
        engine = AdvisorEngine([FixedRule()])
        ordinary = engine.decide(snapshot, RiskContext(position_pct=0, cash_available=100000))
        concentrated = engine.decide(snapshot, RiskContext(position_pct=.7, cash_available=0,
            portfolio_drawdown_pct=.5, high_severity_flags=["禁止新增仓位"]))
        self.assertEqual(ordinary.final_action, SignalAction.BUY)
        self.assertNotEqual(ordinary.final_action, concentrated.final_action)
        self.assertEqual(independent_market_prediction(ordinary), independent_market_prediction(concentrated))
        self.assertEqual(concentrated.model_confidence, .8)
        self.assertEqual(independent_market_prediction(concentrated)["direction"], "up")

    def test_no_return_or_probability_is_invented_from_a_score(self):
        prediction = independent_market_prediction({"score": 1, "raw_action": "buy", "model_confidence": 1})
        self.assertIsNone(prediction["expected_return"])
        self.assertIsNone(prediction["return_interval"])
        self.assertIsNone(prediction["raw_probability"])
        self.assertIsNone(prediction["calibrated_probability"])

    def test_old_risk_adjusted_action_and_confidence_are_not_used_as_independent_output(self):
        prediction = independent_market_prediction({"score": .6, "final_action": "sell", "confidence": .2})
        self.assertEqual(prediction["direction"], "up")
        self.assertIsNone(prediction["model_action"])
        self.assertIsNone(prediction["rule_conformance_score"])


if __name__ == "__main__":
    unittest.main()
