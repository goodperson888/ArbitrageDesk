"""Nonblocking, rate-bounded history enrichment, separate from live execution."""
import threading
import time

from src.common.market_data import utc_now


class HistorySync:
    def __init__(self, fetch, execution_active, now=time.monotonic, interval=180):
        self.fetch, self.execution_active, self.now = fetch, execution_active, now
        self.interval = interval
        self._guard = threading.Lock()
        self._thread = None
        self._next = {}
        self._failures = {}
        self._last = {}
        self._symbol_last = {}
        self._global_next = 0
        self._state = {'state': 'idle', 'message': '平台成交历史自动补齐已启用'}

    def poll(self, overview):
        """Schedule at most one small pair batch; never block a page poll."""
        with self._guard:
            if self._thread and self._thread.is_alive():
                return dict(self._state)
            if self.execution_active():
                return {**self._state, 'state': 'deferred',
                        'message': '实盘运行期间复用已有成交证据，结束后自动补查平台历史'}
            groups = {}
            for row in overview.get('open_positions', []) + overview.get('closed_positions', []):
                if row.get('status') == 'closed' and not row.get('accounting_pending'):
                    continue
                pair = (row.get('variational_venue'), row.get('lighter_venue'))
                if len(set(pair)) != 2 or any(venue not in ('lighter', 'lighter_rh', 'arcus') for venue in pair):
                    continue
                if row.get('symbol'):
                    groups.setdefault(pair, []).append(row['symbol'])
            current = self.now()
            due = [pair for pair in groups if current >= max(self._global_next, self._next.get(pair, 0))]
            if due:
                pair = min(due, key=lambda value: self._last.get(value, -1))
                symbols = sorted(dict.fromkeys(groups[pair]), key=lambda symbol: self._symbol_last.get((pair, symbol), -1))[:5]
                for symbol in symbols:
                    self._symbol_last[pair, symbol] = current
                self._last[pair] = current
                self._next[pair] = current + self.interval
                self._global_next = current + 30
                self._state = {'state': 'refreshing', 'venues': list(pair), 'symbols': symbols,
                               'message': '正在后台补查平台成交、手续费和资金费'}
                self._thread = threading.Thread(target=self._run, args=(pair, symbols), daemon=True)
                self._thread.start()
            result = dict(self._state)
            if groups and not due and result.get('state') != 'refreshing':
                result['retry_after_seconds'] = max(0, round(max(self._global_next, min(self._next.get(pair, current) for pair in groups)) - current))
            return result

    def _run(self, pair, symbols):
        try:
            if self.execution_active():
                report = {'success': False, 'orders_submitted': False,
                          'message': '实盘已启动，本轮历史补查延后', 'errors': []}
            else:
                report = self.fetch(list(pair), symbols, 100)
            success = report.get('success') is True and report.get('orders_submitted') is False
            with self._guard:
                failed = 0 if success else self._failures.get(pair, 0) + 1
                self._failures[pair] = failed
                self._next[pair] = self.now() + min(1800, self.interval * (2 ** min(failed, 3)))
                if failed:
                    self._global_next = max(self._global_next, self.now() + 60)
                self._state = {'state': 'ready' if success else 'partial', 'checked_at': utc_now(),
                               'venues': list(pair), 'symbols': symbols,
                               'errors': report.get('errors', []),
                               'message': '平台成交历史已自动更新' if success else
                               '部分平台历史暂未读全，保留已确认成交，稍后自动重试'}
        except Exception:
            with self._guard:
                self._next[pair] = self.now() + max(300, self.interval * 2)
                self._state = {'state': 'partial', 'checked_at': utc_now(),
                               'message': '平台历史补查暂不可用，已保留本地成交证据'}
