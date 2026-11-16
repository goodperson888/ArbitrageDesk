import tempfile
import sqlite3
import unittest
from pathlib import Path

from src.execution.history import TradeHistory
from src.execution.journal import OrderJournal
from src.web.rollback_explanation import attach_rollback_explanations
from src.execution.telemetry import safe_reason


class RollbackExplanationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.history = TradeHistory(self.root / 'hedge_events.sqlite')
        self.row = dict(id='position', execution_id='execution', symbol='UNI',
                        hedge_status='single_leg_recovered', status='closed',
                        variational_venue='lighter', lighter_venue='arcus',
                        variational_side='short', lighter_side='buy',
                        entry_size_variational=2, entry_size_lighter=0,
                        entry_price_variational=100, entry_price_lighter=99,
                        close_reason='第二腿未成对·回补第一腿')

    def append(self, event, **fields):
        self.history.append(event, position_id='position', execution_id='execution', **fields)

    def report(self):
        result = {'closed_positions': [dict(self.row)]}
        attach_rollback_explanations(result, self.root)
        return result['closed_positions'][0]['rollback_explanation']

    def test_protection_skip_is_never_labeled_submitted(self):
        self.history.append('planned', execution_id='execution', quantity=2, net_edge_bps=10)
        self.append('entry_receipt', venue='lighter', filled_size=2, average_price=100,
                    requested_size=2, confirmed=True, order_id='first')
        self.append('hedge_decision', observed_price=101, protected_bound=99,
                    net_edge_bps=4, observed_net_edge_bps=-100, hedge_min_net_bps=5, decision='unwind')
        self.append('hedge_leg_skipped', reason='第二腿重新报价超出保护边界，未提交；立即回补第一腿')
        report = self.report()
        self.assertEqual(report['legs'][1]['requested_size'], 0)
        self.assertEqual(report['legs'][1]['planned_size'], 2)
        self.assertIn('未提交（保护拦截）', report['text'])
        self.assertAlmostEqual(report['observed_gross_bps'], -10000 / 101)
        self.assertIn('计划净价差：10 bps', report['text'])
        self.assertIn('第1腿 Lighter 卖出', report['text'])
        self.assertIn('第2腿 Arcus 买入', report['text'])
        self.assertIn('当时报价下对冲净价差：-100 bps', report['text'])

    def test_category_constraints_and_execution_window_are_visible_without_inventing_old_evidence(self):
        self.append('hedge_decision', decision='unwind', reason_code='edge',
                    constraint_failures=['edge', 'price'], execution_window_ms=1250)
        self.append('hedge_leg_skipped', reason='第二腿净价差不足、价格保护越界', reason_code='edge')
        report = self.report()
        self.assertEqual(report['reason_code'], 'edge')
        self.assertIn('回补分类：净价差不足', report['text'])
        self.assertIn('未通过条件：净价差不足、价格保护越界', report['text'])
        self.assertIn('首腿提交至第二腿复核：1250 ms', report['text'])

    def test_target_return_records_explain_complete_costs_and_frozen_target(self):
        self.append('planned', quantity=2, net_edge_bps=12, edge_basis='target_return',
                    target_context=dict(target_mode='center', target_bps=-30))
        self.append('hedge_decision', edge_basis='target_return', net_edge_bps=5,
                    observed_net_edge_bps=4, hedge_min_net_bps=6, decision='unwind')
        report = self.report()
        self.assertIn('计划预计净回归空间：12 bps', report['text'])
        self.assertIn('冻结回归目标：历史中心 -30 bps', report['text'])
        self.assertIn('当时报价下预计净回归空间：4 bps', report['text'])
        self.assertIn('包含目标平仓成本与资金费预算', report['text'])
        self.assertNotIn('未包含后续平仓成本', report['text'])

    def test_submitted_zero_fill_and_reason_are_distinct(self):
        self.append('entry_receipt', venue='arcus', filled_size=0, requested_size=2,
                    order_id='second', status='canceled', reason='保护限价内无成交')
        self.append('hedge_decision', decision='hedge', observed_price=99, protected_bound=99.1)
        report = self.report()
        self.assertIn('平台已接单，0 成交', report['text'])
        self.assertEqual(report['reason'], '保护限价内无成交')
        self.assertIn('最后一次对冲评估允许提交', report['text'])
        self.assertIsNone(report['legs'][1]['average_price'])

    def test_legacy_rejection_does_not_prove_submission(self):
        self.append('entry_receipt', venue='arcus', filled_size=0, status='rejected',
                    reason='ValueError：检查未通过，详见阶段和代码位置')
        report = self.report()
        self.assertIn('平台接单未知（拒绝回执）', report['text'])
        self.assertIsNone(report['legs'][1]['requested_size'])
        self.assertNotIn('平台已接单，0 成交', report['text'])
        self.assertIn('第二腿决策时毛价差：未知', report['text'])

    def test_old_missing_evidence_is_unknown_and_no_phantom_price(self):
        report = self.report()
        self.assertIn('提交情况未知', report['text'])
        self.assertIsNone(report['legs'][1]['average_price'])
        self.assertIsNone(report['legs'][0]['requested_size'])
        self.assertIn('保护限价下对冲净价差：未知', report['text'])

    def test_journal_recovers_requested_quantity_with_actual_venue(self):
        journal = OrderJournal(self.root / 'execution_orders.sqlite')
        journal.reserve('arcus', 'not-exposed', 'position-l', dict(size='1.5'))
        report = self.report()
        self.assertEqual(report['legs'][1]['requested_size'], 1.5)
        self.assertIn('平台接单未知', report['text'])
        self.assertNotIn('not-exposed', report['text'])

    def test_partial_fill_and_explicit_rollback_cause(self):
        self.row.update(entry_size_lighter=1, entry_price_lighter=99)
        self.append('entry_receipt', venue='arcus', requested_size=2, filled_size=1,
                    average_price=99, status='partial', order_id='partial')
        self.append('entry_rollback', reason='第二腿部分成交，两腿实际成交数量不一致')
        report = self.report()
        self.assertEqual(report['legs'][1]['filled_size'], 1)
        self.assertEqual(report['legs'][1]['requested_size'], 2)
        self.assertIn('两腿实际成交数量不一致', report['reason'])

    def test_journal_proves_not_sent_and_recovers_safe_reason(self):
        journal = OrderJournal(self.root / 'execution_orders.sqlite')
        order, _ = journal.reserve('arcus', 'hidden-account', 'position-l', dict(size='2'))
        journal.finish(order['seq'], dict(confirmed=True, submitted=False, status='rejected',
            filled_size=0, error='提交前账户检查失败，未发送订单：报价时效、数量、合约或执行资格不匹配'))
        self.append('entry_receipt', venue='arcus', filled_size=0, status='rejected', reason='旧版泛化提示')
        report = self.report()
        self.assertIn('未提交（下单检查未通过）', report['text'])
        self.assertEqual(report['reason'], '报价时效、数量、合约或执行资格不匹配')

    def test_tick_rejection_keeps_only_price_and_tick_not_raw_body(self):
        raw = "secret=do-not-expose validation error on field 'price': 0.12347 is not a multiple of tick size 0.00005 for its price tier"
        journal = OrderJournal(self.root / 'execution_orders.sqlite')
        order, _ = journal.reserve('arcus', 'hidden-account', 'position-l', dict(size='2'))
        journal.finish(order['seq'], dict(confirmed=True, submitted=True, status='rejected', filled_size=0, error=raw))
        self.append('entry_receipt', venue='arcus', filled_size=0, status='rejected')
        report = self.report()
        self.assertIn('已发送订单，平台未接单', report['text'])
        self.assertEqual(report['reason'], '订单价格 0.12347 不符合该价格档最小变动单位 0.00005')
        self.assertNotIn('do-not-expose', report['text'])
        self.assertNotIn('do-not-expose', safe_reason(ValueError(raw)))

    def test_events_from_another_execution_never_supply_a_reason(self):
        self.history.append('hedge_leg_skipped', position_id='other', execution_id='other', reason='不属于这笔')
        self.assertNotIn('不属于这笔', self.report()['text'])

    def test_legacy_skip_still_supplies_recorded_quote_and_bound(self):
        self.append('hedge_leg_skipped', observed_price=101, protected_bound=99,
                    reason='第二腿重新报价超出保护边界，未提交')
        report = self.report()
        self.assertAlmostEqual(report['observed_gross_bps'], -10000 / 101)
        self.assertIn('第二腿当时报价 101；保护限价 99', report['text'])

    def test_zero_fill_cancellation_explains_quantity_mismatch(self):
        self.append('entry_receipt', venue='arcus', filled_size=0, order_id='zero', status='canceled')
        self.assertIn('第二腿取消回执，成交 0，未形成等量对冲', self.report()['reason'])

    def test_canceled_sell_shows_budget_rounding_recovery_and_monotonic_timing(self):
        self.row.update(variational_side='long', lighter_side='sell')
        journal = OrderJournal(self.root / 'execution_orders.sqlite')
        order, _ = journal.reserve('arcus', 'hidden-account', 'position-l',
            dict(size='128.5', price='0.093293', reference_price='0.093294', time_in_force='IOC'))
        journal.finish(order['seq'], dict(order_id='second', confirmed=True, submitted=True,
            status='canceled', filled_size=0, raw_status='CANCELED', platform_reason_reported=False))
        self.append('entry_receipt', venue='lighter', filled_size=128.5, average_price=.09321,
                    requested_size=128.5, confirmed=True, execution_total_ms=1000)
        self.append('hedge_decision', decision='hedge', observed_price=.093294,
                    second_reference_price=.093311, price_budget_bound=.0932923378,
                    protected_bound=.0932923378, execution_total_ms=1015)
        self.append('stage_started', stage='Arcus 签名发送', execution_total_ms=1200)
        self.append('entry_receipt', venue='arcus', filled_size=0, order_id='second',
                    requested_size=128.5, confirmed=True, status='canceled', execution_total_ms=1400)
        for _ in range(2):
            self.append('close_receipt', venue='lighter', filled_size=128.5, average_price=.09322,
                        order_id='recovery', confirmed=True)
        report = self.report()
        guard = report['price_guard']
        self.assertAlmostEqual(guard['budget_bps'], 2)
        self.assertAlmostEqual(guard['consumed_bps'], 1.82186451758)
        self.assertAlmostEqual(guard['remaining_budget_bps'], .1781679422)
        self.assertAlmostEqual(guard['remaining_limit_bps'], .1071880292)
        self.assertEqual(report['legs'][0]['rollback_filled_size'], 128.5)
        self.assertEqual(report['legs'][1]['raw_status'], 'CANCELED')
        self.assertEqual(report['timings']['first_receipt_to_second_send_ms'], 200)
        self.assertIn('第二腿复核→发送：185 ms', report['text'])
        self.assertIn('第1腿回补平仓成交：128.5 @ 0.09322', report['text'])
        self.assertIn('平台取消/拒绝原因：未提供', report['text'])
        self.assertIn('原始状态 CANCELED；指令 IOC', report['text'])
        self.assertIn('第二腿实际提交限价 0.093293', report['text'])
        self.assertIn('不能确定是价格变化还是深度不足', report['text'])
        self.assertNotIn('hidden-account', report['text'])

    def test_legacy_timestamps_restore_delay_and_observation_is_bound_to_order_id(self):
        self.append('entry_receipt', venue='lighter', filled_size=2, confirmed=True)
        self.append('hedge_decision', decision='hedge')
        self.append('stage_started', stage='Arcus 签名发送')
        self.append('entry_receipt', venue='arcus', filled_size=0, order_id='second', status='canceled')
        with sqlite3.connect(self.history.path) as db:
            db.execute("UPDATE events SET timestamp='2026-11-06T06:36:28.125429+00:00' WHERE id=1")
            db.execute("UPDATE events SET timestamp='2026-11-06T06:36:28.313043+00:00' WHERE id=3")
        self.append('platform_order_observed', venue='arcus', order_id='foreign',
                    raw_status='MARGIN_CANCELED', time_in_force='FOK')
        report = self.report()
        self.assertAlmostEqual(report['timings']['first_receipt_to_second_send_ms'], 187.614)
        self.assertIsNone(report['legs'][1]['raw_status'])
        self.assertIn('旧记录未保存，无法确认平台是否提供', report['text'])
        self.append('platform_order_observed', venue='arcus', order_id='second', raw_status='CANCELED',
                    time_in_force='IOC', platform_reason_reported=False, evidence_source='platform_history_read')
        report = self.report()
        self.assertEqual(report['legs'][1]['time_in_force'], 'IOC')
        self.assertIn('平台只读补查', report['text'])

    def test_buy_side_budget_and_missing_evidence_do_not_invent_send_or_tif(self):
        self.append('hedge_decision', decision='unwind', second_reference_price=100,
                    observed_price=100.018, price_budget_bound=100.02)
        self.append('hedge_leg_skipped', reason='未提交')
        report = self.report()
        self.assertAlmostEqual(report['price_guard']['budget_bps'], 2)
        self.assertAlmostEqual(report['price_guard']['consumed_bps'], 1.8)
        self.assertAlmostEqual(report['price_guard']['remaining_budget_bps'], .1999640065)
        self.assertIsNone(report['price_guard']['remaining_limit_bps'])
        self.assertIsNone(report['legs'][1]['time_in_force'])
        self.assertEqual(report['timings'], {})
        self.assertNotIn('第二腿实际提交限价', report['text'])
        self.assertNotIn('首腿确认→第二腿发送', report['text'])


if __name__ == '__main__':
    unittest.main()
