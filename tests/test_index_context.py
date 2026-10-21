import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from xiao_a import dashboard


def test_index_context_never_runs_stock_model_or_records_prediction():
    handler = object.__new__(dashboard.DashboardHandler)
    handler.path = '/api/auto-evaluate'
    body = json.dumps({'symbol': 'SH.000001'}).encode()
    handler.headers = {'Content-Length': str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler._try_remote = Mock(return_value=False)
    handler._send_json = Mock()
    analyzer = Mock()
    analyzer.supplemental_provider.fetch_market_limit_up_pool.return_value = SimpleNamespace(
        data={'limit_up_pool': {'available': True, 'count': 10}})
    with patch.object(dashboard, '_AUTO_ANALYZER', analyzer), patch.object(dashboard, '_PREDICTION_LEDGER') as ledger:
        handler.do_POST()
    analyzer.analyze.assert_not_called()
    ledger.append.assert_not_called()
    response = handler._send_json.call_args.args[0]
    assert response['scope'] == 'market_context'
    assert response['supplemental']['limit_up_pool']['count'] == 10
    assert 'decision' not in response
