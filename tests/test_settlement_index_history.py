"""Synthetic fixtures verify plumbing, never real prediction performance."""
import json
from datetime import date, datetime
from unittest.mock import Mock, patch

from xiao_a import dashboard
from xiao_a.data.kline import KlineResult, parse_tencent_kline_payload
from xiao_a.data.symbols import is_market_index
from xiao_a.models import DailyBar, DataStatus
from xiao_a.prediction_audit import TARGET_DEFINITION


def test_index_day_fallback_includes_csi300_without_stock_price_basis_fallback():
    rows = [['2026-10-08', '100', '101', '102', '99', '1000']]
    for symbol, key in [('SH.000300', 'sh000300'), ('SH.000001', 'sh000001'), ('SZ.399001', 'sz399001')]:
        result = parse_tencent_kline_payload(json.dumps({'data': {key: {'day': rows}}}), symbol)
        assert result.status == DataStatus.FRESH
        assert result.bars[0].close == 101
        assert is_market_index(symbol)
    result = parse_tencent_kline_payload(json.dumps({'data': {'sz000001': {'day': rows}}}), 'SZ.000001')
    assert result.status == DataStatus.UNAVAILABLE
    assert not is_market_index('SZ.000001')


def test_stock_qfq_still_preferred_and_unadjusted_request_keeps_day():
    payload = json.dumps({'data': {'sz300750': {
        'day': [['2026-10-08', '100', '101', '102', '99', '1000']],
        'qfqday': [['2026-10-08', '50', '51', '52', '49', '1000']],
    }}})
    assert parse_tencent_kline_payload(payload, 'SZ.300750').bars[0].open == 50
    assert parse_tencent_kline_payload(payload, 'SZ.300750', adjustment='none').bars[0].open == 100


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 15, 10, tzinfo=tz)


def run_settlement(*, cutoff='2026-10-07', benchmark_status=DataStatus.FRESH):
    days = [date(2026, 10, day) for day in (8, 9, 12, 13, 14, 15)]
    bars = [DailyBar(day, 100, 110, 99, 105) for day in days]
    stock = KlineResult(bars, DataStatus.FRESH)
    benchmark = KlineResult(bars, benchmark_status, errors=[] if benchmark_status == DataStatus.FRESH else ['fixture upstream unavailable'])
    provider = Mock()
    provider.fetch_daily.side_effect = lambda symbol, **kw: benchmark if symbol == 'SH.000300' else stock
    ledger = Mock()
    ledger.records.return_value = [{
        'prediction_id': 'fixture-1', 'symbol': 'SZ.300750',
        'information_cutoff': cutoff + 'T15:00:00+08:00', 'rule_score': .5,
        'target': dict(TARGET_DEFINITION),
        'last_settlement_attempt': {'attempt_at': '2026-10-15T09:00:00+08:00'},
    }]
    ledger.settle.return_value = {'prediction_id': 'fixture-1'}
    with patch.object(dashboard, 'datetime', Clock), patch.object(dashboard, '_PREDICTION_LEDGER', ledger), patch.object(dashboard, '_BACKTEST_KLINE_PROVIDER', provider):
        result = dashboard._settle_forward_predictions()
    return result, ledger


def test_same_day_failed_attempt_can_retry_with_complete_closed_window():
    result, ledger = run_settlement()
    assert result['settled_count'] == 1
    label = ledger.settle.call_args.args[1]
    assert label['entry_date'] == '2026-10-08'
    assert label['exit_date'] == '2026-10-14'  # today's unfinished bar excluded
    assert abs(label['target_return'] - .05) < 1e-10
    assert abs(label['benchmark_return'] - .05) < 1e-10
    assert label['execution_simulation'] == 'not_applied'


def test_immature_prediction_not_settled_even_when_today_has_fifth_bar():
    result, ledger = run_settlement(cutoff='2026-10-08')
    ledger.settle.assert_not_called()
    assert result['pending_count'] == 1
    assert '预测期限尚未完整' in result['pending_reasons'][0]['reason']


def test_degraded_benchmark_still_blocks_and_exposes_source_reason():
    result, ledger = run_settlement(benchmark_status=DataStatus.UNAVAILABLE)
    ledger.settle.assert_not_called()
    assert 'fixture upstream unavailable' in result['pending_reasons'][0]['reason']
