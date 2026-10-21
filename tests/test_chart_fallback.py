import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xiao_a import dashboard


class ChartFallbackTests(unittest.TestCase):
    def test_backup_success_has_no_stale_disconnect_warning(self):
        response = json.dumps({'data': {'trends': [
            '2026-09-30 09:30,389.00,389.00,100,3890000'
        ]}}).encode('utf-8')
        requests = []

        def open_url(request, *, timeout):
            requests.append(request.full_url)
            if len(requests) == 1:
                raise ConnectionError('first host disconnected')
            return io.BytesIO(response)

        daily = SimpleNamespace(bars=[], warnings=[], status='fresh', provenance=None)
        with patch.object(dashboard._VOLUME_BASELINE_PROVIDER, 'fetch_daily', return_value=daily), \
                patch.object(dashboard, 'open_market_url', side_effect=open_url):
            result = dashboard._chart_payload('300750')
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(result['intraday']), 1)
        self.assertEqual(result['warnings'], [])
        self.assertIn('eastmoney-intraday', result['sources'])

    def test_total_intraday_failure_is_chinese_and_daily_survives(self):
        daily = SimpleNamespace(bars=[{'close': 389}], warnings=[], status='fresh', provenance=None)
        with patch.object(dashboard._VOLUME_BASELINE_PROVIDER, 'fetch_daily', return_value=daily), \
                patch.object(dashboard, 'open_market_url', side_effect=ConnectionError('closed')):
            result = dashboard._chart_payload('300750')
        self.assertEqual(result['daily'], [{'close': 389}])
        self.assertEqual(result['intraday'], [])
        self.assertEqual(result['warnings'], ['分时数据暂不可用，已尝试东方财富和腾讯接口；K线仍可查看'])
