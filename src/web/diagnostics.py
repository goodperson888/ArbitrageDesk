"""Bounded local diagnostic records with no request bodies or exception messages."""
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from src.common.market_data import utc_now

MESSAGES = {
    'SERVICE_STARTED': '本地监控服务已启动，实盘执行关闭',
    'HTTP_REJECTED': '本地接口拒绝请求；根据接口路径和状态码定位',
    'UNHANDLED_ERROR': '服务异常；已记录调用位置，未记录请求内容',
    'REFERENCE_FAILED': '参考行情刷新失败',
    'SAMPLE_FAILED': '行情历史采样失败',
    'BOOK_FAILED': 'Lighter 盘口刷新失败',
    'FEED_INIT_FAILED': 'Lighter 实时流初始化失败',
    'LIGHTER_VERIFIED': 'Lighter 只读鉴权通过，未发送订单',
    'LIGHTER_VERIFY_FAILED': 'Lighter 只读鉴权失败，查看密钥连接面板的诊断',
    'CAPTURE_RECEIVED': '已收到并保存手动提交响应，HTTP 成功不代表成交',
    'EXECUTION_STARTED': '用户发起单次执行，查看执行历史与账户确认成交',
    'EXECUTION_FINISHED': '单次执行进程退出；不代表已平仓',
    'EXECUTION_FAILED': '执行进程失败或未启动；不重试，需核对账户和执行历史',
    'EXECUTION_STOP_REQUESTED': '用户请求停止新开仓；不代表已平仓',
    'EXECUTION_STOP_RESET': '只读账户核对通过，已解除停止；未启用实盘、未下单',
    'EXECUTION_STOP_RESET_BLOCKED': '解除停止检查未通过；查看账户、挂单及未核对订单',
    'EXECUTION_CHECKED': '完成只读实盘条件检查；没有发送订单',
}


class Diagnostics:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.last = {}

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=2)
        db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, at TEXT, code TEXT, route TEXT, status INTEGER, event_id TEXT, location TEXT)')
        return db

    def record(self, code, route=None, status=None, event_id=None, exc=None):
        if code not in MESSAGES:
            return
        # All caller-supplied paths are Flask route templates, never URL queries.
        route = route if isinstance(route, str) and route.startswith('/api/') and len(route) < 100 else None
        status = status if type(status) is int and 100 <= status <= 599 else None
        event_id = event_id or uuid.uuid4().hex[:12]
        location = None
        if exc is not None:
            import traceback
            frames = traceback.extract_tb(exc.__traceback__)[-4:]
            location = ' → '.join(f'{Path(f.filename).name}:{f.lineno}:{f.name}' for f in frames)
        with self.lock:
            key = (code, route, status)
            now = time.monotonic()
            if now - self.last.get(key, -100) < 10:
                return
            self.last[key] = now
            with self._connect() as db:
                db.execute('INSERT INTO events(at,code,route,status,event_id,location) VALUES(?,?,?,?,?,?)',
                           (utc_now(), code, route, status, event_id, location))
                db.execute('DELETE FROM events WHERE seq <= (SELECT COALESCE(MAX(seq),0)-2000 FROM events)')

    def recent(self, limit=40):
        with self.lock, self._connect() as db:
            db.row_factory = sqlite3.Row
            return [{**dict(row), 'message': MESSAGES.get(row['code'], '未知诊断代码')}
                    for row in db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT ?', (min(limit, 200),))]
