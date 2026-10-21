from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from math import tanh
from pathlib import Path
import unittest
from unittest.mock import patch

from xiao_a.auto import AutoAnalyzer
from xiao_a.data.fallback import parse_sina_quote_payload
from xiao_a.data.kline import KlineResult, parse_eastmoney_kline_payload, parse_tencent_kline_payload
from xiao_a.data.tencent import parse_tencent_quote_payload
from xiao_a.data.volume import SHANGHAI, average_prior_volume, projected_volume_ratio, trading_progress
from xiao_a.dashboard import _VOLUME_BASELINE_CACHE, _average_daily_volume, _volume_price_relation, _volume_ratio
from xiao_a.features import build_features
from xiao_a.models import DailyBar, DataStatus, Provenance, Quote


class VolumeCalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads((Path(__file__).parent / 'fixtures/volume_sample_20260930.json').read_text(encoding='utf-8'))
        cls.raw = fixture
        cls.quote = parse_tencent_quote_payload(fixture['tencent_quote']['payload'], '300750')
        cls.tencent = parse_tencent_kline_payload(fixture['tencent_daily']['payload'], '300750')
        cls.eastmoney = parse_eastmoney_kline_payload(fixture['eastmoney_daily']['payload'], '300750')
        cls.sina = parse_sina_quote_payload(fixture['sina_quote']['payload'], '300750')

    def setUp(self):
        _VOLUME_BASELINE_CACHE.clear()

    def tearDown(self):
        _VOLUME_BASELINE_CACHE.clear()

    def test_real_snapshots_match_lots_and_turnover_implied_price(self):
        self.assertEqual(self.quote.volume, 243948)
        self.assertEqual(self.tencent.bars[-1].volume, self.quote.volume)
        self.assertEqual(self.eastmoney.bars[-1].volume, self.quote.volume)
        self.assertLess(abs(self.sina.volume - self.quote.volume), 1)
        # If either source is incorrectly divided/multiplied by 100, the implied
        # average trade price falls far outside the day's actual high/low.
        for volume in (self.quote.volume, self.sina.volume, self.eastmoney.bars[-1].volume):
            average_price = self.sina.amount / (volume * 100)
            self.assertGreaterEqual(average_price, self.quote.low)
            self.assertLessEqual(average_price, self.quote.high)

    def test_quote_keeps_exchange_timestamp_and_reported_ratio(self):
        self.assertEqual(self.quote.provenance.as_of, datetime(2026, 9, 30, 16, 14, 30, tzinfo=SHANGHAI))
        self.assertEqual(self.sina.provenance.as_of, datetime(2026, 9, 30, 16, 29, 15, tzinfo=SHANGHAI))
        self.assertEqual(self.quote.volume_ratio, .83)

    def test_reported_ratio_has_priority_without_history_request(self):
        with patch('xiao_a.dashboard._VOLUME_BASELINE_PROVIDER.fetch_daily', side_effect=AssertionError('unneeded history')):
            self.assertEqual(_volume_ratio(self.quote), .83)

    def test_holiday_fallback_excludes_quote_day_and_matches_reported_ratio(self):
        # Reopen September 30 data on October 2: the baseline must end on Sept 29.
        quote = replace(self.quote, volume_ratio=None)
        for provider_result in (self.tencent, self.eastmoney):
            _VOLUME_BASELINE_CACHE.clear()
            with patch('xiao_a.dashboard._VOLUME_BASELINE_PROVIDER.fetch_daily', return_value=provider_result):
                with patch('xiao_a.dashboard.datetime') as wall_clock:
                    wall_clock.now.side_effect = AssertionError('must not use current time')
                    average = _average_daily_volume(quote.symbol, date(2026, 9, 30))
                    ratio = _volume_ratio(quote)
                self.assertAlmostEqual(average, 293036.6)
                self.assertAlmostEqual(ratio, 243948 / 293036.6)
                self.assertEqual(round(ratio, 2), .83)
                self.assertEqual(_volume_price_relation(389, 392.91, ratio), '量平价跌')

    def test_real_second_stock_ratio_and_relation(self):
        quote = parse_tencent_quote_payload(self.raw['tencent_quote']['payload'], '300059')
        self.assertEqual(_volume_ratio(quote), .67)
        self.assertEqual(_volume_price_relation(quote.price, quote.previous_close, _volume_ratio(quote)), '缩量下跌')

    def test_history_with_or_without_quote_day_gives_same_baseline(self):
        with_day = average_prior_volume(self.tencent.bars, date(2026, 9, 30))
        without_day = average_prior_volume(self.tencent.bars[:-1], date(2026, 9, 30))
        self.assertEqual(with_day, without_day)
        self.assertIsNone(average_prior_volume(self.tencent.bars[-4:], date(2026, 9, 30)))

    def test_intraday_projection_uses_quote_time(self):
        quote = replace(self.quote, volume_ratio=None, volume=73259.15,
                        provenance=replace(self.quote.provenance, as_of=datetime(2026, 9, 30, 10, 30, tzinfo=SHANGHAI)))
        with patch('xiao_a.dashboard._VOLUME_BASELINE_PROVIDER.fetch_daily', return_value=self.tencent):
            self.assertAlmostEqual(_volume_ratio(quote), 1.0)

    def test_unknown_or_preopen_timestamp_does_not_guess_ratio(self):
        self.assertIsNone(_volume_ratio(replace(self.quote, provenance=None)))
        unknown = replace(self.quote, provenance=replace(self.quote.provenance, as_of=None))
        self.assertIsNone(_volume_ratio(unknown))
        preopen = replace(self.quote, provenance=replace(self.quote.provenance, as_of=datetime(2026, 9, 30, 9, 25, tzinfo=SHANGHAI)))
        self.assertIsNone(_volume_ratio(preopen))

    def test_progress_uses_exchange_timezone_and_excludes_lunch(self):
        self.assertEqual(trading_progress(datetime(2026, 9, 30, 10, 30, tzinfo=SHANGHAI)), .25)
        self.assertEqual(trading_progress(datetime(2026, 9, 30, 12, 0, tzinfo=SHANGHAI)), .5)
        self.assertEqual(trading_progress(datetime(2026, 9, 30, 14, 0, tzinfo=SHANGHAI)), .75)
        self.assertEqual(trading_progress(datetime(2026, 9, 30, 2, 30, tzinfo=timezone.utc)), .25)
        self.assertEqual(trading_progress(self.quote.provenance.as_of), 1)
        self.assertIsNone(trading_progress(None))
        self.assertIsNone(trading_progress(datetime(2026, 9, 30, 9, 30, tzinfo=SHANGHAI)))
        self.assertAlmostEqual(trading_progress(datetime(2026, 9, 30, 9, 30, 30, tzinfo=SHANGHAI)), .5 / 240)

    def test_invalid_inputs_do_not_turn_into_a_normal_ratio(self):
        for volume, average, fraction in [(0, 100, .5), (100, 0, .5), (100, 100, None), (100, 100, 0), (float('nan'), 100, .5)]:
            self.assertIsNone(projected_volume_ratio(volume, average, fraction))
        self.assertEqual(projected_volume_ratio(25000, 100000, .25), 1)

    def test_holiday_does_not_manufacture_new_daily_bar(self):
        bars = AutoAnalyzer._apply_live_quote(self.tencent.bars, self.quote)
        self.assertEqual(len(bars), len(self.tencent.bars))
        self.assertEqual(bars[-1].trading_date, date(2026, 9, 30))
        self.assertEqual(bars[-1].volume, 243948)

    def test_only_actual_new_session_is_appended_with_its_own_high_low(self):
        previous = self.tencent.bars[:-1]
        bars = AutoAnalyzer._apply_live_quote(previous, self.quote)
        self.assertEqual(len(bars), len(previous) + 1)
        self.assertEqual(bars[-1].trading_date, date(2026, 9, 30))
        self.assertEqual(bars[-1].low, 387.01)
        self.assertEqual(bars[-1].high, 403.99)
        self.assertIs(AutoAnalyzer._apply_live_quote(previous, replace(self.quote, provenance=None)), previous)

    def test_older_quote_cannot_overwrite_newer_history(self):
        old_quote = replace(self.quote, provenance=replace(self.quote.provenance, as_of=datetime(2026, 9, 29, 15, tzinfo=SHANGHAI)))
        self.assertIs(AutoAnalyzer._apply_live_quote(self.tencent.bars, old_quote), self.tencent.bars)

    def test_model_partial_day_feature_is_independent_of_report_generation_date(self):
        session = date(2026, 9, 30)
        history = [DailyBar(session - timedelta(days=days), 10, 10, 10, 10, volume=100000, previous_close=10) for days in range(20, 0, -1)]
        history.append(DailyBar(session, 10, 10.4, 10, 10.4, volume=25000, previous_close=10))
        timestamp = datetime(2026, 9, 30, 10, 30, tzinfo=SHANGHAI)
        quote = Quote('SZ.300750', 'test', 10.4, 10, volume=25000, provenance=Provenance('test', '', timestamp, timestamp))
        result = build_features(history, quote=quote)
        self.assertAlmostEqual(result.values['volume_price_signal'], tanh(1) / 2)


if __name__ == '__main__':
    unittest.main()
