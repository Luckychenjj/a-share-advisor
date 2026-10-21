import json
from pathlib import Path
import unittest
from unittest.mock import patch

from xiao_a.data.limit_pool import parse_cls, parse_eastmoney, enrich_pool
from xiao_a.data.supplemental import SupplementalDataProvider, parse_fund_flow_payload, parse_cls_fund_flow

FIXTURES = Path(__file__).parent / 'fixtures' / 'supplemental'


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


class SupplementalTests(unittest.TestCase):
    def test_real_pool_complete_and_correct_consecutive_count(self):
        pool = parse_cls(fixture('cls_pool.json'), '600825')
        self.assertEqual(pool['count'], 56)
        self.assertEqual(pool['max_continuous'], 7)
        self.assertEqual(pool['trade_date'], '2026-09-30')
        self.assertEqual(pool['top'][0]['name'], '新华传媒')
        self.assertEqual(len(pool['top']), 10)
        self.assertEqual(pool['selected']['continuous'], 7)
        self.assertAlmostEqual(next(x for x in pool['items'] if x['name'] == '康希诺')['change_pct'], 20)

    def test_enrichment_preserves_full_universe_and_actual_units(self):
        cls = parse_cls(fixture('cls_pool.json'), '002058')
        em = parse_eastmoney(fixture('eastmoney_pool.json'), '002058')
        pool = enrich_pool(cls, em, '002058')
        self.assertEqual(len(pool['items']), 56)
        stock = pool['selected']
        self.assertEqual(stock['price'], 20.2)
        self.assertAlmostEqual(stock['turnover_pct'], 1.76576316)
        self.assertEqual(stock['first_limit_time'], '09:25:00')
        self.assertEqual(stock['reopen_count'], 0)
        self.assertEqual(stock['sector'], '电池')
        self.assertEqual(stock['seal_amount'], 69384959)
        missing = [x for x in pool['items'] if x.get('turnover_pct') is None]
        self.assertEqual(len(missing), 4)

    def test_different_dates_never_enriched(self):
        cls = parse_cls(fixture('cls_pool.json'), '300750')
        em = parse_eastmoney(fixture('eastmoney_pool.json'), '300750')
        em['trade_date'] = '2026-09-29'
        self.assertIs(enrich_pool(cls, em, '300750'), cls)

    def test_incomplete_pool_rejected(self):
        raw = json.loads(fixture('eastmoney_pool.json'))
        raw['data']['pool'].pop()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            parse_eastmoney(json.dumps(raw), '300750')

    def test_current_flow_is_not_five_day_flow(self):
        flow, provenance = parse_fund_flow_payload(fixture('funds.json'))
        self.assertEqual(flow['main_net_inflow'], -113567824)
        self.assertIsNone(flow['five_day_net_inflow'])
        self.assertEqual(provenance.as_of.date().isoformat(), '2026-09-30')

    def test_invalid_and_duplicate_history_does_not_inflate_five_days(self):
        rows = [f'2026-09-{day},100,0,0,0,0' for day in range(25, 30)]
        rows += [rows[-1], '2026-09-30,nan,0,0,0,0', 'not-a-date,100,0,0,0,0']
        flow, _ = parse_fund_flow_payload(json.dumps({'data': {'klines': rows}}))
        self.assertEqual(flow['five_day_net_inflow'], 500)
        self.assertEqual(flow['trade_date'], '2026-09-29')

    def test_backup_current_succeeds_despite_history_failure(self):
        provider = SupplementalDataProvider()
        calls = []
        def get(url, params, **kwargs):
            calls.append(url)
            if 'push2delay' in url:
                return fixture('funds.json')
            raise OSError('unavailable')
        with patch.object(provider, '_get_json', side_effect=get):
            result, _ = provider._eastmoney_fund_flow('300750')
        self.assertTrue(result['available'])
        self.assertIsNone(result['five_day_net_inflow'])
        self.assertEqual(len(calls), 3)

    def test_wrong_stock_funds_rejected(self):
        with patch.object(SupplementalDataProvider, '_get_json', return_value=fixture('funds.json')):
            with self.assertRaisesRegex(ValueError, 'symbol mismatch'):
                SupplementalDataProvider()._eastmoney_fund_flow('300059')

    def test_cls_real_funds_and_five_day_aggregate(self):
        result, provenance = parse_cls_fund_flow(fixture('cls_funds.json'))
        self.assertEqual(result['main_net_inflow'], -105784682)
        self.assertEqual(result['five_day_net_inflow'], -4870240823)
        self.assertEqual(result['main_net_inflow'], result['super_large_inflow'] + result['large_inflow'])
        self.assertEqual(provenance.as_of.date().isoformat(), '2026-09-30')

    def test_cls_failure_switches_to_complete_em_observation(self):
        provider = SupplementalDataProvider()
        with patch.object(provider, '_get_json', side_effect=OSError('offline')), patch.object(
                provider, '_eastmoney_fund_flow', return_value=({'source': 'backup'}, None)) as backup:
            result, _ = provider._fund_flow('300750')
        self.assertEqual(result['source'], 'backup')
        backup.assert_called_once_with('300750')
