import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from src.execution.history import TradeHistory
from src.execution.telemetry import stage,safe_reason
from src.variational.execution import VariationalExecutionAdapter
from src.common.market_data import utc_now

class TelemetryTests(unittest.TestCase):
    def test_unknown_errors_are_redacted_and_stage_is_timed(self):
        with tempfile.TemporaryDirectory() as tmp:
            h=TradeHistory(Path(tmp)/'events.sqlite')
            with self.assertRaises(ValueError):
                with stage(h,'V 数量报价','ETH'):raise ValueError('secret-token=abcdef')
            event=h.recent()[0]
            self.assertEqual(event['event'],'stage_failed')
            self.assertEqual(event['detail']['stage'],'V 数量报价')
            self.assertGreaterEqual(event['detail']['elapsed_ms'],0)
            self.assertNotIn('secret',str(event))
    def test_expected_reason_is_preserved(self):
        self.assertEqual(safe_reason(ValueError('没有符合净价差与金额上限的深度')),'没有符合净价差与金额上限的深度')

    def test_window_decay_rejection_preserves_only_derived_numeric_values(self):
        message = '首腿未提交：近期平仓盘口恶化风险 35.17 bps，扣除后预计对冲净空间 -14.65 bps，未达收益门槛'
        self.assertEqual(safe_reason(ValueError(message)), message)
        for unsafe in (message+' secret-token=abcdef', message.replace('35.17', 'secret'),
                       message.replace('35.17', '1'*20+'.17')):
            self.assertNotEqual(safe_reason(ValueError(unsafe)), unsafe)

    def test_arcus_catalog_errors_preserve_only_safe_symbols(self):
        for prefix in ('Arcus 未找到可交易市场：', 'Arcus 市场当前不可交易：'):
            self.assertEqual(safe_reason(ValueError(prefix+'TRX')), prefix+'TRX')
            for unsafe in ('TRX secret=token', 'TRX\nsecret', 'A'*65):
                self.assertNotEqual(safe_reason(ValueError(prefix+unsafe)), prefix+unsafe)

    def test_arcus_minimum_failure_keeps_only_expected_numeric_fields(self):
        message = 'Arcus 最小下单条件不满足: 数量 0.01 < 0.02 或名义金额 9.123456 < 10'
        self.assertEqual(safe_reason(ValueError(message)), message)
        for unsafe in (message + ' secret-token=abcdef', message.replace('0.02', 'secret-token'),
                       message.replace('Arcus', 'secret-token'), message.replace('0.01', '1' * 41)):
            self.assertNotEqual(safe_reason(ValueError(unsafe)), unsafe)
    def test_base_quantity_quote_needs_no_public_price_or_order(self):
        form,reader,public=Mock(),Mock(),Mock()
        form.assert_market=Mock(return_value='ETH')
        reader.executable_quote.return_value={'symbol':'ETH','size':.004,'bid':2650,'ask':2651,'updated_at':utc_now(),'executable':True,'quote_id':'test','instrument_type':'perpetual','fee_bps':0}
        adapter=VariationalExecutionAdapter(form,reader,None,'test',{'account_verified':True,'receipt_schema_verified':True},enabled=True,public=public)
        adapter.get_executable_quote('ETH',.004)
        form.prepare_quote.assert_called_once_with('ETH',.004,mode='ETH')
        public.get_market_price.assert_not_called();form.click_submit.assert_not_called()
    def test_runtime_preview_never_enters_worker_or_submission(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        from src.execution.runtime import _run_verified
        config=Mock();config.get.return_value=None
        config.get_lighter_config.return_value={};config.get_variational_config.return_value={};config.get_strategy_config.return_value={}
        profile={'symbol':'ETH','browser_debugger_address':'127.0.0.1:9222','response_schema':{},'account_id':'test','lighter_fee_bps':0}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ',{'LIGHTER_ACCOUNT_INDEX':'1','LIGHTER_API_KEY_INDEX':'4','LIGHTER_API_PRIVATE_KEY':'test'}))
            mocks={name:stack.enter_context(patch('src.execution.runtime.'+name)) for name in ('attach_browser','select_market_tab','SDKLoop','LighterApiTrader','OrderJournal','LighterExecutionAdapter','OmniResponseReader','VariationalPublicClient','VariationalExecutionAdapter','OmniForm','ArbitrageStrategy','ExecutionWorker')}
            strategy=mocks['ArbitrageStrategy'].return_value
            strategy.plan_entry.return_value=({'quantity':.004},None,None,None)
            result=_run_verified(config,profile,Path(tmp),plan_only=True)
            self.assertTrue(result['ready']);self.assertFalse(result['orders_submitted'])
            mocks['ExecutionWorker'].assert_not_called()
            strategy.execute_arbitrage.assert_not_called();strategy._save.assert_not_called()
            for name in ('VariationalExecutionAdapter','LighterExecutionAdapter'):
                mocks[name].return_value.place_order.assert_not_called()

    def test_history_recent_supports_descending_offset_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = TradeHistory(Path(tmp) / 'events.sqlite')
            for symbol in ('BTC', 'ETH', 'SOL'):
                history.append('planned', symbol=symbol)
            first = history.recent(2)
            second = history.recent(2, 2)
            self.assertEqual(history.count(), 3)
            self.assertEqual([row['detail']['symbol'] for row in first], ['SOL', 'ETH'])
            self.assertEqual([row['detail']['symbol'] for row in second], ['BTC'])

    def test_close_receipt_prices_are_recovered_as_vwap(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = TradeHistory(Path(tmp) / 'events.sqlite')
            history.append('close_receipt', position_id='p1', venue='arcus',
                           filled_size=1, average_price=100)
            history.append('close_receipt', position_id='p1', venue='arcus',
                           filled_size=3, average_price=104)
            self.assertEqual(history.receipt_prices(['p1']), {'p1': {'arcus': 103.0}})
