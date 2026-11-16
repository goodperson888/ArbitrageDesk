import copy
import unittest
from datetime import datetime, timedelta, timezone

from src.common.market_data import utc_now
from src.execution.scan_status import annotate_entry_scan


class EntryScanStatusTests(unittest.TestCase):
    def worker(self, **scan):
        return dict(running=True, phase='连接签名平台', note='启动提示',
            position_monitor=dict(heartbeat_stale=False, positions=[], entry_scan=scan))

    def test_worker_deadline_is_visible_beyond_thirty_second_failure_window(self):
        until = (datetime.now(timezone.utc)+timedelta(seconds=70)).isoformat()
        execution = self.worker(backoff_until=until, backoff_reason='平台接口限流')
        annotate_entry_scan(execution, [])
        self.assertEqual(execution['entry_scan_backoff_until'], until)
        self.assertIn('限流等待', execution['phase'])
        self.assertIn('70 秒', execution['note'])

    def test_expired_deadline_clears_old_backoff_and_startup_text(self):
        execution = self.worker(backoff_until=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
                                backoff_reason='过去的限流')
        annotate_entry_scan(execution, [])
        self.assertIsNone(execution['entry_scan_backoff_reason'])
        self.assertEqual(execution['phase'], '扫描双平台报价')
        self.assertNotIn('连接', execution['note'])

    def test_recent_memory_preview_is_reported_without_database_event(self):
        execution = self.worker(preview_skipped=20, last_preview_skip=dict(symbol='SUI', updated_at=utc_now()))
        annotate_entry_scan(execution, [])
        self.assertEqual(execution['phase'], '等待入场门槛')
        self.assertIn('20 个', execution['note'])

    def test_recent_cache_coverage_distinguishes_partial_from_full_scope(self):
        for missing in (0, 4):
            execution = self.worker(cached_scan=dict(scope=20, cached_symbols=20-missing,
                missing_symbols=missing, updated_at=utc_now()))
            annotate_entry_scan(execution, [])
            self.assertIn(f'{20-missing}/20', execution['note'])
            self.assertEqual('后台限速补查' in execution['note'], bool(missing))

    def test_stale_cache_coverage_is_not_presented_as_current(self):
        execution = self.worker(cached_scan=dict(scope=20, cached_symbols=20, missing_symbols=0,
            updated_at=(datetime.now(timezone.utc)-timedelta(seconds=20)).isoformat()))
        annotate_entry_scan(execution, [])
        self.assertNotIn('20/20', execution['note'])

    def test_current_direction_counts_and_closest_do_not_show_cumulative_skips(self):
        execution = self.worker(preview_skipped=33375, cached_scan=dict(updated_at=utc_now(),
            scope=4, cached_symbols=3, missing_symbols=1, diagnostics=dict(directions=8,
                reasons=dict(boundary=3, net_return=2, quotes=2, ready=1),
                closest=dict(net_return=dict(symbol='SUI',route='arcus->lighter',gap_bps=1.25)))))
        annotate_entry_scan(execution, [])
        self.assertIn('本轮 8 个方向', execution['note'])
        self.assertIn('未触边 3', execution['note'])
        self.assertIn('差 1.25 bps', execution['note'])
        self.assertNotIn('33375', execution['note'])

    def test_legacy_worker_can_recover_current_cooldown_from_timestamped_receipt(self):
        execution = self.worker()
        del execution['position_monitor']['entry_scan']
        event = dict(event='stage_failed', timestamp=(datetime.now(timezone.utc)-timedelta(seconds=50)).isoformat(),
                     detail=dict(reason='接口限流 HTTP 429；暂停 REST 请求 121 秒'))
        annotate_entry_scan(execution, [event])
        self.assertIn(execution['entry_scan']['backoff_remaining_seconds'], (70, 71))
        self.assertIn('限流等待', execution['phase'])

    def test_stale_wrong_process_or_stopped_telemetry_never_overwrites_state(self):
        for changes in (dict(position_monitor=None), dict(position_monitor=dict(heartbeat_stale=True)),
                        dict(running=False)):
            execution = self.worker()
            execution.update(changes)
            original = copy.deepcopy(execution)
            annotate_entry_scan(execution, [])
            self.assertEqual(execution, original)

    def test_close_stop_positions_and_active_submit_keep_priority(self):
        until = (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
        for changes in (dict(close_only=True), dict(stop_requested=True), dict(tracked_position_count=1),
                        dict(latest_event=dict(event='planned')), dict(latest_event=dict(event='accounting_pending'))):
            execution = self.worker(backoff_until=until)
            execution.update(changes)
            annotate_entry_scan(execution, [])
            self.assertEqual(execution['note'], '启动提示')
